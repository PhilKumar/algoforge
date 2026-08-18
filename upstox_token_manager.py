#!/usr/bin/env python3
"""Auto-refresh the Upstox access token, the way token_manager.py does for Dhan.

WHY THIS IS SHAPED DIFFERENTLY FROM DHAN
----------------------------------------
Dhan exposes an *official* ``generateAccessToken`` endpoint that takes a PIN and
a TOTP and hands back a token -- one clean, documented call.  Upstox does not.
Upstox's documented flow is browser-consent OAuth (see tools/upstox_login.py),
and it issues **no refresh token**, so there is nothing to "renew".  The token
dies daily around 03:30 IST.

To get Dhan-style hands-off refresh we therefore reproduce the *interactive*
login without a browser: post the mobile number, then the TOTP, then the PIN to
Upstox's login service, capture the redirect ``code``, and exchange it for a
token through the OFFICIAL token endpoint.  The final exchange and everything
around it (persistence, expiry detection, refresh-on-401, scheduling) is stable
and unit-tested.  The three consent posts hit ``service.upstox.com`` endpoints
that Upstox does not document and version over time (``v6`` today) -- that step
is isolated in ``_headless_authorize`` and, if it ever breaks, this module logs
loudly and falls back to the manual tool rather than wedging silently.

CONFIG (all in .env; NEVER commit them):
    UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI   -- app credentials
    UPSTOX_TOTP_SECRET   -- base32 TOTP seed from Upstox 2FA setup
    UPSTOX_PIN           -- 6-digit account PIN
    UPSTOX_MOBILE        -- registered mobile (10 digits, no +91)
Output:
    UPSTOX_ACCESS_TOKEN  -- written back into .env and returned in-process

The token is a bearer credential for a live broking account.  It is never
logged, never printed, and only ever written to .env.
"""

from __future__ import annotations

import logging
import os
import time as _time
from pathlib import Path
from typing import Optional

import pyotp
import requests

log = logging.getLogger("upstox.token")

REPO_ROOT = Path(__file__).resolve().parent
ENV_PATH = REPO_ROOT / ".env"

# Official OAuth endpoints (documented, stable).
AUTH_DIALOG = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"
# The validity PROBE. /user/profile answers 401 UDAPI1221 ("static IP") to every
# request from every host, whether or not the token is good -- it has a
# different IP rule from the data endpoints this app actually calls. Probing it
# declared a perfectly valid Analytics token dead on every run and logged a
# refresh attempt that could never be configured. The expiries listing is a
# data endpoint: 200 with a live token, 401 with a dead one.
PROBE_URL = "https://api.upstox.com/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050"

# Undocumented consent endpoints (version-sensitive; see module docstring).
_SVC = "https://service.upstox.com/login/open/v6/auth"
_ONE_FA = f"{_SVC}/1fa"
_TWO_FA = f"{_SVC}/2fa"

# A refresh is expensive and rate-limited upstream; never hammer it.
_last_refresh_at = 0.0
_REFRESH_COOLDOWN_SEC = 60.0


class UpstoxTokenError(RuntimeError):
    """A step in the headless login could not be completed."""


def generate_totp(secret: str) -> str:
    """Six-digit TOTP for the current 30s window."""
    cleaned = (secret or "").replace(" ", "").strip()
    if not cleaned:
        raise UpstoxTokenError("empty TOTP secret")
    return pyotp.TOTP(cleaned).now()


