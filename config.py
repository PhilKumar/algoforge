# ============================================================
#  AlgoForge — Configuration
#  Load credentials from .env file (NEVER hardcode them!)
# ============================================================

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ── Dhan API Credentials ────────────────────────────────────
# Get these from: https://dhanhq.co → API → Generate Token
# ⚠ WARNING: Credentials are loaded from .env (not from source code)
DHAN_CLIENT_ID    = os.getenv('DHAN_CLIENT_ID', 'YOUR_CLIENT_ID_HERE')
DHAN_ACCESS_TOKEN = os.getenv('DHAN_ACCESS_TOKEN', 'YOUR_ACCESS_TOKEN_HERE')

# ── Dhan API Base URLs ──────────────────────────────────────
DHAN_BASE_URL    = "https://api.dhan.co"
DHAN_DATA_URL    = "https://api.dhan.co/v2"

# ── App Settings ────────────────────────────────────────────
APP_HOST         = os.getenv('APP_HOST', '127.0.0.1')
APP_PORT         = int(os.getenv('APP_PORT', '8000'))
DEBUG            = os.getenv('DEBUG', 'false').lower() == 'true'

# ── Backtest Defaults ───────────────────────────────────────
DEFAULT_SYMBOL   = "NIFTY"
DEFAULT_FROM     = "2024-01-01"
DEFAULT_TO       = "2026-02-26"
DEFAULT_CAPITAL  = 500000   # ₹5,00,000

# ── Live Engine Settings ────────────────────────────────────
POLL_INTERVAL_SEC  = 60     # check conditions every 60 seconds
MAX_TRADES_PER_DAY = 1
MARKET_OPEN        = "09:15"
MARKET_CLOSE       = "15:25"

# ── Indicator Defaults (match your strategy) ───────────────
SUPERTREND_PERIOD     = 10
SUPERTREND_MULTIPLIER = 2.7
EMA_PERIOD            = 17
RSI_PERIOD            = 14
CPR_NARROW_RANGE      = 0.2
CPR_MODERATE_RANGE    = 0.5
CPR_WIDE_RANGE        = 0.5