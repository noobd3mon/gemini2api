# AGENTS.md

Working notes for agents/humans editing this repo. Read before touching code.

## What this is

OpenAI-compatible HTTP proxy in front of Gemini Web. Pure stdlib except optional `httpx`
(true streaming). No framework, no database.

## Repo map

| Path | Role |
| --- | --- |
| `gemini_web2api/` | The package used by Docker/Railway (`python -m gemini_web2api`). **The only hand-edited source.** |
| `gemini_web2api/config.py` | Defaults + optional `config.json` + **env var loading** (`load_env_config`) |
| `gemini_web2api/gemini.py` | Gemini StreamGenerate protocol, cookie loading, retries, image output |
| `gemini_web2api/server.py` | HTTP handler: `/v1/chat/completions`, `/v1/responses`, `/v1beta/...`, `/v1/diag`, `/admin/cookie`, `/` health |
| `gemini_web2api/multimodal.py`, `tools.py`, `models.py` | Uploads, tool calling, model table |
| `gemini_web2api.py` | **GENERATED** standalone single-file copy. Never edit it - change the package and run `python build_single_file.py`. |
| `build_single_file.py` | Merges the package modules into `gemini_web2api.py` (strips relative imports, dedupes stdlib imports) |
| `tests/` | Stdlib `unittest` suite: `python -m unittest discover -s tests` |
| `Dockerfile`, `railway.json`, `.env.example` | Deployment: Docker build, Railway config, env template |
| `cloudflare/`, `gemini-cookie-sync-extension/` | Independent side projects, not part of the Python server |

## Configuration contract

Precedence: `DEFAULT_CONFIG` < `config.json` (optional) < environment variables < CLI flags.

- Env mapping lives ONLY in `config.py` (`ENV_STR`, `ENV_INT`, `ENV_BOOL`, `ENV_KEYS`). The
  single-file copy is generated from it, so there is no second mapping to keep in sync.
- Cookie: `GEMINI_COOKIE` (raw `Cookie` header on one line, or JSON `{"cookie":..., "sapisid":...}`).
  Surrounding quotes are stripped, `SAPISID` is parsed out automatically, `GEMINI_SAPISID` overrides it.
  `cookie_file` still works as a fallback and keeps its mtime cache. `POST /admin/cookie` (auth:
  `ADMIN_KEY` env, falling back to API keys) updates the cookie at runtime and invalidates the
  cached cookies/page tokens - no restart needed.
- `PORT` (Railway) and `GEMINI_PORT` both map to `config["port"]`; `HOST` defaults to `0.0.0.0`.
- `API_KEYS` accepts a comma/space/semicolon list or a JSON array. Empty = auth disabled.
  Comparisons are `hmac.compare_digest` (timing-safe).
- Newer keys: `image_format` ("markdown"/"url", env `GEMINI_IMAGE_FORMAT`), `rate_limit`
  (req/min/key, env `GEMINI_RATE_LIMIT`, 0=off), `admin_key` (env `GEMINI_ADMIN_KEY`),
  `token_cache_file` (env `GEMINI_TOKEN_CACHE_FILE`, opt-in file cache for scraped page tokens).
- Do not reintroduce a required `config.json`: the Docker image must boot with env vars only.

## Multimodal (files/images)

- Uploads use Google's Scotty resumable endpoint, not the chat RPC:
  `POST https://push.clients6.google.com/upload/` (fallback `https://content-push.googleapis.com/upload/`).
  - Start: `x-goog-upload-command: start`, `x-goog-upload-protocol: resumable`,
    `x-goog-upload-header-content-length: <bytes>`, body `urlencode({"File name: <name>": ""})`.
    The session URL comes back in the `x-goog-upload-url` response header.
    The browser sends **no** `x-goog-upload-header-content-type` - the mime type only travels in
    the payload binding, so do not add that header back.
  - Finalize: `x-goog-upload-command: upload, finalize`, `x-goog-upload-offset: 0`,
    `Content-Type: application/x-www-form-urlencoded;charset=utf-8` (not the file mime),
    body = raw bytes. The response body **is** the file reference (`/contrib_service/ttl_1d/...`).
  - Required headers: `push-id`, `x-client-pctx`, `x-tenant-id: bard-storage`, cookie,
    `referer: https://gemini.google.com/`. `push-id` / `pctx` are scraped from the app page
    (`"qKIAYe"`, `"Ylro7b"`, 10-minute cache) and fall back to the constants captured in
    `Capture mẫu/capture-gemini.google.com-*.md` (gitignored - captures contain cookies).
    A 3-file capture reused one `push-id` / `pctx` for all three uploads and fired them
    concurrently, so `_upload_attachments` uploads through a `ThreadPoolExecutor`: max 4 workers,
    token cache warmed once before the pool starts, single attachment stays on the direct path,
    failures are skipped and the result keeps attachment order.
