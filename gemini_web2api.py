"""gemini-web2api standalone single-file server. GENERATED FILE - do not edit.

This file is built from the gemini_web2api/ package by build_single_file.py.
Edit the package modules instead, then rerun the build script and commit.
"""

import json
import os
import time
import uuid
import re
import urllib.request
import urllib.parse
import urllib.error
import ssl
import hashlib
import mimetypes
import base64
import io
import hmac
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor
import argparse

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

__version__ = "1.1.0"

# --- config (from gemini_web2api/) ---




DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie": None,        # raw cookie string (env GEMINI_COOKIE)
    "sapisid": None,       # optional override, else parsed from the cookie
    "cookie_file": None,   # local file fallback (legacy / desktop use)
    "proxy": None,
    "api_keys": [],
    "temporary_chats": True,
    "auto_update_bl": True,
    # Generated images: "markdown" (default, ![generated image](url)) or "url".
    "image_format": "markdown",
    # Optional file for the scraped page tokens (push_id/pctx/at/f_sid) so a
    # restart does not need a fresh app-page scrape. Opt-in: never written by
    # default because at/f_sid are session tokens.
    "token_cache_file": None,
    # Max requests per minute per API key (0 = no limit).
    "rate_limit": 0,
    # Optional key for POST /admin/cookie; falls back to api_keys auth.
    "admin_key": None,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_config(path: str = None):
    """Load config from JSON file (optional)."""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None


# --- Environment variables ---------------------------------------------------

_TRUE = ("1", "true", "yes", "on", "y")
_FALSE = ("0", "false", "no", "off", "n")

ENV_STR = {
    "host": ("HOST", "GEMINI_HOST"),
    "cookie": ("GEMINI_COOKIE", "COOKIE"),
    "sapisid": ("GEMINI_SAPISID", "SAPISID"),
    "cookie_file": ("GEMINI_COOKIE_FILE", "COOKIE_FILE"),
    "gemini_bl": ("GEMINI_BL", "BL"),
    "auth_user": ("GEMINI_AUTH_USER", "AUTH_USER"),
    "xsrf_token": ("GEMINI_XSRF_TOKEN", "XSRF_TOKEN"),
    "default_model": ("GEMINI_DEFAULT_MODEL", "DEFAULT_MODEL"),
    "proxy": ("GEMINI_PROXY", "PROXY"),
    "image_format": ("GEMINI_IMAGE_FORMAT", "IMAGE_FORMAT"),
    "token_cache_file": ("GEMINI_TOKEN_CACHE_FILE", "TOKEN_CACHE_FILE"),
    "admin_key": ("GEMINI_ADMIN_KEY", "ADMIN_KEY"),
}

ENV_INT = {
    "port": ("PORT", "GEMINI_PORT"),
    "retry_attempts": ("RETRY_ATTEMPTS",),
    "retry_delay_sec": ("RETRY_DELAY_SEC",),
    "request_timeout_sec": ("REQUEST_TIMEOUT_SEC",),
    "rate_limit": ("GEMINI_RATE_LIMIT", "RATE_LIMIT"),
}

ENV_BOOL = {
    "log_requests": ("LOG_REQUESTS",),
    "temporary_chats": ("TEMPORARY_CHATS",),
    "auto_update_bl": ("AUTO_UPDATE_BL",),
}

ENV_KEYS = ("API_KEYS", "API_KEY", "GEMINI_API_KEYS")


def env_value(*names):
    """Return the first non-empty environment variable among names."""
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return raw.strip()
    return None


def parse_bool(value, default=None):
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def parse_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_keys(value):
    """Accept a JSON array or a comma / space / newline separated list."""
    text = str(value).strip()
    if text.startswith("["):
        try:
            return [str(x).strip() for x in json.loads(text) if str(x).strip()]
        except (TypeError, ValueError):
            pass
    for sep in ("\n", " ", ";"):
        text = text.replace(sep, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def load_env_config():
    """Apply environment variables on top of CONFIG (env wins over the file)."""
    for key, names in ENV_STR.items():
        value = env_value(*names)
        if value is not None:
            CONFIG[key] = value

    for key, names in ENV_INT.items():
        value = env_value(*names)
        if value is not None:
            parsed = parse_int(value)
            if parsed is not None:
                CONFIG[key] = parsed

    for key, names in ENV_BOOL.items():
        value = env_value(*names)
        if value is not None:
            parsed = parse_bool(value)
            if parsed is not None:
                CONFIG[key] = parsed

    keys = env_value(*ENV_KEYS)
    if keys is not None:
        CONFIG["api_keys"] = parse_keys(keys)

    return CONFIG

# --- models (from gemini_web2api/) ---

# MODE_CATEGORY enum from 028-6eb337387583.js:
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.7 Flash)",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "All-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Alias for gemini-3.6-flash (backend upgraded)",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-3.1-pro-enhanced": {
        "mode": 3, "think": 4, "extra": {31: 2, 80: 3},
        "desc": "Pro with enhanced output (experimental)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}


def resolve_model(model_name: str, default: str = "gemini-3.6-flash"):
    """Resolve model name to (name, mode_id, think_mode, error, extra_fields).

    Unknown model names fall back to default rather than erroring,
    since upstream clients may request arbitrary model identifiers.
    """
    think_override = None
    if "@think=" in model_name:
        model_name, think_str = model_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError:
            return None, None, None, f"Invalid think level: {think_str}", None
    cfg = MODELS.get(model_name)
    if not cfg:
        log(f"Unknown model '{model_name}', falling back to '{default}'")
        model_name = default
        cfg = MODELS[default]
    mode_id = cfg["mode"]
    think_mode = think_override if think_override is not None else cfg["think"]
    extra = cfg.get("extra")
    return model_name, mode_id, think_mode, None, extra

# --- gemini protocol (from gemini_web2api/) ---













try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


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

# --- multimodal upload (from gemini_web2api/) ---










# push.clients6 is what the web client uses; content-push is the public alias.
UPLOAD_ENDPOINTS = (
    "https://push.clients6.google.com/upload/",
    "https://content-push.googleapis.com/upload/",
)

DEFAULT_PUSH_ID = "feeds/mcudyrk2a4khkz"
DEFAULT_PCTX = "CgcSBWjK7pYx"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _open(req, timeout):
    """Open a request honouring the configured proxy."""
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, context=ctx, timeout=timeout)


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _enc = mimetypes.guess_type(filename or "")
    return mime or fallback


