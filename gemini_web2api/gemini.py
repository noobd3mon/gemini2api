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
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


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
    """Parse full response to get final text."""
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    if not last_text:
        err = bard_error_message(raw)
        if err:
            raise GeminiUpstreamError(err)
    return clean_text(last_text)


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    """Non-streaming generation with retry."""
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
                            if not t.startswith(emitted_raw_text):
                                raise RuntimeError("Gemini stream content changed during retry")
                            delta = clean_text(t[len(emitted_raw_text):], strip=False)
                            emitted_raw_text = t
                            if delta:
                                yield delta
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
