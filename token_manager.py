"""
token_manager.py — Dhan Access Token Auto-Regeneration
Uses TOTP + Dhan's /app/generateAccessToken endpoint to create
a fresh 24-hour token at every startup, and renews mid-day.

Requires:
  .env variables:
    DHAN_CLIENT_ID    — Your Dhan client ID
    DHAN_PIN          — Your 6-digit Dhan PIN
    DHAN_TOTP_SECRET  — TOTP secret from Dhan (base32 string)
"""

import asyncio
import logging
import os

import pyotp
import requests

log = logging.getLogger("token_manager")

# ── Auth endpoint ─────────────────────────────────────────────
GENERATE_URL = "https://auth.dhan.co/app/generateAccessToken"
RENEW_URL = "https://api.dhan.co/v2/RenewToken"


def generate_totp(secret: str) -> str:
    """Generate a 6-digit TOTP code from the secret."""
    # Strip whitespace/padding issues from the secret
    cleaned = secret.strip().replace(" ", "")
    totp = pyotp.TOTP(cleaned)
    return totp.now()


def generate_access_token(client_id: str, pin: str, totp_secret: str) -> dict:
    """
    Call Dhan's generateAccessToken API with TOTP.
    Returns dict with accessToken, expiryTime, etc. on success.
    """
    totp_code = generate_totp(totp_secret)
    log.info(f"[TokenManager] Generating new token for client {client_id}...")

    resp = requests.post(
        GENERATE_URL,
        params={
            "dhanClientId": client_id,
            "pin": pin,
            "totp": totp_code,
        },
        timeout=15,
    )

    if resp.status_code == 200:
        data = resp.json()
        if "accessToken" in data:
            log.info(f"[TokenManager] ✅ New token generated, expires: {data.get('expiryTime', 'unknown')}")
            return {"success": True, **data}
        else:
            log.error(f"[TokenManager] ❌ No accessToken in response: {data}")
            return {"success": False, "error": str(data)}
    else:
        log.error(f"[TokenManager] ❌ HTTP {resp.status_code}: {resp.text[:300]}")
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}


def renew_access_token(client_id: str, current_token: str) -> dict:
    """
    Renew an active token for another 24 hours.
    Only works if the token is still valid (not expired).
    """
    log.info("[TokenManager] Renewing existing token...")
    resp = requests.get(
        RENEW_URL,
        headers={
            "access-token": current_token,
            "dhanClientId": client_id,
        },
        timeout=15,
    )

    if resp.status_code == 200:
        data = resp.json()
        if "accessToken" in data:
            log.info(f"[TokenManager] ✅ Token renewed, expires: {data.get('expiryTime', 'unknown')}")
            return {"success": True, **data}
        else:
            log.error(f"[TokenManager] ❌ Renewal response: {data}")
            return {"success": False, "error": str(data)}
    else:
        log.error(f"[TokenManager] ❌ Renewal HTTP {resp.status_code}: {resp.text[:300]}")
        return {"success": False, "error": f"HTTP {resp.status_code}"}


def auto_generate_token() -> str | None:
    """
    Main entry point: generate a fresh token using TOTP.
    Returns the new access token string, or None on failure.
    Updates config.DHAN_ACCESS_TOKEN in-memory AND .env on disk.
    """
    import config

    client_id = config.DHAN_CLIENT_ID
    pin = os.getenv("DHAN_PIN", "")
    totp_secret = os.getenv("DHAN_TOTP_SECRET", "")

    if not client_id or client_id == "YOUR_CLIENT_ID_HERE":
        log.warning("[TokenManager] DHAN_CLIENT_ID not configured, skipping auto-token")
        return None
    if not pin:
        log.warning("[TokenManager] DHAN_PIN not set, skipping auto-token")
        return None
    if not totp_secret:
        log.warning("[TokenManager] DHAN_TOTP_SECRET not set, skipping auto-token")
        return None

    result = generate_access_token(client_id, pin, totp_secret)

    if result.get("success"):
        new_token = result["accessToken"]
        # Update in-memory config
        config.DHAN_ACCESS_TOKEN = new_token
        log.info("[TokenManager] ✅ config.DHAN_ACCESS_TOKEN updated in-memory")
        # Persist to .env so it survives restarts
        _update_env_token(new_token)
        return new_token
    else:
        log.error(f"[TokenManager] Failed to generate token: {result.get('error')}")
        return None


def _update_env_token(new_token: str):
    """Update DHAN_ACCESS_TOKEN in the .env file on disk."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
        with open(env_path, "w") as f:
            found = False
            for line in lines:
                if line.startswith("DHAN_ACCESS_TOKEN="):
                    f.write(f"DHAN_ACCESS_TOKEN={new_token}\n")
                    found = True
                else:
                    f.write(line)
            if not found:
                f.write(f"DHAN_ACCESS_TOKEN={new_token}\n")
        log.info("[TokenManager] ✅ .env updated with new token")
    except Exception as e:
        log.warning(f"[TokenManager] Could not update .env: {e}")


async def token_renewal_loop():
    """
    Background loop that renews the token every 12 hours.
    If renewal fails, it falls back to generating a brand new token via TOTP.
    Runs forever — call as an asyncio task.
    """
    import config

    RENEWAL_INTERVAL = 12 * 60 * 60  # 12 hours in seconds

    while True:
        await asyncio.sleep(RENEWAL_INTERVAL)
        log.info("[TokenManager] 🔄 Scheduled token renewal starting...")

        client_id = config.DHAN_CLIENT_ID
        totp_secret = os.getenv("DHAN_TOTP_SECRET", "")

        # Try renewal first (cheaper, no TOTP needed)
        result = renew_access_token(client_id, config.DHAN_ACCESS_TOKEN)
        if result.get("success"):
            config.DHAN_ACCESS_TOKEN = result["accessToken"]
            _update_env_token(result["accessToken"])
            log.info("[TokenManager] ✅ Token renewed via /RenewToken")
            continue

        # Renewal failed → generate fresh token
        log.warning("[TokenManager] Renewal failed, generating fresh token via TOTP...")
        new_token = auto_generate_token()
        if new_token:
            log.info("[TokenManager] ✅ Fresh token generated as renewal fallback")
        else:
            log.error("[TokenManager] ❌ Both renewal and generation failed!")
