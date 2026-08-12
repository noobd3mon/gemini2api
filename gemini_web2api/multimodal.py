"""Multimodal: Scotty resumable upload for Gemini image/file input.

Protocol verified against a real gemini.google.com capture (2026-08-11):
  1. POST https://push.clients6.google.com/upload/
     headers: Push-ID, X-Tenant-Id: bard-storage, X-Client-Pctx,
              X-Goog-Upload-Protocol: resumable, X-Goog-Upload-Command: start,
              X-Goog-Upload-Header-Content-Length: <size>
     body:    "File name: <filename>" (form-urlencoded)
     -> X-Goog-Upload-URL response header
  2. POST <upload url> with X-Goog-Upload-Command: "upload, finalize",
     X-Goog-Upload-Offset: 0 and the raw bytes
     -> response body is the file reference "/contrib_service/ttl_1d/..."

The reference is bound into the StreamGenerate payload as
    inner[0][3] = [[[<file_ref>, 1, None, <mime>], <filename>]]
"""
import base64
import mimetypes
import re
import time
import urllib.parse
import urllib.request

from .config import CONFIG
from .gemini import load_cookie, make_sapisidhash, _account_prefix, _get_ssl_ctx, log

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