def guess_filename(mime: str, index: int = 0) -> str:
    ext = mimetypes.guess_extension(mime or "") or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    kind = "image" if (mime or "").startswith("image/") else "file"
    return f"{kind}_{int(time.time())}_{index}{ext}"


def decode_data_url(url: str):
    """Return (bytes, mime) for a data: URL, or (b\"\", \"\") when it is not one."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return b"", ""
    header, _sep, payload = url.partition(",")
    mime = header[5:].split(";")[0].strip()
    if ";base64" in header:
        try:
            return base64.b64decode(payload), mime
        except Exception as e:
            log(f"Invalid base64 data URL: {e}")
            return b"", mime
    return urllib.parse.unquote_to_bytes(payload), mime


def _get_page_tokens() -> dict:
    """Fetch WIZ_global_data tokens from the Gemini page (Push-ID, X-Client-Pctx)."""
    headers = {"User-Agent": UA}
    cookie_str, _sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    url = "https://gemini.google.com" + _account_prefix() + "/app"
    try:
        req = urllib.request.Request(url, headers=headers)
        html = _open(req, 30).read().decode("utf-8", errors="replace")
        tokens = {}
        for key, pattern in (
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
            ("at", r'"thykhd":"([^"]+)"'),
            ("f_sid", r'"FdrFJe":"([^"]+)"'),
        ):
            m = re.search(pattern, html)
            if m:
                tokens[key] = m.group(1)
        return tokens
    except Exception as e:
        log(f"Page token fetch failed: {e}")
        return {}


_page_tokens_cache = {"tokens": {}, "ts": 0}


def _invalidate_page_tokens() -> None:
    """Force the next token read to re-scrape the app page."""
    _page_tokens_cache["ts"] = 0


def _load_token_cache(path: str):
    """Read a token cache file written by _cached_page_tokens()."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
            return data
    except Exception:
        pass
    return None


def _cached_page_tokens() -> dict:
    """Cached app-page tokens, optionally persisted to token_cache_file.

    The cache file is opt-in (GEMINI_TOKEN_CACHE_FILE): at/f_sid are session
    tokens, so the file is only written when explicitly configured.
    """
    now = time.time()
    if now - _page_tokens_cache["ts"] <= 600:
        return _page_tokens_cache["tokens"]
    cache_file = CONFIG.get("token_cache_file")
    if cache_file:
        data = _load_token_cache(cache_file)
        if data:
            _page_tokens_cache["tokens"] = data["tokens"]
            _page_tokens_cache["ts"] = data.get("ts", 0)
            if now - _page_tokens_cache["ts"] <= 600:
                return _page_tokens_cache["tokens"]
    _page_tokens_cache["tokens"] = _get_page_tokens()
    _page_tokens_cache["ts"] = now
    if cache_file and _page_tokens_cache["tokens"]:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"ts": now, "tokens": _page_tokens_cache["tokens"]}, f)
        except Exception as e:
            log(f"Token cache write failed: {e}")
    return _page_tokens_cache["tokens"]


def upload_file(data: bytes, filename: str = None, mime_type: str = None) -> str:
    """Upload one file via Scotty resumable upload. Returns the file reference."""
    if not data:
        raise ValueError("empty file data")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file too large: {len(data)} bytes (max {MAX_UPLOAD_BYTES})")
    mime_type = mime_type or guess_mime(filename)
    filename = filename or guess_filename(mime_type)

    tokens = _cached_page_tokens()
    push_id = tokens.get("push_id") or DEFAULT_PUSH_ID
    pctx = tokens.get("pctx") or DEFAULT_PCTX
    cookie_str, sapisid = load_cookie()

    common = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "X-Client-Pctx": pctx,
        "Referer": "https://gemini.google.com/",
        "User-Agent": UA,
    }
    if cookie_str:
        common["Cookie"] = cookie_str
    if sapisid:
        common["Authorization"] = make_sapisidhash(sapisid)

    start_headers = dict(common)
    start_headers.update({
        # The browser only declares the length here. The mime type travels in
        # the payload binding, so X-Goog-Upload-Header-Content-Type is not sent.
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    })
    # The web client posts the file name as the form body of the start request.
    start_body = urllib.parse.urlencode({"File name: " + filename: ""}).encode()

    upload_url = None
    last_err = None
    for endpoint in UPLOAD_ENDPOINTS:
        try:
            req = urllib.request.Request(endpoint, data=start_body,
                                         headers=start_headers, method="POST")
            resp = _open(req, 30)
            upload_url = (resp.headers.get("X-Goog-Upload-URL")
                          or resp.headers.get("x-goog-upload-url"))
            if upload_url:
                break
            last_err = RuntimeError("start response had no X-Goog-Upload-URL header")
        except Exception as e:
            last_err = e
            log(f"Upload start failed on {endpoint}: {e}")
    if not upload_url:
        raise RuntimeError(f"Upload start failed: {last_err}")

    finalize_headers = dict(common)
    finalize_headers.update({
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    })
    req2 = urllib.request.Request(upload_url, data=data,
                                  headers=finalize_headers, method="POST")
    file_ref = _open(req2, 120).read().decode("utf-8", errors="replace").strip()
    if not file_ref.startswith("/"):
        raise RuntimeError(f"Invalid file reference: {file_ref[:120]}")
    log(f"Uploaded {filename} ({mime_type}, {len(data)} bytes) -> {file_ref[:48]}...")
    return file_ref


