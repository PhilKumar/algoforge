"""
auth.py — AlgoForge Authentication Module (Multi-Tenant)

Handles password hashing (bcrypt), Fernet encryption for broker creds,
session creation/validation, and the get_current_user FastAPI dependency.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import HTTPException, Request

import config
import db

_logger = logging.getLogger(__name__)

# ── Password Hashing (bcrypt, cost 12) ───────────────────────────


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── Fernet Encryption (broker credentials at rest) ───────────────
_fernet = None


def encryption_enabled() -> bool:
    """Whether encrypted broker credential storage is configured."""
    return bool(config.ENCRYPTION_KEY)


def _get_fernet():
    """Lazy-init Fernet cipher from ENCRYPTION_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet
    key = config.ENCRYPTION_KEY
    if not key:
        _logger.warning("[Auth] ENCRYPTION_KEY not set — broker credentials will be stored in plaintext")
        return None
    from cryptography.fernet import Fernet

    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string. Returns plaintext if no key configured."""
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a string. Returns as-is if no key configured."""
    if not ciphertext:
        return ""
    f = _get_fernet()
    if f is None:
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        # If decryption fails, it's probably plaintext (pre-encryption data)
        return ciphertext


# ── Session Management ───────────────────────────────────────────


async def create_session(user_id: int) -> str:
    """Create a new session token for a user. Returns the token string."""
    token = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=config.SESSION_TTL_HOURS)).isoformat()
    await db.create_session(token, user_id, expires_at)
    return token


async def validate_session(token: str) -> dict | None:
    """Validate a session token. Returns session dict (with user_id) or None."""
    if not token:
        return None
    return await db.get_session(token)


async def destroy_session(token: str) -> None:
    """Destroy a session (logout)."""
    await db.delete_session(token)


# ── Request Helpers ──────────────────────────────────────────────


def get_session_token(request: Request) -> str:
    """Extract session token from cookie or Authorization header."""
    token = request.cookies.get("algoforge_session", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    return token


async def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency — extracts and validates the session, returns the full user dict.
    Raises 401 if not authenticated.
    """
    cached_user = getattr(request.state, "current_user", None)
    if cached_user:
        return cached_user

    token = get_session_token(request)
    session = await validate_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await db.get_user_by_id(session["user_id"])
    if not user or not user["is_active"]:
        if user:
            await db.delete_sessions_for_user(user["id"])
        elif session.get("user_id"):
            await db.delete_session(token)
        raise HTTPException(status_code=401, detail="Account disabled or not found")

    request.state.current_user = user
    return user


async def require_admin(request: Request) -> dict:
    """FastAPI dependency — like get_current_user but requires admin role."""
    user = await get_current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
