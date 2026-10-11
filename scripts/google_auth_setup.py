#!/usr/bin/env python3
"""One-time Google Calendar consent. Writes the refresh token to token.json.

Console setup, once per Google Cloud project:
  1. Enable the Google Calendar API.
  2. Google Auth Platform -> Audience -> External, add every teammate's Gmail
     under Test users. Leave the app in Testing.
  3. Clients -> Create client -> Desktop app -> download the JSON, save it as
     credentials.json in the repo root (client_secret_*.json also works).

Then: python scripts/google_auth_setup.py
credentials.json is shared with the team. token.json is personal, never commit it.
"""

import http.server
import json
import secrets
import socketserver
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.context.oauth import SCOPES, TOKEN_URI, token_path  # noqa: E402

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/"

_received: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.urlparse(self.path).query
        _received.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _received
        self.wfile.write(b"Timo is connected. Close this tab." if ok else b"Authorization failed.")

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    creds = ROOT / "credentials.json"
    if not creds.exists():
        found = sorted(ROOT.glob("client_secret_*.json"))
        if not found:
            print(f"missing {creds}\nSee the steps at the top of this file.")
            return 1
        creds = found[0]
    blob = json.loads(creds.read_text())
    client = blob.get("installed") or blob.get("web")
    if not client:
        print(f"{creds.name} has no 'installed' key. Create a Desktop app client.")
        return 1

    state = secrets.token_urlsafe(16)
    url = f"{AUTH_URI}?" + urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",  # without this Google sends no refresh_token
            "prompt": "consent",  # and omits it on repeat grants unless you force this
            "state": state,
        }
    )
    print(f"opening browser, or visit:\n{url}\n")
    webbrowser.open(url)

    with socketserver.TCPServer(("127.0.0.1", PORT), _Handler) as httpd:
        httpd.handle_request()

    if _received.get("state") != state or "code" not in _received:
        print(f"authorization failed: {_received}")
        return 1

    resp = httpx.post(
        TOKEN_URI,
        data={
            "code": _received["code"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=10.0,
    )
    if resp.status_code != 200:
        print(f"token exchange failed: {resp.status_code} {resp.text}")
        return 1
    tok = resp.json()
    if "refresh_token" not in tok:
        print("no refresh_token returned. Revoke Timo at myaccount.google.com/permissions and rerun.")
        return 1

    token_path().write_text(
        json.dumps(
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": tok["refresh_token"],
                "access_token": tok["access_token"],
                "expires_at": time.time() + tok.get("expires_in", 3600),
            },
            indent=2,
        )
    )
    print(f"wrote {token_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
