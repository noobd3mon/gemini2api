"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import urllib.error
import ssl
import os
import hashlib
import mimetypes

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0}
_httpx_client = None


def log(msg: str):
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)
    return _httpx_client


def _parse_sapisid(cookie_str: str) -> str:
    """Extract SAPISID (or its 3P variant) from a raw cookie header string."""
    pairs = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs.get("SAPISID") or pairs.get("__Secure-3PAPISID") or ""


def _cookie_from_string(raw: str) -> tuple:
    """Parse an inline cookie (env GEMINI_COOKIE). Accepts raw header or JSON."""
    text = raw.strip().strip('"').strip("'")
    sapisid = str(CONFIG.get("sapisid") or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            text = str(data.get("cookie", "")).strip()
            sapisid = sapisid or str(data.get("sapisid", "")).strip()
        except ValueError:
            log("Inline cookie is not valid JSON, treating it as a raw header")
    if not text:
        return "", None
    sapisid = sapisid or _parse_sapisid(text)
    return text, sapisid or None


def load_cookie() -> tuple:
    """Inline cookie (env/config) first, then cookie_file with mtime caching."""
    inline = CONFIG.get("cookie")
    if inline:
        return _cookie_from_string(str(inline))
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


def _invalidate_cookie_cache() -> None:
    """Drop cached cookies and page tokens so a runtime cookie update takes effect."""
    _cookie_cache.update({"str": "", "sapisid": None, "mtime": 0})
    try:
        from .multimodal import _invalidate_page_tokens
        _invalidate_page_tokens()
    except Exception:
        pass


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _page_token(name: str):
    """Read a scraped Gemini app-page token (push_id/pctx/at/f_sid).

    The tokens live in multimodal._cached_page_tokens(); import it lazily to
    avoid a circular import (multimodal imports this module at load time). The
    cache is warmed by the upload step, so this is a cache hit for attachment
    requests and costs nothing (no fetch) for text-only ones.
    """
    try:
        from .multimodal import _cached_page_tokens
        return _cached_page_tokens().get(name)
    except Exception:
        return None


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def fetch_latest_bl() -> str:
    """Fetch the current gemini_bl build label from the Gemini app page."""
    if not CONFIG.get("auto_update_bl", True):
        return None
    try:
        req = urllib.request.Request(
            "https://gemini.google.com/app",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        ctx = _get_ssl_ctx()
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ctx))
            resp = opener.open(req, timeout=15)
        else:
            resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'(boq_assistant-bard-web-server_\d+\.\d+_p\d+)', html)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"BL auto-update fetch failed: {e}")
    return None


def update_bl_if_needed() -> bool:
    """Attempt to fetch and update gemini_bl. Returns True if updated."""
    new_bl = fetch_latest_bl()
    if new_bl and new_bl != CONFIG["gemini_bl"]:
        log(f"BL auto-updated: {CONFIG['gemini_bl']} -> {new_bl}")
        CONFIG["gemini_bl"] = new_bl
        return True
    return False


def _build_headers() -> dict:
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _build_file_bindings(file_refs: list) -> list:
    """Bind uploaded files into payload slot inner[0][3].

    Verified against real gemini.google.com captures (2026-08-11):
        [[[<file_ref>, <kind>, None, <mime>], <filename>], ...]
    <kind> is 1 for images and 3 for any other file type: a 3-file capture sent
    1 for image/png and image/jpeg, but 3 for text/plain.
    Accepts bare refs or (ref, filename, mime) tuples.
    """
    if not file_refs:
        return None
    bindings = []
    for i, item in enumerate(file_refs):
        if isinstance(item, (list, tuple)):
            parts = list(item) + [None] * (3 - len(item))
            ref, filename, mime = parts[0], parts[1], parts[2]
        else:
            ref, filename, mime = item, None, None
        if not ref:
            continue
        if not mime and filename:
            mime = mimetypes.guess_type(filename)[0]
        mime = mime or "application/octet-stream"
        kind = 1 if mime.startswith("image/") else 3
        bindings.append([[ref, kind, None, mime], filename or f"file_{i}"])
    return bindings or None


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web chat persistence flags (temporary chats when enabled).

    The real web client sends inner[41]=[1] + inner[45]=1 (temporary) for every
    StreamGenerate, including file-bearing requests (verified against a
    2026-08-11 capture). The proxy defaults to temporary so API calls do not
    litter the user's Gemini history with saved conversations; TEMPORARY_CHATS=false
    or config.json re-enables saving.
    """
    if CONFIG.get("temporary_chats", True):
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    inner = [None] * 102
    inner[0] = [prompt, 0, None, _build_file_bindings(file_refs), None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    elif file_refs:
        # The browser always sends an `at` XSRF token; use the one scraped from
        # the app page (already cached by the upload step). Text-only requests
        # are tolerated without it, so only attach it for file-bearing calls.
        at = _page_token("at")
        if at:
            params["at"] = at
    return urllib.parse.urlencode(params)


def _get_url(file_refs: list = None) -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix()
    url = (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    # The browser always sends f.sid; only attach it for file-bearing calls so
    # text-only requests stay cheap (no page scrape needed).
    if file_refs:
        fsid = _page_token("f_sid")
        if fsid:
            url += f"&f.sid={fsid}"
    return url


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/(?:card_content|image_generation_content)/\w+\n?', '', text)
    return text.strip() if strip else text


def _parse_inner(line: str):
    """Parse a wrb.fr line into its inner payload list, or None."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return None
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str:
            return None
        inner = json.loads(inner_str)
        return inner if isinstance(inner, list) else None
    except (json.JSONDecodeError, IndexError, TypeError):
        return None


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    inner = _parse_inner(line)
    if not (inner and len(inner) > 4 and inner[4]):
        return []
    texts = []
    for part in inner[4]:
        if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
            for t in part[1]:
                if isinstance(t, str) and t:
                    texts.append(t)
    return texts