- The reference is bound into the chat payload at `inner[0][3]`:
  `[[[<file_ref>, <kind>, null, <mime>], <filename>], ...]` (`_build_file_bindings`).
  `<kind>` is `1` for images and `3` for every other type (capture: image/png and image/jpeg
  -> `1`, text/plain -> `3`). Several files are just more entries in the same list, in
  attachment order.
- `MAX_UPLOAD_BYTES` is 20 MB. Non-image attachments also add an `[Attached file: <name>]` prompt line.
- Accepted input shapes: `image_url`, `input_image`, `image`, `file`, `input_file`, `document`,
  Anthropic `source.data`, and Google `inlineData` / `fileData`. Values may be data URLs, raw base64
  or http(s) URLs (fetched right before upload).
- `/v1/responses` must not flatten list content to text any more - that would drop attachments.
- **Attachments need a signed-in session.** Without a cookie the upload still succeeds and returns a
  `/contrib_service/ttl_1d/...` ref, but StreamGenerate replies with a ~216 byte body containing
  `BardErrorInfo",[1100]` and no text. A bogus ref fails identically, so the ref is not the problem;
  text-only prompts keep working anonymously. Check `GEMINI_COOKIE` on the host before debugging the
  payload. `GET /` reports `"cookie": true/false` for exactly this reason.
- Error detection: Gemini writes `...BardErrorInfo",[1100]]]`, so the old `BardErrorInfo\s*\[(\d+)\]`
  regex never matched and every upstream refusal surfaced as an empty completion. `BARD_ERROR_RE`
  (`BardErrorInfo\D{0,8}\[\s*(\d+)`) plus `bard_error_message()` raise `GeminiUpstreamError`
  with a cookie hint and skip the retry loop. Only raise when no text was parsed - a successful
  reply never contains `BardErrorInfo`.

## Streaming (deltas)

- Gemini frames grow cumulatively but can revise mid-stream (intro + "Anh cần" -> intro +
  "Hôm nay anh cần..."). SSE deltas are append-only, so on a non-prefix frame emit only the part
  of the new text beyond the longest common prefix (`_delta_from`); raising/dropping makes the
  client retry and concatenate, which repeats the whole intro (the bug fixed in 4121fc6).
- `extract_response_text` prefers the LAST non-empty text (the final draft), not the longest.
- The final frame carries a completion status at `inner[26][0][0][0][1][1]` (0 = normal in every
  capture). `extract_finish_status()` reads it; only 0 is mapped, other codes are logged.
- StreamGenerate responses contain NO token usage (checked against 2026-08-11/14 captures):
  `_usage` is a char-count estimate; `stream_options.include_usage` returns that estimate.

## Deployment invariants

- `railway.json`: Dockerfile builder, `deploy.multiRegionConfig["asia-southeast1-eqsg3a"].numReplicas = 1`,
  healthcheck on `/` (this route is intentionally outside API-key auth; only `/v1*` is protected).
- `Dockerfile` copies only `requirements.txt` + `gemini_web2api/`, sets `PYTHONUNBUFFERED=1`
  so Railway logs stream, and runs `python -m gemini_web2api`.
- Secrets never land in the image; `.env` is gitignored, `.env.example` is the template.

## Verify

```cmd
python -m py_compile gemini_web2api.py gemini_web2api\config.py gemini_web2api\gemini.py gemini_web2api\__main__.py gemini_web2api\server.py gemini_web2api\multimodal.py gemini_web2api\tools.py gemini_web2api\models.py
python -m unittest discover -s tests
```

After ANY package change: run `python build_single_file.py`, then re-run the tests - the
`tests/test_single_file.py` parity guard fails when the single file has not been regenerated.

Smoke test locally:

```cmd
set GEMINI_COOKIE=__Secure-1PSID=...; SAPISID=...
set API_KEYS=sk-test
python -m gemini_web2api
curl http://localhost:8081/
```

## Conventions and gotchas

- Shell here is Windows cmd: use `set VAR=value`, backslash paths, `del file` for cleanup.
- Every `.py` file here uses CRLF line endings. Search/replace edits must include `\r\n` in the
  matched text or the match silently fails.
- Code, comments and docs in this repo are English. Keep the existing 4-space, stdlib-first style.
- The startup `bl` refresh hits the network; gate it with `AUTO_UPDATE_BL=false` in offline tests.
- Never log or print the cookie value - print only its source (`env GEMINI_COOKIE` / `file <path>`).