def _config() -> Optional[dict]:
    """Read the Upstox login config from the environment, or None if incomplete.

    Missing config is a normal, quiet state (Upstox simply isn't set up on this
    host) -- it returns None and logs at INFO, never raising, so it can't break
    startup for a box that doesn't use Upstox.
    """
    cfg = {
        "api_key": os.getenv("UPSTOX_API_KEY", "").strip(),
        "api_secret": os.getenv("UPSTOX_API_SECRET", "").strip(),
        "redirect_uri": os.getenv("UPSTOX_REDIRECT_URI", "").strip(),
        "totp_secret": os.getenv("UPSTOX_TOTP_SECRET", "").strip(),
        "pin": os.getenv("UPSTOX_PIN", "").strip(),
        "mobile": os.getenv("UPSTOX_MOBILE", "").strip(),
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        log.info("[UpstoxToken] Not configured for auto-refresh (missing: %s)", ", ".join(missing))
        return None
    return cfg


def token_is_valid(token: str) -> bool:
    """True when the token authenticates against Upstox right now."""
    if not token:
        return False
    try:
        resp = requests.get(
            PROBE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as exc:  # network hiccup is not proof of an invalid token
        log.warning("[UpstoxToken] Validity probe failed (treating as unknown): %s", exc)
        return False


def _headless_authorize(cfg: dict, session: requests.Session) -> str:
    """Drive Upstox's consent flow without a browser and return the auth ``code``.

    THIS is the undocumented, version-sensitive part.  Any non-2xx here raises
    UpstoxTokenError, which auto_generate_token turns into a loud failure plus
    the manual-tool fallback -- it must never return a partial/garbage code.
    """
    device = {"Content-Type": "application/json", "Accept": "application/json"}

    # 1FA -- register the mobile number, receive a validation token.
    r1 = session.post(_ONE_FA, json={"data": {"mobileNumber": cfg["mobile"]}}, headers=device, timeout=20)
    if r1.status_code >= 300:
        raise UpstoxTokenError(f"1FA failed ({r1.status_code})")
    valid_token = (r1.json().get("data") or {}).get("validateOTPToken") or ""
    if not valid_token:
        raise UpstoxTokenError("1FA returned no validateOTPToken")

    # 2FA -- answer with the current TOTP.
    otp = generate_totp(cfg["totp_secret"])
    r2 = session.post(
        _TWO_FA,
        json={"data": {"otp": otp, "validateOTPToken": valid_token, "otpType": "TOTP"}},
        headers=device,
        timeout=20,
    )
    if r2.status_code >= 300:
        raise UpstoxTokenError(f"2FA (TOTP) failed ({r2.status_code})")

    # PIN -- the final factor; on success Upstox 302s to the redirect_uri with
    # ?code=.  We stop the redirect being followed so we can read the code off
    # the Location header rather than chasing it to a dead local listener.
    r3 = session.post(
        f"{_SVC}/pin",
        json={"data": {"pin": cfg["pin"], "validateOTPToken": valid_token}},
        headers=device,
        timeout=20,
    )
    if r3.status_code >= 300:
        raise UpstoxTokenError(f"PIN step failed ({r3.status_code})")

    # Consent/authorize -- ask for the code against our client_id.
    r4 = session.get(
        AUTH_DIALOG,
        params={
            "response_type": "code",
            "client_id": cfg["api_key"],
            "redirect_uri": cfg["redirect_uri"],
        },
        allow_redirects=False,
        timeout=20,
    )
    location = r4.headers.get("Location", "")
    code = ""
    if "code=" in location:
        code = location.split("code=", 1)[1].split("&", 1)[0]
    if not code:
        raise UpstoxTokenError("authorize step returned no code")
    return code


def _exchange_code_for_token(cfg: dict, code: str) -> str:
    """OFFICIAL, documented exchange: auth code -> access token."""
    resp = requests.post(
        TOKEN_URL,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code": code,
            "client_id": cfg["api_key"],
            "client_secret": cfg["api_secret"],
            "redirect_uri": cfg["redirect_uri"],
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if resp.status_code >= 300:
        raise UpstoxTokenError(f"token exchange failed ({resp.status_code})")
    token = (resp.json() or {}).get("access_token") or ""
    if not token:
        raise UpstoxTokenError("token exchange returned no access_token")
    return token


def _update_env_token(new_token: str) -> None:
    """Rewrite (or append) UPSTOX_ACCESS_TOKEN in .env, preserving everything else."""
    if not ENV_PATH.exists():
        log.warning("[UpstoxToken] No %s to persist the token into", ENV_PATH)
        return
    try:
        lines = ENV_PATH.read_text().splitlines(keepends=True)
        out, found = [], False
        for line in lines:
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                out.append(f"UPSTOX_ACCESS_TOKEN={new_token}\n")
                found = True
            else:
                out.append(line)
        if not found:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"UPSTOX_ACCESS_TOKEN={new_token}\n")
        ENV_PATH.write_text("".join(out))
        os.environ["UPSTOX_ACCESS_TOKEN"] = new_token
        log.info("[UpstoxToken] ✅ .env updated with a fresh token")
    except Exception as exc:
        log.warning("[UpstoxToken] Could not update .env: %s", exc)


def auto_generate_token(*, force: bool = False) -> Optional[str]:
    """Mint a fresh Upstox token via headless login and persist it.

    Returns the token, or None when Upstox isn't configured or the login fails.
    Rate-limited by a cooldown so a burst of 401s can't hammer the login service.
    """
    global _last_refresh_at
    now = _time.time()
    if not force and now - _last_refresh_at < _REFRESH_COOLDOWN_SEC:
        log.info("[UpstoxToken] Refresh skipped (cooldown)")
        return None
    cfg = _config()
    if cfg is None:
        return None
    _last_refresh_at = now
    try:
        with requests.Session() as session:
            code = _headless_authorize(cfg, session)
            token = _exchange_code_for_token(cfg, code)
    except UpstoxTokenError as exc:
        log.error("[UpstoxToken] ❌ Headless refresh failed: %s. Run tools/upstox_login.py manually.", exc)
        return None
    except Exception as exc:
        log.error("[UpstoxToken] ❌ Unexpected refresh error: %s", exc)
        return None
    _update_env_token(token)
    log.info("[UpstoxToken] ✅ Token auto-refreshed via headless TOTP login")
    return token


def ensure_fresh_token() -> Optional[str]:
    """Return a valid token, refreshing only if the current one is dead.

    Cheap when the token is still good (one profile probe); this is what the
    premium source calls before a run, and what a 401 handler calls to retry.
    """
    current = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if current and token_is_valid(current):
        return current
    log.info("[UpstoxToken] Current token missing/expired -> attempting refresh")
    return auto_generate_token()
