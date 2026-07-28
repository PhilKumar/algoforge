#!/usr/bin/env python3
"""Get an Upstox access token and put it in .env.

Upstox tokens are daily -- they die around 03:30 IST and the standard OAuth
flow issues no refresh token, so this has to be re-run on any day we actually
talk to Upstox. That is tolerable because the only thing PhilForge wants from
Upstox is a one-time historical pull of expired option premiums, which is then
cached to disk and never fetched again.

    python3 tools/upstox_login.py

Reads UPSTOX_API_KEY, UPSTOX_API_SECRET and UPSTOX_REDIRECT_URI from .env,
opens the consent page, catches the redirect on a local listener, exchanges the
code, and writes UPSTOX_ACCESS_TOKEN back into .env.

The token is never printed. It is a bearer credential for a live broking
account, and a terminal scrollback is not where it should live.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

AUTH_DIALOG = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

# Written back into .env. Kept here so the reader and the writer cannot drift.
TOKEN_KEY = "UPSTOX_ACCESS_TOKEN"


class LoginError(RuntimeError):
    """Something went wrong that the operator has to fix by hand."""


def read_env(path: Path) -> dict[str, str]:
    """Parse .env well enough for our own keys. Not a dotenv replacement."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env_value(path: Path, key: str, value: str) -> None:
    """Set one key in .env, replacing it in place if it is already there.

    Appending blindly would leave two lines for the same key, and which one
    wins then depends on the parser -- a good way to spend an afternoon on a
    token that was updated but not used.
    """
    line = f"{key}={value}"
    if not path.exists():
        path.write_text(line + "\n")
        path.chmod(0o600)
        return

    text = path.read_text()
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    path.write_text(text)


class _CodeCatcher(BaseHTTPRequestHandler):
    """Single-shot handler that grabs ?code= off the redirect."""

    code: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _CodeCatcher.code = (params.get("code") or [None])[0]
        _CodeCatcher.error = (params.get("error_description") or params.get("error") or [None])[0]

        body = (
            b"<h2>Upstox login received.</h2><p>You can close this tab.</p>"
            if _CodeCatcher.code
            else b"<h2>No authorization code came back.</h2><p>Check the terminal.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Silence the default stderr access log -- it echoes the auth code."""


def catch_code(redirect_uri: str, timeout_sec: int) -> str:
    """Serve the redirect URI locally until Upstox calls back with a code."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port is None:
        raise LoginError(
            f"UPSTOX_REDIRECT_URI has no port ({redirect_uri}); use something like "
            "http://127.0.0.1:8765/upstox/callback so this script can listen for it."
        )

    _CodeCatcher.code = None
    _CodeCatcher.error = None
    server = HTTPServer((host, port), _CodeCatcher)
    server.timeout = timeout_sec

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout_sec)
    server.server_close()

    if _CodeCatcher.error:
        raise LoginError(f"Upstox refused the login: {_CodeCatcher.error}")
    if not _CodeCatcher.code:
        raise LoginError(
            f"No redirect arrived within {timeout_sec}s. The usual cause is a redirect URI "
            "in the Upstox app settings that does not match UPSTOX_REDIRECT_URI exactly."
        )
    return _CodeCatcher.code


def exchange_code(code: str, api_key: str, api_secret: str, redirect_uri: str) -> dict:
    """Trade the one-shot code for a bearer token."""
    response = requests.post(
        TOKEN_URL,
        headers={"accept": "application/json", "Api-Version": "2.0"},
        data={
            "code": code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        # The body carries Upstox's own reason; it does not contain the token
        # on a failure path, so it is safe to surface.
        raise LoginError(f"Token exchange failed ({response.status_code}): {response.text[:400]}")
    payload = response.json()
    if not payload.get("access_token"):
        raise LoginError(f"Token exchange returned no access_token: {payload}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Get an Upstox access token into .env")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for the redirect")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    args = parser.parse_args()

    env = read_env(ENV_PATH)
    api_key = os.environ.get("UPSTOX_API_KEY") or env.get("UPSTOX_API_KEY")
    api_secret = os.environ.get("UPSTOX_API_SECRET") or env.get("UPSTOX_API_SECRET")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI") or env.get("UPSTOX_REDIRECT_URI")

    missing = [
        name
        for name, value in (
            ("UPSTOX_API_KEY", api_key),
            ("UPSTOX_API_SECRET", api_secret),
            ("UPSTOX_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        print(f"Missing in {ENV_PATH}: {', '.join(missing)}", file=sys.stderr)
        print("Create the app at https://account.upstox.com/developer/apps first.", file=sys.stderr)
        return 2

    dialog = f"{AUTH_DIALOG}?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri}
    )

    print("Opening the Upstox consent page. Log in and approve.")
    print(f"If nothing opens, visit:\n  {dialog}\n")
    if not args.no_browser:
        webbrowser.open(dialog)

    try:
        code = catch_code(redirect_uri, args.timeout)
        payload = exchange_code(code, api_key, api_secret, redirect_uri)
    except LoginError as err:
        print(f"\n{err}", file=sys.stderr)
        return 1

    write_env_value(ENV_PATH, TOKEN_KEY, payload["access_token"])

    who = payload.get("user_name") or payload.get("user_id") or "the account"
    print(f"\nToken stored in {ENV_PATH} as {TOKEN_KEY} (for {who}).")
    print("It expires around 03:30 IST. Re-run this script on the day you need Upstox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
