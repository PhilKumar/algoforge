"""
app.py — AlgoForge FastAPI Backend
Fixed:
  - Bug 4: yfinance MultiIndex columns flattened correctly
  - Bug 5: live engine uses asyncio.create_task (not background_tasks)
  - Added /logo.jpg route for the frontend
"""

import asyncio
import inspect
import json

try:
    import orjson as _orjson
except ImportError:
    _orjson = None
import logging
import os
import secrets
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
_logger = logging.getLogger(__name__)

try:
    from prometheus_fastapi_instrumentator import Instrumentator as _PFI

    _PROMETHEUS_ENABLED = True
except ImportError:
    _PFI = None
    _PROMETHEUS_ENABLED = False

import pandas as pd

# ── Guaranteed path fix ───────────────────────────────────────────
# inspect.getfile() works even when uvicorn reload corrupts __file__
_HERE = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.chdir(_HERE)
# ─────────────────────────────────────────────────────────────────

import fcntl

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from broker.dhan import DhanClient, ScripMaster
from engine.backtest import DEFAULT_ENTRY_CONDITIONS, DEFAULT_EXIT_CONDITIONS, run_backtest
from engine.live import LiveEngine
from engine.market_feed import HAS_DHAN_FEED, get_market_feed, shutdown_feed
from engine.paper_trading import PaperTradingEngine

try:
    from scalp import ScalpEngine as _ScalpEngineClass

    _HAS_SCALP = True
except ImportError:
    _HAS_SCALP = False
    _ScalpEngineClass = None
import alerter
from token_manager import auto_generate_token, token_renewal_loop

# ── Auto-generate Dhan token at startup (single-worker guard) ────
if config.AUTO_TOKEN_ENABLED:
    _lock_file = os.path.join(_HERE, ".token_lock")
    try:
        _lf = open(_lock_file, "w")
        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock — this worker generates the token
        print("🔑 [TokenManager] Auto-token enabled — generating fresh Dhan token...")
        _new_tok = auto_generate_token()
        if _new_tok:
            # Write token to a shared file so other workers can read it
            _tok_file = os.path.join(_HERE, ".current_token")
            with open(_tok_file, "w") as f:
                f.write(_new_tok)
            print("✅ [TokenManager] Token generated successfully")
        else:
            print("⚠️  [TokenManager] Auto-token failed, using existing DHAN_ACCESS_TOKEN from .env")
        fcntl.flock(_lf, fcntl.LOCK_UN)
        _lf.close()
    except (IOError, OSError):
        # Another worker already holds the lock — read their token
        import time as _t

        _t.sleep(3)  # wait for the first worker to finish
        _tok_file = os.path.join(_HERE, ".current_token")
        if os.path.exists(_tok_file):
            with open(_tok_file) as f:
                _shared_token = f.read().strip()
            if _shared_token:
                config.DHAN_ACCESS_TOKEN = _shared_token
                print("✅ [TokenManager] Loaded token from first worker")
else:
    print("ℹ️  [TokenManager] Auto-token disabled (set DHAN_PIN + DHAN_TOTP_SECRET in .env to enable)")

# Initialize FastAPI app
app = FastAPI(title="AlgoForge", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://philipalgo.github.io",
        "http://philipalgoforge.local",
        "http://65.1.213.207",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from error_handlers import register_error_handlers

register_error_handlers(app)

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize custom client ONCE and pass to engine
dhan = DhanClient()

# ── Multi-Engine Registries (keyed by run_id) ────────────────
# Allows running multiple strategies simultaneously
live_engines: Dict[str, LiveEngine] = {}  # run_id → engine instance
paper_engines: Dict[str, PaperTradingEngine] = {}  # run_id → engine instance
_live_tasks: Dict[str, asyncio.Task] = {}  # run_id → asyncio task

# Backfill status — read by /api/backfill/status
_backfill_state: Dict[str, object] = {
    "status": "idle",  # idle | running | done | error
    "message": "",
    "new_dates": 0,
}
_paper_tasks: Dict[str, asyncio.Task] = {}  # run_id → asyncio task

# Global WebSocket market feed (singleton — shared by paper + live engines)
_market_feed = get_market_feed(dhan) if HAS_DHAN_FEED else None
_scalp_engine: Optional["_ScalpEngineClass"] = None

ws_clients: List[WebSocket] = []


# ── Authentication ────────────────────────────────────────────────
AUTH_PASSWORD = os.getenv("ALGOFORGE_PIN") or os.getenv("ALGOFORGE_PASSWORD")
if not AUTH_PASSWORD:
    raise RuntimeError(
        "[FATAL] ALGOFORGE_PIN environment variable is not set. "
        "The server refuses to start without an explicit PIN. "
        "Set it in your .env file: ALGOFORGE_PIN=<your-pin>"
    )
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
_SESSION_FILE = os.path.join(_HERE, ".sessions.json")

_redis_client = None
_redis_checked = False


def _get_redis():
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    try:
        import redis as _redis_lib

        r = _redis_lib.Redis(host="localhost", port=6379, db=0, decode_responses=True, socket_timeout=1)
        r.ping()
        _redis_client = r
    except Exception:
        _redis_client = None
    return _redis_client


def _load_sessions() -> dict:
    """Load sessions from shared file (works across workers)."""
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE, "r") as f:
                data = json.loads(f.read())
            # Clean expired sessions
            now = datetime.now().isoformat()
            return {k: v for k, v in data.items() if v > now}
    except Exception:
        pass
    return {}


def _save_sessions(sessions: dict):
    """Persist sessions to shared file (atomic write via tmp + os.replace)."""
    import tempfile

    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(_SESSION_FILE), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(sessions))
        os.replace(tmp_path, _SESSION_FILE)
    except Exception:
        # Remove tmp file if os.replace failed
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _create_session() -> str:
    sessions = _load_sessions()
    token = secrets.token_hex(32)
    sessions[token] = (datetime.now() + timedelta(hours=24)).isoformat()
    _save_sessions(sessions)
    return token


def _validate_session(token: str) -> bool:
    if not token:
        return False
    sessions = _load_sessions()
    exp_str = sessions.get(token)
    if not exp_str:
        return False
    if datetime.now() > datetime.fromisoformat(exp_str):
        sessions.pop(token, None)
        _save_sessions(sessions)
        return False
    return True


def _get_session_token(request: Request) -> str:
    """Extract session token from cookie or Authorization header"""
    token = request.cookies.get("algoforge_session", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    return token


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a unique request-id to every request for log tracing."""
    import uuid

    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Global auth — all routes require login unless whitelisted."""
    path = request.url.path
    # Allow login, health, static, and WebSocket without auth
    if path in ("/api/auth/login", "/api/auth/status", "/api/health", "/login", "/"):
        return await call_next(request)
    if path.startswith("/static") or path.startswith("/ws"):
        return await call_next(request)
    token = _get_session_token(request)
    if not _validate_session(token):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


# ── Rate Limiting ─────────────────────────────────────────────────
_rate_limits: dict = defaultdict(list)  # "endpoint:ip" -> [timestamps] (fallback)
_RL_PREFIX = "algoforge:rl:"


def check_rate_limit(endpoint: str, client_ip: str = "global", max_calls: int = 5, window_sec: int = 10):
    """Per-IP rate limiter — Redis sliding window when available, in-memory fallback."""
    key = f"{_RL_PREFIX}{endpoint}:{client_ip}"
    r = _get_redis()
    if r is not None:
        try:
            now_ms = int(time.time() * 1000)
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, now_ms - window_sec * 1000)
            pipe.zcard(key)
            pipe.zadd(key, {str(now_ms): now_ms})
            pipe.expire(key, window_sec + 1)
            _, count, *_ = pipe.execute()
            if count >= max_calls:
                raise HTTPException(
                    status_code=429, detail=f"Rate limit exceeded. Max {max_calls} calls per {window_sec}s."
                )
            return
        except HTTPException:
            raise
        except Exception as e:
            _logger.warning(f"[Redis] check_rate_limit failed, using in-memory: {e}")
    # In-memory fallback (bounded to 50k keys)
    now = time.time()
    mem_key = f"{endpoint}:{client_ip}"
    calls = _rate_limits[mem_key]
    _rate_limits[mem_key] = [t for t in calls if now - t < window_sec]
    if len(_rate_limits[mem_key]) >= max_calls:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Max {max_calls} calls per {window_sec}s.")
    _rate_limits[mem_key].append(now)
    if len(_rate_limits) > 50_000:
        stale = [k for k, v in _rate_limits.items() if not v or now - v[-1] > window_sec]
        for k in stale[:5_000]:
            del _rate_limits[k]


# ── Models ────────────────────────────────────────────────────────
class BacktestRequest(BaseModel):
    from_date: str = config.DEFAULT_FROM
    to_date: str = config.DEFAULT_TO
    symbol: str = "NIFTY"
    initial_capital: float = Field(default=config.DEFAULT_CAPITAL, gt=0)
    entry_conditions: Optional[List[dict]] = None
    exit_conditions: Optional[List[dict]] = None
    strategy_config: Optional[dict] = None


class LiveStartRequest(BaseModel):
    entry_conditions: Optional[List[dict]] = None
    exit_conditions: Optional[List[dict]] = None
    strategy_config: Optional[dict] = None
    # Full strategy fields (used when deploying from modal)
    run_name: str = ""
    instrument: str = ""
    indicators: List[str] = []
    legs: Optional[List[dict]] = None
    deploy_config: Optional[dict] = None
    max_trades_per_day: int = Field(default=1, ge=1, le=100)
    market_open: str = "09:15"
    market_close: str = "15:25"
    max_daily_loss: float = Field(default=0, ge=0)
    lots: int = Field(default=1, ge=1, le=500)
    stoploss_pct: float = Field(default=0.0, ge=0)
    stoploss_rupees: float = Field(default=0.0, ge=0)
    sl_type: str = "pct"
    target_profit_pct: float = Field(default=0.0, ge=0)
    target_profit_rupees: float = Field(default=0.0, ge=0)
    tp_type: str = "pct"


class OrderRequest(BaseModel):
    security_id: str
    exchange_segment: str = "NSE_EQ"
    transaction_type: str
    quantity: int = Field(ge=1, le=100_000)
    order_type: str = "MARKET"
    product_type: str = "INTRADAY"
    price: float = Field(default=0, ge=0)


class StrategyPayload(BaseModel):
    run_name: str = ""
    folder: str = "Intraday"
    segment: str = "indices"
    instrument: str = "26000"
    from_date: str = config.DEFAULT_FROM
    to_date: str = config.DEFAULT_TO
    initial_capital: float = Field(default=500000.0, gt=0)
    lots: int = Field(default=1, ge=1, le=500)
    lot_size: int = Field(default=0, ge=0)
    stoploss_pct: float = Field(default=0.0, ge=0)
    stoploss_rupees: float = Field(default=0.0, ge=0)
    sl_type: str = "pct"
    target_profit_pct: float = Field(default=0.0, ge=0)
    target_profit_rupees: float = Field(default=0.0, ge=0)
    tp_type: str = "pct"
    market_open: str = "09:15"
    market_close: str = "15:25"
    max_trades_per_day: int = Field(default=1, ge=1, le=100)
    max_daily_loss: float = Field(default=0.0, ge=0)
    indicators: List[str] = []
    entry_conditions: Optional[List[dict]] = None
    exit_conditions: Optional[List[dict]] = None
    legs: Optional[List[dict]] = None
    deploy_config: Optional[dict] = None
    combined_sl_rupees: float = 0
    combined_target_rupees: float = 0
    combined_sqoff_time: str = "15:20"
    fee_pct: float = 0.0
    trailing_sl_pct: float = 0.0


