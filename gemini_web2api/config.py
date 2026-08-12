"""Configuration management: env-first, JSON file optional.

Precedence: DEFAULT_CONFIG < config.json (optional) < environment variables < CLI flags.
Designed so the app can run on PaaS (Railway, Fly, Render) with env vars only.
"""
import json
import os

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