def upload_image(image_bytes: bytes, filename: str = "image.png",
                 mime_type: str = "image/png") -> str:
    """Backwards-compatible wrapper around upload_file()."""
    return upload_file(image_bytes, filename, mime_type)


def fetch_file_bytes(url: str):
    """Fetch a remote or data: URL. Returns (bytes, mime)."""
    if isinstance(url, str) and url.startswith("data:"):
        return decode_data_url(url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = _open(req, 60)
        data = resp.read(MAX_UPLOAD_BYTES + 1)
        mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        return data, mime
    except Exception as e:
        log(f"File fetch failed: {e}")
        return b"", ""


def fetch_image_bytes(url: str) -> bytes:
    """Backwards-compatible wrapper: bytes only."""
    return fetch_file_bytes(url)[0]


def prepare_attachment(item, index: int = 0):
    """Normalize an attachment into (bytes, filename, mime), or None when unusable.

    Accepted shapes: bytes, url string, (data_or_url, mime) or
    (data_or_url, mime, filename).
    """
    if isinstance(item, (bytes, bytearray)):
        data, mime, filename = bytes(item), "", ""
    elif isinstance(item, str):
        data, mime, filename = item, "", ""
    elif isinstance(item, (list, tuple)) and item:
        parts = list(item) + [None] * (3 - len(item))
        data, mime, filename = parts[0], parts[1] or "", parts[2] or ""
    else:
        return None
    if isinstance(data, str):
        data, sniffed = fetch_file_bytes(data)
        mime = mime or sniffed
    if not data:
        return None
    if not mime:
        mime = guess_mime(filename) if filename else "application/octet-stream"
    if not filename:
        filename = guess_filename(mime, index)
    return bytes(data), filename, mime

# --- tool calling (from gemini_web2api/) ---









MAX_IMAGE_B64_SIZE = 50000  # ~37KB raw image

# Gemini silently truncates very long prompts; keep the tools block bounded.
PROMPT_MAX_BYTES = 60000


def _log(msg: str) -> None:
    try:
        log(msg)
    except Exception:
        pass


def _decode_data_url(url: str):
    """Return (bytes, mime) for a data: URL."""
    header, _sep, payload = url.partition(",")
    mime = header[5:].split(";")[0].strip() or "application/octet-stream"
    if ";base64" in header:
        try:
            return base64.b64decode(payload), mime
        except Exception as e:
            _log(f"Invalid base64 data URL: {e}")
            return b"", mime
    return urllib.parse.unquote_to_bytes(payload), mime


def _attachment_from_url(url, filename: str = ""):
    """Build a (data_or_url, mime, filename) attachment tuple from a URL."""
    if isinstance(url, dict):
        url = url.get("url", "")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        data, mime = _decode_data_url(url)
        if not data:
            return None
        return (data, mime, filename or "")
    if url.startswith("http://") or url.startswith("https://"):
        name = filename or urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        return (url, "", name)
    return None


def extract_attachment(part: dict):
    """Extract an attachment from an OpenAI / Responses / Anthropic content part.

    Returns (data_or_url, mime, filename) or None. `data_or_url` is raw bytes for
    inline payloads, or a URL string that is fetched right before upload.
    """
    if not isinstance(part, dict):
        return None
    ptype = part.get("type", "")
    filename = part.get("filename") or part.get("name") or ""
    if ptype in ("image_url", "image", "input_image"):
        src = part.get("source")
        if isinstance(src, dict) and src.get("data"):  # Anthropic style
            mime = src.get("media_type") or "image/png"
            try:
                return (base64.b64decode(src["data"]), mime, filename)
            except Exception:
                return None
        url = part.get("image_url") or part.get("url") or part.get("image") or ""
        if not url and isinstance(src, dict):
            url = src.get("url", "")
        return _attachment_from_url(url, filename)
    if ptype in ("file", "input_file", "document"):
        f = part.get("file") if isinstance(part.get("file"), dict) else part
        filename = f.get("filename") or f.get("name") or filename
        src = f.get("source")
        if isinstance(src, dict) and src.get("data"):
            mime = src.get("media_type") or "application/octet-stream"
            try:
                return (base64.b64decode(src["data"]), mime, filename)
            except Exception:
                return None
        raw = f.get("file_data") or f.get("data") or ""
        if isinstance(raw, str) and raw and not raw.startswith(("data:", "http://", "https://")):
            # Raw base64 payload (OpenAI Responses / Anthropic style)
            mime = (f.get("media_type") or mimetypes.guess_type(filename or "")[0]
                    or "application/octet-stream")
            try:
                return (base64.b64decode(raw), mime, filename)
            except Exception:
                return None
        url = raw or f.get("file_url") or f.get("url") or ""
        if not url and isinstance(src, dict):
            url = src.get("url", "")
        return _attachment_from_url(url, filename)
    return None


def _compress_b64_if_needed(b64: str) -> str:
    """Compress image if base64 is too large for text embedding."""
    if len(b64) <= MAX_IMAGE_B64_SIZE:
        return b64
    try:
        from PIL import Image
        img_data = base64.b64decode(b64)
        img = Image.open(io.BytesIO(img_data))
        # Resize to max 256px on longest side
        max_dim = 256
        ratio = min(max_dim / img.width, max_dim / img.height)
        if ratio < 1:
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        # Convert to JPEG with quality reduction
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=60)
        compressed = base64.b64encode(buf.getvalue()).decode()
        return compressed
    except Exception:
        # If PIL not available, truncate (model will get partial data)
        return b64[:MAX_IMAGE_B64_SIZE]


