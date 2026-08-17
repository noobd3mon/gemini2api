Gemini Cookie Sync v1.1

Purpose:
- Read cookies for the current Google/Gemini session.
- Extract the XSRF token named SNlM0e from the Gemini page.
- Extract gemini_bl from cfb2h or from page requests when available.
- Copy a Railway-ready KEY=value env block (paste into Variables > Raw Editor),
  export `gemini-auth.json` locally, or push the session straight to a
  running gemini-web2api proxy via POST /admin/cookie.

Installation:
1. Open `chrome://extensions`
2. Enable Developer mode
3. Click Load unpacked
4. Select this folder
5. Open `https://gemini.google.com/app`, sign in, and refresh the page
6. Click Inspect session
7. Click Copy Railway env (raw), export `gemini-auth.json`, or enter the
   proxy URL and click Push to proxy

Copy Railway env:
With a ready session, click "Copy Railway env (raw)". The extension copies a
KEY=value block to the clipboard (shown in the panel if the clipboard is
blocked). Paste it into Railway > Variables > Raw Editor and apply. Only the
scraped values (cookie, XSRF, bl, auth_user) are filled in; the rest are the
documented defaults. PORT is omitted because Railway injects it.

Push to proxy:
The proxy must be running with ADMIN_KEY set (recommended) or with API-key
auth. Enter the proxy URL (e.g. http://localhost:8081) and the ADMIN_KEY,
then click Push to proxy. The proxy applies the cookie at runtime - no
restart needed. The session data is only ever sent to the URL you enter.

Security:
The session data represents the real Google session and must be treated as
secret. Do not send it anywhere except your own proxy, do not print it, and
do not commit it to Git.