# ── Serve Frontend ────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_frontend(request: Request):
    token = _get_session_token(request)
    if not _validate_session(token):
        # Serve login page
        login_path = os.path.join(_HERE, "login.html")
        if os.path.exists(login_path):
            with open(login_path, encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h2>login.html not found</h2>")
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy.html")
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h2>strategy.html not found. Place it beside app.py</h2>")


@app.get("/logo.jpg")
async def serve_logo():
    """Serves the main application logo."""
    return FileResponse("logo.jpg")


@app.get("/logo.png")
async def serve_logo_png():
    """Serves the PNG application logo."""
    return FileResponse("logo.png")


# ── Chart Viewer ──────────────────────────────────────────────────
import calendar as _cal
import re as _re

CHARTS_DIR = os.getenv("CHARTS_DIR", os.path.join(_HERE, "Daily Charts"))

# Build month-name lookup: JAN→1, JANUARY→1, FEB→2, FEBRUARY→2, …
_MONTH_MAP: dict[str, int] = {}
for _i in range(1, 13):
    _MONTH_MAP[_cal.month_abbr[_i].upper()] = _i
    _MONTH_MAP[_cal.month_name[_i].upper()] = _i


def _parse_month_folder(name: str):
    """Parse 'APR_2023' / 'Apr-2024' / 'JULY_2023' → (month_num, label) or None."""
    parts = _re.split(r"[_-]", name, maxsplit=1)
    if len(parts) < 2:
        return None
    num = _MONTH_MAP.get(parts[0].upper()) or _MONTH_MAP.get(parts[0].upper()[:3])
    if num is None:
        return None
    return num, _cal.month_abbr[num]


def _parse_day_folder(name: str):
    """Parse day folder → (sort_key, display_label) or fallback to name itself."""
    # DD_MM_YYYY or DD-MM-YYYY (all numeric)
    m = _re.match(r"^(\d{1,2})[_-](\d{1,2})[_-](\d{4})$", name)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{yyyy:04d}-{mm:02d}-{dd:02d}", f"{dd:02d} {_cal.month_abbr[mm]}"
    # DD-Mon-YYYY (e.g. 01-Feb-2026)
    m = _re.match(r"^(\d{1,2})-([A-Za-z]+)-(\d{4})$", name)
    if m:
        dd = int(m.group(1))
        num = _MONTH_MAP.get(m.group(2).upper()) or _MONTH_MAP.get(m.group(2).upper()[:3])
        if num:
            return f"{int(m.group(3)):04d}-{num:02d}-{dd:02d}", f"{dd:02d} {_cal.month_abbr[num]}"
    # Fallback (ranges like Feb-12-15, Feb-4-5-6)
    return name, name


def _safe_charts_subpath(*parts: str) -> str | None:
    """Resolve path under CHARTS_DIR; return None if traversal detected."""
    for p in parts:
        if "/" in p or "\\" in p or ".." in p:
            return None
    candidate = os.path.join(CHARTS_DIR, *parts)
    if not os.path.realpath(candidate).startswith(os.path.realpath(CHARTS_DIR)):
        return None
    return candidate


@app.get("/charts-viewer", response_class=HTMLResponse)
async def serve_charts_viewer(request: Request):
    """Serve the historical chart viewer page (auth-protected)."""
    token = _get_session_token(request)
    if not _validate_session(token):
        login_path = os.path.join(_HERE, "login.html")
        if os.path.exists(login_path):
            with open(login_path, encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h2>login.html not found</h2>")
    html_path = os.path.join(_HERE, "charts.html")
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h2>charts.html not found. Place it beside app.py</h2>")


@app.get("/api/charts/tree")
async def charts_tree():
    """Return directory tree adapted to Daily Charts/ folder structure."""
    print(f"[CHARTS] Scanning CHARTS_DIR: {CHARTS_DIR}")
    print(f"[CHARTS] Exists: {os.path.isdir(CHARTS_DIR)}")
    if not os.path.isdir(CHARTS_DIR):
        print("[CHARTS] Directory NOT found – returning empty tree")
        return {"years": {}}
    tree: dict = {}
    for year in sorted(os.listdir(CHARTS_DIR)):
        year_path = os.path.join(CHARTS_DIR, year)
        if not os.path.isdir(year_path) or not year.isdigit():
            continue
        months_list = []
        for mfolder in os.listdir(year_path):
            month_path = os.path.join(year_path, mfolder)
            if not os.path.isdir(month_path):
                continue
            parsed = _parse_month_folder(mfolder)
            if parsed is None:
                continue
            month_num, month_label = parsed
            days_list = []
            for dfolder in os.listdir(month_path):
                day_path = os.path.join(month_path, dfolder)
                if not os.path.isdir(day_path):
                    continue
                has_img = any(f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) for f in os.listdir(day_path))
                if not has_img:
                    continue
                sort_key, day_label = _parse_day_folder(dfolder)
                days_list.append(
                    {
                        "folder": dfolder,
                        "label": day_label,
                        "sort": sort_key,
                    }
                )
            if not days_list:
                continue
            days_list.sort(key=lambda d: d["sort"])
            months_list.append(
                {
                    "folder": mfolder,
                    "label": month_label,
                    "num": month_num,
                    "days": days_list,
                }
            )
        if not months_list:
            continue
        months_list.sort(key=lambda m: m["num"])
        tree[year] = months_list
    print(
        f"[CHARTS] Tree result: {len(tree)} years, total days: {sum(sum(len(m['days']) for m in ms) for ms in tree.values())}"
    )
    return {"years": tree}


@app.get("/api/charts/images/{year}/{month}/{day}")
async def charts_images(year: str, month: str, day: str):
    """Return list of image URLs for a specific date folder."""
    day_path = _safe_charts_subpath(year, month, day)
    if day_path is None:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isdir(day_path):
        return {"images": [], "urls": [], "date": day}
    images = sorted(f for f in os.listdir(day_path) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
    from urllib.parse import quote

    return {
        "images": images,
        "date": day,
        "urls": [f"/charts-static/{quote(year)}/{quote(month)}/{quote(day)}/{quote(img)}" for img in images],
    }


@app.get("/charts-static/{year}/{month}/{day}/{filename}")
async def serve_chart_image(year: str, month: str, day: str, filename: str):
    """Serve a single chart image file."""
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        raise HTTPException(status_code=400, detail="Invalid file type")
    file_path = _safe_charts_subpath(year, month, day, safe_name)
    if file_path is None or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(file_path)


# ── Chart Upload (Ctrl+V paste) ──────────────────────────────────
JOURNAL_DIR = os.path.join(_HERE, "journals")
_ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


@app.post("/api/upload-chart")
async def upload_chart(file: UploadFile):
    """Receive a pasted screenshot, save to Daily Charts/YYYY/Mon-YYYY/DD-Mon-YYYY/."""
    from urllib.parse import quote

    # Validate file type
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    # Read with size limit
    data = await file.read()
    if len(data) > _MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    # Determine extension from content type
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    ext = ext_map.get(file.content_type, ".png")

    # Build date-based folder: Daily Charts/YYYY/Mon-YYYY/DD-Mon-YYYY/
    from datetime import timezone as _tz

    now_ist = datetime.now(_tz(timedelta(hours=5, minutes=30)))
    year_str = str(now_ist.year)
    month_abbr = _cal.month_abbr[now_ist.month]
    month_folder = f"{month_abbr}-{year_str}"
    day_folder = f"{now_ist.day:02d}-{month_abbr}-{year_str}"

    day_path = os.path.join(CHARTS_DIR, year_str, month_folder, day_folder)
    os.makedirs(day_path, exist_ok=True)
    print(f"[CHARTS] Upload target dir: {day_path}")

    # Generate filename: Nifty_DD-MM-YYYY[_N].ext
    date_tag = now_ist.strftime("%d-%m-%Y")
    filename = f"Nifty_{date_tag}{ext}"
    file_path = os.path.join(day_path, filename)

    # Avoid overwrite
    counter = 1
    while os.path.exists(file_path):
        filename = f"Nifty_{date_tag}_{counter}{ext}"
        file_path = os.path.join(day_path, filename)
        counter += 1

    with open(file_path, "wb") as f:
        f.write(data)
    print(f"[CHARTS] Saved upload: {file_path} ({len(data)} bytes)")

    url = f"/charts-static/{quote(year_str)}/{quote(month_folder)}/{quote(day_folder)}/{quote(filename)}"
    return {
        "status": "ok",
        "filename": filename,
        "url": url,
        "year": year_str,
        "month_folder": month_folder,
        "day_folder": day_folder,
    }


# ── Delete a chart image ─────────────────────────────────────────
@app.delete("/api/charts/delete/{year}/{month}/{day}/{filename}")
async def delete_chart(year: str, month: str, day: str, filename: str):
    """Delete a single chart image file."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_IMG_EXT:
        raise HTTPException(status_code=400, detail="Invalid file type")
    file_path = _safe_charts_subpath(year, month, day, filename)
    if file_path is None:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(file_path)
    print(f"[CHARTS] Deleted: {file_path}")
    return {"status": "ok", "deleted": filename}


# ── Rename a chart image ─────────────────────────────────────────
@app.patch("/api/charts/rename/{year}/{month}/{day}/{filename}")
async def rename_chart(year: str, month: str, day: str, filename: str, request: Request):
    """Rename a chart image file."""
    body = await request.json()
    new_name = body.get("new_name", "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="New name is required")
    # Validate old file
    old_ext = os.path.splitext(filename)[1].lower()
    if old_ext not in _ALLOWED_IMG_EXT:
        raise HTTPException(status_code=400, detail="Invalid file type")
    old_path = _safe_charts_subpath(year, month, day, filename)
    if old_path is None:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isfile(old_path):
        raise HTTPException(status_code=404, detail="File not found")
    # Sanitize new name: keep extension, strip dangerous chars
    new_base = _re.sub(r"[^\w\s._-]", "", os.path.splitext(new_name)[0])[:80]
    if not new_base:
        raise HTTPException(status_code=400, detail="Invalid new name")
    new_filename = f"{new_base}{old_ext}"
    new_path = _safe_charts_subpath(year, month, day, new_filename)
    if new_path is None:
        raise HTTPException(status_code=400, detail="Invalid new path")
    if os.path.exists(new_path):
        raise HTTPException(status_code=409, detail="A file with that name already exists")
    os.rename(old_path, new_path)
    from urllib.parse import quote

    new_url = f"/charts-static/{quote(year)}/{quote(month)}/{quote(day)}/{quote(new_filename)}"
    print(f"[CHARTS] Renamed: {filename} → {new_filename}")
    return {"status": "ok", "old_name": filename, "new_name": new_filename, "url": new_url}


# ── Daily Journal (localStorage-backed on frontend, JSON file backup) ─
@app.get("/api/journal/list")
async def list_journals():
    """Return list of all journal dates that have entries."""
    if not os.path.isdir(JOURNAL_DIR):
        return {"entries": []}
    entries = []
    for fname in sorted(os.listdir(JOURNAL_DIR), reverse=True):
        if not fname.endswith(".json"):
            continue
        date_str = fname[:-5]
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            continue
        fpath = os.path.join(JOURNAL_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Include summary fields for the list view
            entries.append(
                {
                    "date": date_str,
                    "asset": data.get("asset", ""),
                    "grade": data.get("grade", ""),
                    "strategy": data.get("strategy", ""),
                }
            )
        except Exception:
            entries.append({"date": date_str, "asset": "", "grade": "", "strategy": ""})
    return {"entries": entries}


@app.get("/api/journal/{date_str}")
async def get_journal(date_str: str):
    """Load journal entry for a date (YYYY-MM-DD)."""
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=400, detail="Invalid date format (use YYYY-MM-DD)")
    journal_file = os.path.join(JOURNAL_DIR, f"{date_str}.json")
    if not os.path.realpath(journal_file).startswith(os.path.realpath(JOURNAL_DIR)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isfile(journal_file):
        return {"date": date_str, "data": None}
    with open(journal_file, "r", encoding="utf-8") as f:
        return {"date": date_str, "data": json.load(f)}


@app.put("/api/journal/{date_str}")
async def save_journal(date_str: str, request: Request):
    """Save journal entry for a date (YYYY-MM-DD)."""
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=400, detail="Invalid date format (use YYYY-MM-DD)")
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    journal_file = os.path.join(JOURNAL_DIR, f"{date_str}.json")
    if not os.path.realpath(journal_file).startswith(os.path.realpath(JOURNAL_DIR)):
        raise HTTPException(status_code=400, detail="Invalid path")
    body = await request.json()
    # Sanitize: only allow known fields
    allowed = {"asset", "strategy", "grade", "went_well", "to_improve", "mental_state"}
    clean = {k: str(v)[:2000] for k, v in body.items() if k in allowed}
    with open(journal_file, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
    return {"status": "ok", "date": date_str}


@app.delete("/api/journal/{date_str}")
async def delete_journal(date_str: str):
    """Delete a journal entry for a date (YYYY-MM-DD)."""
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=400, detail="Invalid date format")
    journal_file = os.path.join(JOURNAL_DIR, f"{date_str}.json")
    if not os.path.realpath(journal_file).startswith(os.path.realpath(JOURNAL_DIR)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isfile(journal_file):
        raise HTTPException(status_code=404, detail="Journal entry not found")
    os.remove(journal_file)
    try:
        localStorage_key = f"cj_journal_{date_str}"
        print(f"[JOURNAL] Deleted: {journal_file}")
    except Exception:
        pass
    return {"status": "ok", "deleted": date_str}


# ── Brute-Force Protection ────────────────────────────────────────
_login_attempts: dict = defaultdict(list)  # ip -> [timestamps] (fallback)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SEC = 300  # 5 minutes
_LOGIN_RL_PREFIX = "algoforge:login:"


def _check_login_rate(ip: str):
    r = _get_redis()
    if r is not None:
        try:
            key = f"{_LOGIN_RL_PREFIX}{ip}"
            count = int(r.get(key) or 0)
            if count >= _LOGIN_MAX_ATTEMPTS:
                raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 5 minutes.")
            return
        except HTTPException:
            raise
        except Exception as e:
            _logger.warning(f"[Redis] _check_login_rate failed, using in-memory: {e}")
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_LOCKOUT_SEC]
    if len(_login_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 5 minutes.")


def _record_failed_login(ip: str):
    r = _get_redis()
    if r is not None:
        try:
            key = f"{_LOGIN_RL_PREFIX}{ip}"
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, _LOGIN_LOCKOUT_SEC)
            pipe.execute()
            return
        except Exception as e:
            _logger.warning(f"[Redis] _record_failed_login failed, using in-memory: {e}")
    _login_attempts[ip].append(time.time())


def _clear_login_attempts(ip: str):
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"{_LOGIN_RL_PREFIX}{ip}")
            return
        except Exception:
            pass
    _login_attempts.pop(ip, None)


# ── Authentication Endpoints ──────────────────────────────────────
@app.post("/api/auth/login")
async def auth_login(request: Request):
    ip = request.client.host if request.client else "unknown"
    _check_login_rate(ip)
    body = await request.json()
    password = body.get("password", "")
    if password == AUTH_PASSWORD:
        _clear_login_attempts(ip)
        token = _create_session()
        resp = JSONResponse({"status": "ok", "message": "Login successful"})
        # secure=True only when behind HTTPS; current setup is HTTP-only
        is_https = request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie("algoforge_session", token, max_age=86400, httponly=True, samesite="lax", secure=is_https)
        return resp
    _record_failed_login(ip)
    raise HTTPException(status_code=401, detail="Invalid password")


@app.get("/api/auth/status")
async def auth_status(request: Request):
    token = _get_session_token(request)
    valid = _validate_session(token)
    return {"authenticated": valid}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = _get_session_token(request)
    sessions = _load_sessions()
    sessions.pop(token, None)
    _save_sessions(sessions)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("algoforge_session")
    return resp


# ── Emergency Stop (Kill Switch) ─────────────────────────────────
@app.post("/api/emergency-stop")
async def emergency_stop(request: Request):
    """Kill switch: stop ALL running strategies immediately"""
    token = _get_session_token(request)
    if not _validate_session(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    results = {}
    stopped_count = 0
    # Stop all paper engines
    for run_id, engine in list(paper_engines.items()):
        try:
            if engine.running:
                engine.stop()
                results[f"paper:{run_id}"] = "stopped"
                stopped_count += 1
            else:
                results[f"paper:{run_id}"] = "not_running"
        except Exception as e:
            results[f"paper:{run_id}"] = f"error: {str(e)}"

    # Stop all live engines
    for run_id, engine in list(live_engines.items()):
        try:
            if engine.running:
                engine.stop()
                results[f"live:{run_id}"] = "stopped"
                stopped_count += 1
            else:
                results[f"live:{run_id}"] = "not_running"
        except Exception as e:
            results[f"live:{run_id}"] = f"error: {str(e)}"

    # Cancel all background tasks
    for name, tasks_dict in [("live", _live_tasks), ("paper", _paper_tasks)]:
        for run_id, task_ref in list(tasks_dict.items()):
            if task_ref and not task_ref.done():
                task_ref.cancel()
                try:
                    await task_ref
                except asyncio.CancelledError:
                    pass
    _live_tasks.clear()
    _paper_tasks.clear()
    live_engines.clear()
    paper_engines.clear()

    return {
        "status": "ok",
        "stopped": stopped_count,
        "message": f"Emergency stop executed — {stopped_count} engine(s) stopped",
        "results": results,
        "timestamp": str(datetime.now()),
    }


# ── Dashboard Summary ─────────────────────────────────────────────
@app.get("/api/dashboard/summary")
async def dashboard_summary(request: Request):
    """Aggregated dashboard data for the homepage"""
    token = _get_session_token(request)
    if not _validate_session(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Strategies count
    strats = _load()
    runs = _load_runs()

    # Active engines
    paper_running = any(e.running for e in paper_engines.values())
    live_running = any(e.running for e in live_engines.values())
    paper_statuses = [e.get_status() for e in paper_engines.values() if e.running]
    live_statuses = [e.get_status() for e in live_engines.values() if e.running]

    # Today's P&L from engines (+ history for idle engines)
    paper_pnl_val = 0
    paper_trades_val = 0
    live_pnl_val = 0
    live_trades_val = 0

    if paper_statuses:
        paper_pnl_val = sum(s.get("total_pnl", 0) for s in paper_statuses)
        paper_trades_val = sum(s.get("trades_today", 0) for s in paper_statuses)
    else:
        # Show last paper run P&L from today (from runs.json)
        from datetime import date as _date

        today_str = str(_date.today())
        for r in reversed(runs):
            if r.get("mode") == "paper":
                created = r.get("created_at", "")
                if created.startswith(today_str):
                    paper_pnl_val = r.get("total_pnl", 0)
                    paper_trades_val = r.get("trade_count", len(r.get("trades", [])))
                break

    if live_statuses:
        live_pnl_val = sum(s.get("total_pnl", 0) for s in live_statuses)
        live_trades_val = sum(s.get("trades_today", 0) for s in live_statuses)

    today_pnl = paper_pnl_val + live_pnl_val

    # Best/worst backtest runs
    best_run = None
    worst_run = None
    total_backtests = len(runs)
    if runs:
        for r in runs:
            pnl = r.get("total_pnl", 0)
            if best_run is None or pnl > best_run.get("total_pnl", 0):
                best_run = {"id": r.get("id"), "name": r.get("run_name", ""), "pnl": pnl}
            if worst_run is None or pnl < worst_run.get("total_pnl", 0):
                worst_run = {"id": r.get("id"), "name": r.get("run_name", ""), "pnl": pnl}

    return {
        "strategy_count": len(strats),
        "backtest_count": total_backtests,
        "paper_running": paper_running,
        "live_running": live_running,
        "paper_strategy": ", ".join(s.get("strategy_name", "") for s in paper_statuses) if paper_statuses else "",
        "live_strategy": ", ".join(s.get("strategy_name", "") for s in live_statuses) if live_statuses else "",
        "today_pnl": round(today_pnl, 2),
        "paper_pnl": round(paper_pnl_val, 2),
        "live_pnl": round(live_pnl_val, 2),
        "paper_trades": paper_trades_val,
        "live_trades": live_trades_val,
        "best_run": best_run,
        "worst_run": worst_run,
    }


# ── Strategy Validation ──────────────────────────────────────────
@app.post("/api/validate-strategy")
async def validate_strategy(request: Request):
    """Deep validation of strategy before deployment"""
    token = _get_session_token(request)
    if not _validate_session(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    errors = []
    warnings = []

    # Instrument
    instrument = body.get("instrument", "")
    if not instrument:
        errors.append("No instrument selected")

    # Conditions
    entry = body.get("entry_conditions", [])
    exit_conds = body.get("exit_conditions", [])
    if not entry:
        errors.append("No entry conditions defined")
    if not exit_conds:
        warnings.append("No exit conditions — trades will only close at square-off time or SL/target")

    # Legs validation
    legs = body.get("legs", [])
    if legs:
        for i, leg in enumerate(legs):
            if not leg.get("lots"):
                errors.append(f"Leg {i + 1}: lot size not specified")
            sl = leg.get("sl_points", 0)
            tp = leg.get("tp_points", 0)
            if sl and tp and tp <= sl:
                warnings.append(f"Leg {i + 1}: target ({tp}) is less than stop-loss ({sl}) — poor risk:reward")

    # Contradictory conditions check
    for c in entry:
        lhs = c.get("lhs", "")
        op = c.get("operator", "")
        rhs = c.get("rhs", "")
        # Check if same indicator has contradictory conditions
        for c2 in entry:
            if c2 is c:
                continue
            if c2.get("lhs") == lhs and c2.get("rhs") == rhs:
                if op in ("is_above", "crosses_above") and c2.get("operator") in ("is_below", "crosses_below"):
                    errors.append(f"Contradictory conditions: {lhs} cannot be both above and below {rhs}")

    # Risk checks
    sl_pct = body.get("stoploss_pct", 0)
    tp_pct = body.get("target_profit_pct", 0)
    if sl_pct and tp_pct and tp_pct < sl_pct:
        warnings.append(f"Risk:Reward unfavorable — SL {sl_pct}% vs Target {tp_pct}%")
    if sl_pct == 0:
        warnings.append("No strategy-level stop-loss set — unlimited downside risk")

    max_trades = body.get("max_trades_per_day", 1)
    if max_trades > 5:
        warnings.append(f"High trade frequency ({max_trades}/day) — check for overtrading")

    # Lot size / capital validation (#13)
    from engine.backtest import get_lot_size

    lots = int(body.get("lots", 1) or 1)
    user_lot_size = int(body.get("lot_size", 0) or 0)
    initial_capital = float(body.get("initial_capital", 500000) or 500000)
    if instrument:
        inst_name = "NIFTY"
        if "26009" in str(instrument) or "BANK" in str(instrument).upper():
            inst_name = "BANKNIFTY"
        elif "26017" in str(instrument) or "FIN" in str(instrument).upper():
            inst_name = "FINNIFTY"
        current_lot = get_lot_size(instrument, date.today())
        if user_lot_size > 0 and user_lot_size != current_lot:
            warnings.append(f"Custom lot size ({user_lot_size}) differs from current {inst_name} lot ({current_lot})")
        effective_lot = user_lot_size if user_lot_size > 0 else current_lot
        total_qty = lots * effective_lot
        # Estimate margin: rough NIFTY option margin ~₹1.5L per lot
        est_margin_per_lot = 150000 if "BANK" in inst_name else 100000
        est_margin = lots * est_margin_per_lot
        if est_margin > initial_capital * 0.8:
            warnings.append(
                f"Estimated margin ₹{est_margin:,.0f} for {lots} lot(s) may exceed 80% of capital ₹{initial_capital:,.0f}"
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "instrument": instrument,
            "entry_conditions": len(entry),
            "exit_conditions": len(exit_conds),
            "legs": len(legs),
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
        },
    }


# ── Portfolio Summary API (#8) ────────────────────────────────────
@app.get("/api/portfolio/summary")
async def portfolio_summary(request: Request):
    """Aggregated portfolio: balance + positions + unrealized P&L in one call"""
    token = _get_session_token(request)
    if not _validate_session(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = {"funds": None, "positions": [], "unrealized_pnl": 0, "total_value": 0, "errors": []}
    # Funds
    try:
        funds = await asyncio.to_thread(dhan.get_funds)
        result["funds"] = funds
        if isinstance(funds, dict):
            result["total_value"] = float(funds.get("availabelBalance", funds.get("available_balance", 0)))
    except Exception as e:
        result["errors"].append(f"Funds: {str(e)}")

    # Positions + unrealized P&L
    try:
        positions = await asyncio.to_thread(dhan.get_positions)
        result["positions"] = positions
        unrealized = 0
        for pos in positions if isinstance(positions, list) else []:
            unrealized += float(pos.get("unrealizedProfit", pos.get("dayProfit", 0)))
        result["unrealized_pnl"] = round(unrealized, 2)
        result["total_value"] = round(result["total_value"] + unrealized, 2)
    except Exception as e:
        result["errors"].append(f"Positions: {str(e)}")

    return result


# ── Strategy Versioning ──────────────────────────────────────────
@app.get("/api/strategies/{sid}/versions")
async def get_strategy_versions(sid: int):
    strats = _load()
    for s in strats:
        if s.get("id") == sid:
            return {"versions": s.get("versions", [])}
    raise HTTPException(status_code=404, detail="Strategy not found")


# ── Health ────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "time": str(datetime.now()),
        "dhan_configured": (
            config.DHAN_CLIENT_ID != "YOUR_CLIENT_ID_HERE" and config.DHAN_ACCESS_TOKEN != "YOUR_ACCESS_TOKEN_HERE"
        ),
        "live_running": any(e.running for e in live_engines.values()),
    }


@app.get("/api/token-status")
async def token_status():
    """Check Dhan API token expiry"""
    return config.get_token_expiry()


# ── Broker Connection Validation ──────────────────────────────────
@app.post("/api/broker/check")
async def check_broker():
    """Check if broker connection is active and valid"""
    try:
        # Check if credentials are configured (not default placeholders)
        if config.DHAN_CLIENT_ID == "YOUR_CLIENT_ID_HERE" or config.DHAN_ACCESS_TOKEN == "YOUR_ACCESS_TOKEN_HERE":
            return {
                "status": "not_configured",
                "broker": "Dhan",
                "message": "Dhan API credentials not configured. Please update .env file.",
            }

        # Test connection by fetching account funds
        funds = dhan.get_funds()

        if funds and isinstance(funds, dict):
            # Valid response - connection is working
            available_balance = float(funds.get("availabelBalance", 0) or 0)
            return {
                "status": "connected",
                "broker": "Dhan",
                "message": "Broker connection active",
                "available_balance": available_balance,
                "funds": funds,
            }
        else:
            # No data returned
            return {"status": "error", "broker": "Dhan", "message": "Invalid response from broker API"}

    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg:
            return {"status": "error", "broker": "Dhan", "message": "Invalid API credentials (401 Unauthorized)"}
        elif "403" in error_msg or "Forbidden" in error_msg:
            return {"status": "error", "broker": "Dhan", "message": "Access forbidden - check API permissions (403)"}
        elif "timeout" in error_msg.lower():
            return {"status": "error", "broker": "Dhan", "message": "Connection timeout - network issue"}
        else:
            return {"status": "error", "broker": "Dhan", "message": f"Connection error: {error_msg[:100]}"}


@app.get("/api/broker/trades")
async def get_broker_trades():
    """Fetch executed trades from Dhan broker account"""
    try:
        # Check if credentials are configured
        if config.DHAN_CLIENT_ID == "YOUR_CLIENT_ID_HERE" or config.DHAN_ACCESS_TOKEN == "YOUR_ACCESS_TOKEN_HERE":
            return {"status": "not_configured", "message": "Dhan API credentials not configured", "trades": []}

        # Fetch trades from Dhan API
        trades_result = dhan.get_trades()
        trades = trades_result if isinstance(trades_result, list) else []

        # Auto-persist daily trade summary for portfolio history
        if trades:
            try:
                _persist_daily_trades(trades)
            except Exception as pe:
                print(f"[TRADE_HISTORY] Persist error: {pe}")

        return {"status": "success", "broker": "Dhan", "count": len(trades), "trades": trades}

    except Exception as e:
        error_msg = str(e)
        return {
            "status": "error",
            "broker": "Dhan",
            "message": f"Failed to fetch trades: {error_msg[:100]}",
            "trades": [],
        }


def _backfill_trade_history(from_date: str = "2024-01-01", force: bool = False):
    """Fetch historical trades from Dhan and backfill trade_history.json.

    Args:
        from_date: Start date in YYYY-MM-DD format.
        force: If True, overwrite existing dates with fresh data from Dhan.
    """
    import time as _time

    try:
        history = _load_trade_history() if not force else {}
        today_str = datetime.now().strftime("%Y-%m-%d")
        existing_dates = set(history.keys())

        # Dhan API returns 20 trades per page, paginate through all
        DHAN_PAGE_SIZE = 20
        MAX_PAGES = 500  # Safety limit (up to 10,000 trades)
        RATE_LIMIT_RETRIES = 3
        PAGE_DELAY = 0.3  # seconds between pages to avoid rate-limit
        all_trades = []
        page = 0
        consecutive_empty = 0
        while page < MAX_PAGES:
            result = dhan.get_trade_history(from_date, today_str, page)

            # Handle rate-limit: retry with exponential backoff
            if result == dhan.RATE_LIMITED:
                retried = False
                for attempt in range(1, RATE_LIMIT_RETRIES + 1):
                    wait = 2**attempt  # 2, 4, 8 seconds
                    print(f"[BACKFILL] Rate limited on page {page}, retry {attempt}/{RATE_LIMIT_RETRIES} after {wait}s")
                    _time.sleep(wait)
                    result = dhan.get_trade_history(from_date, today_str, page)
                    if result != dhan.RATE_LIMITED:
                        retried = True
                        break
                if not retried and result == dhan.RATE_LIMITED:
                    print(f"[BACKFILL] Rate limit persists after {RATE_LIMIT_RETRIES} retries on page {page}, stopping")
                    break

            trades = result if isinstance(result, list) else []
            if not trades:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break  # 3 consecutive empty pages = truly done
                page += 1
                _time.sleep(PAGE_DELAY)
                continue

            consecutive_empty = 0
            all_trades.extend(trades)
            print(f"[BACKFILL] Page {page}: {len(trades)} trades (total so far: {len(all_trades)})")
            if len(trades) < DHAN_PAGE_SIZE:  # Last page
                break
            page += 1
            _time.sleep(PAGE_DELAY)  # Throttle to avoid Dhan rate-limit

        if not all_trades:
            print(f"[BACKFILL] No historical trades returned from Dhan for {from_date} to {today_str}")
            return 0

        print(f"[BACKFILL] Fetched {len(all_trades)} total historical trades from Dhan ({page + 1} pages)")

        # De-duplicate by orderId + transactionType to avoid double-counting
        seen = set()
        unique_trades = []
        for t in all_trades:
            uid = f"{t.get('orderId', '')}_{t.get('transactionType', '')}_{t.get('tradedPrice', '')}"
            if uid not in seen:
                seen.add(uid)
                unique_trades.append(t)
        if len(unique_trades) < len(all_trades):
            print(f"[BACKFILL] De-duplicated: {len(all_trades)} → {len(unique_trades)} unique trades")
        all_trades = unique_trades

        # Group trades by date using exchangeTime
        trades_by_date = {}
        for t in all_trades:
            raw_time = t.get("exchangeTime") or t.get("createTime") or t.get("updateTime") or ""
            date_str = str(raw_time)[:10]
            if not date_str or len(date_str) < 10:
                continue  # Skip invalid dates
            # Skip today only if not forcing — today is handled by live auto-save
            if date_str == today_str and not force:
                continue
            # When not forcing, skip dates we already have
            if not force and date_str in existing_dates:
                continue
            if date_str not in trades_by_date:
                trades_by_date[date_str] = []
            trades_by_date[date_str].append(t)

        # Compute P&L for each date
        new_dates = 0
        for date_str, day_trades in sorted(trades_by_date.items()):
            groups = {}
            for t in day_trades:
                key = t.get("securityId") or t.get("tradingSymbol") or "unknown"
                if key not in groups:
                    # Prefer customSymbol (readable) over tradingSymbol (often empty for options)
                    sym = t.get("customSymbol") or t.get("tradingSymbol") or str(key)
                    groups[key] = {"buys": [], "sells": [], "symbol": sym}
                if t.get("transactionType") == "BUY":
                    groups[key]["buys"].append(t)
                elif t.get("transactionType") == "SELL":
                    groups[key]["sells"].append(t)

            total_pnl = 0
            total_charges = 0
            trade_count = 0
            trade_legs = len(day_trades)  # Total individual trade legs (matches Dhan's count)
            wins = 0
            details = []
            for g in groups.values():
                buy_qty = sum(float(t.get("tradedQuantity", 0)) for t in g["buys"])
                sell_qty = sum(float(t.get("tradedQuantity", 0)) for t in g["sells"])
                buy_val = sum(float(t.get("tradedPrice", 0)) * float(t.get("tradedQuantity", 0)) for t in g["buys"])
                sell_val = sum(float(t.get("tradedPrice", 0)) * float(t.get("tradedQuantity", 0)) for t in g["sells"])
                # Sum charges from all legs (buys + sells)
                leg_charges = 0
                for t in g["buys"] + g["sells"]:
                    for key_c in (
                        "sebiTax",
                        "stt",
                        "brokerageCharges",
                        "serviceTax",
                        "exchangeTransactionCharges",
                        "stampDuty",
                    ):
                        leg_charges += float(t.get(key_c, 0) or 0)
                matched = min(buy_qty, sell_qty)
                if matched > 0 and buy_qty > 0 and sell_qty > 0:
                    buy_avg = buy_val / buy_qty
                    sell_avg = sell_val / sell_qty
                    pnl = round((sell_avg - buy_avg) * matched, 2)
                    total_pnl += pnl
                    total_charges += leg_charges
                    trade_count += 1
                    if pnl > 0:
                        wins += 1
                    details.append(
                        {
                            "symbol": g["symbol"],
                            "pnl": pnl,
                            "qty": int(matched),
                            "buy_avg": round(buy_avg, 2),
                            "sell_avg": round(sell_avg, 2),
                            "charges": round(leg_charges, 2),
                        }
                    )

            if trade_count > 0:
                history[date_str] = {
                    "pnl": round(total_pnl, 2),
                    "net_pnl": round(total_pnl - total_charges, 2),
                    "charges": round(total_charges, 2),
                    "trades": trade_count,
                    "trade_legs": trade_legs,
                    "wins": wins,
                    "mode": "real",
                    "details": details,
                }
                new_dates += 1

        if new_dates > 0:
            _save_trade_history(history)
            print(f"[BACKFILL] {'Refreshed' if force else 'Added'} {new_dates} dates in trade_history.json")
        else:
            print("[BACKFILL] No new dates to add (all existing)")

        return new_dates
    except Exception as e:
        print(f"[BACKFILL] Error: {e}")
        import traceback

        traceback.print_exc()
        return 0


@app.get("/api/portfolio/backfill")
async def portfolio_backfill(force: bool = False):
    """Manually trigger historical trade backfill from Dhan.

    Args:
        force: If true, re-fetch ALL trades and overwrite existing data.
    """
    try:
        count = _backfill_trade_history("2024-01-01", force=force)
        return {"status": "success", "new_dates": count, "force": force}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/backfill/status")
async def backfill_status():
    """Return current background backfill state (polled by frontend)."""
    return _backfill_state


@app.get("/api/portfolio/history")
async def get_portfolio_history():
    """Return combined historical P&L from real trades + paper runs for monthly/yearly charts."""
    try:
        daily = {}  # { "YYYY-MM-DD": { real_pnl, paper_pnl, real_trades, paper_trades, real_wins, paper_wins } }

        # 1) Real trade history from trade_history.json
        real_history = _load_trade_history()
        for date_str, entry in real_history.items():
            if date_str not in daily:
                daily[date_str] = {
                    "real_pnl": 0,
                    "real_net_pnl": 0,
                    "real_charges": 0,
                    "paper_pnl": 0,
                    "real_trades": 0,
                    "real_trade_legs": 0,
                    "paper_trades": 0,
                    "real_wins": 0,
                    "paper_wins": 0,
                }
            daily[date_str]["real_pnl"] = entry.get("pnl", 0)
            daily[date_str]["real_net_pnl"] = entry.get("net_pnl", entry.get("pnl", 0))
            daily[date_str]["real_charges"] = entry.get("charges", 0)
            daily[date_str]["real_trades"] = entry.get("trades", 0)
            daily[date_str]["real_trade_legs"] = entry.get("trade_legs", entry.get("trades", 0))
            daily[date_str]["real_wins"] = entry.get("wins", 0)

        # 2) Paper runs from runs.json
        runs = _load_runs()
        for r in runs:
            if r.get("mode") != "paper":
                continue
            # Extract date from the paper run
            run_date = None
            started = r.get("started_at", r.get("created_at", ""))
            if started:
                run_date = str(started)[:10]  # YYYY-MM-DD

            # Also extract per-trade dates for more granular breakdown
            trades = r.get("trades", [])
            if trades:
                # Group paper trades by exit_time date
                paper_by_date = {}
                for t in trades:
                    t_date = str(t.get("exit_time", t.get("entry_time", "")))[:10]
                    if not t_date or len(t_date) < 10:
                        t_date = run_date or ""
                    if not t_date:
                        continue
                    if t_date not in paper_by_date:
                        paper_by_date[t_date] = {"pnl": 0, "count": 0, "wins": 0}
                    pnl = t.get("pnl", 0)
                    paper_by_date[t_date]["pnl"] += pnl
                    paper_by_date[t_date]["count"] += 1
                    if pnl > 0:
                        paper_by_date[t_date]["wins"] += 1

                for d, data in paper_by_date.items():
                    if d not in daily:
                        daily[d] = {
                            "real_pnl": 0,
                            "real_net_pnl": 0,
                            "real_charges": 0,
                            "paper_pnl": 0,
                            "real_trades": 0,
                            "paper_trades": 0,
                            "real_wins": 0,
                            "paper_wins": 0,
                        }
                    daily[d]["paper_pnl"] += round(data["pnl"], 2)
                    daily[d]["paper_trades"] += data["count"]
                    daily[d]["paper_wins"] += data["wins"]
            elif run_date:
                # No individual trades, use run-level P&L
                if run_date not in daily:
                    daily[run_date] = {
                        "real_pnl": 0,
                        "real_net_pnl": 0,
                        "real_charges": 0,
                        "paper_pnl": 0,
                        "real_trades": 0,
                        "paper_trades": 0,
                        "real_wins": 0,
                        "paper_wins": 0,
                    }
                daily[run_date]["paper_pnl"] += r.get("total_pnl", 0)
                daily[run_date]["paper_trades"] += r.get("trade_count", 0)
                stats = r.get("stats", {})
                daily[run_date]["paper_wins"] += stats.get("winning_trades", 0)

        # Build monthly and yearly aggregates
        monthly = {}
        yearly = {}
        for date_str, d in daily.items():
            ym = date_str[:7]
            y = date_str[:4]
            if ym not in monthly:
                monthly[ym] = {
                    "real_pnl": 0,
                    "real_net_pnl": 0,
                    "real_charges": 0,
                    "paper_pnl": 0,
                    "total_pnl": 0,
                    "trades": 0,
                    "wins": 0,
                }
            monthly[ym]["real_pnl"] += d["real_pnl"]
            monthly[ym]["real_net_pnl"] += d.get("real_net_pnl", d["real_pnl"])
            monthly[ym]["real_charges"] += d.get("real_charges", 0)
            monthly[ym]["paper_pnl"] += d["paper_pnl"]
            monthly[ym]["total_pnl"] += d["real_pnl"] + d["paper_pnl"]
            monthly[ym]["trades"] += d["real_trades"] + d["paper_trades"]
            monthly[ym]["wins"] += d["real_wins"] + d["paper_wins"]

            if y not in yearly:
                yearly[y] = {
                    "real_pnl": 0,
                    "real_net_pnl": 0,
                    "real_charges": 0,
                    "paper_pnl": 0,
                    "total_pnl": 0,
                    "trades": 0,
                    "wins": 0,
                }
            yearly[y]["real_pnl"] += d["real_pnl"]
            yearly[y]["real_net_pnl"] += d.get("real_net_pnl", d["real_pnl"])
            yearly[y]["real_charges"] += d.get("real_charges", 0)
            yearly[y]["paper_pnl"] += d["paper_pnl"]
            yearly[y]["total_pnl"] += d["real_pnl"] + d["paper_pnl"]
            yearly[y]["trades"] += d["real_trades"] + d["paper_trades"]
            yearly[y]["wins"] += d["real_wins"] + d["paper_wins"]

        # Round all values
        for m in monthly.values():
            for k in ["real_pnl", "real_net_pnl", "real_charges", "paper_pnl", "total_pnl"]:
                m[k] = round(m[k], 2)
        for y in yearly.values():
            for k in ["real_pnl", "real_net_pnl", "real_charges", "paper_pnl", "total_pnl"]:
                y[k] = round(y[k], 2)

        return {"status": "success", "daily": daily, "monthly": monthly, "yearly": yearly}
    except Exception as e:
        print(f"[PORTFOLIO] History error: {e}")
        return {"status": "error", "message": str(e), "daily": {}, "monthly": {}, "yearly": {}}


@app.post("/api/broker/connect")
async def connect_broker():
    """Establish and validate broker connection"""
    try:
        # Check if credentials are configured (not default placeholders)
        if config.DHAN_CLIENT_ID == "YOUR_CLIENT_ID_HERE" or config.DHAN_ACCESS_TOKEN == "YOUR_ACCESS_TOKEN_HERE":
            return {
                "status": "not_configured",
                "broker": "Dhan",
                "message": "Dhan API credentials not configured. Please add them to .env file.",
            }

        # Test connection by attempting to fetch account funds
        funds = dhan.get_funds()

        if funds and isinstance(funds, dict):
            # Successfully connected and validated
            return {
                "status": "connected",
                "broker": "Dhan",
                "message": "Successfully connected to Dhan broker",
                "available_balance": funds.get("availabelBalance", 0),
                "client_id": config.DHAN_CLIENT_ID,
            }
        else:
            # Connection made but no valid data
            return {"status": "error", "broker": "Dhan", "message": "Broker returned empty or invalid response"}

    except Exception as e:
        error_msg = str(e)
        alerter.alert("Broker Connect Failed", f"Error: {error_msg[:200]}", level="warn")

        # Provide specific error messages based on error type
        if "401" in error_msg or "Unauthorized" in error_msg:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Invalid API credentials. Please check your Client ID and Access Token.",
            }
        elif "403" in error_msg or "Forbidden" in error_msg:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Access forbidden. Your API token may have expired or lacks permissions.",
            }
        elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Connection timeout. Please check your internet connection.",
            }
        elif "connection" in error_msg.lower():
            return {"status": "error", "broker": "Dhan", "message": "Network error. Unable to reach Dhan API servers."}
        else:
            return {"status": "error", "broker": "Dhan", "message": f"Connection failed: {error_msg[:100]}"}


# ── Instrument Mapping ────────────────────────────────────────────
# Maps frontend values to Dhan API params
# IMPORTANT: Dhan security IDs for indices are DIFFERENT from scrip IDs
# Use Dhan's scrip master CSV to find correct security IDs
INSTRUMENT_MAP = {
    # Indices — Dhan security IDs (from Dhan scrip master)
    "26000": {"name": "NIFTY 50", "dhan_id": "13", "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26009": {"name": "BANK NIFTY", "dhan_id": "25", "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "1": {
        "name": "SENSEX",
        "dhan_id": "51",
        "dhan_seg": "IDX_I",
        "dhan_type": "INDEX",
    },  # BSE SENSEX: Try ID 51 for BSE
    "26017": {"name": "NIFTY FIN SVC", "dhan_id": "27", "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26037": {"name": "NIFTY MIDCAP", "dhan_id": "49", "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26074": {"name": "NIFTY NEXT 50", "dhan_id": "26", "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26013": {"name": "NIFTY IT", "dhan_id": "30", "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    # Stocks — Dhan NSE security IDs
    "RELIANCE": {"name": "Reliance", "dhan_id": "2885", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "TCS": {"name": "TCS", "dhan_id": "11536", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "HDFCBANK": {"name": "HDFC Bank", "dhan_id": "1333", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "INFY": {"name": "Infosys", "dhan_id": "1594", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ICICIBANK": {"name": "ICICI Bank", "dhan_id": "4963", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "HINDUNILVR": {"name": "HUL", "dhan_id": "1394", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ITC": {"name": "ITC", "dhan_id": "1660", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "SBIN": {"name": "SBI", "dhan_id": "3045", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "BHARTIARTL": {"name": "Bharti Airtel", "dhan_id": "10604", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "BAJFINANCE": {"name": "Bajaj Finance", "dhan_id": "317", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "KOTAKBANK": {"name": "Kotak Bank", "dhan_id": "1922", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "LT": {"name": "L&T", "dhan_id": "11483", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "HCLTECH": {"name": "HCL Tech", "dhan_id": "7229", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ASIANPAINT": {"name": "Asian Paints", "dhan_id": "236", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "AXISBANK": {"name": "Axis Bank", "dhan_id": "5900", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "MARUTI": {"name": "Maruti", "dhan_id": "10999", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "SUNPHARMA": {"name": "Sun Pharma", "dhan_id": "3351", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "TITAN": {"name": "Titan", "dhan_id": "3506", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ULTRACEMCO": {"name": "UltraTech", "dhan_id": "11532", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "BAJAJFINSV": {"name": "Bajaj Finserv", "dhan_id": "16675", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "WIPRO": {"name": "Wipro", "dhan_id": "3787", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "NESTLEIND": {"name": "Nestle", "dhan_id": "17963", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "TATAMOTORS": {"name": "Tata Motors", "dhan_id": "3456", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "M_M": {"name": "M&M", "dhan_id": "2031", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "POWERGRID": {"name": "Power Grid", "dhan_id": "14977", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
}


# ── Data Fetch (Dhan only — variable timeframe via chunking) ──────────
INTRADAY_MAX_DAYS = 750  # Dhan intraday API returns ~2 years max; 750 days threshold (~2y + margin)


def _fetch_data(
    instrument: str, from_date: str, to_date: str, segment: str = "indices", candle_interval: str = "5"
) -> pd.DataFrame:
    """
    Fetches OHLCV candles from Dhan API at specified interval.
    - For date ranges ≤ ~2 years: fetches intraday candles in 28-day chunks.
    - For date ranges > ~2 years: automatically falls back to DAILY candles
      (Dhan historical API supports 10+ years of daily data).
    """
    inst_info = INSTRUMENT_MAP.get(instrument)
    if not inst_info:
        raise Exception(f"Unknown instrument: {instrument}. Not found in instrument map.")

    from datetime import datetime as dt
    from datetime import timedelta

    from_dt = dt.strptime(from_date, "%Y-%m-%d")
    to_dt = dt.strptime(to_date, "%Y-%m-%d")
    day_span = (to_dt - from_dt).days

    # Auto-detect: if range > ~2 years, use daily candles (Dhan intraday limit)
    use_daily = day_span > INTRADAY_MAX_DAYS
    effective_interval = "D" if use_daily else str(candle_interval)

    if use_daily:
        print(
            f"[DATA] ⚠️  Date range is {day_span} days (>{INTRADAY_MAX_DAYS}d). "
            f"Auto-switching to DAILY candles for full coverage."
        )

    print(
        f"[DATA] Instrument={instrument} ({inst_info['name']}), DhanID={inst_info['dhan_id']}, "
        f"Segment={inst_info['dhan_seg']}, Interval={'Daily' if use_daily else candle_interval + 'm'}, "
        f"From={from_date}, To={to_date}, Span={day_span}d"
    )

    if use_daily:
        # Daily candles — single request, no chunking needed
        try:
            df = dhan.get_historical_data(
                security_id=inst_info["dhan_id"],
                exchange_segment=inst_info["dhan_seg"],
                instrument_type=inst_info["dhan_type"],
                from_date=from_date,
                to_date=to_date,
                candle_type="D",
            )
            if df is not None and not df.empty:
                df = df[~df.index.duplicated(keep="first")]
                print(f"[DATA] ✅ Total: {len(df)} daily candles, {df.index[0]} → {df.index[-1]}")
                return df
        except Exception as e:
            raise Exception(f"Daily data fetch failed: {str(e)}")
        raise Exception(f"No daily data from Dhan for {inst_info['name']}.")

    # Intraday candles — chunk into 28-day windows
    # Dhan rate limit: ~10 requests/second. We add delay + retry on 429.
    import time as _time

    CHUNK_DAYS = 28
    RATE_LIMIT_DELAY = 0.5  # seconds between API calls
    MAX_RETRIES = 3  # retry on 429 rate-limit errors
    all_dfs = []
    chunk_start = from_dt
    chunk_num = 0
    last_error = None

    while chunk_start < to_dt:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), to_dt)
        chunk_num += 1

        cs = chunk_start.strftime("%Y-%m-%d")
        ce = chunk_end.strftime("%Y-%m-%d")

        print(f"[DATA] Chunk {chunk_num}: {cs} → {ce}")

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                df_chunk = dhan.get_historical_data(
                    security_id=inst_info["dhan_id"],
                    exchange_segment=inst_info["dhan_seg"],
                    instrument_type=inst_info["dhan_type"],
                    from_date=cs,
                    to_date=ce,
                    candle_type=effective_interval,
                )
                if df_chunk is not None and not df_chunk.empty:
                    all_dfs.append(df_chunk)
                    print(f"[DATA]   → {len(df_chunk)} candles")
                else:
                    print("[DATA]   → 0 candles (empty or None)")
                success = True
                break
            except Exception as e:
                last_error = str(e)
                if "429" in str(e) or "Rate_Limit" in str(e) or "DH-904" in str(e):
                    wait = RATE_LIMIT_DELAY * (2**attempt)  # exponential backoff: 1s, 2s, 4s
                    print(f"[DATA]   → Rate limited (attempt {attempt}/{MAX_RETRIES}), waiting {wait:.1f}s...")
                    _time.sleep(wait)
                else:
                    print(f"[DATA]   → Error: {last_error}")
                    break  # non-rate-limit error, skip this chunk

        if not success and attempt == MAX_RETRIES:
            print(f"[DATA]   → Failed after {MAX_RETRIES} retries")

        # Throttle between chunks to avoid rate limiting
        _time.sleep(RATE_LIMIT_DELAY)

        chunk_start = chunk_end + timedelta(days=1)

    if not all_dfs:
        error_detail = f"No intraday data from Dhan for {inst_info['name']}. Check API subscription and date range."
        if last_error:
            error_detail += f" Last error: {last_error}"
        raise Exception(error_detail)

    df = pd.concat(all_dfs).sort_index()
    # Remove duplicates (overlapping chunk boundaries)
    df = df[~df.index.duplicated(keep="first")]

    print(
        f"[DATA] ✅ Total: {len(df)} {candle_interval}-min candles across {chunk_num} chunks, "
        f"{df.index[0]} → {df.index[-1]}"
    )
    return df


# ── Backtest ──────────────────────────────────────────────────────
@app.post("/api/backtest")
async def api_run_backtest(payload: StrategyPayload):
    try:
        from_date = payload.from_date or config.DEFAULT_FROM
        to_date = payload.to_date or config.DEFAULT_TO

        # Extract timeframe from indicators (e.g., Supertrend_10_3_3m → 3)
        # Validate against supported Dhan intervals: 1, 5, 15, 25, 60, D
        candle_interval = "5"  # default
        valid_intervals = ["1", "5", "15", "25", "60", "D"]
        if payload.indicators:
            for ind in payload.indicators:
                if "_" in ind and ind.endswith("m"):
                    parts = ind.split("_")
                    for p in parts:
                        if p.endswith("m") and p[:-1].isdigit():
                            candidate = p[:-1]
                            if candidate in valid_intervals:
                                candle_interval = candidate
                            else:
                                print(f"[BACKTEST] Unsupported timeframe {candidate}m, using 5m")
                                candle_interval = "5"
                            break

        print(f"\n{'=' * 60}")
        print(f"[BACKTEST] Run: {payload.run_name}")
        print(f"[BACKTEST] Instrument: {payload.instrument}, Segment: {payload.segment}")
        print(f"[BACKTEST] Timeframe: {candle_interval}-minute candles")
        print(f"[BACKTEST] Indicators: {payload.indicators}")
        print(f"[BACKTEST] Entry conditions: {payload.entry_conditions}")
        print(f"[BACKTEST] Exit conditions: {payload.exit_conditions}")
        print(f"[BACKTEST] Legs: {payload.legs}")
        print("[BACKTEST] ⚠️  Using ESTIMATED option premiums (not historical data)")
        print(f"{'=' * 60}")

        # 1. Fetch data with segment-aware routing + fallback
        print(f"[BACKTEST] Fetching data from {from_date} to {to_date}...")
        try:
            df_raw = _fetch_data(
                instrument=payload.instrument,
                from_date=from_date,
                to_date=to_date,
                segment=payload.segment,
                candle_interval=candle_interval,
            )
        except Exception as fetch_err:
            error_msg = f"Data fetch failed: {str(fetch_err)}"
            print(f"[BACKTEST] {error_msg}")
            return {"status": "error", "message": error_msg}

        if df_raw is None or df_raw.empty:
            error_msg = "No data returned. Check credentials and date range."
            print(f"[BACKTEST] {error_msg}")
            return {"status": "error", "message": error_msg}

        print(f"[BACKTEST] Data: {len(df_raw)} candles, {df_raw.index[0]} → {df_raw.index[-1]}")

        # Warn if actual data range is shorter than requested, or if using daily candles
        data_range_warning = None
        from datetime import datetime as _dtw

        _from_dt = _dtw.strptime(from_date, "%Y-%m-%d")
        _to_dt = _dtw.strptime(to_date, "%Y-%m-%d")
        _day_span = (_to_dt - _from_dt).days
        if _day_span > INTRADAY_MAX_DAYS:
            data_range_warning = (
                f"📊 Date range is {_day_span} days — automatically using DAILY candles "
                f"for full {from_date} → {to_date} coverage. "
                f"(Dhan intraday API is limited to ~2 years. Daily candles go back 10+ years.)"
            )
            print(f"[BACKTEST] {data_range_warning}")
        else:
            actual_start = (
                str(df_raw.index[0].date()) if hasattr(df_raw.index[0], "date") else str(df_raw.index[0])[:10]
            )
            if actual_start > from_date:
                data_range_warning = (
                    f"⚠️ Data starts from {actual_start} (requested {from_date}). "
                    f"Some data may not be available for the requested period."
                )
                print(f"[BACKTEST] {data_range_warning}")

        # 2. Build strategy_config
        strategy_config = payload.model_dump()

        # 3. Run backtest
        print("[BACKTEST] Running backtest engine...")
        try:
            results = run_backtest(
                df_raw=df_raw,
                entry_conditions=payload.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
                exit_conditions=payload.exit_conditions or DEFAULT_EXIT_CONDITIONS,
                strategy_config=strategy_config,
            )
        except Exception as bt_err:
            error_msg = f"Backtest execution failed: {str(bt_err)}"
            print(f"[BACKTEST] {error_msg}")
            import traceback

            traceback.print_exc()
            return {"status": "error", "message": error_msg}

        print(f"[BACKTEST] Result: {results.get('status')}, Trades: {results.get('stats', {}).get('total_trades', 0)}")

        # Save the run
        if results.get("status") == "success":
            runs = _load_runs()
            # Use max ID to avoid duplicates after deletes
            max_id = max([r.get("id", 0) for r in runs], default=0)
            run_entry = {
                "id": max_id + 1,
                "mode": "backtest",
                "run_name": payload.run_name,
                "folder": payload.folder,
                "segment": payload.segment,
                "instrument": payload.instrument,
                "from_date": from_date,
                "to_date": to_date,
                "lots": payload.lots,
                "lot_size": payload.lot_size,
                "stoploss_pct": payload.stoploss_pct,
                "stoploss_rupees": getattr(payload, "stoploss_rupees", 0),
                "sl_type": getattr(payload, "sl_type", "pct"),
                "target_profit_pct": getattr(payload, "target_profit_pct", 0),
                "target_profit_rupees": getattr(payload, "target_profit_rupees", 0),
                "tp_type": getattr(payload, "tp_type", "pct"),
                "indicators": payload.indicators,
                "entry_conditions": payload.entry_conditions,
                "exit_conditions": payload.exit_conditions,
                "legs": payload.legs,
                "market_open": getattr(payload, "market_open", "09:15") or "09:15",
                "market_close": getattr(payload, "market_close", "15:25") or "15:25",
                "max_trades_per_day": getattr(payload, "max_trades_per_day", 1),
                "stats": results["stats"],
                "monthly": results.get("monthly", []),
                "day_of_week": results.get("day_of_week", []),
                "yearly": results.get("yearly", []),
                "trade_count": results["stats"]["total_trades"],
                "total_pnl": results["stats"]["total_pnl"],
                "created_at": str(datetime.now()),
            }
            # Store all trades (no need to trim)
            all_trades = results.get("trades", [])
            run_entry["trades"] = all_trades
            run_entry["equity"] = results.get("equity", [])
            runs.append(run_entry)
            _save_runs(runs)
            results["run_id"] = run_entry["id"]
            print(f"[BACKTEST] Saved as Run #{run_entry['id']}")

        if data_range_warning:
            results["data_range_warning"] = data_range_warning

        return results

    except Exception as e:
        import traceback

        error_msg = f"Backtest failed: {str(e)}"
        print(f"[BACKTEST] FATAL ERROR: {error_msg}")
        traceback.print_exc()
        return {"status": "error", "message": error_msg, "details": str(e)}


# ── Live Engine ───────────────────────────────────────────────────
@app.post("/api/live/start")
async def live_start(req: LiveStartRequest):
    """Start live auto-trading with full strategy configuration."""
    # Build strategy dict from the request
    strategy_dict = {}
    if req.strategy_config:
        strategy_dict = dict(req.strategy_config)
    else:
        strategy_dict = {
            "run_name": req.run_name or "Live Strategy",
            "instrument": req.instrument or "26000",
            "indicators": req.indicators or [],
            "max_trades_per_day": int(req.max_trades_per_day or 1),
            "market_open": req.market_open or "09:15",
            "market_close": req.market_close or "15:25",
            "legs": req.legs or [],
            "deploy_config": req.deploy_config or {},
            "max_daily_loss": float(req.max_daily_loss or 0),
            "lots": req.lots,
            "stoploss_pct": req.stoploss_pct,
            "stoploss_rupees": req.stoploss_rupees,
            "sl_type": req.sl_type,
            "target_profit_pct": req.target_profit_pct,
            "target_profit_rupees": req.target_profit_rupees,
            "tp_type": req.tp_type,
            "poll_interval": 10,
        }

    deploy_config = req.deploy_config or strategy_dict.get("deploy_config", {})

    # Generate run_id from strategy name
    run_id = strategy_dict.get("run_name", "live") or "live"

    # If an engine with same run_id exists, save its results before replacing
    old_engine = live_engines.get(run_id)
    if old_engine:
        try:
            old_status = old_engine.get_status()
            if old_engine.running:
                old_engine.stop()
                task = _live_tasks.pop(run_id, None)
                if task and not task.done():
                    task.cancel()
            _save_live_run_to_history(old_status)
        except Exception as e:
            print(f"[LIVE] Failed to save old engine {run_id}: {e}")
        live_engines.pop(run_id, None)

    # Create a new engine instance for this strategy
    engine = LiveEngine(dhan, run_id=run_id)
    engine.configure(
        strategy=strategy_dict,
        entry_conditions=req.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
        exit_conditions=req.exit_conditions or DEFAULT_EXIT_CONDITIONS,
        deploy_config=deploy_config,
    )

    # Inject WebSocket feed if available — starts WS + subscribes index
    if _market_feed and HAS_DHAN_FEED:
        instrument = strategy_dict.get("instrument", "26000")
        _market_feed.subscribe_index(instrument)
        if not _market_feed.is_running:
            _market_feed.start()
        engine.set_feed(_market_feed)

    # Set running IMMEDIATELY so UI never sees a stale "stopped" state
    engine.running = True
    engine.event_log = []
    engine.positions = []
    engine.closed_trades = []
    engine.in_trade = False
    engine.trades_today = 0

    async def broadcast(event: dict):
        for ws in ws_clients.copy():
            try:
                await ws.send_json({"source": "live", "run_id": run_id, **event})
            except Exception:
                if ws in ws_clients:
                    ws_clients.remove(ws)

    # Store engine and start task
    live_engines[run_id] = engine
    _live_tasks[run_id] = asyncio.create_task(engine.start(callback=broadcast))

    # Persist config + state immediately so it survives server restarts
    engine.session_date = date.today()
    engine._save_state()

    return {"status": "started", "run_id": run_id, "message": "Auto trading started with REAL orders"}


@app.post("/api/live/stop")
async def live_stop(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    run_id = body.get("run_id", "")

    # If no run_id, stop the first (or only) running engine
    if not run_id:
        running = [rid for rid, e in live_engines.items() if e.running]
        if running:
            run_id = running[0]
        else:
            return {"status": "not_running"}

    engine = live_engines.get(run_id)
    if not engine:
        return {"status": "not_found", "run_id": run_id}

    # Capture results BEFORE stopping
    status_before = engine.get_status()

    engine.stop()
    task = _live_tasks.pop(run_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    live_engines.pop(run_id, None)

    # Delete state file so engine doesn't auto-restore on next startup
    engine._delete_state_file()

    # Persist live run to runs.json (same as paper)
    _save_live_run_to_history(status_before)

    return {"status": "stopped", "run_id": run_id}


@app.get("/api/live/status")
async def live_status(run_id: str = ""):
    """Get live engine status. If run_id empty, returns first running engine."""
    if run_id and run_id in live_engines:
        return live_engines[run_id].get_status()
    # Return first running engine's status
    for rid, engine in live_engines.items():
        if engine.running:
            return engine.get_status()
    # Nothing running — return idle status
    return {
        "running": False,
        "run_id": "",
        "mode": "auto",
        "in_trade": False,
        "positions": [],
        "closed_trades": [],
        "total_pnl": 0,
        "trades_today": 0,
        "strategy_name": "",
        "instrument": "",
        "current_candle": {},
        "current_indicators": {},
        "event_log": [],
    }


@app.get("/api/live/debug")
async def live_debug(run_id: str = ""):
    """Deep diagnostic of live engine state — call when trades aren't triggering."""
    engine = None
    if run_id and run_id in live_engines:
        engine = live_engines[run_id]
    else:
        for e in live_engines.values():
            if e.running:
                engine = e
                break
    if not engine:
        return {"error": "No live engine running", "engines": list(live_engines.keys())}
    return engine.debug_engine_state()


@app.get("/api/live/trades/csv")
async def export_live_trades_csv(run_id: str = ""):
    """Export live auto-trading trades to CSV"""
    import csv as csv_mod
    import io

    engine = live_engines.get(run_id) if run_id else None
    if not engine:
        # Find first engine with trades
        for e in live_engines.values():
            if e.closed_trades:
                engine = e
                break
    if not engine or not engine.closed_trades:
        raise HTTPException(status_code=404, detail="No live trades available")
    output = io.StringIO()
    fields = [
        "id",
        "leg_num",
        "transaction_type",
        "option_type",
        "strike",
        "entry_time",
        "exit_time",
        "entry_premium",
        "exit_premium",
        "lots",
        "lot_size",
        "pnl",
        "exit_reason",
        "entry_order_id",
        "exit_order_id",
    ]
    writer = csv_mod.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for t in engine.closed_trades:
        row = {k: (str(v) if k in ("entry_time", "exit_time") else v) for k, v in t.items() if k in fields}
        writer.writerow(row)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=live_trades_{datetime.now().strftime('%Y%m%d')}.csv"},
    )


# ── Paper Trading (Real Market Data) ──────────────────────────────
@app.post("/api/paper/start")
async def paper_start(payload: StrategyPayload):
    """Start paper trading with real live market data"""
    _crash_log = os.path.join(_HERE, "crash.log")
    with open(_crash_log, "a") as _f:
        _f.write(f"\n[PAPER] paper_start ENTERED at {datetime.now()}\n")
        _f.write(f"[PAPER] payload.instrument={payload.instrument}, run_name={payload.run_name}\n")
    try:
        return await _paper_start_impl(payload)
    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        msg = f"[PAPER] paper_start crashed: {e}\n{tb}"
        print(msg, flush=True)
        _logger.error("[PAPER] paper_start crashed: %s\n%s", e, tb)
        with open(_crash_log, "a") as _f:
            _f.write(f"\n{'=' * 60}\n{msg}\n")
        raise


async def _paper_start_impl(payload: StrategyPayload):
    # Configure strategy — pass ALL fields needed for SL/TP/strike logic
    strategy_dict = {
        "run_name": payload.run_name,
        "instrument": payload.instrument,
        "indicators": payload.indicators or [],
        "max_trades_per_day": int(payload.max_trades_per_day or 1),
        "market_open": payload.market_open or "09:15",
        "market_close": payload.market_close or "15:25",
        "legs": payload.legs or [],
        "deploy_config": payload.deploy_config or {},
        "poll_interval": 10,  # Check every 10 seconds
        # Strategy-level SL/TP
        "lots": payload.lots,
        "lot_size": payload.lot_size,
        "stoploss_pct": payload.stoploss_pct,
        "stoploss_rupees": payload.stoploss_rupees,
        "sl_type": payload.sl_type,
        "target_profit_pct": payload.target_profit_pct,
        "target_profit_rupees": payload.target_profit_rupees,
        "tp_type": payload.tp_type,
        "max_daily_loss": payload.max_daily_loss,
        "combined_sqoff_time": payload.combined_sqoff_time,
    }

    # Generate run_id from strategy name
    run_id = strategy_dict.get("run_name", "paper") or "paper"

    # If an engine with same run_id exists, save its results before replacing
    old_engine = paper_engines.get(run_id)
    if old_engine:
        try:
            old_status = old_engine.get_status()
            if old_engine.running:
                old_engine.stop()
                task = _paper_tasks.pop(run_id, None)
                if task and not task.done():
                    task.cancel()
            _save_paper_run_to_history(old_status)
        except Exception as e:
            print(f"[PAPER] Failed to save old engine {run_id}: {e}")
        paper_engines.pop(run_id, None)

    # Create a new engine instance for this strategy
    engine = PaperTradingEngine(dhan, run_id=run_id)
    engine.configure(
        strategy=strategy_dict,
        entry_conditions=payload.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
        exit_conditions=payload.exit_conditions or DEFAULT_EXIT_CONDITIONS,
    )

    # Inject WebSocket feed if available — starts WS + subscribes index
    if _market_feed and HAS_DHAN_FEED:
        instrument = strategy_dict.get("instrument", "26000")
        _market_feed.subscribe_index(instrument)
        if not _market_feed.is_running:
            _market_feed.start()
        engine.set_feed(_market_feed)

    # Set running IMMEDIATELY so UI never sees a stale "stopped" state
    engine.running = True
    engine.event_log = []
    engine.positions = []
    engine.closed_trades = []
    engine.in_trade = False
    engine.trades_today = 0

    # Broadcast updates to WebSocket clients
    async def broadcast(event: dict):
        for ws in ws_clients.copy():
            try:
                await ws.send_json({"source": "paper", "run_id": run_id, **event})
            except Exception:
                if ws in ws_clients:
                    ws_clients.remove(ws)

    # Store engine and start task
    paper_engines[run_id] = engine
    _paper_tasks[run_id] = asyncio.create_task(engine.start(callback=broadcast))

    return {"status": "started", "run_id": run_id, "message": "Paper trading started with LIVE market data"}


@app.post("/api/paper/stop")
async def paper_stop(request: Request):
    """Stop paper trading and persist results to runs.json"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    run_id = body.get("run_id", "")

    # If no run_id, stop the first (or only) running engine
    if not run_id:
        running = [rid for rid, e in paper_engines.items() if e.running]
        if running:
            run_id = running[0]
        else:
            return {"status": "not_running"}

    engine = paper_engines.get(run_id)
    if not engine:
        return {"status": "not_found", "run_id": run_id}

    # Capture results BEFORE stopping (stop() may close positions)
    status_before = engine.get_status()

    engine.stop()

    task = _paper_tasks.pop(run_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    paper_engines.pop(run_id, None)

    # Delete state file so engine doesn't auto-restore on next startup
    engine._delete_state_file()

    # Save paper run to runs.json so it persists across restarts
    _save_paper_run_to_history(status_before)

    return {"status": "stopped", "run_id": run_id}


def _save_paper_run_to_history(status: dict):
    """Save a completed paper trading run to runs.json for history."""
    try:
        closed = status.get("closed_trades", [])
        if not closed:
            print("[PAPER] No closed trades — skipping runs.json")
            return

        runs = _load_runs()
        max_id = max([r.get("id", 0) for r in runs], default=0)

        total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
        winners = [t for t in closed if t.get("pnl", 0) > 0]
        losers = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = round(len(winners) / len(closed) * 100, 2) if closed else 0

        paper_run = {
            "id": max_id + 1,
            "mode": "paper",
            "run_name": status.get("strategy_name", "Paper Run"),
            "instrument": status.get("instrument", ""),
            "status": "completed",
            "started_at": str(datetime.now()),
            "stopped_at": str(datetime.now()),
            "trade_count": len(closed),
            "total_pnl": total_pnl,
            "stats": {
                "total_trades": len(closed),
                "winning_trades": len(winners),
                "losing_trades": len(losers),
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "avg_profit": round(sum(t["pnl"] for t in winners) / len(winners), 2) if winners else 0,
                "avg_loss": round(sum(t["pnl"] for t in losers) / len(losers), 2) if losers else 0,
            },
            "trades": closed,
            "created_at": str(datetime.now()),
            # Strategy details for View modal
            **{
                k: v
                for k, v in (status.get("strategy") or {}).items()
                if k
                in (
                    "indicators",
                    "entry_conditions",
                    "exit_conditions",
                    "legs",
                    "lots",
                    "lot_size",
                    "stoploss_pct",
                    "stoploss_rupees",
                    "sl_type",
                    "target_profit_pct",
                    "target_profit_rupees",
                    "tp_type",
                    "market_open",
                    "market_close",
                    "folder",
                    "max_trades_per_day",
                )
            },
        }

        runs.append(paper_run)
        _save_runs(runs)
        print(f"[PAPER] Saved run #{paper_run['id']} to runs.json: {len(closed)} trades, P&L=₹{total_pnl}")
    except Exception as e:
        print(f"[PAPER] Failed to save run to history: {e}")


def _save_scalp_run_to_history(eng) -> None:
    """Persist a completed scalp session to runs.json so it appears on the Results page."""
    try:
        status = eng.get_status()
        closed = status.get("closed_trades", [])
        if not closed:
            print("[SCALP] No closed trades — skipping runs.json")
            return

        runs = _load_runs()
        max_id = max((r.get("id", 0) for r in runs), default=0)

        total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
        winners = [t for t in closed if t.get("pnl", 0) > 0]
        losers = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = round(len(winners) / len(closed) * 100, 2) if closed else 0

        # Derive a human-readable name from the underlyings traded
        underlyings = list(dict.fromkeys(t.get("underlying", "") for t in closed if t.get("underlying")))
        run_name = "Scalp — " + ", ".join(underlyings) if underlyings else "Scalp Session"

        scalp_run = {
            "id": max_id + 1,
            "mode": "scalp",
            "run_name": run_name,
            "instrument": underlyings[0] if underlyings else "",
            "status": "completed",
            "started_at": closed[-1].get("entry_time", str(datetime.now())),
            "stopped_at": str(datetime.now()),
            "trade_count": len(closed),
            "total_pnl": total_pnl,
            "stats": {
                "total_trades": len(closed),
                "winning_trades": len(winners),
                "losing_trades": len(losers),
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "avg_profit": round(sum(t["pnl"] for t in winners) / len(winners), 2) if winners else 0,
                "avg_loss": round(sum(t["pnl"] for t in losers) / len(losers), 2) if losers else 0,
            },
            "trades": closed,
            "created_at": str(datetime.now()),
        }

        runs.append(scalp_run)
        _save_runs(runs)
        print(f"[SCALP] Saved run #{scalp_run['id']} to runs.json: {len(closed)} trades, P&L=₹{total_pnl}")
    except Exception as e:
        print(f"[SCALP] Failed to save run to history: {e}")


def _save_live_run_to_history(status: dict):
    """Save a completed live (auto) trading run to runs.json for history."""
    try:
        closed = status.get("closed_trades", [])
        if not closed:
            print("[LIVE] No closed trades — skipping runs.json")
            return

        runs = _load_runs()
        max_id = max([r.get("id", 0) for r in runs], default=0)

        total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
        winners = [t for t in closed if t.get("pnl", 0) > 0]
        losers = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = round(len(winners) / len(closed) * 100, 2) if closed else 0

        live_run = {
            "id": max_id + 1,
            "mode": "live",
            "run_name": status.get("strategy_name", "Live Run"),
            "instrument": status.get("instrument", ""),
            "status": "completed",
            "started_at": str(datetime.now()),
            "stopped_at": str(datetime.now()),
            "trade_count": len(closed),
            "total_pnl": total_pnl,
            "stats": {
                "total_trades": len(closed),
                "winning_trades": len(winners),
                "losing_trades": len(losers),
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "avg_profit": round(sum(t["pnl"] for t in winners) / len(winners), 2) if winners else 0,
                "avg_loss": round(sum(t["pnl"] for t in losers) / len(losers), 2) if losers else 0,
            },
            "trades": closed,
            "created_at": str(datetime.now()),
            # Strategy details for View modal
            **{
                k: v
                for k, v in (status.get("strategy") or {}).items()
                if k
                in (
                    "indicators",
                    "entry_conditions",
                    "exit_conditions",
                    "legs",
                    "lots",
                    "lot_size",
                    "stoploss_pct",
                    "stoploss_rupees",
                    "sl_type",
                    "target_profit_pct",
                    "target_profit_rupees",
                    "tp_type",
                    "market_open",
                    "market_close",
                    "folder",
                    "max_trades_per_day",
                )
            },
        }

        runs.append(live_run)
        _save_runs(runs)
        print(f"[LIVE] Saved run #{live_run['id']} to runs.json: {len(closed)} trades, P&L=₹{total_pnl}")
    except Exception as e:
        print(f"[LIVE] Failed to save run to history: {e}")


@app.get("/api/paper/status")
async def paper_status(run_id: str = ""):
    """Get paper trading status. If run_id empty, returns first running engine."""
    if run_id and run_id in paper_engines:
        return paper_engines[run_id].get_status()

    # Return first running engine's status
    for rid, engine in paper_engines.items():
        if engine.running:
            return engine.get_status()

    # No running engines — check for last saved paper run from history
    status = {
        "running": False,
        "run_id": "",
        "mode": "paper",
        "in_trade": False,
        "positions": [],
        "closed_trades": [],
        "total_pnl": 0,
        "trades_today": 0,
        "strategy_name": "",
        "instrument": "",
        "current_candle": {},
        "current_indicators": {},
        "event_log": [],
    }
    try:
        runs = _load_runs()
        paper_runs = [r for r in runs if r.get("mode") == "paper"]
        if paper_runs:
            last = paper_runs[-1]
            trades = last.get("trades", [])
            status["strategy_name"] = last.get("run_name", "Last Paper Run")
            status["instrument"] = last.get("instrument", "")
            status["closed_trades"] = trades
            status["trades_today"] = len(trades)
            status["total_pnl"] = last.get("total_pnl", 0)
            status["_from_history"] = True
    except Exception:
        pass

    return status


# ── Combined Engines Status (Multi-Strategy Monitor) ─────────────
@app.get("/api/engines/all")
async def engines_all():
    """Return status of ALL running engines (paper + live) for multi-strategy Live page."""
    engines = []

    # Add all paper engines
    for run_id, engine in paper_engines.items():
        if engine.running:
            st = engine.get_status()
            st["run_id"] = run_id
            st["mode"] = "paper"
            engines.append(st)

    # Add all live engines
    for run_id, engine in live_engines.items():
        if engine.running:
            st = engine.get_status()
            st["run_id"] = run_id
            st["mode"] = "auto"
            engines.append(st)

    # If nothing running, show last paper run from history
    if not engines:
        try:
            runs = _load_runs()
            paper_runs = [r for r in runs if r.get("mode") == "paper"]
            if paper_runs:
                last = paper_runs[-1]
                trades = last.get("trades", [])
                engines.append(
                    {
                        "running": False,
                        "run_id": "",
                        "mode": "paper",
                        "in_trade": False,
                        "positions": [],
                        "closed_trades": trades,
                        "total_pnl": last.get("total_pnl", 0),
                        "trades_today": len(trades),
                        "strategy_name": last.get("run_name", "Last Paper Run"),
                        "instrument": last.get("instrument", ""),
                        "current_candle": {},
                        "current_indicators": {},
                        "event_log": [],
                        "_from_history": True,
                    }
                )
        except Exception:
            pass

    return {"engines": engines, "count": len(engines)}


# ── WebSocket ─────────────────────────────────────────────────────


# Event-driven signal: set whenever scalp state changes (entry/exit/modify)
_scalp_ws_event: asyncio.Event | None = None


def _get_scalp_ws_event() -> asyncio.Event:
    global _scalp_ws_event
    if _scalp_ws_event is None:
        _scalp_ws_event = asyncio.Event()
    return _scalp_ws_event


def _notify_scalp_ws():
    """Signal all WS clients to push scalp update immediately."""
    evt = _get_scalp_ws_event()
    evt.set()


def _ws_serialize(payload: dict) -> bytes:
    """Serialize WS payload using orjson (fast) with stdlib json fallback."""
    if _orjson is not None:
        return _orjson.dumps(payload)
    return json.dumps(payload).encode("utf-8")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Authenticate WebSocket via session cookie
    token = ws.cookies.get("algoforge_session", "")
    if not _validate_session(token):
        await ws.close(code=4001, reason="Unauthorized")
        return
    await ws.accept()
    ws_clients.append(ws)

    scalp_evt = _get_scalp_ws_event()
    engine_tick = 0  # counter: send full engine status every 20 cycles (~5s)

    try:
        while True:
            # Wait for either: scalp event fires OR 250ms timeout
            try:
                await asyncio.wait_for(scalp_evt.wait(), timeout=0.25)
                scalp_evt.clear()
            except asyncio.TimeoutError:
                pass

            # Scalp status — every cycle (250ms)
            scalp_data = None
            if _HAS_SCALP and _scalp_engine is not None:
                try:
                    scalp_data = _scalp_engine.get_status()
                except Exception:
                    pass

            payload = {"type": "status", "_ts": time.time()}

            if scalp_data is not None:
                payload["scalp"] = scalp_data

            # Engine status — every ~5s (20 × 250ms) to avoid waste
            engine_tick += 1
            if engine_tick >= 20:
                engine_tick = 0
                paper_sts = {rid: e.get_status() for rid, e in paper_engines.items()}
                live_sts = {rid: e.get_status() for rid, e in live_engines.items()}
                payload["paper_engines"] = paper_sts
                payload["live_engines"] = live_sts
                payload["paper_running"] = any(s.get("running") for s in paper_sts.values())
                payload["live_running"] = any(s.get("running") for s in live_sts.values())

            await ws.send_bytes(_ws_serialize(payload))
    except (WebSocketDisconnect, Exception):
        if ws in ws_clients:
            ws_clients.remove(ws)


# ── Orders / Positions / Funds ────────────────────────────────────
@app.post("/api/orders/place")
async def place_order(req: OrderRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit("place_order", ip, max_calls=3, window_sec=5)  # Max 3 orders per 5s per IP
    try:
        return dhan.place_order(
            security_id=req.security_id,
            exchange_segment=req.exchange_segment,
            transaction_type=req.transaction_type,
            quantity=req.quantity,
            order_type=req.order_type,
            product_type=req.product_type,
            price=req.price,
        )
    except Exception as e:
        alerter.alert(
            "Order Failed",
            f"Security: {req.security_id}\nType: {req.transaction_type}\nQty: {req.quantity}\nError: {e}",
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders")
async def get_orders():
    try:
        orders = dhan.get_order_book()
        return {"status": "success", "data": orders if isinstance(orders, list) else []}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100], "data": []}


@app.get("/api/positions")
async def get_positions():
    try:
        positions = dhan.get_positions()
        return {"status": "success", "data": positions if isinstance(positions, list) else []}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100], "data": []}


@app.get("/api/funds")
async def get_funds():
    try:
        return dhan.get_funds()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    try:
        return dhan.cancel_order(order_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Strategy CRUD ─────────────────────────────────────────────────
STRAT_FILE = "strategies.json"
RUNS_FILE = "runs.json"
TRADE_HISTORY_FILE = "trade_history.json"


def _load_trade_history():
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}


def _save_trade_history(d):
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(d, f, indent=2)


def _persist_daily_trades(trades: list):
    """Auto-save today's real Dhan trade P&L summary to trade_history.json.

    Only overwrites existing entry if the new data has MORE trade legs
    (i.e., more complete data from later in the day).
    """
    if not trades:
        return
    today_str = datetime.now().strftime("%Y-%m-%d")
    trade_legs = len(trades)  # Total individual order legs

    # Pair BUY/SELL per securityId to compute real P&L
    groups = {}
    for t in trades:
        key = t.get("securityId") or t.get("tradingSymbol") or "unknown"
        if key not in groups:
            sym = t.get("customSymbol") or t.get("tradingSymbol") or str(key)
            groups[key] = {"buys": [], "sells": [], "symbol": sym}
        if t.get("transactionType") == "BUY":
            groups[key]["buys"].append(t)
        elif t.get("transactionType") == "SELL":
            groups[key]["sells"].append(t)

    total_pnl = 0
    trade_count = 0
    wins = 0
    total_charges = 0
    trade_details = []
    for g in groups.values():
        buy_qty = sum(float(t.get("tradedQuantity", 0)) for t in g["buys"])
        sell_qty = sum(float(t.get("tradedQuantity", 0)) for t in g["sells"])
        buy_val = sum(float(t.get("tradedPrice", 0)) * float(t.get("tradedQuantity", 0)) for t in g["buys"])
        sell_val = sum(float(t.get("tradedPrice", 0)) * float(t.get("tradedQuantity", 0)) for t in g["sells"])
        matched = min(buy_qty, sell_qty)
        if matched > 0 and buy_qty > 0 and sell_qty > 0:
            buy_avg = buy_val / buy_qty
            sell_avg = sell_val / sell_qty
            pnl = round((sell_avg - buy_avg) * matched, 2)
            total_pnl += pnl
            trade_count += 1
            if pnl > 0:
                wins += 1
            # Sum charges from all legs (buys + sells)
            leg_charges = 0
            for t in g["buys"] + g["sells"]:
                for key_c in (
                    "sebiTax",
                    "stt",
                    "brokerageCharges",
                    "serviceTax",
                    "exchangeTransactionCharges",
                    "stampDuty",
                ):
                    leg_charges += float(t.get(key_c, 0) or 0)
            total_charges += leg_charges
            trade_details.append(
                {
                    "symbol": g["symbol"],
                    "pnl": pnl,
                    "qty": int(matched),
                    "buy_avg": round(buy_avg, 2),
                    "sell_avg": round(sell_avg, 2),
                    "charges": round(leg_charges, 2),
                }
            )

    if trade_count == 0:
        return

    history = _load_trade_history()

    # Only overwrite if new data has more trade legs (more complete)
    existing = history.get(today_str, {})
    existing_legs = existing.get("trade_legs", existing.get("trades", 0))
    if existing_legs > trade_legs:
        print(f"[TRADE_HISTORY] Skipping update — existing has {existing_legs} legs vs new {trade_legs}")
        return

    # Preserve charges from historical API if current has none
    if total_charges == 0 and existing.get("charges", 0) > 0:
        total_charges = existing["charges"]
        # Also preserve per-trade charges
        old_details_map = {d["symbol"]: d.get("charges", 0) for d in existing.get("details", [])}
        for detail in trade_details:
            if detail["charges"] == 0 and detail["symbol"] in old_details_map:
                detail["charges"] = old_details_map[detail["symbol"]]

    history[today_str] = {
        "pnl": round(total_pnl, 2),
        "net_pnl": round(total_pnl - total_charges, 2),
        "charges": round(total_charges, 2),
        "trades": trade_count,
        "trade_legs": trade_legs,
        "wins": wins,
        "mode": "real",
        "details": trade_details,
    }
    _save_trade_history(history)
    print(
        f"[TRADE_HISTORY] Saved {today_str}: {trade_count} trades ({trade_legs} legs), P&L=₹{total_pnl:.2f}, charges=₹{total_charges:.2f}"
    )


def _load():
    if os.path.exists(STRAT_FILE):
        try:
            with open(STRAT_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []


def _save(d):
    # Atomic write (tmp + rename) so a crash mid-write won't corrupt the file
    tmp = STRAT_FILE + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(d, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, STRAT_FILE)


def _load_runs():
    if os.path.exists(RUNS_FILE):
        try:
            with open(RUNS_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []


def _save_runs(d):
    # Atomic write with exclusive lock so concurrent workers don't interleave
    tmp = RUNS_FILE + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(d, f, indent=2, default=str)
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, RUNS_FILE)


@app.get("/api/strategies")
async def get_strategies():
    return _load()


@app.post("/api/strategies")
async def save_strategy(strategy: dict):
    strats = _load()
    max_id = max([s.get("id", 0) for s in strats], default=0)
    strategy.update(
        {
            "id": max_id + 1,
            "created_at": str(datetime.now()),
            "version": 1,
            "versions": [{"version": 1, "saved_at": str(datetime.now()), "changes": "Initial save"}],
        }
    )
    strats.append(strategy)
    _save(strats)
    return strategy


@app.delete("/api/strategies/{sid}")
async def delete_strategy(sid: int):
    _save([s for s in _load() if s.get("id") != sid])
    return {"deleted": sid}


@app.put("/api/strategies/{sid}")
async def update_strategy(sid: int, updates: dict):
    strats = _load()
    for s in strats:
        if s.get("id") == sid:
            # Track version history
            ver = s.get("version", 1) + 1
            versions = s.get("versions", [])
            versions.append(
                {
                    "version": ver,
                    "saved_at": str(datetime.now()),
                    "changes": updates.get("_change_note", f"Updated to v{ver}"),
                }
            )
            # Keep only last 20 versions
            if len(versions) > 20:
                versions = versions[-20:]
            updates.pop("_change_note", None)
            s.update(updates)
            s["version"] = ver
            s["versions"] = versions
            s["updated_at"] = str(datetime.now())
            break
    _save(strats)
    return {"updated": sid}


# ── Backtest Runs CRUD ────────────────────────────────────────────
@app.get("/api/runs")
async def get_runs():
    runs = _load_runs()
    result = []
    for r in runs:
        summary = {k: v for k, v in r.items() if k not in ("trades", "equity")}
        trades = r.get("trades") or []
        if trades:
            summary["first_entry_time"] = str(trades[0].get("entry_time") or "")
            summary["last_exit_time"] = str(trades[-1].get("exit_time") or "")
        result.append(summary)
    return result


@app.post("/api/runs/bulk-delete")
async def bulk_delete_runs(request: Request):
    body = await request.json()
    ids = body.get("ids", [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    id_set = set(ids)
    runs = _load_runs()
    _save_runs([r for r in runs if r.get("id") not in id_set])
    return {"deleted": len(id_set)}


@app.post("/api/runs/cleanup-empty")
async def cleanup_empty_runs():
    """Remove all 0-trade paper/live runs from runs.json."""
    runs = _load_runs()
    before = len(runs)
    cleaned = [
        r for r in runs if r.get("mode") == "backtest" or len(r.get("trades") or []) > 0 or r.get("trade_count", 0) > 0
    ]
    _save_runs(cleaned)
    removed = before - len(cleaned)
    return {"removed": removed, "remaining": len(cleaned)}


@app.get("/api/runs/{rid}")
async def get_run(rid: int):
    runs = _load_runs()
    for r in runs:
        if r.get("id") == rid:
            return r
    raise HTTPException(status_code=404, detail="Run not found")


@app.delete("/api/runs/{rid}")
async def delete_run(rid: int):
    runs = _load_runs()
    _save_runs([r for r in runs if r.get("id") != rid])
    return {"deleted": rid}


@app.get("/api/runs/{rid}/csv")
async def export_run_csv(rid: int):
    """Export backtest trades to CSV"""
    import csv
    import io

    runs = _load_runs()
    run = None
    for r in runs:
        if r.get("id") == rid:
            run = r
            break
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    trades = run.get("trades", [])
    if not trades:
        raise HTTPException(status_code=404, detail="No trades in this run")
    output = io.StringIO()
    fields = [
        "id",
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "pnl",
        "cumulative",
        "exit_reason",
        "option_type",
        "strike",
        "qty",
        "txn_type",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        writer.writerow(t)
    output.seek(0)
    name = run.get("run_name", f"run_{rid}").replace(" ", "_")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}_trades.csv"},
    )


# ── Scalp Trades CRUD (scalp_trades.json) ──────────────────────────
_SCALP_FILE = os.path.join(_HERE, "scalp_trades.json")


def _load_scalp_trades():
    if os.path.exists(_SCALP_FILE):
        try:
            with open(_SCALP_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_scalp_trades(trades):
    tmp = _SCALP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trades, f, indent=2, default=str)
    os.replace(tmp, _SCALP_FILE)


@app.get("/api/scalp/trades")
async def get_scalp_trades():
    """Return all closed scalp trades from scalp_trades.json."""
    return _load_scalp_trades()


@app.post("/api/scalp/trades/bulk-delete")
async def bulk_delete_scalp_trades(request: Request):
    """Bulk-delete scalp trades by trade_id list."""
    body = await request.json()
    ids = body.get("ids", [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    id_set = set(ids)
    trades = _load_scalp_trades()
    _save_scalp_trades([t for t in trades if t.get("trade_id") not in id_set])
    if _scalp_engine is not None:
        _scalp_engine.closed_trades = [t for t in _scalp_engine.closed_trades if t.get("trade_id") not in id_set]
    _notify_scalp_ws()
    return {"deleted": len(id_set)}


@app.delete("/api/scalp/trades/{tid}")
async def delete_scalp_trade(tid: int):
    """Delete a single scalp trade by trade_id (from disk AND engine memory)."""
    trades = _load_scalp_trades()
    _save_scalp_trades([t for t in trades if t.get("trade_id") != tid])
    # Also remove from in-memory engine closed_trades so it doesn't reappear
    if _scalp_engine is not None:
        _scalp_engine.closed_trades = [t for t in _scalp_engine.closed_trades if t.get("trade_id") != tid]
    _notify_scalp_ws()
    return {"deleted": tid}


# ── Scalp Engine (live session, in-memory) ───────────────────────


def _get_scalp_engine():
    global _scalp_engine
    if not _HAS_SCALP:
        raise HTTPException(status_code=503, detail="scalp.py not available")
    if _scalp_engine is None:
        _scalp_engine = _ScalpEngineClass(dhan, _market_feed)
    return _scalp_engine


@app.get("/api/scalp/status")
async def get_scalp_status():
    eng = _get_scalp_engine()
    status = eng.get_status()
    # Merge in closed trades from file (persist across restarts)
    file_trades = _load_scalp_trades()
    status["file_trades"] = list(reversed(file_trades[-50:]))
    return status


@app.post("/api/scalp/start")
async def start_scalp_engine():
    eng = _get_scalp_engine()
    eng.start()
    _notify_scalp_ws()
    return {"status": "started"}


@app.post("/api/scalp/stop")
async def stop_scalp_engine():
    eng = _get_scalp_engine()
    _save_scalp_run_to_history(eng)
    eng.stop()
    _notify_scalp_ws()
    return {"status": "stopped"}


class ScalpEntryReq(BaseModel):
    underlying: str
    strike: int
    option_type: str
    expiry: str
    transaction_type: str = "BUY"
    lots: int = 1
    lot_size: int = 75
    target_premium: float = 0.0
    sl_premium: float = 0.0
    target_pct: float = 0.0
    sl_pct: float = 0.0
    target_rupees: float = 0.0
    sl_rupees: float = 0.0
    sqoff_time: str = "15:20"
    mode: str = "live"


_scalp_entry_lock = asyncio.Lock()
_last_scalp_entry_ts: float = 0.0


@app.post("/api/scalp/entry")
async def scalp_entry(req: ScalpEntryReq):
    global _last_scalp_entry_ts
    async with _scalp_entry_lock:
        # Cooldown guard INSIDE lock to prevent race condition
        now = asyncio.get_event_loop().time()
        if now - _last_scalp_entry_ts < 2.0:
            return {"status": "error", "message": "Duplicate entry blocked — please wait 2 seconds between entries"}
        _last_scalp_entry_ts = now
        eng = _get_scalp_engine()
        try:
            result = await eng.enter_trade(
                underlying=req.underlying,
                strike=req.strike,
                option_type=req.option_type,
                expiry=req.expiry,
                transaction_type=req.transaction_type,
                lots=req.lots,
                lot_size=req.lot_size,
                target_premium=req.target_premium,
                sl_premium=req.sl_premium,
                target_pct=req.target_pct,
                sl_pct=req.sl_pct,
                target_rupees=req.target_rupees,
                sl_rupees=req.sl_rupees,
                sqoff_time=req.sqoff_time,
                mode=req.mode,
            )
            if result.get("status") == "error":
                alerter.alert(
                    "Scalp Entry Failed",
                    f"Symbol: {req.underlying} {req.strike}{req.option_type}\nMode: {req.mode}\nError: {result.get('message', 'unknown')}",
                )
            _notify_scalp_ws()
            return result
        except Exception as e:
            alerter.alert(
                "Scalp Entry Error",
                f"Symbol: {req.underlying} {req.strike}{req.option_type}\nMode: {req.mode}\nError: {e}",
            )
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/scalp/exit/{trade_id}")
async def scalp_exit(trade_id: int):
    eng = _get_scalp_engine()
    try:
        result = await eng.exit_trade(trade_id, reason="manual")
        if result.get("status") == "ok":
            trades = _load_scalp_trades()
            trades.append(result["trade"])
            _save_scalp_trades(trades)
        elif result.get("status") == "error":
            alerter.alert("Scalp Exit Failed", f"Trade ID: {trade_id}\nError: {result.get('message', 'unknown')}")
        _notify_scalp_ws()
        return result
    except Exception as e:
        alerter.alert("Scalp Exit Error", f"Trade ID: {trade_id}\nError: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ScalpTargetsReq(BaseModel):
    target_premium: Optional[float] = None
    sl_premium: Optional[float] = None
    target_rupees: Optional[float] = None
    sl_rupees: Optional[float] = None
    sqoff_time: Optional[str] = None


@app.put("/api/scalp/trades/{trade_id}/targets")
async def update_scalp_targets(trade_id: int, req: ScalpTargetsReq):
    eng = _get_scalp_engine()
    result = await eng.update_trade_targets(trade_id, **{k: v for k, v in req.dict().items() if v is not None})
    _notify_scalp_ws()
    return result


@app.get("/api/option-ltp")
async def get_option_ltp(underlying: str, strike: int, expiry: str, option_type: str):
    """Get live LTP for a specific option contract."""
    if not dhan._is_configured():
        return {"status": "error", "message": "Broker not configured"}
    try:
        ltp = dhan.get_option_ltp(underlying, strike, expiry, option_type)
        return {"status": "ok", "ltp": ltp}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/paper/trades/csv")
async def export_paper_trades_csv(run_id: str = ""):
    """Export paper trading trades to CSV"""
    import csv
    import io

    engine = paper_engines.get(run_id) if run_id else None
    if not engine:
        # Find first engine with trades
        for e in paper_engines.values():
            if e.closed_trades:
                engine = e
                break
    if not engine or not engine.closed_trades:
        raise HTTPException(status_code=404, detail="No paper trades available")
    output = io.StringIO()
    fields = [
        "id",
        "leg_num",
        "transaction_type",
        "option_type",
        "strike",
        "entry_time",
        "exit_time",
        "entry_premium",
        "exit_premium",
        "lots",
        "lot_size",
        "pnl",
        "exit_reason",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for t in engine.closed_trades:
        row = {k: (str(v) if k in ("entry_time", "exit_time") else v) for k, v in t.items() if k in fields}
        writer.writerow(row)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=paper_trades_{datetime.now().strftime('%Y%m%d')}.csv"},
    )


# ── Live Ticker (Dhan LTP) ───────────────────────────────────────


# Ticker caching
_ticker_cache = {"data": None, "timestamp": 0, "ttl": 30}  # Cache for 30 seconds
_prev_close_cache = {"data": {}, "date": None}  # Cache prev close for the day
_vix_cache = {"price": 0, "prev_close": 0, "timestamp": 0, "ttl": 60}  # NSE VIX cache (60s)


def _fetch_nse_vix() -> dict:
    """Fetch India VIX from NSE allIndices API. Returns {price, prev_close} or cached."""
    now = time.time()
    if _vix_cache["price"] > 0 and (now - _vix_cache["timestamp"]) < _vix_cache["ttl"]:
        return {"price": _vix_cache["price"], "prev_close": _vix_cache["prev_close"]}
    try:
        import httpx

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        with httpx.Client(headers=headers, follow_redirects=True, timeout=8) as client:
            client.get("https://www.nseindia.com")  # get cookies
            r = client.get("https://www.nseindia.com/api/allIndices")
            if r.status_code == 200:
                for idx in r.json().get("data", []):
                    if idx.get("indexSymbol") == "INDIA VIX":
                        price = float(idx.get("last", 0))
                        prev = float(idx.get("previousClose", 0))
                        if price > 0:
                            _vix_cache["price"] = price
                            _vix_cache["prev_close"] = prev
                            _vix_cache["timestamp"] = now
                            print(f"[TICKER] NSE VIX={price} (prev={prev})")
                            return {"price": price, "prev_close": prev}
    except Exception as e:
        print(f"[TICKER] NSE VIX fetch failed: {e}")
    return {"price": _vix_cache["price"], "prev_close": _vix_cache["prev_close"]}


def _get_prev_close():
    """Get previous day close for indices. Cached per day. Uses yfinance (once/day)."""
    from datetime import date

    today = date.today()
    if _prev_close_cache["date"] == str(today) and _prev_close_cache["data"]:
        return _prev_close_cache["data"]
    try:
        import yfinance as yf

        result = {}
        for sym, key in [("^NSEI", "nifty"), ("^BSESN", "sensex")]:
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist) >= 2:
                result[key] = float(hist["Close"].iloc[-2])
                result[f"{key}_ltp"] = float(hist["Close"].iloc[-1])
            elif len(hist) == 1:
                result[key] = float(hist["Close"].iloc[0])
                result[f"{key}_ltp"] = float(hist["Close"].iloc[0])
        _prev_close_cache["data"] = result
        _prev_close_cache["date"] = str(today)
        print(f"[TICKER] Prev close from yfinance (cached for today): {result}")
        return result
    except Exception as e:
        print(f"[TICKER] Prev close fetch failed: {e}")
        return {}


@app.get("/api/ticker")
async def get_ticker():
    """Fetch live index + ATM prices — Dhan OHLC (single call), change% from yfinance prev close"""
    global _ticker_cache

    # Return cached data if still valid
    if _ticker_cache["data"] and (time.time() - _ticker_cache["timestamp"]) < _ticker_cache["ttl"]:
        return _ticker_cache["data"]

    # ── PRIMARY: Dhan OHLC API (one call for LTP + ATM CE/PE) ──
    if dhan._is_configured():
        try:
            print("[TICKER] Fetching from Dhan OHLC API...")

            # Resolve ATM option security IDs FIRST (no API call)
            ce_sid, pe_sid, atm_strike = None, None, 0
            try:
                ScripMaster.ensure_loaded()
                expiry = ScripMaster.get_nearest_expiry("NIFTY")
                if expiry:
                    last_nifty = 0
                    if _ticker_cache["data"]:
                        last_nifty = _ticker_cache["data"].get("nifty", {}).get("price", 0)
                    if last_nifty <= 0:
                        last_nifty = 24500
                    atm_strike = round(last_nifty / 50) * 50
                    ce_sid = ScripMaster.lookup("NIFTY", atm_strike, expiry, "CE")
                    pe_sid = ScripMaster.lookup("NIFTY", atm_strike, expiry, "PE")
                    print(f"[TICKER] ATM strike={atm_strike}, CE_sid={ce_sid}, PE_sid={pe_sid}, expiry={expiry}")
            except Exception as e:
                print(f"[TICKER] ATM lookup error: {e}")

            # SINGLE Dhan API call: IDX_I + NSE_FNO together
            # sid 13=NIFTY, 25=BANKNIFTY, 49=MIDCPNIFTY, 51=SENSEX (IDX_I). VIX from yfinance.
            segments = {"IDX_I": [13, 25, 49, 51]}
            if ce_sid and pe_sid:
                segments["NSE_FNO"] = [int(ce_sid), int(pe_sid)]

            all_data = dhan.get_ohlc_multi(segments)

            idx = all_data.get("IDX_I", {})
            fno = all_data.get("NSE_FNO", {})

            def _extract_ltp(d, sid):
                info = d.get(str(sid), {})
                if isinstance(info, dict):
                    return float(info.get("last_price", 0))
                return 0.0

            def _extract_prev_close(d, sid):
                """Extract previous day close from Dhan OHLC response (ohlc.close = prev day close)."""
                info = d.get(str(sid), {})
                if isinstance(info, dict):
                    ohlc = info.get("ohlc", {})
                    if isinstance(ohlc, dict):
                        return float(ohlc.get("close", 0))
                return 0.0

            nifty_ltp = _extract_ltp(idx, 13)
            banknifty_ltp = _extract_ltp(idx, 25)
            midcpnifty_ltp = _extract_ltp(idx, 49)
            sensex_ltp = _extract_ltp(idx, 51)

            if nifty_ltp > 0:
                # ATM check
                correct_atm = round(nifty_ltp / 50) * 50
                if correct_atm != atm_strike and ce_sid and pe_sid:
                    print(f"[TICKER] ATM shifted {atm_strike} → {correct_atm}, will correct next cycle")

                # ATM CE/PE from same response (with change% from ohlc.close)
                atm_ce = {"price": 0, "change": 0, "pct": 0}
                atm_pe = {"price": 0, "change": 0, "pct": 0}
                if ce_sid:
                    ce_p = _extract_ltp(fno, ce_sid)
                    ce_prev = _extract_prev_close(fno, ce_sid)
                    if ce_p > 0:
                        ce_chg = round(ce_p - ce_prev, 2) if ce_prev > 0 else 0
                        ce_pct = round(((ce_p - ce_prev) / ce_prev) * 100, 2) if ce_prev > 0 else 0
                        atm_ce = {"price": round(ce_p, 2), "change": ce_chg, "pct": ce_pct}
                if pe_sid:
                    pe_p = _extract_ltp(fno, pe_sid)
                    pe_prev = _extract_prev_close(fno, pe_sid)
                    if pe_p > 0:
                        pe_chg = round(pe_p - pe_prev, 2) if pe_prev > 0 else 0
                        pe_pct = round(((pe_p - pe_prev) / pe_prev) * 100, 2) if pe_prev > 0 else 0
                        atm_pe = {"price": round(pe_p, 2), "change": pe_chg, "pct": pe_pct}
                if ce_sid or pe_sid:
                    print(f"[TICKER] ATM {atm_strike}: CE={atm_ce['price']}, PE={atm_pe['price']}")

                # Index change% from Dhan OHLC prev close (ohlc.close = prev day close)
                # Fallback to yfinance if Dhan prev close is missing
                def _chg_from_ohlc(ltp, d, sid):
                    pc = _extract_prev_close(d, sid)
                    if pc > 0:
                        return round(ltp - pc, 2), round(((ltp - pc) / pc) * 100, 2)
                    return 0, 0

                n_chg, n_pct = _chg_from_ohlc(nifty_ltp, idx, 13)
                s_chg, s_pct = _chg_from_ohlc(sensex_ltp, idx, 51)
                bn_chg, bn_pct = _chg_from_ohlc(banknifty_ltp, idx, 25)
                mc_chg, mc_pct = _chg_from_ohlc(midcpnifty_ltp, idx, 49)

                # If Dhan didn't provide prev close, try yfinance as fallback
                if n_chg == 0 and n_pct == 0 and nifty_ltp > 0:
                    prev = _get_prev_close()

                    def _chg_yf(ltp, key):
                        pc = prev.get(key, 0)
                        if pc > 0:
                            return round(ltp - pc, 2), round(((ltp - pc) / pc) * 100, 2)
                        return 0, 0

                    n_chg, n_pct = _chg_yf(nifty_ltp, "nifty")
                    s_chg, s_pct = _chg_yf(sensex_ltp, "sensex")

                # VIX from NSE India (yfinance ^INDIAVIX delisted)
                vix_data = _fetch_nse_vix()
                vix_ltp = vix_data["price"]
                vix_prev = vix_data["prev_close"]
                v_chg = round(vix_ltp - vix_prev, 2) if vix_prev > 0 else 0
                v_pct = round(((vix_ltp - vix_prev) / vix_prev) * 100, 2) if vix_prev > 0 else 0

                result = {
                    "status": "ok",
                    "source": "dhan",
                    "nifty": {"price": round(nifty_ltp, 2), "change": n_chg, "pct": n_pct},
                    "banknifty": {"price": round(banknifty_ltp, 2), "change": bn_chg, "pct": bn_pct},
                    "midcpnifty": {"price": round(midcpnifty_ltp, 2), "change": mc_chg, "pct": mc_pct},
                    "sensex": {"price": round(sensex_ltp, 2), "change": s_chg, "pct": s_pct},
                    "vix": {"price": round(vix_ltp, 2), "change": v_chg, "pct": v_pct},
                    "atmCE": atm_ce,
                    "atmPE": atm_pe,
                }
                _ticker_cache["data"] = result
                _ticker_cache["timestamp"] = time.time()
                print(
                    f"[TICKER] Dhan: NIFTY={nifty_ltp} ({n_chg:+.2f}, {n_pct:+.2f}%), SENSEX={sensex_ltp}, VIX={vix_ltp}"
                )
                return result
            else:
                print("[TICKER] Dhan returned 0 for NIFTY — market may be closed, trying yfinance...")
        except Exception as e:
            print(f"[TICKER] Dhan API failed: {type(e).__name__}: {str(e)[:100]}, trying yfinance...")

    # ── FALLBACK: yfinance ────────────────────────────────────
    try:
        import yfinance as yf

        print("[TICKER] Fetching from yfinance (fallback)...")

        def _last_close_and_change(symbol: str):
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="2d")
            if hist.empty:
                return 0.0, 0.0, 0.0
            close = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else close
            change = close - prev
            pct = (change / prev * 100) if prev else 0.0
            return close, change, pct

        nifty_price, nifty_chg, nifty_pct = _last_close_and_change("^NSEI")
        sensex_price, sensex_chg, sensex_pct = _last_close_and_change("^BSESN")
        vix_data = _fetch_nse_vix()
        vix_price = vix_data["price"]
        vix_prev = vix_data["prev_close"]
        vix_chg = round(vix_price - vix_prev, 2) if vix_prev > 0 else 0
        vix_pct = round(((vix_price - vix_prev) / vix_prev) * 100, 2) if vix_prev > 0 else 0

        if nifty_price > 0:
            result = {
                "status": "ok",
                "source": "yfinance",
                "nifty": {"price": round(nifty_price, 2), "change": round(nifty_chg, 2), "pct": round(nifty_pct, 2)},
                "sensex": {
                    "price": round(sensex_price, 2),
                    "change": round(sensex_chg, 2),
                    "pct": round(sensex_pct, 2),
                },
                "vix": {"price": round(vix_price, 2), "change": round(vix_chg, 2), "pct": round(vix_pct, 2)},
                "atmCE": {"price": 0, "change": 0, "pct": 0},
                "atmPE": {"price": 0, "change": 0, "pct": 0},
            }
            _ticker_cache["data"] = result
            _ticker_cache["timestamp"] = time.time()
            print(f"[TICKER] yfinance: NIFTY={nifty_price}, SENSEX={sensex_price}")
            return result

        print("[TICKER] yfinance also returned no data")
    except Exception as yf_err:
        print(f"[TICKER] yfinance fallback failed: {yf_err}")

    return {"status": "error", "msg": "No price data available from any source"}


# ── Expiry Dates ──────────────────────────────────────────────────
@app.get("/api/expiry-dates")
async def get_expiry_dates():
    """Return nearest expiry dates for NIFTY, BANKNIFTY, SENSEX"""
    try:
        ScripMaster.ensure_loaded()
        nifty_exp = ScripMaster.get_nearest_expiry("NIFTY") or ""
        bn_exp = ScripMaster.get_nearest_expiry("BANKNIFTY") or ""
        sensex_exp = ScripMaster.get_nearest_expiry("SENSEX") or ""
        return {
            "status": "ok",
            "nifty": nifty_exp,
            "banknifty": bn_exp,
            "sensex": sensex_exp,
        }
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.get("/api/expiry-list/{symbol}")
async def get_expiry_list(symbol: str):
    """Return all available expiry dates for a given underlying symbol."""
    try:
        symbol = symbol.upper()
        ScripMaster.ensure_loaded()
        expiries = ScripMaster.get_expiries(symbol)
        # Only return future expiries (>= today)
        today = datetime.now().strftime("%Y-%m-%d")
        future = [e for e in expiries if e >= today]
        return {"status": "ok", "symbol": symbol, "expiries": future}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def _refresh_recent_charges(history: dict):
    """Re-fetch today & yesterday from Dhan historical API to fill in charges.

    The live get_trades() endpoint doesn't return charge fields (stt, sebiTax etc).
    Once those trades appear in get_trade_history(), we can update charges.
    """
    import time as _time

    try:
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        # Check last 3 days (in case of weekends)
        dates_to_check = []
        for delta in range(3):
            d = (today - timedelta(days=delta)).strftime("%Y-%m-%d")
            entry = history.get(d, {})
            # Only re-fetch if entry exists but has 0 charges
            if entry and entry.get("charges", 0) == 0 and entry.get("trades", 0) > 0:
                dates_to_check.append(d)

        if not dates_to_check:
            return

        from_date = min(dates_to_check)
        to_date = max(dates_to_check)
        print(f"📊 [CHARGES] Refreshing charges for {dates_to_check}...")

        result = dhan.get_trade_history(from_date, to_date, 0)
        if not isinstance(result, list) or not result:
            print(f"📊 [CHARGES] No historical data available yet for {from_date} to {to_date}")
            return

        # Paginate to get all trades
        all_trades = list(result)
        page = 1
        while len(result) >= 20:  # Dhan page size
            _time.sleep(0.3)
            result = dhan.get_trade_history(from_date, to_date, page)
            if not isinstance(result, list) or not result:
                break
            all_trades.extend(result)
            page += 1

        # Group by date
        trades_by_date = {}
        for t in all_trades:
            raw_time = t.get("exchangeTime") or t.get("createTime") or ""
            d = str(raw_time)[:10]
            if d in dates_to_check:
                if d not in trades_by_date:
                    trades_by_date[d] = []
                trades_by_date[d].append(t)

        updated = 0
        for date_str, day_trades in trades_by_date.items():
            groups = {}
            for t in day_trades:
                key = t.get("securityId") or t.get("tradingSymbol") or "unknown"
                if key not in groups:
                    sym = t.get("customSymbol") or t.get("tradingSymbol") or str(key)
                    groups[key] = {"buys": [], "sells": [], "symbol": sym}
                if t.get("transactionType") == "BUY":
                    groups[key]["buys"].append(t)
                elif t.get("transactionType") == "SELL":
                    groups[key]["sells"].append(t)

            total_pnl = 0
            total_charges = 0
            trade_count = 0
            wins = 0
            details = []
            for g in groups.values():
                buy_qty = sum(float(t.get("tradedQuantity", 0)) for t in g["buys"])
                sell_qty = sum(float(t.get("tradedQuantity", 0)) for t in g["sells"])
                buy_val = sum(float(t.get("tradedPrice", 0)) * float(t.get("tradedQuantity", 0)) for t in g["buys"])
                sell_val = sum(float(t.get("tradedPrice", 0)) * float(t.get("tradedQuantity", 0)) for t in g["sells"])
                leg_charges = 0
                for t in g["buys"] + g["sells"]:
                    for key_c in (
                        "sebiTax",
                        "stt",
                        "brokerageCharges",
                        "serviceTax",
                        "exchangeTransactionCharges",
                        "stampDuty",
                    ):
                        leg_charges += float(t.get(key_c, 0) or 0)
                matched = min(buy_qty, sell_qty)
                if matched > 0 and buy_qty > 0 and sell_qty > 0:
                    buy_avg = buy_val / buy_qty
                    sell_avg = sell_val / sell_qty
                    pnl = round((sell_avg - buy_avg) * matched, 2)
                    total_pnl += pnl
                    total_charges += leg_charges
                    trade_count += 1
                    if pnl > 0:
                        wins += 1
                    details.append(
                        {
                            "symbol": g["symbol"],
                            "pnl": pnl,
                            "qty": int(matched),
                            "buy_avg": round(buy_avg, 2),
                            "sell_avg": round(sell_avg, 2),
                            "charges": round(leg_charges, 2),
                        }
                    )

            if trade_count > 0 and total_charges > 0:
                history[date_str] = {
                    "pnl": round(total_pnl, 2),
                    "net_pnl": round(total_pnl - total_charges, 2),
                    "charges": round(total_charges, 2),
                    "trades": trade_count,
                    "trade_legs": len(day_trades),
                    "wins": wins,
                    "mode": "real",
                    "details": details,
                }
                updated += 1
                print(
                    f"📊 [CHARGES] Updated {date_str}: charges=₹{total_charges:.2f}, P&L=₹{total_pnl:.2f} ({trade_count} trades, {len(day_trades)} legs)"
                )

        if updated > 0:
            _save_trade_history(history)
            print(f"📊 [CHARGES] Refreshed charges for {updated} dates")
    except Exception as e:
        print(f"📊 [CHARGES] Refresh failed: {e}")


# ── Token renewal background task ────────────────────────────────
_token_renewal_task = None


async def _prefetch_scrip_master():
    """Download/refresh Scrip Master cache in background — non-blocking."""
    try:
        loaded = await asyncio.to_thread(ScripMaster.ensure_loaded)
        if loaded:
            _logger.info(f"[SCRIP] Background prefetch complete ({len(ScripMaster._options_cache)} contracts)")
        else:
            _logger.warning("[SCRIP] Background prefetch returned False — will retry on first order")
    except Exception as e:
        _logger.warning(f"[SCRIP] Background prefetch failed: {e}")


async def _backfill_in_background():
    """Run the blocking backfill in a thread so the event loop stays free."""
    global _backfill_state
    _backfill_state["status"] = "running"
    _backfill_state["message"] = "Fetching historical trades from Dhan..."
    loop = asyncio.get_event_loop()
    try:
        history = _load_trade_history()
        force = len(history) <= 2
        if force:
            _backfill_state["message"] = "First-run: full backfill in progress..."
            print("📊 [BACKFILL] Auto-backfilling trade history from Dhan (force)...")
        count = await loop.run_in_executor(None, lambda: _backfill_trade_history("2024-01-01", force=force))
        if not force:
            loaded = _load_trade_history()
            await loop.run_in_executor(None, lambda: _refresh_recent_charges(loaded))
            print(f"📊 [TRADE_HISTORY] {len(loaded)} days of trade data ({count} new)")
        else:
            print(f"📊 [BACKFILL] Done — loaded {count} days of historical trades")
        _backfill_state.update({"status": "done", "message": "Trade history up to date.", "new_dates": count})
    except Exception as e:
        print(f"📊 [BACKFILL] Startup backfill failed: {e}")
        _backfill_state.update({"status": "error", "message": str(e)})


# ── Prometheus instrumentation (must run before app starts) ────
if _PROMETHEUS_ENABLED:
    _PFI(app).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    _logger.info("[Prometheus] Metrics exposed at /metrics")


@app.on_event("startup")
async def _start_token_renewal():
    global _token_renewal_task
    if config.AUTO_TOKEN_ENABLED:
        _token_renewal_task = asyncio.create_task(token_renewal_loop())
        print("🔄 [TokenManager] Background token renewal scheduled (every 12h)")
    if _market_feed:
        print(f"⚡ [MarketFeed] WebSocket feed ready (dhanhq {'available' if HAS_DHAN_FEED else 'NOT available'})")
    # ── Pre-cache Scrip Master in background (non-blocking) ────
    asyncio.create_task(_prefetch_scrip_master())

    # Auto-backfill trade history — runs in a thread so startup returns instantly
    asyncio.create_task(_backfill_in_background())

    # Cleanup 0-trade paper/live entries left by prior deploys/restarts
    runs = _load_runs()
    before = len(runs)
    cleaned = [
        r for r in runs if r.get("mode") == "backtest" or len(r.get("trades") or []) > 0 or r.get("trade_count", 0) > 0
    ]
    if len(cleaned) < before:
        _save_runs(cleaned)
        print(f"🧹 [STARTUP] Removed {before - len(cleaned)} empty 0-trade runs from runs.json")

    # ── Auto-restore live engines from persisted state ────────
    asyncio.create_task(_restore_live_engines())

    # ── Auto-restore paper engines from persisted state ────────
    asyncio.create_task(_restore_paper_engines())


async def _restore_live_engines():
    """Scan for live_state_*.json files and re-start engines that were running."""
    import json as _json
    from datetime import date as date_type

    _here = os.path.dirname(__file__) or "."
    today = str(date_type.today())
    restored = 0

    for fname in os.listdir(_here):
        if not fname.startswith("live_state_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(_here, fname)
        try:
            with open(fpath, "r") as f:
                state = _json.load(f)

            # Skip stale sessions (not from today)
            if state.get("session_date") != today:
                print(f"🔄 [Restore] Skipping stale state: {fname} (date={state.get('session_date')})")
                continue

            strategy = state.get("strategy", {})
            entry_conditions = state.get("entry_conditions", [])
            exit_conditions = state.get("exit_conditions", [])
            deploy_config = state.get("deploy_config", {})
            run_id = strategy.get("run_name", "live") or "live"

            # Skip if an engine with this run_id already exists
            if run_id in live_engines:
                print(f"🔄 [Restore] Engine '{run_id}' already running — skipping")
                continue

            # Reconstruct engine with full config
            engine = LiveEngine(dhan, run_id=run_id)
            engine.configure(
                strategy=strategy,
                entry_conditions=entry_conditions or DEFAULT_ENTRY_CONDITIONS,
                exit_conditions=exit_conditions or DEFAULT_EXIT_CONDITIONS,
                deploy_config=deploy_config,
            )

            # Inject WebSocket feed if available
            if _market_feed and HAS_DHAN_FEED:
                instrument = strategy.get("instrument", "26000")
                _market_feed.subscribe_index(instrument)
                if not _market_feed.is_running:
                    _market_feed.start()
                engine.set_feed(_market_feed)

            # Restore trading state (positions, in_trade, closed trades, P&L, etc.)
            engine._load_state()
            engine.running = True

            async def broadcast(event: dict, _rid=run_id):
                for ws in ws_clients.copy():
                    try:
                        await ws.send_json({"source": "live", "run_id": _rid, **event})
                    except Exception:
                        if ws in ws_clients:
                            ws_clients.remove(ws)

            live_engines[run_id] = engine
            _live_tasks[run_id] = asyncio.create_task(engine.start(callback=broadcast))
            restored += 1
            print(f"✅ [Restore] Live engine '{run_id}' restored and started")

        except Exception as e:
            print(f"❌ [Restore] Failed to restore {fname}: {e}")

    if restored:
        print(f"🔄 [Restore] {restored} live engine(s) auto-restored from saved state")


async def _restore_paper_engines():
    """Scan for paper_state_*.json files and re-start engines that were running."""
    import json as _json
    from datetime import date as date_type

    _here = os.path.dirname(__file__) or "."
    today = str(date_type.today())
    restored = 0

    for fname in os.listdir(_here):
        if not fname.startswith("paper_state_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(_here, fname)
        try:
            with open(fpath, "r") as f:
                state = _json.load(f)

            # Skip stale sessions (not from today)
            if state.get("session_date") != today:
                print(f"🔄 [Restore] Skipping stale paper state: {fname} (date={state.get('session_date')})")
                continue

            strategy = state.get("strategy", {})
            entry_conditions = state.get("entry_conditions", [])
            exit_conditions = state.get("exit_conditions", [])

            # Require full config — can't restore from legacy format
            if not strategy:
                print(f"🔄 [Restore] Skipping {fname}: no full strategy config saved")
                continue

            run_id = strategy.get("run_name", "paper") or "paper"

            # Skip if already running
            if run_id in paper_engines:
                print(f"🔄 [Restore] Paper engine '{run_id}' already running — skipping")
                continue

            engine = PaperTradingEngine(dhan, run_id=run_id)
            engine.configure(
                strategy=strategy,
                entry_conditions=entry_conditions or DEFAULT_ENTRY_CONDITIONS,
                exit_conditions=exit_conditions or DEFAULT_EXIT_CONDITIONS,
            )

            # Inject WebSocket feed if available
            if _market_feed and HAS_DHAN_FEED:
                instrument = strategy.get("instrument", "26000")
                _market_feed.subscribe_index(instrument)
                if not _market_feed.is_running:
                    _market_feed.start()
                engine.set_feed(_market_feed)

            # Restore trading state (positions, in_trade, closed trades, P&L, etc.)
            engine._load_state()
            engine.running = True

            async def broadcast(event: dict, _rid=run_id):
                for ws in ws_clients.copy():
                    try:
                        await ws.send_json({"source": "paper", "run_id": _rid, **event})
                    except Exception:
                        if ws in ws_clients:
                            ws_clients.remove(ws)

            paper_engines[run_id] = engine
            _paper_tasks[run_id] = asyncio.create_task(engine.start(callback=broadcast))
            restored += 1
            print(f"✅ [Restore] Paper engine '{run_id}' restored and started")

        except Exception as e:
            print(f"❌ [Restore] Failed to restore paper {fname}: {e}")

    if restored:
        print(f"🔄 [Restore] {restored} paper engine(s) auto-restored from saved state")


@app.on_event("shutdown")
async def _shutdown_cleanup():
    """Save all running engine results and clean up."""
    # Save all running paper engines
    for run_id, engine in list(paper_engines.items()):
        try:
            status = engine.get_status()
            if engine.running:
                engine.stop()
            _save_paper_run_to_history(status)
            print(f"🛑 [Shutdown] Saved paper engine: {run_id}")
        except Exception as e:
            print(f"🛑 [Shutdown] Failed to save paper engine {run_id}: {e}")
    # Save all running live engines (state file for auto-restore + runs.json for history)
    for run_id, engine in list(live_engines.items()):
        try:
            status = engine.get_status()
            if engine.running:
                engine.stop()  # stop() calls _save_state() internally
            _save_live_run_to_history(status)
            print(f"🛑 [Shutdown] Saved live engine: {run_id}")
        except Exception as e:
            print(f"🛑 [Shutdown] Failed to save live engine {run_id}: {e}")
    shutdown_feed()
    await alerter.shutdown()
    print("🛑 [MarketFeed] WebSocket feed shut down")


# ── Feed Status ───────────────────────────────────────────────────
@app.get("/api/feed/status")
async def feed_status():
    """Get WebSocket market feed status."""
    if not _market_feed:
        return {"status": "unavailable", "reason": "dhanhq MarketFeed not installed"}
    return {
        "status": "running" if _market_feed.is_running else "stopped",
        "has_dhan_feed": HAS_DHAN_FEED,
        "subscriptions": len(_market_feed._subscriptions),
        "ltp_cache_size": len(_market_feed._ltp_cache),
        "aggregators": list(_market_feed._aggregators.keys()),
    }


# ── Run ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    # Phase 2: Install uvloop for C-level event-loop speed (~2-4x faster I/O scheduling)
    try:
        import uvloop

        uvloop.install()
        _loop_name = "uvloop"
    except ImportError:
        _loop_name = "asyncio (install uvloop for +30% speed)"

    print("=" * 60)
    print("  AlgoForge — Starting Backend")
    print(f"  Event loop : {_loop_name}")
    print(f"  Open: http://{config.APP_HOST}:{config.APP_PORT}")
    print("=" * 60)
    uvicorn.run(
        "app:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        reload=False,
        log_level="info",
        loop="uvloop" if _loop_name == "uvloop" else "auto",
    )
