"""Tool calling and multimodal message parsing."""
import json
import re
import uuid
import base64
import binascii
import mimetypes
import urllib.parse
from urllib.parse import unquote_to_bytes

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
            from .gemini import log as log
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