# Generated images come back as lh3.googleusercontent.com/gg-dl/<token> resolver URLs
# nested in the response. GET-ing a gg-dl URL returns text/plain = the directly
# viewable rd-gg-dl/<token> image URL (image/jpeg, no auth - just a gemini referer).
# The text part of an image reply is a useless image_generation_content placeholder,
# which clean_text strips; we append the resolved image instead.
GG_DL_URL_RE = re.compile(r'https://lh3\.googleusercontent\.com/gg-dl/[A-Za-z0-9_-]+')


def _find_gg_dl_urls(obj) -> list:
    """Recursively collect de-duplicated gg-dl image resolver URLs from a parsed response."""
    found = []

    def walk(x):
        if isinstance(x, str):
            if "lh3.googleusercontent.com/gg-dl/" in x:
                m = GG_DL_URL_RE.search(x)
                if m and m.group(0) not in found:
                    found.append(m.group(0))
        elif isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(obj)
    return found


def _extract_image_urls_from_line(line: str) -> list:
    """Parse a wrb.fr line and return any gg-dl image URLs it carries."""
    inner = _parse_inner(line)
    if inner is None:
        return []
    return _find_gg_dl_urls(inner)


def resolve_image_url(gg_url: str, timeout: int = 15) -> str:
    """Resolve a gg-dl resolver URL to the directly viewable image URL.

    Returns the rd-gg-dl/<token> URL (image/jpeg) that clients can hotlink, or the
    original gg-dl URL if resolution fails.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gemini.google.com/",
    }
    try:
        req = urllib.request.Request(gg_url, headers=headers)
        resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=timeout)
        body = resp.read().decode("utf-8", errors="replace").strip()
        if body.startswith("http") and "usercontent" in body:
            return body
    except Exception as e:
        log(f"image resolve failed: {e}")
    return gg_url


def _format_image_output(url: str) -> str:
    """Render a generated-image URL the way the client expects it.

    image_format config/env (GEMINI_IMAGE_FORMAT): "markdown" (default) appends
    an ![generated image](url) line; "url" appends the bare resolved URL, which
    is easier to consume for clients that do not render markdown.
    """
    fmt = str(CONFIG.get("image_format") or "markdown").strip().lower()
    if fmt in ("url", "link"):
        return url
    return f"![generated image]({url})"


class GeminiUpstreamError(RuntimeError):
    """Gemini refused the request itself; resending the same payload will not help."""


# Real responses look like `...BardErrorInfo",[1100]]]`, so allow the quote/comma.
BARD_ERROR_RE = re.compile(r'BardErrorInfo\D{0,8}\[\s*(\d+)')


def bard_error_message(raw: str) -> str:
    """Describe a BardErrorInfo code, with a hint for the common attachment failure."""
    match = BARD_ERROR_RE.search(raw)
    if not match:
        return ""
    code = match.group(1)
    msg = f"Gemini upstream rejected request: BardErrorInfo [{code}]"
    if code == "1100":
        if load_cookie()[0]:
            msg += (" - the attachment was refused. The cookie is most likely expired"
                    " or has no file access; refresh GEMINI_COOKIE and retry.")
        else:
            msg += (" - file/image input needs a signed-in session. Set GEMINI_COOKIE;"
                    " anonymous requests can only send text.")
    return msg


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text, with generated images appended."""
    last_text = ""
    image_urls = []
    seen = set()
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            # Frames grow cumulatively, but Gemini can revise mid-stream: the
            # LAST frame carries the authoritative draft, so prefer it over the
            # longest one (which can be a stale middle draft).
            if t:
                last_text = t
        for gg in _extract_image_urls_from_line(line):
            if gg not in seen:
                seen.add(gg)
                image_urls.append(gg)
    if not last_text and not image_urls:
        err = bard_error_message(raw)
        if err:
            raise GeminiUpstreamError(err)
    text = clean_text(last_text)
    for gg in image_urls:
        text += f"\n\n{_format_image_output(resolve_image_url(gg))}"
    return text


