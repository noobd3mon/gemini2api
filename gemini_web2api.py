#!/usr/bin/env python3
"""
gemini-web2api - Gemini Web to OpenAI API proxy.

Converts Google Gemini's web interface into an OpenAI-compatible API server.
Zero authentication required. Works on any platform (Windows/macOS/Linux).

Usage:
    pip install httpx
    python gemini_web2api.py [--port 8081] [--config config.json]

Client configuration (Cherry Studio, ChatBox, etc.):
    Base URL: http://localhost:8081/v1
    API Key: (anything or empty)

How it works:
    Sends requests directly to Gemini's public StreamGenerate endpoint.
    The backend does not verify authentication for basic text generation.
    Model selection via MODE_CATEGORY field [79] in the request payload.
    This is NOT a user-tier spoofing attack - the endpoint simply doesn't
    require auth for anonymous access.
"""
import json
import urllib.request
import urllib.parse
import time
import ssl
import sys
import uuid
import re
import os
import hashlib
import argparse
import base64
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

__version__ = "1.1.0"

# ─── Configuration ───────────────────────────────────────────────────────────

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
    "cookie": None,
    "sapisid": None,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": True,
    "auto_update_bl": True,
}

CONFIG = dict(DEFAULT_CONFIG)

# ─── Models ──────────────────────────────────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum (028-6eb337387583.js)
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "Gemini 3.7 Flash (FAST; mode 1 - same wire id as 3.6-flash, renamed)",
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

# ─── Utilities ───────────────────────────────────────────────────────────────

