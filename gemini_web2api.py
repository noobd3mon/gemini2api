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
import urllib.error
import urllib.parse
import ssl
import hashlib
import mimetypes
import base64
import ipaddress
import socket
import binascii
from urllib.parse import unquote_to_bytes
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
    "cookie": None,        # raw cookie string (env GEMINI_COOKIE), preferred over cookie_file
    "sapisid": None,       # optional override, else parsed from the cookie
    "cookie_file": None,  # local file fallback (legacy / desktop use)
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
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
}

ENV_INT = {
    "port": ("PORT", "GEMINI_PORT"),
    "retry_attempts": ("RETRY_ATTEMPTS",),
    "retry_delay_sec": ("RETRY_DELAY_SEC",),
    "request_timeout_sec": ("REQUEST_TIMEOUT_SEC",),
}

ENV_BOOL = {
    "log_requests": ("LOG_REQUESTS",),
    "temporary_chats": ("TEMPORARY_CHATS",),
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


def load_cookie() -> tuple:
    """Load the Gemini cookie.

    An inline cookie (CONFIG["cookie"], usually from the GEMINI_COOKIE env var)
    is preferred and parsed in memory; cookie_file is a file-based fallback with
    mtime-based caching. Returns (cookie_str, sapisid).
    """
    inline = (CONFIG.get("cookie") or "").strip()
    if inline.startswith('"') and inline.endswith('"'):
        inline = inline[1:-1].strip()
    if inline:
        pairs = dict(p.split("=", 1) for p in inline.split("; ") if "=" in p)
        sapisid = CONFIG.get("sapisid") or pairs.get("SAPISID")
        return inline, sapisid or None

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


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _page_token(name: str):
    """Read a scraped Gemini app-page token (push_id/pctx/at/f_sid).

    The tokens live in multimodal._cached_page_tokens(). In the package the
    name comes from a relative import; in the merged single file it is a
    shared global, so try the global first and fall back (the build strips
    relative import lines, so it must never stand alone as a block body).
    The cache is warmed by the upload step, so this is a cache hit for
    attachment requests and costs nothing (no fetch) for text-only ones.
    """
    try:
        return _cached_page_tokens().get(name)
    except NameError:
        try:
            return _cached_page_tokens().get(name)
        except Exception:
            return None
    except Exception:
        return None


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
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
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
    """Return (bytes, mime) for a data: URL, or (b"", "") when it is not one."""
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


def detect_image_mime(image_bytes: bytes, fallback: str = "image/png") -> str:
    """Infer a common raster image MIME type from its file signature."""
    if not isinstance(image_bytes, (bytes, bytearray)):
        return fallback
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
    return fallback


def _get_page_tokens() -> dict:
    """Fetch WIZ_global_data tokens from the Gemini page.

    Scrapes Push-ID (qKIAYe), X-Client-Pctx (Ylro7b), XSRF `at` (thykhd) and
    f.sid (FdrFJe); the latter two are required on file-bearing StreamGenerate
    requests, the way the web client always sends them.
    """
    headers = {"User-Agent": UA}
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
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


def _cached_page_tokens() -> dict:
    now = time.time()
    if now - _page_tokens_cache["ts"] > 600:
        _page_tokens_cache["tokens"] = _get_page_tokens()
        _page_tokens_cache["ts"] = now
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

    log(f"Upload session started: {upload_url[:80]}...")

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


def _is_private_ip(hostname: str) -> bool:
    """Check if hostname resolves to a private/internal IP address."""
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass
    try:
        ip_str = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except (socket.gaierror, OSError, ValueError):
        return True


def fetch_file_bytes(url: str):
    """Fetch a remote or data: URL. Returns (bytes, mime).

    Blocks private/internal addresses so a client-supplied URL cannot be used
    to probe the host network or cloud metadata endpoints (SSRF).
    """
    if isinstance(url, str) and url.startswith("data:"):
        return decode_data_url(url)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        log(f"File fetch skipped for unsupported URL scheme: {parsed.scheme or 'none'}")
        return b"", ""
    hostname = parsed.hostname or parsed.netloc
    if not hostname or _is_private_ip(hostname):
        log(f"File fetch blocked: private/internal address {hostname}")
        return b"", ""
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
    (data_or_url, mime, filename). Remote URLs are fetched here (with SSRF
    blocking) and the mime is refined from the file signature when it is a
    known raster format.
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
    mime = detect_image_mime(data, mime)
    if not filename:
        filename = guess_filename(mime, index)
    return bytes(data), filename, mime

# --- tool calling (from gemini_web2api/) ---










# Gemini silently truncates very long prompts; keep the tools block bounded.
PROMPT_MAX_BYTES = 60000


def _log(msg: str) -> None:
    """Log through gemini.log.

    In the package the name comes from a relative import; in the merged single
    file `log` is a shared global, so try the global first and fall back (the
    build strips relative import lines, so it must never stand alone as a
    block body).
    """
    try:
        log(msg)
    except NameError:
        try:
            log(msg)
        except Exception:
            pass
    except Exception:
        pass


def _decode_data_url(url: str):
    """Return (bytes, mime) for a data: URL, or (b"", mime) when undecodable."""
    header, _sep, payload = url.partition(",")
    mime = header[5:].split(";")[0].strip() or "application/octet-stream"
    if ";base64" in header:
        try:
            return base64.b64decode(payload), mime
        except Exception as e:
            _log(f"Invalid base64 data URL: {e}")
            return b"", mime
    return unquote_to_bytes(payload), mime


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
        att = _attachment_from_url(url, filename)
        if att:
            return att
        raw = part.get("data") or part.get("base64")
        if isinstance(raw, str) and raw:
            if raw.startswith("data:"):
                data, mime = _decode_data_url(raw)
                return (data, mime, filename) if data else None
            try:
                mime = (part.get("mime_type") or part.get("media_type")
                        or mimetypes.guess_type(filename or "")[0] or "image/png")
                return (base64.b64decode(raw, validate=True), mime, filename)
            except (ValueError, TypeError, binascii.Error):
                return None
        return None
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
    """Convert OpenAI messages to (prompt_str, attachments_list).

    Returns (prompt, attachments) where attachments is a list of
    (data_or_url, mime, filename) tuples; data_or_url is raw bytes for inline
    payloads or a URL string fetched right before upload.
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
                    # URL attachments have no mime yet; guess from the filename
                    # so image URLs still get the image marker.
                    mime = att[1] or mimetypes.guess_type(att[2] or "")[0] or ""
                    if mime.startswith("image/"):
                        text_parts.append("[Image attached]")
                    else:
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
    """Convert Google API contents/tools/systemInstruction to (prompt_str, attachments_list).

    Returns (prompt, attachments) where attachments is a list of
    (data_or_url, mime, filename) tuples; data_or_url is raw bytes for inline
    payloads or a URL string fetched right before upload.
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
                    raw = base64.b64decode(data.get("data", ""), validate=True)
                    if not raw:
                        raise ValueError("empty inlineData payload")
                    images.append((raw, mime, name))
                    if mime.startswith("image/"):
                        msg_parts.append("[Image attached]")
                    else:
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
                    else:
                        msg_parts.append("[Image attached]")
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
    p = len(prompt) // 4
    c = len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _upload_attachments(attachments: list) -> list:
    """Upload images/files, returning [(file_ref, filename, mime), ...] or None.

    Accepts the (data_or_url, mime, filename) tuples produced by the prompt
    converters; remote URLs are fetched and data: URLs decoded before upload.
    Several attachments are uploaded concurrently, the way the web client fires
    its start requests; the result keeps attachment order because Gemini reads
    the payload bindings in that order. Failed attachments are skipped, but when
    every attachment fails a RuntimeError is raised so the caller can return a
    502 instead of silently generating without the files the user sent.
    """
    if not attachments:
        return None
    if not load_cookie()[0]:
        log("Attachments without GEMINI_COOKIE: Gemini only accepts files on a signed-in session")
    errors = []

    def run(pair):
        index, item = pair
        try:
            prepared = prepare_attachment(item, index)
            if not prepared:
                errors.append(f"attachment {index + 1}: unusable input")
                return None
            data, filename, mime = prepared
            return (upload_file(data, filename, mime), filename, mime)
        except Exception as e:
            errors.append(f"attachment {index + 1}: {e}")
            return None

    if len(attachments) == 1:
        results = [run((0, attachments[0]))]
    else:
        # Warm the shared token cache once so the workers do not all scrape the app page.
        _cached_page_tokens()
        with ThreadPoolExecutor(max_workers=min(4, len(attachments))) as pool:
            results = list(pool.map(run, enumerate(attachments)))

    file_refs = [r for r in results if r]
    if not file_refs:
        raise RuntimeError("attachment upload failed: " + "; ".join(errors))
    return file_refs


# Backwards-compatible alias for the previous image-only helper name.
_upload_images = _upload_attachments


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

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks = []
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size_text = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    raise ValueError("invalid chunked request body")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        # Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in keys:
            return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h, "") in keys:
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and pair[4:] in keys:
                    return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
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
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__, "models": list(MODELS.keys()),
                                "cookie": bool(load_cookie()[0])})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            body = self._read_request_body()
            if self.path == "/v1/chat/completions":
                self._handle_chat(body)
            elif self.path == "/v1/responses":
                self._handle_responses(body)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
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
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        try:
            file_refs = _upload_attachments(images)
        except RuntimeError as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if stream and (not tools or tool_choice == "none"):
            try:
                self._start_sse()
                first_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }],
                }
                self.wfile.write(f"data: {json.dumps(first_chunk)}\n\n".encode())
                self.wfile.flush()
                for delta in generate_stream(prompt, model_id, think_mode, file_refs, extra_fields):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                end = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            self._start_sse()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text or "")//4,
                          "total_tokens": (len(prompt)+len(text or ""))//4},
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
                    elif item.get("type") in ("input_text", "input_image", "image"):
                        messages.append({"role": "user", "content": [item]})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text":
                                        text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call":
                                        tc_list.append(c)
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
                        messages.append({"role": role, "content": item.get("content", "")})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            file_refs = _upload_attachments(images)
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
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

        if req.get("stream"):
            self._start_sse()
            sequence_number = 0

            def emit(event_type, **fields):
                nonlocal sequence_number
                sequence_number += 1
                event = {
                    "type": event_type,
                    "sequence_number": sequence_number,
                    **fields,
                }
                self.wfile.write(
                    f"event: {event_type}\ndata: {json.dumps(event)}\n\n".encode()
                )

            usage = {
                "input_tokens": len(prompt) // 4,
                "output_tokens": len(text or "") // 4,
                "total_tokens": (len(prompt) + len(text or "")) // 4,
            }
            base_response = {
                "id": rid,
                "object": "response",
                "created_at": int(time.time()),
                "model": model_name,
            }
            emit(
                "response.created",
                response={
                    **base_response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            )
            emit(
                "response.in_progress",
                response={
                    **base_response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            )
            for output_index, item in enumerate(output):
                if item["type"] == "function_call":
                    pending_item = {
                        "type": "function_call",
                        "id": item["id"],
                        "call_id": item["call_id"],
                        "name": item["name"],
                        "arguments": "",
                        "status": "in_progress",
                    }
                    emit(
                        "response.output_item.added",
                        output_index=output_index,
                        item=pending_item,
                    )
                    emit(
                        "response.function_call_arguments.delta",
                        item_id=item["id"],
                        output_index=output_index,
                        delta=item["arguments"],
                    )
                    emit(
                        "response.function_call_arguments.done",
                        item_id=item["id"],
                        output_index=output_index,
                        arguments=item["arguments"],
                    )
                    emit(
                        "response.output_item.done",
                        output_index=output_index,
                        item=item,
                    )
                elif item["type"] == "message":
                    pending_item = {
                        "type": "message",
                        "id": item["id"],
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    emit(
                        "response.output_item.added",
                        output_index=output_index,
                        item=pending_item,
                    )
                    for content_index, content_part in enumerate(item["content"]):
                        event_fields = {
                            "item_id": item["id"],
                            "output_index": output_index,
                            "content_index": content_index,
                        }
                        emit(
                            "response.content_part.added",
                            **event_fields,
                            part={
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        )
                        emit(
                            "response.output_text.delta",
                            **event_fields,
                            delta=content_part["text"],
                        )
                        emit(
                            "response.output_text.done",
                            **event_fields,
                            text=content_part["text"],
                        )
                        emit(
                            "response.content_part.done",
                            **event_fields,
                            part=content_part,
                        )
                    emit(
                        "response.output_item.done",
                        output_index=output_index,
                        item=item,
                    )
            emit(
                "response.completed",
                response={
                    **base_response,
                    "status": "completed",
                    "output": output,
                    "usage": usage,
                },
            )
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text or "")//4, "total_tokens": (len(prompt)+len(text or ""))//4}})

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

        try:
            file_refs = _upload_attachments(images)
        except RuntimeError as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        log(f"Google API: model={model_name} stream={stream} tools={has_tools} prompt_len={len(prompt)}")

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
            except Exception as e:
                log(f"Google stream error: {e}")
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if not text:
            log("Warning: empty response from Gemini")

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

    # Precedence: DEFAULT_CONFIG < config.json < environment variables < CLI flags.
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

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    if CONFIG.get("cookie"):
        cookie_src = "env GEMINI_COOKIE"
    elif CONFIG.get("cookie_file"):
        cookie_src = f"file {CONFIG['cookie_file']}"
    else:
        cookie_src = "none (anonymous)"
    print(f"  Cookie:    {cookie_src}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'system env'}")
    print(f"  Streaming: {'httpx (true streaming)' if HAS_HTTPX else 'urllib (buffered)'}")
    print(f"  Temporary: {'yes' if CONFIG.get('temporary_chats', False) else 'no'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