def extract_finish_status(raw: str):
    """Best-effort finish status from the final response frame.

    Captures (2026-08-11/14) carry the completion status at
    inner[26][0][0][0][1][1], always 0 for a normal reply; the text candidate is
    absent for image-only replies. Returns the int, or None when no frame has one.
    Only 0 is mapped to "stop" today - other codes are logged by the server so
    future captures can extend the mapping.
    """
    status = None
    for line in raw.split("\n"):
        inner = _parse_inner(line)
        if inner is None or len(inner) <= 26:
            continue
        try:
            s = inner[26][0][0][0][1][1]
        except (IndexError, TypeError):
            continue
        if isinstance(s, int) and not isinstance(s, bool):
            status = s
    return status


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
             extra_fields: dict = None, out: dict = None) -> str:
    """Non-streaming generation with retry.

    `out` is an optional dict filled with extra result info
    (out["finish_status"]) for callers that want it.
    """
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    url = _get_url(file_refs)
    headers = _build_headers()
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            raw = resp.read().decode("utf-8", errors="replace")
            if out is not None:
                out["finish_status"] = extract_finish_status(raw)
            return extract_response_text(raw)
        except GeminiUpstreamError:
            raise  # Gemini refused the payload; retrying only wastes time.
        except urllib.error.HTTPError as e:
            # 4xx (other than 405) is a permanent rejection - surface Gemini's
            # body so the cause can be diagnosed instead of silently retrying.
            if 400 <= e.code < 500 and e.code != 405:
                snippet = ""
                try:
                    snippet = e.read().decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                raise GeminiUpstreamError(
                    f"StreamGenerate HTTP {e.code} {e.reason}"
                    + (f": {snippet}" if snippet else ""))
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the longest common prefix of two strings (codepoint-wise)."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _delta_from(emitted: str, t: str) -> str:
    """Client-visible delta for a new draft `t` given what was already emitted.

    Prefix-grown frames emit the suffix. A mid-stream revision (non-prefix) emits
    only the part of the new text beyond the longest common prefix, so the stream
    completes instead of dropping (a drop makes the client retry and concatenate,
    which repeats the whole intro). Returns "" when nothing new should be sent.
    """
    if t == emitted or emitted.startswith(t):
        return ""
    if t.startswith(emitted):
        return clean_text(t[len(emitted):], strip=False)
    cp = _common_prefix_len(emitted, t)
    delta = clean_text(t[cp:], strip=False)
    if delta and delta in emitted:
        return ""  # the new text is a subset of what was already sent
    return delta


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None):
    """Streaming generation via httpx with retry on connection failure."""
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
    url = _get_url(file_refs)
    headers = _build_headers()
    client = _get_httpx_client()

    last_err = None
    emitted_raw_text = ""
    emitted_images = set()
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    if "BardErrorInfo" in buf and not emitted_raw_text:
                        err = bard_error_message(buf)
                        if err:
                            raise GeminiUpstreamError(err)
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        for t in _extract_texts_from_line(line):
                            if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                continue
                            delta = _delta_from(emitted_raw_text, t)
                            emitted_raw_text = t
                            if delta:
                                yield delta
                        for gg in _extract_image_urls_from_line(line):
                            if gg not in emitted_images:
                                emitted_images.add(gg)
                                yield f"\n\n{_format_image_output(resolve_image_url(gg))}\n"
            return
        except GeminiUpstreamError:
            raise  # Gemini refused the payload; retrying only wastes time.
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if 400 <= code < 500 and code != 405:
                snippet = ""
                try:
                    snippet = e.response.read().decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                raise GeminiUpstreamError(
                    f"StreamGenerate HTTP {code}"
                    + (f": {snippet}" if snippet else ""))
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err