def _build_tool_choice_instruction(tool_choice, tool_defs: list) -> str:
    """Build tool_choice constraint instruction.

    tool_choice values:
      - "none": do not call any tool
      - "auto": decide whether to call tools (default)
      - "required": must call at least one tool
      - {"type": "function", "function": {"name": "xxx"}}: must call specific tool
    """
    if tool_choice == "none":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if tool_choice == "required":
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    if isinstance(tool_choice, dict):
        fn_name = tool_choice.get("function", {}).get("name", "")
        if fn_name:
            return f'\n\nIMPORTANT: You MUST call the tool "{fn_name}". Do not call other tools.'
    return ""


def messages_to_prompt(messages: list, tools: list = None, tool_choice=None) -> tuple:
    """Convert OpenAI messages to (prompt_str, images_list).

    Returns (prompt, images) where images is a list of (bytes, mime_type) tuples.
    """
    parts = []
    images = []

    if tools and tool_choice != "none":
        tool_defs = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            tools_json = json.dumps(tool_defs, indent=2)
            # Large tool lists silently blow the prompt budget: keep names and
            # descriptions but drop JSON schemas instead of truncating the prompt.
            if len(tools_json) > PROMPT_MAX_BYTES // 2:
                slim = [{"name": t.get("name", ""), "description": t.get("description", "")}
                        for t in tool_defs]
                tools_json = json.dumps(slim, indent=2)
                _log(f"Tools block too large ({len(tool_defs)} tools), stripped parameters")
            constraint = _build_tool_choice_instruction(tool_choice, tool_defs)
            parts.append(
                "# Tool Use\n\n"
                "You can call the following tools. Call format:\n"
                '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n'
                "When calling tools, output ONLY the tool_call block(s).\n\n"
                f"Available tools:\n{tools_json}"
                f"{constraint}"
            )

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            for c in content:
                if isinstance(c, str):
                    text_parts.append(c)
                    continue
                if not isinstance(c, dict):
                    continue
                if c.get("type") in ("text", "input_text", "output_text"):
                    text_parts.append(c.get("text", ""))
                    continue
                att = extract_attachment(c)
                if att:
                    images.append(att)
                    if not (att[1] or "").startswith("image/"):
                        text_parts.append(f"[Attached file: {att[2] or 'attachment'}]")
            content = " ".join(p for p in text_parts if p)

        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_strs = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tc_strs.append(
                        f'```tool_call\n{{"name": "{fn.get("name")}", '
                        f'"arguments": {fn.get("arguments", "{}")}}}\n```'
                    )
                parts.append(f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs))
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            parts.append(f"[Tool result for {msg.get('name', '')}]: {content}")
        else:
            parts.append(content if content else "")

    prompt = "\n\n".join(p for p in parts if p)
    return prompt, images


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    pattern = r'```tool_call\s*\n(.*?)\n```'
    clean_parts = []
    last_end = 0
    for m in re.finditer(pattern, text, re.DOTALL):
        clean_parts.append(text[last_end:m.start()])
        last_end = m.end()
        try:
            data = json.loads(m.group(1).strip())
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": data["name"],
                    "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                },
            })
        except (json.JSONDecodeError, KeyError):
            pass
    clean_parts.append(text[last_end:])
    clean = "".join(clean_parts).strip()
    return clean, tool_calls


# ─── Google Native API helpers ─────────────────────────────────────────────────


def build_tool_prompt(tool_defs: list) -> str:
    """Build natural tool-use prompt for Gemini Web that avoids prompt-injection detection."""
    tool_spec = json.dumps(tool_defs, indent=2, ensure_ascii=False)
    if len(tool_spec) > PROMPT_MAX_BYTES // 2:
        slim = [{"name": t.get("name", ""), "description": t.get("description", "")}
                for t in tool_defs]
        tool_spec = json.dumps(slim, indent=2, ensure_ascii=False)
        _log(f"Tools block too large ({len(tool_defs)} tools), stripped parameters")
    return (
        "# Tool Use\n\n"
        "You can call the following tools to help accomplish tasks. "
        "These tools connect to the user's local environment and will execute when called.\n\n"
        "Call format (use this exact format):\n"
        "```function_call\n"
        '{"name": "<tool_name>", "args": {<arguments>}}\n'
        "```\n\n"
        "When calling tools:\n"
        "- Output ONLY the function_call block(s), nothing else\n"
        "- You may call multiple tools with multiple blocks\n"
        "- After receiving a [Tool result for ...], use that data to answer the user\n\n"
        f"Available tools:\n{tool_spec}"
    )


def _google_tool_choice_instruction(req: dict) -> str:
    """Extract tool_choice constraint from Google API toolConfig."""
    tool_config = req.get("toolConfig", {})
    fc_config = tool_config.get("functionCallingConfig", {})
    mode = fc_config.get("mode", "AUTO")
    allowed = fc_config.get("allowedFunctionNames", [])

    if mode == "NONE":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if mode == "ANY":
        if allowed:
            names = ", ".join(f'"{n}"' for n in allowed)
            return f"\n\nIMPORTANT: You MUST call one of these tools: {names}. Do not respond with text only."
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    return ""