def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


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
    """Inline cookie (env/config) first, then cookie_file. Returns (cookie_str, sapisid)."""
    inline = CONFIG.get("cookie")
    if inline:
        return _cookie_from_string(str(inline))
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file:
        return "", None
    if not os.path.exists(cookie_file):
        return "", None
    try:
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
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload.

    The real web client sends inner[41]=[1] + inner[45]=1 (temporary) for every
    StreamGenerate, including file-bearing requests (verified against a
    2026-08-11 capture). The proxy defaults to temporary so API calls do not
    litter the user's Gemini history with saved conversations.
    """
    if CONFIG.get("temporary_chats", True):
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def fetch_latest_bl() -> str | None:
    """Fetch the latest gemini_bl from gemini.google.com page."""
    if not CONFIG.get("auto_update_bl", True):
        return None
    try:
        req = urllib.request.Request(
            "https://gemini.google.com/app",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        ctx = ssl.create_default_context()
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


# ─── Gemini Protocol ─────────────────────────────────────────────────────────

# ─── Multimodal: Scotty resumable upload ─────────────────────────────────────

UPLOAD_ENDPOINTS = (
    "https://push.clients6.google.com/upload/",
    "https://content-push.googleapis.com/upload/",
)
DEFAULT_PUSH_ID = "feeds/mcudyrk2a4khkz"
DEFAULT_PCTX = "CgcSBWjK7pYx"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PROMPT_MAX_BYTES = 60000
_page_tokens_cache = {"tokens": {}, "ts": 0.0}


def _upload_open(req, timeout: int = 60):
    """urlopen with the same SSL/proxy behaviour as the chat calls."""
    ctx = ssl.create_default_context()
    proxy = CONFIG.get("proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
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


def decode_data_url(url: str) -> tuple:
    """Return (bytes, mime) for a data: URL."""
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


def fetch_file_bytes(url: str) -> tuple:
    """Fetch a remote (or data:) URL. Returns (bytes, mime)."""
    if isinstance(url, str) and url.startswith("data:"):
        return decode_data_url(url)
    try:
        resp = _upload_open(urllib.request.Request(url, headers={"User-Agent": UPLOAD_UA}), 60)
        data = resp.read(MAX_UPLOAD_BYTES + 1)
        return data, (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    except Exception as e:
        log(f"File fetch failed: {e}")
        return b"", ""


def get_page_tokens() -> dict:
    """Scrape Push-ID / pctx / at tokens from the Gemini app page (cached 10 min)."""
    now = time.time()
    if _page_tokens_cache["tokens"] and now - _page_tokens_cache["ts"] <= 600:
        return _page_tokens_cache["tokens"]
    headers = {"User-Agent": UPLOAD_UA}
    cookie_str = load_cookie()[0]
    if cookie_str:
        headers["Cookie"] = cookie_str
    tokens = {}
    try:
        url = "https://gemini.google.com" + account_prefix() + "/app"
        html = _upload_open(urllib.request.Request(url, headers=headers), 30).read().decode("utf-8", errors="replace")
        for key, pattern in (("push_id", r'"qKIAYe":"([^"]+)"'),
                             ("pctx", r'"Ylro7b":"([^"]+)"'),
                             ("at", r'"thykhd":"([^"]+)"'),
                             ("f_sid", r'"FdrFJe":"([^"]+)"')):
            m = re.search(pattern, html)
            if m:
                tokens[key] = m.group(1)
    except Exception as e:
        log(f"Page token fetch failed: {e}")
    _page_tokens_cache["tokens"] = tokens
    _page_tokens_cache["ts"] = now
    return tokens


def upload_file(data: bytes, filename: str = None, mime_type: str = None) -> str:
    """Upload bytes via Scotty resumable upload; returns the file reference path."""
    if not data:
        raise ValueError("empty file data")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file too large: {len(data)} bytes (max {MAX_UPLOAD_BYTES})")
    mime = mime_type or guess_mime(filename or "")
    name = filename or guess_filename(mime)
    tokens = get_page_tokens()
    cookie_str = load_cookie()[0]
    base_headers = {
        "User-Agent": UPLOAD_UA,
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "Push-ID": tokens.get("push_id", DEFAULT_PUSH_ID),
        "X-Client-Pctx": tokens.get("pctx", DEFAULT_PCTX),
        "X-Tenant-Id": "bard-storage",
        "X-Goog-Upload-Protocol": "resumable",
    }
    if cookie_str:
        base_headers["Cookie"] = cookie_str
    last_error = None
    for endpoint in UPLOAD_ENDPOINTS:
        try:
            start_headers = dict(base_headers)
            start_headers["X-Goog-Upload-Command"] = "start"
            start_headers["X-Goog-Upload-Header-Content-Length"] = str(len(data))
            start_headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            start_body = urllib.parse.urlencode({"File name: " + name: ""}).encode()
            resp = _upload_open(urllib.request.Request(endpoint, data=start_body, headers=start_headers), 60)
            upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get("x-goog-upload-url")
            resp.read()
            if not upload_url:
                raise RuntimeError("no upload URL returned")
            finish_headers = dict(base_headers)
            finish_headers["X-Goog-Upload-Command"] = "upload, finalize"
            finish_headers["X-Goog-Upload-Offset"] = "0"
            # Capture shows the browser finalizes as a form post, not as the file mime.
            finish_headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"
            resp2 = _upload_open(urllib.request.Request(upload_url, data=data, headers=finish_headers), 120)
            file_ref = resp2.read().decode("utf-8", errors="replace").strip()
            if not file_ref:
                raise RuntimeError("empty upload response")
            log(f"Uploaded {name} ({len(data)} bytes, {mime})")
            return file_ref
        except Exception as e:
            last_error = e
            log(f"Upload endpoint failed: {e}")
    raise RuntimeError(f"upload failed: {last_error}")


def build_file_bindings(file_refs: list) -> list:
    """inner[0][3] format: [[[<ref>, <kind>, None, <mime>], <filename>], ...]

    <kind> is 1 for images and 3 for any other file type: a real 3-file capture
    sent 1 for image/png and image/jpeg, but 3 for text/plain.
    """
    if not file_refs:
        return None
    bindings = []
    for entry in file_refs:
        if isinstance(entry, (list, tuple)):
            ref = entry[0]
            filename = entry[1] if len(entry) > 1 else ""
            mime = entry[2] if len(entry) > 2 else ""
        else:
            ref, filename, mime = entry, "", ""
        if not ref:
            continue
        if not mime and filename:
            mime = mimetypes.guess_type(filename)[0] or ""
        mime = mime or "application/octet-stream"
        kind = 1 if mime.startswith("image/") else 3
        bindings.append([[ref, kind, None, mime], filename or guess_filename(mime)])
    return bindings or None


def gemini_stream_generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None) -> str:
    """Send prompt to Gemini StreamGenerate with retry."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, build_file_bindings(file_refs), None, None, 0]
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
    apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    elif file_refs:
        at = get_page_tokens().get("at")
        if at:
            params["at"] = at
    body = urllib.parse.urlencode(params).encode()
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    if file_refs:
        fsid = get_page_tokens().get("f_sid")
        if fsid:
            url += f"&f.sid={fsid}"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])

    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 405 and update_bl_if_needed():
                reqid = int(time.time()) % 1000000
                url = (
                    f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
                    "assistant.lamda.BardFrontendService/StreamGenerate"
                    f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
                )
                if file_refs:
                    fsid = get_page_tokens().get("f_sid")
                    if fsid:
                        url += f"&f.sid={fsid}"
                log("Retrying with updated BL...")
                last_err = e
                continue
            if 400 <= e.code < 500:
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


