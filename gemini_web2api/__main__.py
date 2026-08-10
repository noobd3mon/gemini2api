"""Entry point: python -m gemini_web2api"""
import argparse
import os

from .config import CONFIG, load_config, find_config, load_env_config
from .models import MODELS
from .gemini import HAS_HTTPX, load_cookie
from .server import GeminiHandler, ThreadedServer
from . import __version__


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
    print(f"  Proxy:     {CONFIG.get('proxy') or 'system env'}")
    print(f"  Streaming: {'httpx (true streaming)' if HAS_HTTPX else 'urllib (buffered)'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
