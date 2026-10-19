"""One-time helper: obtain the Google Ads OAuth refresh token.

Run this ONCE, on a machine with a browser, signed in as the Google account
that owns the MCC. It prints a refresh token to paste into .env.

    python get_refresh_token.py

WHY THIS EXISTS AND META NEEDS NO EQUIVALENT: Meta's System User token is
minted by clicking a button in the Business Manager UI. Google has no such
button. A refresh token can only come from an interactive consent flow -- a
human signs in, approves the scope, and Google hands back a one-time code
that is exchanged for the refresh token. There is no REST-only path to it.

TWO PARAMETERS THAT ARE NOT OPTIONAL, and are the usual reason this is done
twice:

    access_type=offline   without it, Google returns an access token only and
                          no refresh token at all.
    prompt=consent        without it, a Google account that has ALREADY
                          approved this client gets a response with no
                          refresh_token field -- Google only re-issues one on
                          an explicit re-consent. The first run looks fine and
                          every run after it silently returns nothing.

Deliberately implemented on the standard library plus requests, rather than
pulling in google-auth-oauthlib, so this package keeps the same one-dependency
footprint as the rest of the PoC.

Prerequisites:
  1. A Google Cloud project with the Google Ads API enabled.
  2. An OAuth 2.0 Client ID of type "Desktop app" (Cloud Console ->
     APIs & Services -> Credentials -> Create credentials -> OAuth client ID).
  3. GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET set in .env.
  4. The signing-in account added under Google Auth Platform -> Audience ->
     Test users (the app is unverified; otherwise sign-in fails with
     "Error 403: access_denied").

The local listener has no timeout: it waits until the redirect arrives or you
press Ctrl+C. See the note at the wait loop for why.
"""
from __future__ import annotations

import http.server
import os
import socket
import sys
import threading
import urllib.parse
import webbrowser

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dependency
    pass

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/adwords"

_received: dict[str, str] = {}
_done = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches Google's redirect and pulls the one-time code out of it."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        # Browsers also request /favicon.ico and the like. Those carry neither
        # a code nor an error, so they must not be mistaken for the callback.
        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.end_headers()
            return

        _received.update({k: v[0] for k, v in params.items()})
        _done.set()

        ok = "code" in _received
        body = (
            "<h2>Authorization complete.</h2><p>You can close this tab and "
            "return to the terminal.</p>"
            if ok else
            f"<h2>Authorization failed.</h2><pre>{_received.get('error', 'no code returned')}</pre>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body style='font-family:sans-serif'>{body}</body></html>".encode())

    def log_message(self, *_args) -> None:
        """Silence the default stderr access log."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    client_id = os.getenv("GOOGLE_ADS_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Set GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET in .env first.")
        print("Both come from the OAuth 2.0 Client ID (type: Desktop app) in the")
        print("Google Cloud Console under APIs & Services -> Credentials.")
        return 2

    port = _free_port()
    redirect_uri = f"http://localhost:{port}"

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1  # handle_request() wakes every second so Ctrl+C works

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # required for a refresh token at all
        "prompt": "consent",        # required for a refresh token on re-auth
    })

    print("=" * 68)
    print("Google Ads -- one-time refresh token")
    print("=" * 68)
    print("Opening a browser. Sign in as the account that owns the MCC and")
    print("approve access. The account must be listed as a test user on the")
    print("OAuth consent screen. Google will also show a 'hasn't verified this")
    print("app' warning -- click Advanced, then continue.\n")
    print(f"If the browser does not open, paste this URL manually:\n\n{auth_url}\n")
    webbrowser.open(auth_url)

    # No deadline, on purpose. While the OAuth app is unverified, Google shows
    # a "hasn't verified this app" warning that needs an extra click, so the
    # redirect can arrive well after a fixed timeout would have shut the
    # listener down (the browser then shows ERR_CONNECTION_REFUSED even though
    # the URL carries a valid code=). Stay up until the redirect lands.
    print("Waiting for the redirect (no timeout -- press Ctrl+C to abort)...")
    try:
        while not _done.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        print("\nAborted before the redirect arrived.")
        return 1
    finally:
        server.server_close()

    if "code" not in _received:
        print(f"\nFAILED: {_received.get('error_description') or _received.get('error') or 'no code returned'}")
        return 1

    print("Code received. Exchanging it for a refresh token...")
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _received["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)

    if not resp.ok:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        print(f"\nFAILED: {body.get('error', resp.status_code)} -- "
              f"{body.get('error_description', resp.text[:200])}")
        return 1

    payload = resp.json()
    refresh_token = payload.get("refresh_token")

    if not refresh_token:
        print("\nNo refresh_token in the response.")
        print("This is the prompt=consent case described at the top of this file:")
        print("the account had already approved this client, so Google returned")
        print("an access token only. Revoke access at")
        print("https://myaccount.google.com/permissions and run this again.")
        return 1

    print("\n" + "=" * 68)
    print("SUCCESS -- add this line to .env:\n")
    print(f"GOOGLE_ADS_REFRESH_TOKEN={refresh_token}")
    print("\n" + "=" * 68)
    print("This is a SECRET and it is shared across every client. Treat it the")
    print("way the Meta System User token is treated: .env only, never a")
    print("commit, never a ticket, never a chat message.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