def attachment_from_url(url, filename: str = ""):
    """Normalise a URL-ish value into (bytes_or_url, mime, filename) or None."""
    if isinstance(url, dict):
        url = url.get("url") or url.get("uri") or ""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        data, mime = decode_data_url(url)
        if not data:
            return None
        return (data, mime, filename or guess_filename(mime))
    if url.startswith("http://") or url.startswith("https://"):
        name = filename or urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        return (url, "", name)
    return None


def extract_attachment(part: dict):
    """Extract an attachment from an OpenAI/Anthropic style content part."""
    if not isinstance(part, dict):
        return None
    ptype = part.get("type", "")
    if ptype in ("image_url", "input_image", "image"):
        source = part.get("image_url") or part.get("image") or part.get("source") or part
        if isinstance(source, dict) and (source.get("type") == "base64" or source.get("data")):
            try:
                data = base64.b64decode(source.get("data", ""))
            except Exception as e:
                log(f"Invalid base64 image: {e}")
                return None
            mime = source.get("media_type") or "image/png"
            return (data, mime, source.get("filename") or guess_filename(mime))
        url = source.get("url") if isinstance(source, dict) else source
        return attachment_from_url(url or part.get("url"), part.get("filename", ""))
    if ptype in ("file", "input_file", "document"):
        source = part.get("file") or part.get("source") or part
        if isinstance(source, dict):
            raw = source.get("file_data") or source.get("data")
            if raw:
                if isinstance(raw, str) and raw.startswith("data:"):
                    return attachment_from_url(raw, source.get("filename", ""))
                try:
                    data = base64.b64decode(raw)
                except Exception as e:
                    log(f"Invalid base64 file: {e}")
                    return None
                mime = source.get("media_type") or guess_mime(source.get("filename", ""))
                return (data, mime, source.get("filename") or guess_filename(mime))
            url = source.get("file_url") or source.get("url") or source.get("uri")
            if url:
                return attachment_from_url(url, source.get("filename", ""))
    return None


def prepare_attachment(item, index: int = 0):
    """Normalise (data|url, mime, filename) into (bytes, filename, mime)."""
    mime, filename = "", ""
    if isinstance(item, (list, tuple)):
        data = item[0]
        mime = item[1] if len(item) > 1 else ""
        filename = item[2] if len(item) > 2 else ""
    else:
        data = item
    if isinstance(data, str):
        fetched, fetched_mime = fetch_file_bytes(data)
        if not fetched:
            return None
        data, mime = fetched, mime or fetched_mime
    if not data:
        return None
    if not mime:
        mime = guess_mime(filename) if filename else "application/octet-stream"
    return data, filename or guess_filename(mime, index), mime


