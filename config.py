# ============================================================
#  PhilForge — Configuration
#  Load credentials from .env file (NEVER hardcode them!)
# ============================================================

import base64
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ── Dhan API Credentials ────────────────────────────────────
# Get these from: https://dhanhq.co → API → Generate Token
# ⚠ WARNING: Credentials are loaded from .env (not from source code)
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "YOUR_CLIENT_ID_HERE")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN_HERE")

# ── TOTP Auto-Token Settings ────────────────────────────────
# Enable TOTP on Dhan Web, save the secret here for auto-generation
DHAN_PIN = os.getenv("DHAN_PIN", "")
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "")
AUTO_TOKEN_ENABLED = bool(DHAN_PIN and DHAN_TOTP_SECRET)


def get_token_expiry() -> dict:
    """Decode JWT token and return expiry info without external libs"""
    try:
        parts = DHAN_ACCESS_TOKEN.split(".")
        if len(parts) < 2:
            return {"valid": False, "error": "Not a valid JWT token"}
        payload = parts[1]
        # Add padding
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp_ts = decoded.get("exp", 0)
        if not exp_ts:
            return {"valid": False, "error": "No expiry found in token"}
        exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        days_left = (exp_dt - now).days
        return {
            "valid": True,
            "expiry_date": exp_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "days_left": days_left,
            "expired": days_left < 0,
            "warning": days_left <= 7,
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


# ── Dhan API Base URLs ──────────────────────────────────────
DHAN_BASE_URL = "https://api.dhan.co"
DHAN_DATA_URL = "https://api.dhan.co/v2"

# ── App Settings ────────────────────────────────────────────
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ── Backtest Defaults ───────────────────────────────────────
DEFAULT_SYMBOL = "NIFTY"
DEFAULT_FROM = "2024-01-01"
DEFAULT_TO = "2026-02-26"
DEFAULT_CAPITAL = 500000  # ₹5,00,000

# ── Live Engine Settings ────────────────────────────────────
POLL_INTERVAL_SEC = 10  # REST fallback poll interval (seconds)
MAX_TRADES_PER_DAY = 1
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:25"

# ── Indicator Defaults (match your strategy) ───────────────
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 2.7
EMA_PERIOD = 17
RSI_PERIOD = 14
CPR_NARROW_RANGE = 0.2
CPR_MODERATE_RANGE = 0.5
CPR_WIDE_RANGE = 0.5

# ── Multi-Tenant Database & Auth ──────────────────────────
_CONFIG_ROOT = os.path.dirname(__file__)
_LEGACY_DB_PATH = os.path.join(_CONFIG_ROOT, "algoforge.db")
_DEFAULT_DB_PATH = os.path.join(_CONFIG_ROOT, "philforge.db")


def _env_first(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if value not in (None, ""):
            return value
    return default


DB_PATH = _env_first(
    "PHILFORGE_DB",
    "ALGOFORGE_DB",
    default=_LEGACY_DB_PATH if os.path.exists(_LEGACY_DB_PATH) else _DEFAULT_DB_PATH,
)
USER_DATA_ROOT = _env_first(
    "PHILFORGE_USER_DATA_ROOT",
    "ALGOFORGE_USER_DATA_ROOT",
    default=os.path.join(_CONFIG_ROOT, "data", "users"),
)
ADMIN_USERNAME = (os.getenv("ADMIN_USERNAME", "admin") or "admin").strip()
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")  # Fernet key for broker creds at rest
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
MAX_LOGIN_ATTEMPTS = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "5"))
BACKUP_ROOT = _env_first(
    "PHILFORGE_BACKUP_ROOT",
    "ALGOFORGE_BACKUP_ROOT",
    default=os.path.join(_CONFIG_ROOT, "backups"),
)
BACKUP_RETENTION_DAYS = int(
    _env_first("PHILFORGE_BACKUP_RETENTION_DAYS", "ALGOFORGE_BACKUP_RETENTION_DAYS", default="14")
)
BACKUP_MIN_FREE_MB = int(_env_first("PHILFORGE_BACKUP_MIN_FREE_MB", "ALGOFORGE_BACKUP_MIN_FREE_MB", default="1024"))
DHAN_REFERRAL_URL = (os.getenv("DHAN_REFERRAL_URL", "") or "").strip()
