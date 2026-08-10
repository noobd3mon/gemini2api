# AGENTS.md

Working notes for agents/humans editing this repo. Read before touching code.

## What this is

OpenAI-compatible HTTP proxy in front of Gemini Web. Pure stdlib except optional `httpx`
(true streaming). No framework, no database.

## Repo map

| Path | Role |
| --- | --- |
| `gemini_web2api/` | The package used by Docker/Railway (`python -m gemini_web2api`) |
| `gemini_web2api/config.py` | Defaults + optional `config.json` + **env var loading** (`load_env_config`) |
| `gemini_web2api/gemini.py` | Gemini StreamGenerate protocol, cookie loading, retries |
| `gemini_web2api/server.py` | HTTP handler: `/v1/chat/completions`, `/v1/responses`, `/v1beta/...`, `/` health |
| `gemini_web2api/multimodal.py`, `tools.py`, `models.py` | Uploads, tool calling, model table |
| `gemini_web2api.py` | Standalone single-file copy of the same server (kept in sync manually) |
| `Dockerfile`, `railway.json`, `.env.example` | Deployment: Docker build, Railway config, env template |
| `cloudflare/`, `gemini-cookie-sync-extension/` | Independent side projects, not part of the Python server |

## Configuration contract

Precedence: `DEFAULT_CONFIG` < `config.json` (optional) < environment variables < CLI flags.

- Env mapping lives in `config.py` (`ENV_STR`, `ENV_INT`, `ENV_BOOL`, `ENV_KEYS`) and is mirrored
  in `apply_env_overrides()` inside the single-file `gemini_web2api.py`. **Change both.**
- Cookie: `GEMINI_COOKIE` (raw `Cookie` header on one line, or JSON `{"cookie":..., "sapisid":...}`).
  Surrounding quotes are stripped, `SAPISID` is parsed out automatically, `GEMINI_SAPISID` overrides it.
  `cookie_file` still works as a fallback and keeps its mtime cache.
- `PORT` (Railway) and `GEMINI_PORT` both map to `config["port"]`; `HOST` defaults to `0.0.0.0`.
- `API_KEYS` accepts a comma/space/semicolon list or a JSON array. Empty = auth disabled.
- Do not reintroduce a required `config.json`: the Docker image must boot with env vars only.

## Deployment invariants

- `railway.json`: Dockerfile builder, `deploy.multiRegionConfig["asia-southeast1-eqsg3a"].numReplicas = 1`,
  healthcheck on `/` (this route is intentionally outside API-key auth; only `/v1*` is protected).
- `Dockerfile` copies only `requirements.txt` + `gemini_web2api/`, sets `PYTHONUNBUFFERED=1`
  so Railway logs stream, and runs `python -m gemini_web2api`.
- Secrets never land in the image; `.env` is gitignored, `.env.example` is the template.

## Verify (no test suite in this repo)

```cmd
python -m py_compile gemini_web2api.py gemini_web2api\config.py gemini_web2api\gemini.py gemini_web2api\__main__.py gemini_web2api\server.py
```

For behaviour changes write a temporary `_verify_*.py` that sets `os.environ`, calls
`load_env_config()` / `load_cookie()`, asserts the result, validates `railway.json`, then delete it.
Load the single-file variant with `importlib.util.spec_from_file_location` - a plain
`import gemini_web2api` resolves to the package directory instead.

Smoke test locally:

```cmd
set GEMINI_COOKIE=__Secure-1PSID=...; SAPISID=...
set API_KEYS=sk-test
python -m gemini_web2api
curl http://localhost:8081/
```

## Conventions and gotchas

- Shell here is Windows cmd: use `set VAR=value`, backslash paths, `del file` for cleanup.
- Code, comments and docs in this repo are English. Keep the existing 4-space, stdlib-first style.
- The startup `bl` refresh hits the network; gate it with `AUTO_UPDATE_BL=false` in offline tests.
- `README_CN.md` still documents the old `config.json`-only flow; update it when touching README.md docs.
- Never log or print the cookie value - print only its source (`env GEMINI_COOKIE` / `file <path>`).