def google_contents_to_prompt(req: dict) -> tuple:
    """Convert Google API contents/tools/systemInstruction to (prompt_str, images_list).

    Returns (prompt, images) where images is a list of (bytes, mime_type) tuples.
    """
    parts = []
    images = []

    tool_config = req.get("toolConfig", {})
    fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")

    tools = req.get("tools")
    tool_defs = []
    if tools and fc_mode != "NONE":
        for tool_group in tools:
            for fn in tool_group.get("functionDeclarations", []):
                td = {"name": fn.get("name", ""), "description": fn.get("description", "")}
                params = fn.get("parameters") or fn.get("parametersJsonSchema")
                if params:
                    td["parameters"] = params
                tool_defs.append(td)

    sys_inst = req.get("systemInstruction")
    if sys_inst:
        sys_parts = sys_inst.get("parts", [])
        sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
        if sys_text:
            if tool_defs:
                constraint = _google_tool_choice_instruction(req)
                parts.append(sys_text + "\n\n" + build_tool_prompt(tool_defs) + constraint)
            else:
                parts.append(sys_text)
    elif tool_defs:
        constraint = _google_tool_choice_instruction(req)
        parts.append(build_tool_prompt(tool_defs) + constraint)

    for content in req.get("contents", []):
        role = content.get("role", "user")
        msg_parts = []
        for p in content.get("parts", []):
            if p.get("text"):
                msg_parts.append(p["text"])
            elif p.get("inlineData"):
                data = p["inlineData"]
                mime = data.get("mimeType") or "application/octet-stream"
                name = data.get("displayName") or data.get("fileName") or ""
                try:
                    images.append((base64.b64decode(data.get("data", "")), mime, name))
                    if not mime.startswith("image/"):
                        msg_parts.append(f"[Attached file: {name or mime}]")
                except Exception as e:
                    _log(f"Invalid inlineData payload: {e}")
            elif p.get("fileData"):
                fd = p["fileData"]
                uri = fd.get("fileUri") or fd.get("file_uri") or ""
                att = _attachment_from_url(uri, fd.get("displayName", ""))
                if att:
                    images.append(att)
                    if not (fd.get("mimeType") or "").startswith("image/"):
                        msg_parts.append(f"[Attached file: {att[2] or uri}]")
            elif p.get("functionCall"):
                fc = p["functionCall"]
                msg_parts.append(
                    f'```function_call\n{json.dumps({"name": fc["name"], "args": fc.get("args", {})}, ensure_ascii=False)}\n```'
                )
            elif p.get("functionResponse"):
                fr = p["functionResponse"]
                msg_parts.append(
                    f'[Tool result for {fr.get("name", "")}]: {json.dumps(fr.get("response", {}), ensure_ascii=False)}'
                )
        text = "\n".join(msg_parts)
        if role == "model":
            parts.append(f"[Assistant]: {text}")
        else:
            parts.append(text)

    return "\n\n".join(p for p in parts if p), images


def parse_google_function_calls(text: str) -> tuple:
    """Extract function_call blocks from model output.

    Handles 3 formats:
    1. ```function_call\\n{...}\\n``` (standard)
    2. function_call\\n{...} (without backticks)
    3. Raw JSON with "name" + "args" keys

    Returns (clean_text, [{"name": ..., "args": ...}])
    """
    function_calls = []
    pattern1 = r'```function_call\s*\n(.*?)\n```'
    pattern2 = r'(?:^|\n)function_call\s*\n(\{[^`]*?\})'
    clean = text
    for pattern in [pattern1, pattern2]:
        for match in re.findall(pattern, clean, re.DOTALL):
            try:
                data = json.loads(match.strip())
                if "name" in data:
                    function_calls.append({
                        "name": data["name"],
                        "args": data.get("args", data.get("arguments", {})),
                    })
            except (json.JSONDecodeError, KeyError):
                pass
        clean = re.sub(pattern, '', clean, flags=re.DOTALL).strip()
    if not function_calls and clean.strip().startswith("{"):
        try:
            data = json.loads(clean.strip())
            if "name" in data and ("args" in data or "arguments" in data):
                function_calls.append({
                    "name": data["name"],
                    "args": data.get("args", data.get("arguments", {})),
                })
                clean = ""
        except (json.JSONDecodeError, KeyError):
            pass
    return clean, function_calls

# --- http server (from gemini_web2api/) ---













def _usage(prompt: str, text: str) -> dict:
    # Gemini Web's StreamGenerate does not carry token counts (verified against
    # 2026-08-11/14 captures), so this is a char-based estimate.
    p = len(prompt) // 4
    c = len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _upload_one(index: int, item):
    """Prepare and upload a single attachment. Returns None when it fails."""
    try:
        prepared = prepare_attachment(item, index)
        if not prepared:
            return None
        data, filename, mime = prepared
        return (upload_file(data, filename, mime), filename, mime)
    except Exception as e:
        log(f"Attachment upload failed: {e}")
        return None