def upload_one_attachment(index: int, item):
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


def upload_attachments(attachments: list):
    """Upload attachments; returns [(file_ref, filename, mime), ...] or None.

    Several attachments go up concurrently, the way the web client fires its
    start requests; the result keeps attachment order because Gemini reads the
    payload bindings in that order.
    """
    if not attachments:
        return None
    if not load_cookie()[0]:
        log("Attachments without GEMINI_COOKIE: Gemini only accepts files on a signed-in session")
    if len(attachments) == 1:
        results = [upload_one_attachment(0, attachments[0])]
    else:
        # Warm the shared token cache once so the workers do not all scrape the app page.
        get_page_tokens()
        with ThreadPoolExecutor(max_workers=min(4, len(attachments))) as pool:
            results = list(pool.map(lambda pair: upload_one_attachment(*pair),
                                    enumerate(attachments)))
    return [r for r in results if r] or None


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the longest common prefix of two strings (codepoint-wise)."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def gemini_stream_generate_iter(prompt: str, model_id: int, think_mode: int, file_refs: list = None):
    """Send prompt and yield incremental text deltas using httpx streaming."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, build_file_bindings(file_refs), None, None, 0]
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
    apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    elif file_refs:
        at = get_page_tokens().get("at")
        if at:
            params["at"] = at
    body = urllib.parse.urlencode(params)
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    if file_refs:
        fsid = get_page_tokens().get("f_sid")
        if fsid:
            url += f"&f.sid={fsid}"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    proxy = CONFIG.get("proxy")

    if not HAS_HTTPX:
        # Fallback: non-streaming with urllib
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    prev_text = ""
    emitted_images = set()
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
        try:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    if "BardErrorInfo" in buf and not prev_text:
                        err = bard_error_message(buf)
                        if err:
                            raise GeminiUpstreamError(err)
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if '"wrb.fr"' not in line or len(line) < 200:
                            continue
                        try:
                            arr = json.loads(line)
                            inner_str = arr[0][2]
                            if not inner_str or len(inner_str) < 50:
                                continue
                            inner2 = json.loads(inner_str)
                            if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                                for part in inner2[4]:
                                    if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                        for t in part[1]:
                                            if not isinstance(t, str) or not t:
                                                continue
                                            if t == prev_text or prev_text.startswith(t):
                                                continue
                                            if t.startswith(prev_text):
                                                delta = clean_gemini_text(t[len(prev_text):], strip=False)
                                            else:
                                                # Gemini revised mid-stream: `t` is not a prefix-extension
                                                # of what we already streamed. SSE deltas are append-only so
                                                # we can't retract the old text; emit only the part beyond
                                                # the common prefix so the stream completes instead of
                                                # dropping (a drop makes the client retry and concatenate,
                                                # repeating the whole intro).
                                                cp = _common_prefix_len(prev_text, t)
                                                delta = clean_gemini_text(t[cp:], strip=False)
                                                if delta and delta in prev_text:
                                                    delta = ""
                                            prev_text = t
                                            if delta:
                                                yield delta
                            for gg in _find_gg_dl_urls(inner2):
                                if gg not in emitted_images:
                                    emitted_images.add(gg)
                                    yield f"\n\n![generated image]({resolve_image_url(gg)})\n"
                        except (json.JSONDecodeError, IndexError, TypeError):
                            pass
        except Exception as e:
            if HAS_HTTPX and hasattr(e, 'response') and getattr(e.response, 'status_code', 0) == 405:
                if update_bl_if_needed():
                    log("BL updated, falling back to non-streaming for this request")
                    raw = gemini_stream_generate(prompt, model_id, think_mode, file_refs)
                    text = extract_response_text(raw)
                    if text:
                        yield text
                    return
            if HAS_HTTPX and hasattr(e, 'response') and 400 <= getattr(e.response, 'status_code', 0) < 500:
                snippet = ""
                try:
                    snippet = e.response.read().decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                raise GeminiUpstreamError(
                    f"StreamGenerate HTTP {e.response.status_code}"
                    + (f": {snippet}" if snippet else ""))
            raise


def clean_gemini_text(text: str, strip: bool = True) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/(?:card_content|image_generation_content)/\w+\n?', '', text)
    return text.strip() if strip else text


# Generated images come back as lh3.googleusercontent.com/gg-dl/<token> resolver URLs
# nested in the response. GET-ing a gg-dl URL returns text/plain = the directly
# viewable rd-gg-dl/<token> image URL (image/jpeg, no auth - just a gemini referer).
# The text part of an image reply is a useless image_generation_content placeholder,
# which clean_gemini_text strips; we append the resolved image as markdown instead.
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
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str:
            return []
        return _find_gg_dl_urls(json.loads(inner_str))
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def resolve_image_url(gg_url: str, timeout: int = 15) -> str:
    """Resolve a gg-dl resolver URL to the directly viewable image URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gemini.google.com/",
    }
    try:
        req = urllib.request.Request(gg_url, headers=headers)
        resp = urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=timeout)
        body = resp.read().decode("utf-8", errors="replace").strip()
        if body.startswith("http") and "usercontent" in body:
            return body
    except Exception as e:
        log(f"image resolve failed: {e}")
    return gg_url


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
    """Parse StreamGenerate response to extract final text + generated images."""
    texts = []
    image_urls = []
    seen = set()
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
        for gg in _extract_image_urls_from_line(line):
            if gg not in seen:
                seen.add(gg)
                image_urls.append(gg)
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    if not text and not image_urls:
        err = bard_error_message(raw)
        if err:
            raise GeminiUpstreamError(err)
    text = clean_gemini_text(text)
    for gg in image_urls:
        text += f"\n\n![generated image]({resolve_image_url(gg)})"
    return text


