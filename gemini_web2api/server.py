"""HTTP server: OpenAI-compatible API endpoints."""
import json
import time
import uuid
import re
import hmac
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor

from .config import CONFIG
from .models import MODELS, resolve_model
from .gemini import (generate, generate_stream, load_cookie, log, _account_prefix,
                     _invalidate_cookie_cache)
from .tools import messages_to_prompt, parse_tool_calls, google_contents_to_prompt, parse_google_function_calls
from .multimodal import (upload_file, prepare_attachment, _cached_page_tokens,
                         _get_page_tokens)
from . import __version__


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