def _upload_attachments(attachments: list) -> list:
    """Upload images/files, returning [(file_ref, filename, mime), ...] or None.

    Accepts the (data_or_url, mime, filename) tuples produced by the prompt
    converters; remote URLs are fetched and data: URLs decoded before upload.
    Several attachments are uploaded concurrently, the way the web client fires
    its start requests; the result keeps attachment order because Gemini reads
    the payload bindings in that order.
    """
    if not attachments:
        return None
    if not load_cookie()[0]:
        log("Attachments without GEMINI_COOKIE: Gemini only accepts files on a signed-in session")
    if len(attachments) == 1:
        results = [_upload_one(0, attachments[0])]
    else:
        # Warm the shared token cache once so the workers do not all scrape the app page.
        _cached_page_tokens()
        with ThreadPoolExecutor(max_workers=min(4, len(attachments))) as pool:
            results = list(pool.map(lambda pair: _upload_one(*pair),
                                    enumerate(attachments)))
    file_refs = [r for r in results if r]
    return file_refs or None


# Backwards-compatible alias for the previous image-only helper name.
_upload_images = _upload_attachments


# --- Rate limiting (in-memory token bucket per key) --------------------------

_rate_lock = threading.Lock()
_rate_buckets = {}      # key -> {"tokens": float, "ts": monotonic}
_request_counts = {}    # key -> {"total": int, "rejected": int}


def _check_rate(key: str) -> tuple:
    """Token bucket per client key. Returns (allowed, retry_after_sec)."""
    limit = int(CONFIG.get("rate_limit") or 0)
    if limit <= 0:
        return True, 0
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets.get(key)
        if bucket is None:
            bucket = _rate_buckets[key] = {"tokens": float(limit), "ts": now}
        elapsed = now - bucket["ts"]
        bucket["tokens"] = min(float(limit), bucket["tokens"] + elapsed * limit / 60.0)
        bucket["ts"] = now
        counter = _request_counts.setdefault(key, {"total": 0, "rejected": 0})
        counter["total"] += 1
        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            return True, 0
        counter["rejected"] += 1
        retry_after = max(1, int(60 * (1 - bucket["tokens"]) / limit))
        return False, retry_after


def _request_counter_snapshot() -> dict:
    with _rate_lock:
        return {k: dict(v) for k, v in _request_counts.items()}