# ─── OpenAI Format Helpers ───────────────────────────────────────────────────

def messages_to_prompt(messages: list, tools: list = None) -> tuple:
    """Convert OpenAI messages to (prompt, attachments)."""
    parts = []
    attachments = []
    if tools:
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
            # Large tool lists blow the prompt budget: keep names/descriptions
            # but drop JSON schemas instead of truncating the prompt.
            if len(tools_json) > PROMPT_MAX_BYTES // 2:
                slim = [{"name": t.get("name", ""), "description": t.get("description", "")}
                        for t in tool_defs]
                tools_json = json.dumps(slim, indent=2)
                log(f"Tools block too large ({len(tool_defs)} tools), stripped parameters")
            parts.append(
                "[System instruction]: You have access to tools. "
                "To call a tool, respond with:\n"
                '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n'
                "Only use tool_call blocks when needed.\n\n"
                f"Available tools:\n{tools_json}"
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
                    attachments.append(att)
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
    return "\n\n".join(p for p in parts if p), attachments


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    pattern = r'```tool_call\s*\n(.*?)\n```'
    for match in re.findall(pattern, text, re.DOTALL):
        try:
            data = json.loads(match.strip())
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
    clean = re.sub(pattern, '', text, flags=re.DOTALL).strip()
    return clean, tool_calls


# ─── HTTP Handler ────────────────────────────────────────────────────────────

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
                self._handle_google_models_list()
            elif self.path == "/v1/diag":
                self._diag()
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__,
                                # Attachments need a signed-in session, so surface it here.
                                "cookie": bool(load_cookie()[0]),
                                "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

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
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            body = self._read_request_body()
            if self.path == "/v1/chat/completions":
                self.handle_chat(body)
            elif self.path == "/v1/responses":
                self.handle_responses(body)
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

    def _diag(self):
        """Read-only diagnostic: did the Gemini app-page token scrape succeed?

        Reports token presence (not values) so an attachment BardErrorInfo [1100]
        can be traced to a failed page scrape (default push_id/pctx, missing
        at/f.sid) instead of guessing. Never prints the cookie value.
        """
        cookie_str, sapisid = load_cookie()
        try:
            _page_tokens_cache["ts"] = 0  # force a fresh scrape, ignore the cache
            tokens = get_page_tokens()
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
            "account_prefix": account_prefix(),
            "bl": CONFIG.get("gemini_bl"),
            "temporary_chats": CONFIG.get("temporary_chats", True),
            "xsrf_token_configured": bool(CONFIG.get("xsrf_token")),
            "page_scrape_ok": scrape_ok,
            "page_tokens": present,
            "hint": hint,
        })

    def _resolve_model(self, model_name):
        think_override = None
        if "@think=" in model_name:
            model_name, think_str = model_name.rsplit("@think=", 1)
            think_override = int(think_str)
        cfg = MODELS.get(model_name)
        if not cfg:
            return None, None, None, f"Unknown model: {model_name}"
        return model_name, cfg["mode"], (think_override if think_override is not None else cfg["think"]), None

    def _call_gemini(self, prompt, model_id, think_mode, tools, file_refs=None):
        raw = gemini_stream_generate(prompt, model_id, think_mode, file_refs)
        text = extract_response_text(raw)
        tool_calls = None
        if tools and text:
            text, tool_calls = parse_tool_calls(text)
        return text or "", tool_calls

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        prompt, attachments = messages_to_prompt(req.get("messages", []), tools)
        file_refs = upload_attachments(attachments)
        if not prompt.strip() and not file_refs:
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if stream and not tools:
            # True streaming: forward chunks as they arrive
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                first_chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                               "model": model_name, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(first_chunk)}\n\n".encode())
                for delta_text in gemini_stream_generate_iter(prompt, model_id, think_mode, file_refs):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                # Final chunk
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        # Non-streaming (or tool calling which needs full response)
        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools, file_refs)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            # Stream mode with tools: send as single chunk (need full parse for tool_calls)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
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
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text)//4,
                          "total_tokens": (len(prompt)+len(text))//4},
            })

    def handle_responses(self, body: bytes):
        """OpenAI Responses API for Codex CLI compatibility."""
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
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

        prompt, attachments = messages_to_prompt(messages, tools)
        file_refs = upload_attachments(attachments)
        if not prompt.strip() and not file_refs:
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools, file_refs)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            seq = [0]

            def emit(ev_type, **fields):
                seq[0] += 1
                ev = {"type": ev_type, "sequence_number": seq[0], **fields}
                self.wfile.write(f"event: {ev_type}\ndata: {json.dumps(ev)}\n\n".encode())

            usage = {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}
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
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}})


    # ─── Google Native API (Gemini CLI compatible) ────────────────────────────

    def _parse_google_model_from_path(self):
        """Extract model name from /v1beta/models/{model}:method path."""
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        if m:
            return m.group(1)
        return None

    def _handle_google_models_list(self):
        """GET /v1beta/models — Google AI format model list."""
        models = []
        for name, cfg in MODELS.items():
            models.append({
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            })
        self.send_json({"models": models})

    def _google_contents_to_prompt(self, req: dict) -> tuple:
        """Convert Google API contents format to (prompt, attachments)."""
        parts = []
        attachments = []
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                parts.append(f"[System instruction]: {sys_text}")

        for content in req.get("contents", []):
            role = content.get("role", "user")
            text_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    text_parts.append(p["text"])
                elif p.get("inlineData"):
                    data = p["inlineData"]
                    mime = data.get("mimeType") or "application/octet-stream"
                    name = data.get("displayName") or data.get("fileName") or ""
                    try:
                        attachments.append((base64.b64decode(data.get("data", "")), mime, name))
                        if not mime.startswith("image/"):
                            text_parts.append(f"[Attached file: {name or mime}]")
                    except Exception as e:
                        log(f"Invalid inlineData payload: {e}")
                elif p.get("fileData"):
                    fd = p["fileData"]
                    uri = fd.get("fileUri") or fd.get("file_uri") or ""
                    att = attachment_from_url(uri, fd.get("displayName", ""))
                    if att:
                        attachments.append(att)
                        if not (fd.get("mimeType") or "").startswith("image/"):
                            text_parts.append(f"[Attached file: {att[2] or uri}]")
            text = " ".join(text_parts)
            if role == "model":
                parts.append(f"[Assistant]: {text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p), attachments

    def _handle_google_generate(self, body: bytes, stream: bool):
        """Handle Google native generateContent / streamGenerateContent."""
        req = json.loads(body)
        model_name = self._parse_google_model_from_path()
        if not model_name:
            self.send_json({"error": {"message": "model not specified in path"}}, 400)
            return

        model_name, model_id, think_mode, err = self._resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        prompt, attachments = self._google_contents_to_prompt(req)
        file_refs = upload_attachments(attachments)
        if not prompt.strip() and not file_refs:
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            text, _ = self._call_gemini(prompt, model_id, think_mode, None, file_refs)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        candidate = {
            "content": {"parts": [{"text": text or ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4,
            "totalTokenCount": (len(prompt) + len(text)) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(response_obj)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(path: str):
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        log(f"Config loaded: {path}")


def apply_env_overrides():
    """Apply environment variables on top of CONFIG (env wins over config.json)."""
    def env(*names):
        for name in names:
            raw = os.environ.get(name)
            if raw is not None and raw.strip() != "":
                return raw.strip()
        return None

    str_map = {
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
    int_map = {
        "port": ("PORT", "GEMINI_PORT"),
        "retry_attempts": ("RETRY_ATTEMPTS",),
        "retry_delay_sec": ("RETRY_DELAY_SEC",),
        "request_timeout_sec": ("REQUEST_TIMEOUT_SEC",),
    }
    bool_map = {
        "log_requests": ("LOG_REQUESTS",),
        "temporary_chats": ("TEMPORARY_CHATS",),
        "auto_update_bl": ("AUTO_UPDATE_BL",),
    }

    for key, names in str_map.items():
        value = env(*names)
        if value is not None:
            CONFIG[key] = value

    for key, names in int_map.items():
        value = env(*names)
        if value is not None:
            try:
                CONFIG[key] = int(value)
            except ValueError:
                log(f"Invalid integer for {key}: {value}")

    for key, names in bool_map.items():
        value = env(*names)
        if value is not None:
            low = value.lower()
            if low in ("1", "true", "yes", "on", "y"):
                CONFIG[key] = True
            elif low in ("0", "false", "no", "off", "n"):
                CONFIG[key] = False

    keys = env("API_KEYS", "API_KEY", "GEMINI_API_KEYS")
    if keys is not None:
        parsed = None
        if keys.startswith("["):
            try:
                parsed = [str(k).strip() for k in json.loads(keys) if str(k).strip()]
            except ValueError:
                parsed = None
        if parsed is None:
            for sep in ("\n", " ", ";"):
                keys = keys.replace(sep, ",")
            parsed = [k.strip() for k in keys.split(",") if k.strip()]
        CONFIG["api_keys"] = parsed

    return CONFIG


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG")
    if not config_path:
        for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)
    apply_env_overrides()

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    new_bl = fetch_latest_bl()
    if new_bl:
        CONFIG["gemini_bl"] = new_bl

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

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
    print(f"  Config:    {config_path or 'env vars only'}")
    print(f"  Cookie:    {cookie_src}")
    print(f"  API keys:  {len(CONFIG.get('api_keys') or [])} configured")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print(f"  BL:        {CONFIG['gemini_bl']}")
    print(f"  Temporary: {'yes' if CONFIG.get('temporary_chats', True) else 'no'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