class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _parse_body(self, body: bytes) -> dict:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        # Constant-time comparison so the API key cannot be recovered by timing.
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if token and any(hmac.compare_digest(token, k) for k in keys):
                return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for h in ("x-api-key", "x-goog-api-key"):
            token = self.headers.get(h, "")
            if token and any(hmac.compare_digest(token, k) for k in keys):
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key="):
                    token = pair[4:]
                    if token and any(hmac.compare_digest(token, k) for k in keys):
                        return True
        return False

    def _client_key(self) -> str:
        """Identify the requester for rate limiting (key value or 'anonymous')."""
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return "anonymous"
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h):
                return self.headers[h]
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key="):
                    return pair[4:]
        return "anonymous"

    def _rate_check(self) -> bool:
        allowed, retry_after = _check_rate(self._client_key())
        if allowed:
            return True
        body = json.dumps({"error": {"message": "rate limit exceeded"}}).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _admin_authorized(self):
        admin = CONFIG.get("admin_key")
        if admin:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return hmac.compare_digest(auth[7:], str(admin))
            for h in ("x-admin-key", "x-api-key"):
                token = self.headers.get(h, "")
                if token and hmac.compare_digest(token, str(admin)):
                    return True
            return False
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True  # auth disabled entirely
        return self._authorized()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/v1"):
                if not self._authorized():
                    self.send_json({"error": {"message": "invalid api key"}}, 401)
                    return
                if not self._rate_check():
                    return
            if self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path == "/v1/diag":
                self._diag()
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__,
                            # Attachments only work on a signed-in session, so surface it here.
                            "cookie": bool(load_cookie()[0]),
                            "models": list(MODELS.keys()),
                            "image_format": CONFIG.get("image_format", "markdown"),
                            "rate_limit": CONFIG.get("rate_limit", 0)})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_request_body(self) -> bytes:
        """Read the request body, supporting Transfer-Encoding: chunked."""
        encoding = (self.headers.get("Transfer-Encoding", "") or "").lower().strip()
        if encoding == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if b";" in line:
                    line = line.split(b";", 1)[0]
                try:
                    size = int(line, 16)
                except ValueError:
                    break
                if size == 0:
                    while self.rfile.readline().strip():
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # trailing CRLF
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        try:
            if self.path == "/admin/cookie":
                self._handle_admin_cookie(self._read_request_body())
                return
            if self.path.startswith("/v1"):
                if not self._authorized():
                    self.send_json({"error": {"message": "invalid api key"}}, 401)
                    return
                if not self._rate_check():
                    return
            body = self._read_request_body()
            if self.path == "/v1/chat/completions":
                self._handle_chat(body)
            elif self.path == "/v1/responses":
                self._handle_responses(body)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    # ─── /admin/cookie (runtime cookie update, e.g. from the sync extension) ──

    def _handle_admin_cookie(self, body: bytes):
        """Apply a new cookie + session metadata at runtime (no restart).

        Auth: ADMIN_KEY env (or any API key when admin_key is unset). The payload
        matches what gemini-cookie-sync-extension exports: cookie, sapisid,
        auth_user, xsrf_token, gemini_bl. The cookie value is never logged.
        """
        if not self._admin_authorized():
            self.send_json({"error": {"message": "invalid admin key"}}, 401)
            return
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        cookie = str(req.get("cookie") or "").strip()
        if not cookie:
            self.send_json({"error": {"message": "cookie is required"}}, 400)
            return
        CONFIG["cookie"] = cookie
        if req.get("sapisid"):
            CONFIG["sapisid"] = str(req["sapisid"]).strip()
        if req.get("auth_user") is not None:
            CONFIG["auth_user"] = str(req["auth_user"]).strip() or None
        if req.get("xsrf_token"):
            CONFIG["xsrf_token"] = str(req["xsrf_token"]).strip()
        bl = req.get("gemini_bl")
        if bl and re.fullmatch(r"boq_assistant-bard-web-server_\d+\.\d+_p\d+", str(bl)):
            CONFIG["gemini_bl"] = str(bl)
        _invalidate_cookie_cache()
        log("Cookie updated at runtime (POST /admin/cookie)")
        self.send_json({"ok": True, "cookie": bool(load_cookie()[0]),
                        "has_sapisid": bool(load_cookie()[1]),
                        "bl": CONFIG["gemini_bl"], "auth_user": CONFIG.get("auth_user")})

    # ─── /v1/diag (read-only diagnostic) ───────────────────────────────────────

    def _diag(self):
        """Read-only diagnostic: did the Gemini app-page token scrape succeed?

        Reports token presence (not values) so an attachment BardErrorInfo [1100]
        can be traced to a failed page scrape (default push_id/pctx, missing
        at/f.sid) instead of guessing. Never prints the cookie value.
        """
        cookie_str, sapisid = load_cookie()
        try:
            tokens = _get_page_tokens()  # fresh scrape; ignore the 10-min cache
        except Exception as e:
            self.send_json({"cookie": bool(cookie_str), "has_sapisid": bool(sapisid),
                            "page_scrape_ok": False, "page_scrape_error": str(e)})
            return

        # at/f_sid are what the file-request path actually sends; their absence
        # means the at/f.sid fix cannot attach them and the attachment is refused.
        present = {k: bool(tokens.get(k)) for k in ("push_id", "pctx", "at", "f_sid")}
        scrape_ok = present["at"] and present["f_sid"]
        if not scrape_ok:
            hint = ("app page did not yield at/f.sid -> file requests send no XSRF and "
                    "the attachment is refused ([1100]). The cookie may not load /app "
                    "authenticated: ensure GEMINI_COOKIE is the FULL Cookie header (incl. "
                    "__Secure-1PSID) and GEMINI_AUTH_USER matches the account.")
        else:
            hint = ("at/f.sid present and sent on file requests. If attachments still "
                    "return [1100], the cookie lacks file-access scope (text works but "
                    "Gemini gates file input for this account/region); re-paste a fresh "
                    "full cookie from a signed-in browser tab that can attach files.")
        self.send_json({
            "cookie": bool(cookie_str),
            "has_sapisid": bool(sapisid),
            "auth_user": CONFIG.get("auth_user"),
            "account_prefix": _account_prefix(),
            "bl": CONFIG.get("gemini_bl"),
            "temporary_chats": CONFIG.get("temporary_chats", True),
            "xsrf_token_configured": bool(CONFIG.get("xsrf_token")),
            "page_scrape_ok": scrape_ok,
            "page_tokens": present,
            "rate_limit": CONFIG.get("rate_limit", 0),
            "requests": _request_counter_snapshot(),
            "hint": hint,
        })

    # ─── /v1/chat/completions ─────────────────────────────────────────────────

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(req.get("messages", []), tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        stream_opts = req.get("stream_options")
        include_usage = bool(stream_opts.get("include_usage")) if isinstance(stream_opts, dict) else False
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        t0 = time.perf_counter()

        if stream and (not tools or tool_choice == "none"):
            try:
                self._start_sse()
                first_chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                               "model": model_name,
                               "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(first_chunk)}\n\n".encode())
                full_text = ""
                for delta in generate_stream(prompt, model_id, think_mode, _upload_attachments(images), extra_fields):
                    full_text += delta
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                end = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
                if include_usage:
                    usage_chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                   "model": model_name, "choices": [], "usage": _usage(prompt, full_text)}
                    self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                log(f"chat: model={model_name} stream=True tools={bool(tools)} "
                    f"prompt_len={len(prompt)} {int((time.perf_counter()-t0)*1000)}ms")
            return

        out = {}
        try:
            text = generate(prompt, model_id, think_mode, _upload_attachments(images), extra_fields, out)
        except Exception as e:
            log(f"chat: model={model_name} stream=False tools={bool(tools)} "
                f"prompt_len={len(prompt)} error={type(e).__name__} "
                f"{int((time.perf_counter()-t0)*1000)}ms")
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        finish_status = out.get("finish_status")
        if finish_status not in (None, 0):
            log(f"Unmapped Gemini finish status {finish_status} (model={model_name})")

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        log(f"chat: model={model_name} stream=False tools={bool(tools)} "
            f"prompt_len={len(prompt)} {int((time.perf_counter()-t0)*1000)}ms")

        if stream:
            self._start_sse()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            if include_usage:
                usage_chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                               "model": model_name, "choices": [], "usage": _usage(prompt, text)}
                self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": _usage(prompt, text),
            })

    # ─── /v1/responses (Codex CLI) ───────────────────────────────────────────

    def _handle_responses(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")
        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text": text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call": tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        content = item.get("content", "")
                        # Keep list content as-is so image/file parts survive into
                        # messages_to_prompt() and get uploaded as attachments.
                        messages.append({"role": role, "content": content})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        t0 = time.perf_counter()
        try:
            text = generate(prompt, model_id, think_mode, _upload_attachments(images), extra_fields)
        except Exception as e:
            log(f"responses: model={model_name} error={type(e).__name__} "
                f"{int((time.perf_counter()-t0)*1000)}ms")
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        usage = {"input_tokens": _usage(prompt, text)["prompt_tokens"],
                 "output_tokens": _usage(prompt, text)["completion_tokens"],
                 "total_tokens": _usage(prompt, text)["total_tokens"]}
        log(f"responses: model={model_name} {int((time.perf_counter()-t0)*1000)}ms")

        if req.get("stream"):
            self._start_sse()
            seq = [0]

            def emit(ev_type, **fields):
                seq[0] += 1
                ev = {"type": ev_type, "sequence_number": seq[0], **fields}
                self.wfile.write(f"event: {ev_type}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()

            base_resp = {"id": rid, "object": "response", "created_at": int(time.time()), "model": model_name}
            emit("response.created", response={**base_resp, "status": "in_progress", "output": [], "usage": None})
            emit("response.in_progress", response={**base_resp, "status": "in_progress", "output": [], "usage": None})
            for oi, item in enumerate(output):
                if item["type"] == "function_call":
                    pending = {"type": "function_call", "id": item["id"], "call_id": item["call_id"],
                               "name": item["name"], "arguments": "", "status": "in_progress"}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    emit("response.function_call_arguments.delta", item_id=item["id"], output_index=oi, delta=item["arguments"])
                    emit("response.function_call_arguments.done", item_id=item["id"], output_index=oi, arguments=item["arguments"])
                    emit("response.output_item.done", output_index=oi, item=item)
                elif item["type"] == "message":
                    pending = {"type": "message", "id": item["id"], "role": "assistant", "status": "in_progress", "content": []}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    for ci, cp in enumerate(item["content"]):
                        emit("response.content_part.added", item_id=item["id"], output_index=oi, content_index=ci,
                             part={"type": "output_text", "text": "", "annotations": []})
                        emit("response.output_text.delta", item_id=item["id"], output_index=oi, content_index=ci, delta=cp["text"])
                        emit("response.output_text.done", item_id=item["id"], output_index=oi, content_index=ci, text=cp["text"])
                        emit("response.content_part.done", item_id=item["id"], output_index=oi, content_index=ci, part=cp)
                    emit("response.output_item.done", output_index=oi, item=item)
            emit("response.completed", response={**base_resp, "status": "completed", "output": output, "usage": usage})
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output, "usage": usage})

    # ─── /v1beta/models (Google Gemini CLI) ──────────────────────────────────

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        model_name = m.group(1) if m else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"
        prompt, images = google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        file_refs = _upload_attachments(images)
        t0 = time.perf_counter()

        if stream and not has_tools:
            try:
                self._start_sse()
                full_text = ""
                for delta in generate_stream(prompt, model_id, think_mode, file_refs, extra_fields):
                    if not delta:
                        continue
                    full_text += delta
                    chunk_obj = {
                        "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"}, "index": 0}],
                        "modelVersion": model_name,
                    }
                    self.wfile.write(f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                }
                self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                log(f"Google API: model={model_name} stream=True tools={has_tools} "
                    f"prompt_len={len(prompt)} {int((time.perf_counter()-t0)*1000)}ms")
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            log(f"Google API: model={model_name} stream={stream} tools={has_tools} "
                f"prompt_len={len(prompt)} error={type(e).__name__} "
                f"{int((time.perf_counter()-t0)*1000)}ms")
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if not text:
            log("Warning: empty response from Gemini")

        log(f"Google API: model={model_name} stream={stream} tools={has_tools} "
            f"prompt_len={len(prompt)} {int((time.perf_counter()-t0)*1000)}ms")

        response_parts = []
        if has_tools and text:
            clean_text, function_calls = parse_google_function_calls(text)
            if function_calls:
                if clean_text:
                    response_parts.append({"text": clean_text})
                for fc in function_calls:
                    response_parts.append({"functionCall": {"name": fc["name"], "args": fc["args"]}})
            else:
                response_parts.append({"text": text})
        else:
            response_parts.append({"text": text or "I apologize, but I was unable to generate a response. Please try again."})

        candidate = {
            "content": {"parts": response_parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text or "") // 4,
            "totalTokenCount": (len(prompt) + len(text or "")) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self._start_sse()
            self.wfile.write(f"data: {json.dumps(response_obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# --- entry point (from gemini_web2api/__main__.py) ---

def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    # Precedence: defaults < config.json (optional) < env vars < CLI flags
    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG") or find_config()
    if config_path:
        load_config(config_path)
    load_env_config()

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    # Keep the StreamGenerate build label fresh; gated by AUTO_UPDATE_BL=false.
    update_bl_if_needed()

    cookie_str, sapisid = load_cookie()
    if CONFIG.get("cookie"):
        cookie_src = "env GEMINI_COOKIE"
    elif cookie_str:
        cookie_src = f"file {CONFIG.get('cookie_file')}"
    else:
        cookie_src = "none (anonymous)"

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://{CONFIG['host']}:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Config:    {config_path or 'env vars only'}")
    print(f"  Cookie:    {cookie_src}{' + SAPISID' if sapisid else ''}")
    print(f"  API keys:  {len(CONFIG.get('api_keys') or [])} configured")
    print(f"  Admin key: {'set' if CONFIG.get('admin_key') else 'not set'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'system env'}")
    print(f"  Streaming: {'httpx (true streaming)' if HAS_HTTPX else 'urllib (buffered)'}")
    print(f"  Rate limit: {CONFIG.get('rate_limit', 0)} req/min/key"
          f"{' (disabled)' if not CONFIG.get('rate_limit') else ''}")
    print(f"  Image format: {CONFIG.get('image_format', 'markdown')}")
    print(f"  BL:        {CONFIG['gemini_bl']}"
          f"{' (auto-update off)' if not CONFIG.get('auto_update_bl', True) else ''}")
    print(f"  Token cache: {CONFIG.get('token_cache_file') or 'off'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
