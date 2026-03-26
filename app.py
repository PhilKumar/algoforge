"""
app.py — AlgoForge FastAPI Backend
Fixed:
  - Bug 4: yfinance MultiIndex columns flattened correctly
  - Bug 5: live engine uses asyncio.create_task (not background_tasks)
  - Added /logo.jpg route for the frontend
"""

import asyncio
import hashlib
import inspect
import json
from html import escape as _escape_html
from urllib.parse import quote as _url_quote

try:
    import orjson as _orjson
except ImportError:
    _orjson = None
import logging
import os
import secrets
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

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

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth as _auth_mod
import config
import db as _db_mod
from broker.dhan import DhanClient, ScripMaster
from engine.backtest import DEFAULT_ENTRY_CONDITIONS, DEFAULT_EXIT_CONDITIONS, get_strike_step, run_backtest
from engine.live import LiveEngine
from engine.market_feed import HAS_DHAN_FEED, get_market_feed, shutdown_feed
from engine.paper_trading import PaperTradingEngine
from engine.strike_utils import round_half_up
from engine.timeframes import (
    INTRADAY_CHUNK_DAYS,
    MAX_INTRADAY_HISTORY_DAYS,
    derived_timeframe_warning,
    describe_timeframe,
    resolve_strategy_timeframe,
)

try:
    from scalp import ScalpEngine as _ScalpEngineClass

    _HAS_SCALP = True
except ImportError:
    _HAS_SCALP = False
    _ScalpEngineClass = None
import alerter
from token_manager import auto_generate_token, token_renewal_loop


def _generate_startup_token_once():
    """Generate or share a Dhan token for the real app startup.

    This must not run during import-time smoke checks, otherwise deploy pre-flight
    burns a token refresh before the standby instance actually starts.
    """
    if not config.AUTO_TOKEN_ENABLED:
        print("ℹ️  [TokenManager] Auto-token disabled (set DHAN_PIN + DHAN_TOTP_SECRET in .env to enable)")
        return

    lock_file = os.path.join(_HERE, ".token_lock")
    token_file = os.path.join(_HERE, ".current_token")
    try:
        lock_handle = open(lock_file, "w")
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("🔑 [TokenManager] Auto-token enabled — generating fresh Dhan token...")
        try:
            new_token = auto_generate_token()
        except Exception as tok_err:
            print(f"⚠️  [TokenManager] Token generation error: {tok_err}")
            new_token = None
        if new_token:
            with open(token_file, "w") as f:
                f.write(new_token)
            print("✅ [TokenManager] Token generated successfully")
        else:
            print("⚠️  [TokenManager] Auto-token failed, using existing DHAN_ACCESS_TOKEN from .env")
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()
    except (IOError, OSError):
        import time as _t

        _t.sleep(3)
        if os.path.exists(token_file):
            with open(token_file) as f:
                shared_token = f.read().strip()
            if shared_token:
                config.DHAN_ACCESS_TOKEN = shared_token
                print("✅ [TokenManager] Loaded token from first worker")


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
IST = ZoneInfo("Asia/Kolkata")

# ── Multi-Engine Registries (scoped by user_id, then run_id) ────
live_engines: Dict[int, Dict[str, LiveEngine]] = defaultdict(dict)
paper_engines: Dict[int, Dict[str, PaperTradingEngine]] = defaultdict(dict)
_live_tasks: Dict[int, Dict[str, asyncio.Task]] = defaultdict(dict)
_paper_tasks: Dict[int, Dict[str, asyncio.Task]] = defaultdict(dict)


def _registry_bucket(registry: dict, user_id: int) -> dict:
    return registry.setdefault(int(user_id), {})


def _iter_registry_items(registry: dict):
    for owner_id, bucket in registry.items():
        for run_id, engine in bucket.items():
            yield int(owner_id), run_id, engine


def _get_engine_owner_id(engine) -> int:
    try:
        return int(getattr(engine, "_user_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _find_user_engine(registry: dict, user_id: int, run_id: str = ""):
    bucket = _registry_bucket(registry, user_id)
    if run_id:
        return run_id, bucket.get(run_id)
    for candidate_run_id, engine in bucket.items():
        if getattr(engine, "running", False):
            return candidate_run_id, engine
    return "", None


def _now_ist() -> datetime:
    return datetime.now(IST)


def _ist_date_str(value: datetime | None = None) -> str:
    dt = value or _now_ist()
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d")
    return dt.astimezone(IST).strftime("%Y-%m-%d")


def _new_portfolio_day_bucket() -> dict:
    return {
        "real_pnl": 0.0,
        "real_net_pnl": 0.0,
        "real_charges": 0.0,
        "real_brokerage": 0.0,
        "real_total_costs": 0.0,
        "paper_pnl": 0.0,
        "real_trades": 0,
        "real_trade_legs": 0,
        "real_order_count": 0,
        "paper_trades": 0,
        "real_wins": 0,
        "paper_wins": 0,
    }


def _new_portfolio_period_bucket() -> dict:
    return {
        "real_pnl": 0.0,
        "real_net_pnl": 0.0,
        "real_charges": 0.0,
        "real_brokerage": 0.0,
        "real_total_costs": 0.0,
        "paper_pnl": 0.0,
        "total_pnl": 0.0,
        "total_net_pnl": 0.0,
        "trades": 0,
        "wins": 0,
    }


def _aggregate_portfolio_history(real_history: dict[str, dict] | None, runs: list[dict] | None):
    """Combine persisted real trade history and paper runs into daily/monthly/yearly buckets."""

    daily: dict[str, dict] = {}

    for date_str, entry in (real_history or {}).items():
        bucket = daily.setdefault(str(date_str), _new_portfolio_day_bucket())
        bucket["real_pnl"] = round(float(entry.get("pnl", 0) or 0), 2)
        bucket["real_net_pnl"] = round(float(entry.get("net_pnl", entry.get("pnl", 0)) or 0), 2)
        bucket["real_charges"] = round(float(entry.get("charges", 0) or 0), 2)
        bucket["real_brokerage"] = round(float(entry.get("brokerage", 0) or 0), 2)
        bucket["real_total_costs"] = round(
            float(entry.get("total_costs", bucket["real_charges"] + bucket["real_brokerage"]) or 0),
            2,
        )
        bucket["real_trade_legs"] = int(entry.get("trade_legs", entry.get("trades", 0)) or 0)
        bucket["real_trades"] = bucket["real_trade_legs"]
        bucket["real_order_count"] = int(entry.get("order_count", 0) or 0)
        bucket["real_wins"] = int(entry.get("wins", 0) or 0)

    for run in runs or []:
        if run.get("mode") != "paper":
            continue

        run_date = None
        started = run.get("started_at", run.get("created_at", ""))
        if started:
            run_date = str(started)[:10]

        trades = run.get("trades", [])
        if trades:
            paper_by_date: dict[str, dict] = {}
            for trade in trades:
                trade_date = str(trade.get("exit_time", trade.get("entry_time", "")))[:10]
                if not trade_date or len(trade_date) < 10:
                    trade_date = run_date or ""
                if not trade_date:
                    continue
                if trade_date not in paper_by_date:
                    paper_by_date[trade_date] = {"pnl": 0.0, "count": 0, "wins": 0}
                pnl = float(trade.get("pnl", 0) or 0)
                paper_by_date[trade_date]["pnl"] += pnl
                paper_by_date[trade_date]["count"] += 1
                if pnl > 0:
                    paper_by_date[trade_date]["wins"] += 1

            for trade_date, trade_data in paper_by_date.items():
                bucket = daily.setdefault(trade_date, _new_portfolio_day_bucket())
                bucket["paper_pnl"] += round(float(trade_data["pnl"]), 2)
                bucket["paper_trades"] += int(trade_data["count"])
                bucket["paper_wins"] += int(trade_data["wins"])
        elif run_date:
            bucket = daily.setdefault(run_date, _new_portfolio_day_bucket())
            bucket["paper_pnl"] += round(float(run.get("total_pnl", 0) or 0), 2)
            bucket["paper_trades"] += int(run.get("trade_count", 0) or 0)
            stats = run.get("stats", {})
            bucket["paper_wins"] += int(stats.get("winning_trades", 0) or 0)

    monthly: dict[str, dict] = {}
    yearly: dict[str, dict] = {}
    for date_str, day in daily.items():
        ym = date_str[:7]
        year = date_str[:4]
        monthly_bucket = monthly.setdefault(ym, _new_portfolio_period_bucket())
        yearly_bucket = yearly.setdefault(year, _new_portfolio_period_bucket())

        real_pnl = float(day.get("real_pnl", 0) or 0)
        real_net_pnl = float(day.get("real_net_pnl", real_pnl) or 0)
        real_charges = float(day.get("real_charges", 0) or 0)
        real_brokerage = float(day.get("real_brokerage", 0) or 0)
        real_total_costs = float(day.get("real_total_costs", real_charges + real_brokerage) or 0)
        paper_pnl = float(day.get("paper_pnl", 0) or 0)
        total_trades = int(day.get("real_trades", 0) or 0) + int(day.get("paper_trades", 0) or 0)
        total_wins = int(day.get("real_wins", 0) or 0) + int(day.get("paper_wins", 0) or 0)

        for bucket in (monthly_bucket, yearly_bucket):
            bucket["real_pnl"] += real_pnl
            bucket["real_net_pnl"] += real_net_pnl
            bucket["real_charges"] += real_charges
            bucket["real_brokerage"] += real_brokerage
            bucket["real_total_costs"] += real_total_costs
            bucket["paper_pnl"] += paper_pnl
            bucket["total_pnl"] += real_pnl + paper_pnl
            bucket["total_net_pnl"] += real_net_pnl + paper_pnl
            bucket["trades"] += total_trades
            bucket["wins"] += total_wins

    for period in (monthly, yearly):
        for bucket in period.values():
            for key in (
                "real_pnl",
                "real_net_pnl",
                "real_charges",
                "real_brokerage",
                "real_total_costs",
                "paper_pnl",
                "total_pnl",
                "total_net_pnl",
            ):
                bucket[key] = round(float(bucket.get(key, 0) or 0), 2)

    return daily, monthly, yearly


_TRADE_STATUTORY_CHARGE_FIELDS = (
    "sebiTax",
    "stt",
    "serviceTax",
    "exchangeTransactionCharges",
    "stampDuty",
)
_TRADE_BROKERAGE_FIELDS = ("brokerageCharges", "brokerage")
_TRADE_HISTORY_SCHEMA_VERSION = 4
_TRADE_HISTORY_REPAIR_COOLDOWN_SECONDS = 300
_trade_history_repair_attempts: dict[int, float] = {}


def _trade_fill_id_value(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"0", "0.0", "na", "none", "null"}:
        return None
    return text


def _trade_fill_dedupe_key(trade: dict) -> str:
    for key in ("exchangeTradeId", "tradeId", "tradeNumber"):
        value = _trade_fill_id_value(trade.get(key))
        if value is not None:
            return f"{key}:{value}"
    parts = [
        trade.get("orderId", ""),
        trade.get("exchangeOrderId", ""),
        trade.get("transactionType", ""),
        trade.get("securityId", ""),
        trade.get("tradedQuantity", ""),
        trade.get("tradedPrice", ""),
        trade.get("exchangeTime", ""),
        trade.get("createTime", ""),
        trade.get("updateTime", ""),
    ]
    return "|".join(str(part) for part in parts)


def _dedupe_trade_fills(trades: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for trade in trades or []:
        key = _trade_fill_dedupe_key(trade)
        if key in seen:
            continue
        seen.add(key)
        unique.append(trade)
    return unique


def _trade_sort_key(trade: dict):
    return (
        str(trade.get("exchangeTime") or trade.get("createTime") or trade.get("updateTime") or ""),
        str(trade.get("orderId") or trade.get("exchangeOrderId") or ""),
        str(trade.get("exchangeTradeId") or trade.get("tradeId") or trade.get("tradeNumber") or ""),
        str(trade.get("transactionType") or ""),
        str(trade.get("securityId") or trade.get("tradingSymbol") or ""),
        str(trade.get("tradedPrice") or ""),
        str(trade.get("tradedQuantity") or ""),
    )


def _trade_order_key(trade: dict) -> str:
    for key in ("orderId", "exchangeOrderId"):
        value = trade.get(key)
        if value not in (None, ""):
            return str(value)
    return _trade_fill_dedupe_key(trade)


def _trade_statutory_charge_total(trade: dict) -> float:
    return sum(float(trade.get(field, 0) or 0) for field in _TRADE_STATUTORY_CHARGE_FIELDS)


def _trade_brokerage_total(trade: dict) -> float:
    return sum(float(trade.get(field, 0) or 0) for field in _TRADE_BROKERAGE_FIELDS)


def _trade_total_costs(trade: dict) -> float:
    return _trade_statutory_charge_total(trade) + _trade_brokerage_total(trade)


def _trade_qty(trade: dict) -> float:
    return abs(float(trade.get("tradedQuantity", 0) or 0))


def _trade_price(trade: dict) -> float:
    return float(trade.get("tradedPrice", 0) or 0)


def _trade_symbol_key(trade: dict) -> str:
    return str(trade.get("securityId") or trade.get("tradingSymbol") or "unknown")


def _trade_symbol_label(trade: dict) -> str:
    return str(trade.get("customSymbol") or trade.get("tradingSymbol") or _trade_symbol_key(trade))


def _trade_date_str(trade: dict) -> str:
    raw_time = trade.get("exchangeTime") or trade.get("createTime") or trade.get("updateTime") or ""
    date_str = str(raw_time)[:10]
    return date_str if date_str and len(date_str) >= 10 else ""


def _trade_history_entry_needs_refresh(
    entry: dict | None,
    *,
    trade_date: str | None = None,
    today_str: str | None = None,
) -> bool:
    if not isinstance(entry, dict) or not entry:
        return True
    try:
        if int(entry.get("schema_version") or 0) < _TRADE_HISTORY_SCHEMA_VERSION:
            return True
    except Exception:
        return True
    if trade_date and trade_date != (today_str or _ist_date_str()):
        if str(entry.get("source") or "") != "historical_fifo":
            return True
    return False


def _trade_history_needs_repair(user_id: int, history: dict[str, dict]) -> bool:
    if not history:
        return True
    today_str = _ist_date_str()
    if not any(
        _trade_history_entry_needs_refresh(entry, trade_date=date_str, today_str=today_str)
        for date_str, entry in history.items()
    ):
        return False
    last_attempt = float(_trade_history_repair_attempts.get(int(user_id), 0) or 0)
    return (time.monotonic() - last_attempt) >= _TRADE_HISTORY_REPAIR_COOLDOWN_SECONDS


def _trade_history_refresh_start(
    history: dict[str, dict] | None,
    default_from_date: str = "2024-01-01",
    *,
    today_str: str | None = None,
    recent_window_days: int = 120,
) -> str:
    try:
        refresh_floor = date.fromisoformat(str(default_from_date)[:10])
    except ValueError:
        refresh_floor = date(2024, 1, 1)

    today_value = str(today_str or _ist_date_str())[:10]
    try:
        today_date = date.fromisoformat(today_value)
    except ValueError:
        today_date = datetime.now(_IST).date()

    stale_dates: list[date] = []
    for trade_date, entry in (history or {}).items():
        trade_date_str = str(trade_date)[:10]
        try:
            parsed_date = date.fromisoformat(trade_date_str)
        except ValueError:
            continue
        if _trade_history_entry_needs_refresh(entry, trade_date=trade_date_str, today_str=today_value):
            stale_dates.append(parsed_date)

    if not stale_dates:
        return refresh_floor.isoformat()

    recent_cutoff = today_date - timedelta(days=max(int(recent_window_days or 0), 0))
    recent_stale_dates = [value for value in stale_dates if value >= recent_cutoff]
    refresh_start = min(recent_stale_dates) if recent_stale_dates else max(stale_dates)
    refresh_start = max(refresh_floor, refresh_start.replace(day=1))
    return refresh_start.isoformat()


def _new_trade_history_entry(*, source: str, calculation_mode: str) -> dict:
    return {
        "schema_version": _TRADE_HISTORY_SCHEMA_VERSION,
        "source": source,
        "calculation_mode": calculation_mode,
        "pnl": 0.0,
        "net_pnl": 0.0,
        "charges": 0.0,
        "brokerage": 0.0,
        "total_costs": 0.0,
        "trades": 0,
        "trade_legs": 0,
        "order_count": 0,
        "wins": 0,
        "mode": "real",
        "details": [],
    }


def _summarize_real_trade_history(
    trades: list[dict],
    *,
    source: str,
    carry_inventory: bool,
) -> dict[str, dict]:
    unique_trades = _dedupe_trade_fills(trades)
    if not unique_trades:
        return {}

    sorted_trades = sorted(unique_trades, key=_trade_sort_key)
    open_longs: dict[str, deque] = defaultdict(deque)
    open_shorts: dict[str, deque] = defaultdict(deque)
    entries: dict[str, dict] = {}
    order_keys_by_day: dict[str, set[str]] = defaultdict(set)
    symbol_details_by_day: dict[str, dict[str, dict]] = defaultdict(dict)
    current_date = ""

    for trade in sorted_trades:
        date_str = _trade_date_str(trade)
        if not date_str:
            continue
        if not carry_inventory and date_str != current_date:
            open_longs = defaultdict(deque)
            open_shorts = defaultdict(deque)
            current_date = date_str
        entry = entries.setdefault(
            date_str,
            _new_trade_history_entry(
                source=source,
                calculation_mode="cross_day_fifo" if carry_inventory else "day_fifo",
            ),
        )
        entry["trade_legs"] += 1
        entry["trades"] += 1
        order_keys_by_day[date_str].add(_trade_order_key(trade))

        side = str(trade.get("transactionType") or "").upper()
        qty = _trade_qty(trade)
        price = _trade_price(trade)

        symbol_key = _trade_symbol_key(trade)
        detail = symbol_details_by_day[date_str].setdefault(
            symbol_key,
            {
                "symbol": _trade_symbol_label(trade),
                "pnl": 0.0,
                "charges": 0.0,
                "brokerage": 0.0,
                "total_costs": 0.0,
                "qty": 0.0,
                "buy_qty": 0.0,
                "buy_value": 0.0,
                "sell_qty": 0.0,
                "sell_value": 0.0,
                "closed_segments": 0,
                "fill_count": 0,
            },
        )
        statutory_charges = _trade_statutory_charge_total(trade)
        brokerage = _trade_brokerage_total(trade)
        entry["charges"] += statutory_charges
        entry["brokerage"] += brokerage
        detail["charges"] += statutory_charges
        detail["brokerage"] += brokerage
        detail["total_costs"] += statutory_charges + brokerage
        detail["fill_count"] += 1

        if side not in {"BUY", "SELL"} or qty <= 0:
            continue

        remaining = qty
        if side == "BUY":
            detail["buy_qty"] += qty
            detail["buy_value"] += qty * price
            while remaining > 1e-9 and open_shorts[symbol_key]:
                open_fill = open_shorts[symbol_key][0]
                matched = min(remaining, open_fill["qty"])
                pnl = (open_fill["price"] - price) * matched
                entry["pnl"] += pnl
                detail["pnl"] += pnl
                detail["qty"] += matched
                detail["closed_segments"] += 1
                if pnl > 0:
                    entry["wins"] += 1
                open_fill["qty"] -= matched
                remaining -= matched
                if open_fill["qty"] <= 1e-9:
                    open_shorts[symbol_key].popleft()
            if remaining > 1e-9:
                open_longs[symbol_key].append({"qty": remaining, "price": price})
        else:
            detail["sell_qty"] += qty
            detail["sell_value"] += qty * price
            while remaining > 1e-9 and open_longs[symbol_key]:
                open_fill = open_longs[symbol_key][0]
                matched = min(remaining, open_fill["qty"])
                pnl = (price - open_fill["price"]) * matched
                entry["pnl"] += pnl
                detail["pnl"] += pnl
                detail["qty"] += matched
                detail["closed_segments"] += 1
                if pnl > 0:
                    entry["wins"] += 1
                open_fill["qty"] -= matched
                remaining -= matched
                if open_fill["qty"] <= 1e-9:
                    open_longs[symbol_key].popleft()
            if remaining > 1e-9:
                open_shorts[symbol_key].append({"qty": remaining, "price": price})

    for date_str, entry in entries.items():
        entry["pnl"] = round(float(entry.get("pnl", 0) or 0), 2)
        entry["charges"] = round(float(entry.get("charges", 0) or 0), 2)
        entry["brokerage"] = round(float(entry.get("brokerage", 0) or 0), 2)
        entry["total_costs"] = round(entry["charges"] + entry["brokerage"], 2)
        entry["net_pnl"] = round(entry["pnl"] - entry["total_costs"], 2)
        entry["trades"] = int(entry.get("trades", 0) or 0)
        entry["trade_legs"] = int(entry.get("trade_legs", entry["trades"]) or 0)
        entry["order_count"] = len(order_keys_by_day.get(date_str) or set())

        details = []
        for detail in symbol_details_by_day.get(date_str, {}).values():
            details.append(
                {
                    "symbol": detail["symbol"],
                    "pnl": round(detail["pnl"], 2),
                    "qty": int(round(detail["qty"])),
                    "buy_avg": round(detail["buy_value"] / detail["buy_qty"], 2) if detail["buy_qty"] else 0.0,
                    "sell_avg": round(detail["sell_value"] / detail["sell_qty"], 2) if detail["sell_qty"] else 0.0,
                    "charges": round(detail["charges"], 2),
                    "brokerage": round(detail["brokerage"], 2),
                    "total_costs": round(detail["total_costs"], 2),
                    "fill_count": int(detail["fill_count"]),
                    "closed_segments": int(detail["closed_segments"]),
                }
            )
        details.sort(key=lambda item: item["symbol"])
        entry["details"] = details

    return entries


def _summarize_real_trade_fills(trades: list[dict]) -> dict | None:
    """Summarize the latest live-trade day from Dhan fills.

    Live get_trades() snapshots are treated as day-local. Completed dates are
    later rebuilt from historical trade history using cross-day FIFO.
    """

    entries = _summarize_real_trade_history(trades, source="live_day_fifo", carry_inventory=False)
    if not entries:
        return None
    latest_date = max(entries.keys())
    return entries.get(latest_date)


def _running_statuses_for_user(registry: dict, user_id: int) -> list[dict]:
    return [engine.get_status() for engine in _registry_bucket(registry, user_id).values() if engine.running]


def _any_running(registry: dict, user_id: int | None = None) -> bool:
    if user_id is None:
        return any(engine.running for _, _, engine in _iter_registry_items(registry))
    return any(engine.running for engine in _registry_bucket(registry, user_id).values())


def _engine_state_dir(user_id: int, create: bool = True) -> str:
    state_dir = os.path.join(config.USER_DATA_ROOT, str(int(user_id or 0)), "engine_state")
    if create:
        os.makedirs(state_dir, exist_ok=True)
    return state_dir


def _iter_user_state_files(prefix: str):
    if not os.path.isdir(config.USER_DATA_ROOT):
        return
    for user_folder in sorted(os.listdir(config.USER_DATA_ROOT)):
        if not str(user_folder).isdigit():
            continue
        user_id = int(user_folder)
        state_dir = _engine_state_dir(user_id, create=False)
        if not os.path.isdir(state_dir):
            continue
        for fname in os.listdir(state_dir):
            if fname.startswith(prefix) and fname.endswith(".json"):
                yield user_id, state_dir, fname, os.path.join(state_dir, fname)


# Backfill status — read by /api/backfill/status
_backfill_state: Dict[str, object] = {
    "status": "idle",  # idle | running | done | error
    "message": "",
    "new_dates": 0,
}

# Stopped engine snapshots — persisted per user under engine_state/
_stopped_engines: Dict[int, Dict[str, dict]] = {}


def _stopped_engines_file(user_id: int) -> str:
    return os.path.join(_engine_state_dir(user_id), "stopped_engines.json")


def _load_stopped_engines(user_id: int) -> dict:
    cached = _stopped_engines.get(int(user_id))
    if cached is not None:
        return cached
    data: dict = {}
    file_path = _stopped_engines_file(user_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    _stopped_engines[int(user_id)] = data
    return data


def _save_stopped_engines(user_id: int):
    try:
        data = _stopped_engines.get(int(user_id), {})
        file_path = _stopped_engines_file(user_id)
        tmp = file_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, file_path)
    except Exception:
        pass


def _state_file_snapshots(user_id: int) -> list[dict]:
    """Build Live-page snapshots from today's persisted engine state files.

    This preserves previously visible Live tabs after migration/redeploy even
    when the in-memory engine bucket is empty and no stopped snapshot file has
    been written yet.
    """

    state_dir = _engine_state_dir(user_id, create=False)
    if not os.path.isdir(state_dir):
        return []

    today = str(date.today())
    snapshots: list[dict] = []

    def _snapshot_from_state(fname: str, state: dict, mode: str) -> dict | None:
        strategy = state.get("strategy") or {}
        run_id = strategy.get("run_name") or state.get("strategy_name") or fname.rsplit(".", 1)[0]
        if not run_id:
            return None

        closed_trades = state.get("closed_trades") or []
        total_pnl = state.get("daily_pnl")
        if total_pnl is None:
            total_pnl = sum((t or {}).get("pnl", 0) for t in closed_trades)

        return {
            "run_id": run_id,
            "mode": mode,
            "running": False,
            "in_trade": bool(state.get("in_trade", False)),
            "positions": state.get("positions") or [],
            "closed_trades": closed_trades,
            "total_pnl": round(float(total_pnl or 0), 2),
            "trades_today": int(state.get("trades_today") or len(closed_trades)),
            "strategy_name": state.get("strategy_name") or run_id,
            "instrument": state.get("instrument") or strategy.get("instrument") or "",
            "current_candle": state.get("current_candle") or {},
            "current_indicators": state.get("current_indicators") or {},
            "event_log": state.get("event_log") or [],
            "current_time": state.get("current_time") or "",
            "strategy": strategy,
            "_snapshot_source": "state_file",
            "_snapshot_saved_at": state.get("saved_at") or "",
        }

    for fname in sorted(os.listdir(state_dir)):
        if not fname.endswith(".json"):
            continue
        if fname.startswith("paper_state_"):
            mode = "paper"
        elif fname.startswith("live_state_"):
            mode = "auto"
        else:
            continue

        fpath = os.path.join(state_dir, fname)
        try:
            with open(fpath, "r") as f:
                state = json.load(f)
        except Exception as e:
            _logger.warning("[LivePanels] Failed to read state snapshot %s for user %s: %s", fpath, user_id, e)
            continue

        if not isinstance(state, dict):
            continue
        if state.get("session_date") != today:
            continue

        snap = _snapshot_from_state(fname, state, mode)
        if snap:
            snapshots.append(snap)

    snapshots.sort(key=lambda s: (s.get("_snapshot_saved_at") or "", s.get("run_id") or ""), reverse=True)
    return snapshots


# Trade state tracker for Telegram alerts (keyed by run_id)
_alert_state: Dict[str, dict] = {}  # {"in_trade": bool, "closed_count": int}


def _alert_state_key(user_id: int | None, run_id: str) -> str:
    return f"{int(user_id or 0)}:{run_id}"


def _check_trade_alerts(run_id: str, mode_label: str, event: dict, user_id: int | None = None):
    """Detect trade entry/exit from engine status updates and fire Telegram alerts."""
    if event.get("type") in ("status", "price_update"):
        return  # Skip non-status-change events
    in_trade = event.get("in_trade", False)
    closed_trades = event.get("closed_trades", [])
    positions = event.get("positions", [])
    total_pnl = event.get("total_pnl", 0)
    state_key = _alert_state_key(user_id, run_id)
    prev = _alert_state.get(state_key, {"in_trade": False, "closed_count": 0})

    # Detect entry: was not in trade, now in trade
    if in_trade and not prev["in_trade"]:
        pos_lines = []
        for p in positions:
            sym = p.get("symbol") or p.get("trading_symbol") or "—"
            txn = p.get("transaction_type", "")
            premium = p.get("entry_premium", 0)
            pos_lines.append(f"  {txn} {sym} @ ₹{premium:.2f}")
        body = f"Strategy: {run_id}\nMode: {mode_label}\n" + "\n".join(pos_lines)
        alerter.alert("Trade Entry", body, level="info")

    # Detect exit: closed_trades count increased
    new_count = len(closed_trades)
    if new_count > prev["closed_count"]:
        new_trades = closed_trades[prev["closed_count"] :]
        for t in new_trades:
            sym = t.get("symbol") or t.get("trading_symbol") or "—"
            pnl = round(t.get("pnl", 0), 2)
            reason = t.get("exit_reason") or t.get("reason") or "—"
            level = "info" if pnl >= 0 else "warn"
            body = (
                f"Strategy: {run_id}\nMode: {mode_label}\n"
                f"Symbol: {sym}\nP&L: ₹{pnl:.2f}\nReason: {reason}\n"
                f"Total P&L: ₹{round(total_pnl, 2):.2f}"
            )
            alerter.alert("Trade Exit", body, level=level)

    _alert_state[state_key] = {"in_trade": in_trade, "closed_count": new_count}


# Global WebSocket market feed (singleton — shared by paper + live engines)
_market_feed = get_market_feed(dhan) if HAS_DHAN_FEED else None
_scalp_engines: Dict[int, "_ScalpEngineClass"] = {}
_SKIP_STARTUP_JOBS = os.getenv("ALGOFORGE_SKIP_STARTUP_JOBS", "").lower() in {"1", "true", "yes"}

ws_clients: Dict[int, List[WebSocket]] = defaultdict(list)


def _user_ws_clients(user_id: int) -> List[WebSocket]:
    return ws_clients.setdefault(int(user_id), [])


async def _broadcast_user_ws_json(user_id: int, payload: dict):
    for ws in _user_ws_clients(user_id).copy():
        try:
            await ws.send_json(payload)
        except Exception:
            if ws in _user_ws_clients(user_id):
                _user_ws_clients(user_id).remove(ws)


# ── Authentication ────────────────────────────────────────────────
# Legacy PIN kept as fallback for first-run admin bootstrap only
AUTH_PASSWORD = (os.getenv("ALGOFORGE_PIN") or os.getenv("ALGOFORGE_PASSWORD") or "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

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


async def _get_preferred_admin_user() -> dict | None:
    """Return the configured admin account, with fallback for legacy installs."""
    return await _db_mod.get_admin_user(config.ADMIN_USERNAME)


def _get_bootstrap_admin_password() -> str:
    """Return the bootstrap admin password for first-run provisioning."""
    return AUTH_PASSWORD


def _request_user_id(request: Request) -> int:
    """Return the authenticated user id from middleware state."""
    user_id = getattr(request.state, "user_id", 0)
    return int(user_id or 0)


async def _resolve_history_user_id(explicit_user_id: int | None = None, source: dict | None = None) -> int:
    """Resolve a run-history owner from request context, engine state, or admin fallback."""
    candidates: list[object] = [explicit_user_id]
    if isinstance(source, dict):
        candidates.append(source.get("_user_id"))
        candidates.append(source.get("user_id"))
        strategy = source.get("strategy")
        if isinstance(strategy, dict):
            candidates.append(strategy.get("_user_id"))
            candidates.append(strategy.get("user_id"))
    for candidate in candidates:
        try:
            if candidate is not None and str(candidate).strip():
                return int(candidate)
        except (TypeError, ValueError):
            continue
    admin = await _get_preferred_admin_user()
    if admin:
        return int(admin["id"])
    raise RuntimeError("No user context available for run history persistence")


def _default_history_user_id_sync() -> int:
    """Resolve the admin user id for sync startup/backfill helpers."""
    admin = _db_mod.get_admin_user_sync(config.ADMIN_USERNAME)
    if admin:
        return int(admin["id"])
    raise RuntimeError("No admin user available for trade-history persistence")


def _user_broker_credentials(user: dict | None) -> tuple[str, str, str, str]:
    if not user:
        return "", "", "", ""
    return (
        str(user.get("dhan_client_id", "") or "").strip(),
        str(user.get("dhan_access_token", "") or "").strip(),
        str(user.get("dhan_pin", "") or "").strip(),
        str(user.get("dhan_totp_secret", "") or "").strip(),
    )


def _user_broker_fields(user: dict | None) -> tuple[str, str]:
    client_id, access_token, _, _ = _user_broker_credentials(user)
    return client_id, access_token


def _user_broker_auto_refresh_ready(user: dict | None) -> bool:
    client_id, access_token, pin, totp = _user_broker_credentials(user)
    return bool(client_id and access_token and pin and totp)


def _persist_user_access_token_sync(user_id: int, access_token: str) -> None:
    token = str(access_token or "").strip()
    if not token:
        return
    _db_mod.update_user_sync(int(user_id), dhan_access_token=token)


def _broker_not_configured_message(user: dict | None, source: str) -> str:
    if source == "partial":
        return "Broker credentials are incomplete for this user. Add both Client ID and Access Token."
    if user and user.get("role") == "admin":
        return "Dhan API credentials not configured. Add user broker credentials or keep the admin .env fallback configured."
    return "Broker credentials are not configured for this user."


def _looks_like_broker_auth_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(
        part in text
        for part in (
            "authentication failed",
            "invalid token",
            "dh-906",
            "unauthorized",
            "api returned 400",
        )
    )


def _market_probe_has_instruments(payload) -> bool:
    """Return True when a market-feed payload contains at least one instrument quote."""
    if isinstance(payload, dict):
        if any(key in payload for key in ("last_price", "ltp", "LTP", "ohlc")):
            return True
        return any(_market_probe_has_instruments(value) for value in payload.values())
    if isinstance(payload, list):
        if not payload:
            return False
        return any(_market_probe_has_instruments(value) for value in payload)
    return payload not in (None, "", 0, 0.0, False)


def _probe_market_data_connection(broker_client: DhanClient) -> bool:
    """Check whether market-data APIs are reachable without treating empty probe data as fatal."""
    probe_segments = {"IDX_I": [13]}
    probe_calls = (
        lambda: broker_client.get_ltp_multi(probe_segments),
        lambda: broker_client.get_ohlc_multi(probe_segments),
    )
    saw_empty_payload = False
    last_non_auth_error = None

    for probe_call in probe_calls:
        try:
            payload = probe_call()
        except Exception as exc:
            if _looks_like_broker_auth_error(str(exc)):
                raise
            last_non_auth_error = exc
            continue
        if _market_probe_has_instruments(payload):
            return True
        saw_empty_payload = True

    if saw_empty_payload and last_non_auth_error is None:
        return False
    if last_non_auth_error is not None:
        raise last_non_auth_error
    return False


def _mask_value(value: str, *, prefix: int = 3, suffix: int = 2) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= prefix + suffix:
        return "•" * len(text)
    return f"{text[:prefix]}{'•' * max(4, len(text) - (prefix + suffix))}{text[-suffix:]}"


def _trade_mode_value(trade) -> str:
    if isinstance(trade, dict):
        return str(trade.get("mode", "") or "").lower()
    return str(getattr(trade, "mode", "") or "").lower()


def _user_broker_settings_lock(user_id: int) -> tuple[bool, str]:
    if _any_running(live_engines, user_id):
        return True, "Stop live strategies before editing broker credentials."
    eng = _scalp_engines.get(int(user_id))
    if eng:
        live_scalp_open = any(_trade_mode_value(trade) == "live" for trade in getattr(eng, "open_trades", {}).values())
        if live_scalp_open:
            return True, "Close live scalp trades before editing broker credentials."
    return False, ""


def _broker_profile_payload(user: dict | None) -> dict:
    client_id, access_token, pin, totp = _user_broker_credentials(user)
    _, source = _resolve_user_broker_client(user)
    locked, lock_reason = _user_broker_settings_lock(int(user["id"])) if user else (False, "")
    return {
        "configured": bool(client_id and access_token),
        "partial": bool((client_id and not access_token) or (access_token and not client_id)),
        "source": source,
        "client_id": client_id,
        "client_id_masked": _mask_value(client_id),
        "access_token_saved": bool(access_token),
        "pin_saved": bool(pin),
        "totp_saved": bool(totp),
        "auto_refresh_ready": bool(client_id and access_token and pin and totp),
        "encryption_ready": bool(config.ENCRYPTION_KEY),
        "manage_locked": locked,
        "manage_lock_reason": lock_reason,
    }


def _resolve_user_broker_client(
    user: dict | None,
    *,
    allow_admin_fallback: bool = True,
) -> tuple[DhanClient | None, str]:
    client_id, access_token, pin, totp = _user_broker_credentials(user)
    if client_id and access_token:
        token_update_cb = None
        if user and user.get("id"):
            user_id = int(user["id"])

            def _token_update_cb(new_token: str, *, _user_id: int = user_id) -> None:
                _persist_user_access_token_sync(_user_id, new_token)

            token_update_cb = _token_update_cb
        return (
            DhanClient(
                client_id=client_id,
                access_token=access_token,
                pin=pin,
                totp_secret=totp,
                token_update_cb=token_update_cb,
            ),
            "user",
        )
    if client_id or access_token:
        return None, "partial"
    if allow_admin_fallback and user and user.get("role") == "admin" and dhan._is_configured():
        return dhan, "global"
    return None, "missing"


async def _request_broker_context(
    request: Request,
    *,
    allow_admin_fallback: bool = True,
) -> tuple[dict, DhanClient | None, str]:
    user = getattr(request.state, "current_user", None)
    if not user:
        user = await _auth_mod.get_current_user(request)
    broker_client, source = _resolve_user_broker_client(user, allow_admin_fallback=allow_admin_fallback)
    return user, broker_client, source


def _engine_status_summary(engine, run_id: str, default_mode: str) -> dict:
    try:
        status = engine.get_status() or {}
    except Exception:
        status = {}
    return {
        "run_id": run_id,
        "mode": status.get("mode") or default_mode,
        "strategy_name": status.get("strategy_name") or run_id,
        "instrument": status.get("instrument") or "",
        "in_trade": bool(status.get("in_trade")),
        "trades_today": int(status.get("trades_today") or 0),
        "total_pnl": float(status.get("total_pnl") or 0),
    }


# ── DB-backed session helpers (thin wrappers for sync-style code paths) ──
# These bridge the old middleware (sync-ish) to the async DB via asyncio


async def _validate_session_async(token: str) -> dict | None:
    """Validate session token via DB. Returns session dict or None."""
    return await _auth_mod.validate_session(token)


def _get_session_token(request: Request) -> str:
    """Extract session token from cookie or Authorization header."""
    return _auth_mod.get_session_token(request)


async def _get_page_user(request: Request) -> dict | None:
    """Resolve the logged-in user for HTML page routes, treating disabled users as logged out."""
    token = _get_session_token(request)
    session = await _validate_session_async(token)
    if not session:
        return None
    user = await _db_mod.get_user_by_id(session["user_id"])
    if not user or not user["is_active"]:
        if user:
            await _db_mod.delete_sessions_for_user(user["id"])
        elif token:
            await _db_mod.delete_session(token)
        return None
    return user


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
    if path in (
        "/api/auth/login",
        "/api/auth/status",
        "/api/health",
        "/api/save-state",
        "/login",
        "/",
        "/charts-viewer",
        "/logo.jpg",
        "/logo.png",
        "/favicon.ico",
    ):
        return await call_next(request)
    if path.startswith("/static") or path.startswith("/ws"):
        return await call_next(request)
    # Admin routes have their own Depends() guard, but still need basic session check
    token = _get_session_token(request)
    session = await _validate_session_async(token)
    if not session:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    user = await _db_mod.get_user_by_id(session["user_id"])
    if not user or not user["is_active"]:
        if user:
            await _db_mod.delete_sessions_for_user(user["id"])
        elif token:
            await _db_mod.delete_session(token)
        response = JSONResponse(status_code=401, content={"detail": "Account disabled or not found"})
        response.delete_cookie("algoforge_session")
        return response
    # Stash current user on request state to avoid repeated lookups downstream
    request.state.user_id = user["id"]
    request.state.current_user = user
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
    initial_capital: float = Field(default=500000.0, gt=0)
    execution_profile: str = "auto"
    enforce_capital: bool = False
    capital_buffer_pct: float = Field(default=0.0, ge=0, lt=100)
    sell_option_margin_per_lot: float = Field(default=0.0, ge=0)
    strategy_id: int = Field(default=0, ge=0)


class OrderRequest(BaseModel):
    security_id: str
    exchange_segment: str = "NSE_EQ"
    transaction_type: str
    quantity: int = Field(ge=1, le=100_000)
    order_type: str = "MARKET"
    product_type: str = "INTRADAY"
    price: float = Field(default=0, ge=0)


class StrategyPayload(BaseModel):
    strategy_id: int = Field(default=0, ge=0)
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
    execution_profile: str = "auto"
    spread_bps: float = Field(default=0.0, ge=0)
    entry_slippage_bps: float = Field(default=0.0, ge=0)
    exit_slippage_bps: float = Field(default=0.0, ge=0)
    entry_delay_candles: int = Field(default=0, ge=0, le=25)
    signal_exit_delay_candles: int = Field(default=0, ge=0, le=25)
    enforce_capital: bool = False
    capital_buffer_pct: float = Field(default=0.0, ge=0, lt=100)
    sell_option_margin_per_lot: float = Field(default=0.0, ge=0)
    allow_synthetic_option_fallback: bool = False


def _render_login_page() -> HTMLResponse:
    login_path = os.path.join(_HERE, "login.html")
    if not os.path.exists(login_path):
        return HTMLResponse("<h2>login.html not found</h2>")
    with open(login_path, encoding="utf-8") as f:
        login_html = f.read()
    referral_url = config.DHAN_REFERRAL_URL
    referral_qr_url = (
        f"https://api.qrserver.com/v1/create-qr-code/?size=180x180&data={_url_quote(referral_url, safe='')}"
        if referral_url
        else ""
    )
    login_html = login_html.replace("__DHAN_REFERRAL_URL__", _escape_html(referral_url or "#", quote=True))
    login_html = login_html.replace("__DHAN_REFERRAL_QR_URL__", _escape_html(referral_qr_url, quote=True))
    login_html = login_html.replace("__DHAN_REFERRAL_HIDDEN_CLASS__", "" if referral_url else " hidden")
    return HTMLResponse(login_html)


# ── Serve Frontend ────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_frontend(request: Request):
    user = await _get_page_user(request)
    if not user:
        return _render_login_page()
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
_USER_DATA_ROOT = config.USER_DATA_ROOT

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
    # DD-Mon (no year, e.g. 13-Feb)
    m = _re.match(r"^(\d{1,2})-([A-Za-z]+)$", name)
    if m:
        dd = int(m.group(1))
        num = _MONTH_MAP.get(m.group(2).upper()) or _MONTH_MAP.get(m.group(2).upper()[:3])
        if num:
            return f"9999-{num:02d}-{dd:02d}", f"{dd:02d} {_cal.month_abbr[num]}"
    # Mon-DD-DD or Mon-DD-DD-DD (ranges like Feb-12-15, Feb-4-5-6)
    m = _re.match(r"^([A-Za-z]+)-(\d{1,2})", name)
    if m:
        num = _MONTH_MAP.get(m.group(1).upper()) or _MONTH_MAP.get(m.group(1).upper()[:3])
        dd = int(m.group(2))
        if num:
            return f"9999-{num:02d}-{dd:02d}", name
    # Fallback — sort after all dated entries
    return f"9999-99-{name}", name


def _user_storage_root(user_id: int) -> str:
    return os.path.join(_USER_DATA_ROOT, str(int(user_id or 0)))


def _user_charts_root(user_id: int) -> str:
    return os.path.join(_user_storage_root(user_id), "charts")


def _safe_charts_subpath(user_id: int, *parts: str, create_root: bool = False) -> str | None:
    """Resolve path under the current user's charts root; return None on traversal."""
    for p in parts:
        if "/" in p or "\\" in p or ".." in p:
            return None
    root = _user_charts_root(user_id)
    if create_root:
        os.makedirs(root, exist_ok=True)
    candidate = os.path.join(root, *parts)
    if not os.path.realpath(candidate).startswith(os.path.realpath(root)):
        return None
    return candidate


@app.get("/charts-viewer", response_class=HTMLResponse)
async def serve_charts_viewer(request: Request):
    """Serve the historical chart viewer page (auth-protected)."""
    user = await _get_page_user(request)
    if not user:
        return _render_login_page()
    html_path = os.path.join(_HERE, "charts.html")
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h2>charts.html not found. Place it beside app.py</h2>")


@app.get("/api/charts/tree")
async def charts_tree(request: Request):
    """Return directory tree adapted to Daily Charts/ folder structure."""
    user_id = _request_user_id(request)
    charts_root = _user_charts_root(user_id)
    print(f"[CHARTS] Scanning user charts dir for user {user_id}: {charts_root}")
    print(f"[CHARTS] Exists: {os.path.isdir(charts_root)}")
    if not os.path.isdir(charts_root):
        print("[CHARTS] Directory NOT found – returning empty tree")
        return {"years": {}}
    tree: dict = {}
    for year in sorted(os.listdir(charts_root)):
        year_path = os.path.join(charts_root, year)
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
                files = os.listdir(day_path)
                has_img = any(f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) for f in files)
                has_keep = ".keep" in files
                if not has_img and not has_keep:
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
            # Check for custom sort order
            _sort_file = os.path.join(month_path, "_sort_order.json")
            if os.path.isfile(_sort_file):
                try:
                    with open(_sort_file, "r") as _sf:
                        _custom_order = json.load(_sf)  # list of folder names
                    _order_map = {name: i for i, name in enumerate(_custom_order)}
                    days_list.sort(key=lambda d: _order_map.get(d["folder"], 9999))
                except Exception:
                    days_list.sort(key=lambda d: d["sort"])
            else:
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
async def charts_images(year: str, month: str, day: str, request: Request):
    """Return list of image URLs for a specific date folder."""
    day_path = _safe_charts_subpath(_request_user_id(request), year, month, day)
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
async def serve_chart_image(year: str, month: str, day: str, filename: str, request: Request):
    """Serve a single chart image file."""
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        raise HTTPException(status_code=400, detail="Invalid file type")
    file_path = _safe_charts_subpath(_request_user_id(request), year, month, day, safe_name)
    if file_path is None or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(file_path)


# ── Chart Upload (Ctrl+V paste) ──────────────────────────────────
_ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


@app.post("/api/upload-chart")
async def upload_chart(
    request: Request,
    file: UploadFile,
    target_year: str | None = Form(None),
    target_month: str | None = Form(None),
    target_day: str | None = Form(None),
):
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

    # Use target folder if provided, otherwise default to today's date
    if target_year and target_month and target_day:
        year_str = target_year
        month_folder = target_month
        day_folder = target_day
    else:
        from datetime import timezone as _tz

        now_ist = datetime.now(_tz(timedelta(hours=5, minutes=30)))
        year_str = str(now_ist.year)
        month_abbr = _cal.month_abbr[now_ist.month]
        month_folder = f"{month_abbr}-{year_str}"
        day_folder = f"{now_ist.day:02d}-{month_abbr}-{year_str}"

    day_path = _safe_charts_subpath(_request_user_id(request), year_str, month_folder, day_folder, create_root=True)
    if day_path is None:
        raise HTTPException(status_code=400, detail="Invalid target path")
    os.makedirs(day_path, exist_ok=True)
    print(f"[CHARTS] Upload target dir: {day_path}")

    # Generate filename: Nifty_DD-MM-YYYY[_N].ext
    if target_year and target_month and target_day:
        from datetime import timezone as _tz

        now_ist = datetime.now(_tz(timedelta(hours=5, minutes=30)))
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
    # Remove .keep placeholder if present (created by create-folder)
    keep_file = os.path.join(day_path, ".keep")
    if os.path.isfile(keep_file):
        os.remove(keep_file)
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
async def delete_chart(year: str, month: str, day: str, filename: str, request: Request):
    """Delete a single chart image file."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_IMG_EXT:
        raise HTTPException(status_code=400, detail="Invalid file type")
    file_path = _safe_charts_subpath(_request_user_id(request), year, month, day, filename)
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
    user_id = _request_user_id(request)
    old_path = _safe_charts_subpath(user_id, year, month, day, filename)
    if old_path is None:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isfile(old_path):
        raise HTTPException(status_code=404, detail="File not found")
    # Sanitize new name: keep extension, strip dangerous chars
    new_base = _re.sub(r"[^\w\s._-]", "", os.path.splitext(new_name)[0])[:80]
    if not new_base:
        raise HTTPException(status_code=400, detail="Invalid new name")
    new_filename = f"{new_base}{old_ext}"
    new_path = _safe_charts_subpath(user_id, year, month, day, new_filename)
    if new_path is None:
        raise HTTPException(status_code=400, detail="Invalid new path")
    if os.path.exists(new_path):
        raise HTTPException(status_code=409, detail="A file with that name already exists")
    os.rename(old_path, new_path)
    from urllib.parse import quote

    new_url = f"/charts-static/{quote(year)}/{quote(month)}/{quote(day)}/{quote(new_filename)}"
    print(f"[CHARTS] Renamed: {filename} → {new_filename}")
    return {"status": "ok", "old_name": filename, "new_name": new_filename, "url": new_url}


@app.patch("/api/charts/rename-folder")
async def rename_chart_folder(request: Request):
    """Rename a day folder in Chart History."""
    body = await request.json()
    year = body.get("year", "")
    month = body.get("month", "")
    old_day = body.get("old_day", "")
    new_day = body.get("new_day", "").strip()
    if not all([year, month, old_day, new_day]):
        raise HTTPException(status_code=400, detail="year, month, old_day, new_day required")
    user_id = _request_user_id(request)
    old_path = _safe_charts_subpath(user_id, year, month, old_day)
    if old_path is None or not os.path.isdir(old_path):
        raise HTTPException(status_code=404, detail="Folder not found")
    safe_new = _re.sub(r"[^\w\s._-]", "", new_day)[:80]
    if not safe_new:
        raise HTTPException(status_code=400, detail="Invalid new name")
    new_path = _safe_charts_subpath(user_id, year, month, safe_new)
    if new_path is None:
        raise HTTPException(status_code=400, detail="Invalid new path")
    if os.path.exists(new_path):
        raise HTTPException(status_code=409, detail="Folder already exists")
    os.rename(old_path, new_path)
    print(f"[CHARTS] Renamed folder: {old_day} → {safe_new}")
    return {"status": "ok", "old_name": old_day, "new_name": safe_new}


@app.post("/api/charts/create-folder")
async def create_chart_folder(request: Request):
    """Create a new day folder in Chart History."""
    body = await request.json()
    year = body.get("year", "")
    month = body.get("month", "")
    day_name = body.get("day_name", "").strip()
    if not all([year, month, day_name]):
        raise HTTPException(status_code=400, detail="year, month, day_name required")
    user_id = _request_user_id(request)
    safe_name = _re.sub(r"[^\w\s._-]", "", day_name)[:80]
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid folder name")
    # Ensure year and month directories exist
    year_path = _safe_charts_subpath(user_id, year, create_root=True)
    if year_path is None:
        raise HTTPException(status_code=400, detail="Invalid year")
    month_path = _safe_charts_subpath(user_id, year, month, create_root=True)
    if month_path is None:
        raise HTTPException(status_code=400, detail="Invalid month")
    os.makedirs(month_path, exist_ok=True)
    folder_path = os.path.join(month_path, safe_name)
    if os.path.exists(folder_path):
        raise HTTPException(status_code=409, detail="Folder already exists")
    os.makedirs(folder_path)
    # Create a placeholder so it shows in the tree (tree requires at least one image)
    placeholder = os.path.join(folder_path, ".keep")
    with open(placeholder, "w") as f:
        f.write("")
    print(f"[CHARTS] Created folder: {year}/{month}/{safe_name}")
    return {"status": "ok", "folder": safe_name}


@app.post("/api/charts/reorder")
async def reorder_chart_folders(request: Request):
    """Save custom sort order for day folders within a month."""
    body = await request.json()
    year = body.get("year", "")
    month = body.get("month", "")
    order = body.get("order", [])  # list of folder names in desired order
    if not all([year, month]) or not isinstance(order, list):
        raise HTTPException(status_code=400, detail="year, month, order[] required")
    month_path = _safe_charts_subpath(_request_user_id(request), year, month)
    if month_path is None or not os.path.isdir(month_path):
        raise HTTPException(status_code=404, detail="Month folder not found")
    sort_file = os.path.join(month_path, "_sort_order.json")
    with open(sort_file, "w") as f:
        json.dump(order, f)
    print(f"[CHARTS] Saved custom order for {year}/{month}: {len(order)} folders")
    return {"status": "ok"}


# ── Daily Journal (localStorage-backed on frontend, JSON file backup) ─
@app.get("/api/journal/list")
async def list_journals(request: Request):
    """Return list of all journal dates that have entries."""
    return {"entries": await _db_mod.list_journal_entries(_request_user_id(request))}


@app.get("/api/journal/{date_str}")
async def get_journal(date_str: str, request: Request):
    """Load journal entry for a date (YYYY-MM-DD)."""
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=400, detail="Invalid date format (use YYYY-MM-DD)")
    data = await _db_mod.get_journal_entry(_request_user_id(request), date_str)
    return {"date": date_str, "data": data}


@app.put("/api/journal/{date_str}")
async def save_journal(date_str: str, request: Request):
    """Save journal entry for a date (YYYY-MM-DD)."""
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=400, detail="Invalid date format (use YYYY-MM-DD)")
    body = await request.json()
    # Sanitize: only allow known fields
    allowed = {"asset", "strategy", "grade", "went_well", "to_improve", "mental_state"}
    clean = {k: str(v)[:2000] for k, v in body.items() if k in allowed}
    await _db_mod.upsert_journal_entry(_request_user_id(request), date_str, clean)
    return {"status": "ok", "date": date_str}


@app.delete("/api/journal/{date_str}")
async def delete_journal(date_str: str, request: Request):
    """Delete a journal entry for a date (YYYY-MM-DD)."""
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=400, detail="Invalid date format")
    deleted = await _db_mod.delete_journal_entry(_request_user_id(request), date_str)
    if not deleted:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    print(f"[JOURNAL] Deleted entry for {_request_user_id(request)}: {date_str}")
    return {"status": "ok", "deleted": date_str}


def _default_financial_plan() -> dict:
    return {
        "monthly_expense": 0.0,
        "assets_value": 0.0,
        "years_to_reserve": 10,
        "years_to_ffv": 10,
        "monthly_income": 0.0,
        "phv_increase": 0.0,
    }


def _sanitize_financial_plan(body: dict) -> dict:
    default = _default_financial_plan()
    body = body if isinstance(body, dict) else {}

    def _clean_money(field: str) -> float:
        try:
            return round(max(0.0, float(body.get(field, default[field]) or 0.0)), 2)
        except (TypeError, ValueError):
            return float(default[field])

    def _clean_years(field: str) -> int:
        try:
            return min(max(1, int(body.get(field, default[field]) or default[field])), 50)
        except (TypeError, ValueError):
            return int(default[field])

    return {
        "monthly_expense": _clean_money("monthly_expense"),
        "assets_value": _clean_money("assets_value"),
        "years_to_reserve": _clean_years("years_to_reserve"),
        "years_to_ffv": _clean_years("years_to_ffv"),
        "monthly_income": _clean_money("monthly_income"),
        "phv_increase": _clean_money("phv_increase"),
    }


@app.get("/api/financial-plan")
async def get_financial_plan(request: Request):
    """Load the saved financial planner for the current user."""
    saved = await _db_mod.get_financial_plan(_request_user_id(request))
    if not saved:
        plan = _default_financial_plan()
    else:
        plan = _sanitize_financial_plan(saved)
        if saved.get("updated_at"):
            plan["updated_at"] = saved["updated_at"]
    return {"status": "ok", "plan": plan}


@app.put("/api/financial-plan")
async def save_financial_plan(request: Request):
    """Save the embedded financial planner for the current user."""
    body = await request.json()
    clean = _sanitize_financial_plan(body if isinstance(body, dict) else {})
    await _db_mod.upsert_financial_plan(_request_user_id(request), clean)
    return {"status": "ok", "plan": clean}


# ── Brute-Force Protection ────────────────────────────────────────
_login_attempts: dict = defaultdict(list)  # login-key -> [timestamps] (fallback)
_LOGIN_MAX_ATTEMPTS = config.MAX_LOGIN_ATTEMPTS
_LOGIN_LOCKOUT_SEC = config.LOGIN_LOCKOUT_MINUTES * 60
_LOGIN_RL_PREFIX = "algoforge:login:"
_LEGACY_PIN_LENGTH = 6


def _password_policy_message(label: str = "Password") -> str:
    return f"{label} must be at least 8 characters, or exactly 6 digits for PIN mode"


def _is_valid_account_password(password: str) -> bool:
    password = str(password or "")
    return bool(_re.fullmatch(rf"\d{{{_LEGACY_PIN_LENGTH}}}", password)) or len(password) >= 8


def _require_valid_account_password(password: str, label: str = "Password") -> None:
    if not _is_valid_account_password(password):
        raise HTTPException(status_code=400, detail=_password_policy_message(label))


def _login_lockout_message() -> str:
    minutes = config.LOGIN_LOCKOUT_MINUTES
    return f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."


def _login_key(username: str, client_ip: str) -> str:
    username = (username or "").strip().lower()
    if username:
        return f"user:{username}"
    return f"ip:{client_ip or 'unknown'}"


def _check_login_rate(login_key: str):
    r = _get_redis()
    if r is not None:
        try:
            key = f"{_LOGIN_RL_PREFIX}{login_key}"
            count = int(r.get(key) or 0)
            if count >= _LOGIN_MAX_ATTEMPTS:
                raise HTTPException(status_code=429, detail=_login_lockout_message())
            return
        except HTTPException:
            raise
        except Exception as e:
            _logger.warning(f"[Redis] _check_login_rate failed, using in-memory: {e}")
    now = time.time()
    _login_attempts[login_key] = [t for t in _login_attempts[login_key] if now - t < _LOGIN_LOCKOUT_SEC]
    if len(_login_attempts[login_key]) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail=_login_lockout_message())


def _record_failed_login(login_key: str):
    r = _get_redis()
    if r is not None:
        try:
            key = f"{_LOGIN_RL_PREFIX}{login_key}"
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, _LOGIN_LOCKOUT_SEC)
            pipe.execute()
            return
        except Exception as e:
            _logger.warning(f"[Redis] _record_failed_login failed, using in-memory: {e}")
    _login_attempts[login_key].append(time.time())


def _clear_login_attempts(login_key: str):
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"{_LOGIN_RL_PREFIX}{login_key}")
            return
        except Exception:
            pass
    _login_attempts.pop(login_key, None)


# ── Authentication Endpoints ──────────────────────────────────────
@app.post("/api/auth/login")
async def auth_login(request: Request):
    ip = request.client.host if request.client else "unknown"
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    login_key = _login_key(username or config.ADMIN_USERNAME, ip)
    _check_login_rate(login_key)

    # If no username provided, treat as legacy PIN login → look up configured admin user
    if username:
        user = await _db_mod.get_user_by_username(username)
    else:
        user = await _get_preferred_admin_user()
    if not user:
        _record_failed_login(login_key)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account is disabled")

    if not _auth_mod.verify_password(password, user["password_hash"]):
        _record_failed_login(login_key)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Success — create DB session
    _clear_login_attempts(login_key)
    await _db_mod.cleanup_expired_sessions()
    token = await _auth_mod.create_session(user["id"])
    await _db_mod.update_last_login(user["id"])

    resp = JSONResponse(
        {
            "status": "ok",
            "message": "Login successful",
            "username": user["username"],
            "role": user["role"],
        }
    )
    is_https = request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(
        "algoforge_session",
        token,
        max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return resp


@app.get("/api/auth/status")
async def auth_status(request: Request):
    token = _get_session_token(request)
    session = await _validate_session_async(token)
    if not session:
        return {"authenticated": False}
    user = await _db_mod.get_user_by_id(session["user_id"])
    if not user or not user["is_active"]:
        if user:
            await _db_mod.delete_sessions_for_user(user["id"])
        elif token:
            await _db_mod.delete_session(token)
        resp = JSONResponse({"authenticated": False})
        resp.delete_cookie("algoforge_session")
        return resp
    return {
        "authenticated": True,
        "username": user["username"],
        "role": user["role"],
        "user_id": user["id"],
    }


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = _get_session_token(request)
    await _auth_mod.destroy_session(token)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("algoforge_session")
    return resp


# ── Admin Routes ──────────────────────────────────────────────────


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    """List all users (admin only)."""
    await _auth_mod.require_admin(request)
    users = await _db_mod.list_users()
    return {"users": users}


@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    """Create a new user (admin only)."""
    admin = await _auth_mod.require_admin(request)
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "user")
    email = body.get("email", "").strip() or None

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
    _require_valid_account_password(password)

    # Check if username already exists
    existing = await _db_mod.get_user_by_username(username)
    if existing:
        raise HTTPException(status_code=409, detail=f"Username '{username}' already taken")

    hashed = _auth_mod.hash_password(password)
    user_id = await _db_mod.create_user(username, hashed, role=role, email=email)
    _logger.info(f"[Admin] User '{username}' created by '{admin['username']}' (id={user_id})")
    return {"status": "ok", "user_id": user_id, "username": username, "role": role}


@app.put("/api/admin/users/{user_id}/toggle")
async def admin_toggle_user(user_id: int, request: Request):
    """Enable or disable a user account (admin only)."""
    admin = await _auth_mod.require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")
    user = await _db_mod.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    new_state = not bool(user["is_active"])
    await _db_mod.set_user_active(user_id, new_state)
    if not new_state:
        await _db_mod.delete_sessions_for_user(user_id)
    action = "enabled" if new_state else "disabled"
    _logger.info(f"[Admin] User '{user['username']}' {action} by '{admin['username']}'")
    return {"status": "ok", "user_id": user_id, "is_active": new_state}


@app.put("/api/admin/users/{user_id}/password")
async def admin_reset_password(user_id: int, request: Request):
    """Reset a user's password (admin only)."""
    await _auth_mod.require_admin(request)
    body = await request.json()
    new_password = body.get("password", "")
    _require_valid_account_password(new_password)
    user = await _db_mod.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    hashed = _auth_mod.hash_password(new_password)
    await _db_mod.update_user(user_id, password_hash=hashed)
    await _db_mod.delete_sessions_for_user(user_id)
    return {"status": "ok", "message": f"Password reset for '{user['username']}'"}


# ── User Self-Service Routes ─────────────────────────────────────


@app.put("/api/user/password")
async def change_own_password(request: Request):
    """Change your own password."""
    user = await _auth_mod.get_current_user(request)
    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")

    if not _auth_mod.verify_password(current, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    _require_valid_account_password(new_pw, "New password")

    hashed = _auth_mod.hash_password(new_pw)
    await _db_mod.update_user(user["id"], password_hash=hashed)
    await _db_mod.delete_sessions_for_user(user["id"])
    resp = JSONResponse({"status": "ok", "message": "Password changed. Please log in again."})
    resp.delete_cookie("algoforge_session")
    return resp


@app.get("/api/user/profile")
async def get_user_profile(request: Request):
    """Return authenticated user profile + broker settings metadata."""
    user = await _auth_mod.get_current_user(request)
    return {
        "status": "ok",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email"),
            "role": user["role"],
            "is_active": bool(user.get("is_active", 1)),
            "created_at": user.get("created_at"),
            "last_login": user.get("last_login"),
        },
        "broker": _broker_profile_payload(user),
    }


@app.put("/api/user/broker")
async def update_own_broker_settings(request: Request):
    """Create or update stored broker credentials for the current user."""
    user = await _auth_mod.get_current_user(request)
    locked, reason = _user_broker_settings_lock(int(user["id"]))
    if locked:
        raise HTTPException(status_code=409, detail=reason)
    if not _auth_mod.encryption_enabled():
        raise HTTPException(
            status_code=503,
            detail="Broker credential storage is disabled until ENCRYPTION_KEY is configured on the server.",
        )

    body = await request.json()
    client_id_input = body.get("client_id")
    access_token_input = body.get("access_token")
    pin_input = body.get("pin")
    totp_input = body.get("totp_secret")

    current_client_id = str(user.get("dhan_client_id", "") or "").strip()
    current_access_token = str(user.get("dhan_access_token", "") or "").strip()
    current_pin = str(user.get("dhan_pin", "") or "").strip()
    current_totp = str(user.get("dhan_totp_secret", "") or "").strip()

    new_client_id = current_client_id if client_id_input is None else str(client_id_input or "").strip()
    new_access_token = current_access_token if access_token_input is None else str(access_token_input or "").strip()
    new_pin = current_pin if pin_input is None else str(pin_input or "").strip()
    new_totp = current_totp if totp_input is None else str(totp_input or "").strip()

    if not (new_client_id or new_access_token or new_pin or new_totp):
        raise HTTPException(status_code=400, detail="Provide broker credentials to save, or use Clear to remove them.")
    if bool(new_client_id) != bool(new_access_token):
        raise HTTPException(status_code=400, detail="Both Client ID and Access Token are required together.")
    if (new_pin or new_totp) and not (new_client_id and new_access_token):
        raise HTTPException(
            status_code=400, detail="PIN/TOTP can only be saved together with Client ID and Access Token."
        )

    await _db_mod.update_user(
        user["id"],
        dhan_client_id=new_client_id,
        dhan_access_token=new_access_token,
        dhan_pin=new_pin,
        dhan_totp_secret=new_totp,
    )
    fresh_user = await _db_mod.get_user_by_id(user["id"])
    return {
        "status": "ok",
        "message": "Broker credentials saved.",
        "broker": _broker_profile_payload(fresh_user),
    }


@app.delete("/api/user/broker")
async def clear_own_broker_settings(request: Request):
    """Remove stored broker credentials for the current user."""
    user = await _auth_mod.get_current_user(request)
    locked, reason = _user_broker_settings_lock(int(user["id"]))
    if locked:
        raise HTTPException(status_code=409, detail=reason)

    cleared_fields = {key: str() for key in ("dhan_client_id", "dhan_access_token", "dhan_pin", "dhan_totp_secret")}
    await _db_mod.update_user(user["id"], **cleared_fields)
    fresh_user = await _db_mod.get_user_by_id(user["id"])
    return {
        "status": "ok",
        "message": "Stored broker credentials cleared.",
        "broker": _broker_profile_payload(fresh_user),
    }


@app.get("/api/admin/engines")
async def admin_list_engine_status(request: Request):
    """Summarize running engines across users (admin only)."""
    await _auth_mod.require_admin(request)
    known_users = {int(user["id"]): user for user in await _db_mod.list_users()}
    owner_ids = sorted(set(known_users) | set(paper_engines) | set(live_engines) | set(_scalp_engines))
    rows: list[dict] = []

    for owner_id in owner_ids:
        user = known_users.get(owner_id) or {
            "id": owner_id,
            "username": f"User {owner_id}",
            "role": "user",
            "is_active": True,
        }
        paper_runs = [
            _engine_status_summary(engine, run_id, "paper")
            for run_id, engine in _registry_bucket(paper_engines, owner_id).items()
            if getattr(engine, "running", False)
        ]
        live_runs = [
            _engine_status_summary(engine, run_id, "live")
            for run_id, engine in _registry_bucket(live_engines, owner_id).items()
            if getattr(engine, "running", False)
        ]
        scalp_engine = _scalp_engines.get(owner_id)
        scalp_open = list(getattr(scalp_engine, "open_trades", {}).values()) if scalp_engine else []
        scalp_live_open = sum(1 for trade in scalp_open if _trade_mode_value(trade) == "live")
        rows.append(
            {
                "user_id": owner_id,
                "username": user["username"],
                "role": user.get("role", "user"),
                "is_active": bool(user.get("is_active", 1)),
                "paper_running": len(paper_runs),
                "live_running": len(live_runs),
                "scalp_running": bool(scalp_engine and getattr(scalp_engine, "_running", False)),
                "scalp_open_trades": len(scalp_open),
                "scalp_live_open_trades": scalp_live_open,
                "paper_runs": paper_runs,
                "live_runs": live_runs,
            }
        )

    return {"status": "ok", "users": rows}


# ── Emergency Stop (Kill Switch) ─────────────────────────────────
@app.post("/api/emergency-stop")
async def emergency_stop(request: Request):
    """Kill switch: stop ALL running strategies immediately"""
    token = _get_session_token(request)
    if not await _validate_session_async(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    results = {}
    stopped_count = 0
    user = getattr(request.state, "current_user", {}) or {}
    caller_id = _request_user_id(request)
    if user.get("role") == "admin":
        target_user_ids = sorted(set(paper_engines) | set(live_engines))
    else:
        target_user_ids = [caller_id]

    # Stop all paper engines for target users
    for owner_id in target_user_ids:
        paper_bucket = _registry_bucket(paper_engines, owner_id)
        for run_id, engine in list(paper_bucket.items()):
            try:
                if engine.running:
                    engine.stop()
                    results[f"paper:{owner_id}:{run_id}"] = "stopped"
                    stopped_count += 1
                else:
                    results[f"paper:{owner_id}:{run_id}"] = "not_running"
                _alert_state.pop(_alert_state_key(owner_id, run_id), None)
            except Exception as e:
                results[f"paper:{owner_id}:{run_id}"] = f"error: {str(e)}"

    # Stop all live engines for target users
    for owner_id in target_user_ids:
        live_bucket = _registry_bucket(live_engines, owner_id)
        for run_id, engine in list(live_bucket.items()):
            try:
                if engine.running:
                    engine.stop()
                    results[f"live:{owner_id}:{run_id}"] = "stopped"
                    stopped_count += 1
                else:
                    results[f"live:{owner_id}:{run_id}"] = "not_running"
                _alert_state.pop(_alert_state_key(owner_id, run_id), None)
            except Exception as e:
                results[f"live:{owner_id}:{run_id}"] = f"error: {str(e)}"

    # Cancel background tasks and clear registries for target users
    for owner_id in target_user_ids:
        for tasks_dict in (_live_tasks, _paper_tasks):
            task_bucket = _registry_bucket(tasks_dict, owner_id)
            for _, task_ref in list(task_bucket.items()):
                if task_ref and not task_ref.done():
                    task_ref.cancel()
                    try:
                        await task_ref
                    except asyncio.CancelledError:
                        pass
            task_bucket.clear()
        _registry_bucket(live_engines, owner_id).clear()
        _registry_bucket(paper_engines, owner_id).clear()

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
    if not await _validate_session_async(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = _request_user_id(request)

    # Strategies count
    strats = await _db_mod.list_strategies(user_id)
    runs = await _db_mod.list_runs(user_id)
    real_strats = [
        s for s in strats if not s.get("_placeholder") and str(s.get("run_name") or s.get("name") or "").strip()
    ]

    # Active engines
    paper_statuses = _running_statuses_for_user(paper_engines, user_id)
    live_statuses = _running_statuses_for_user(live_engines, user_id)
    paper_running = bool(paper_statuses)
    live_running = bool(live_statuses)
    scalp_running = False
    scalp_pnl_val = 0.0
    scalp_trades_val = 0
    scalp_name = ""

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

    scalp_engine = _scalp_engines.get(int(user_id))
    scalp_status = None
    if scalp_engine is not None:
        try:
            scalp_status = scalp_engine.get_status()
        except Exception:
            scalp_status = None
    if isinstance(scalp_status, dict) and scalp_status.get("running"):
        scalp_running = True
        scalp_pnl_val = float(scalp_status.get("total_pnl") or 0)
        scalp_trades_val = len(scalp_status.get("closed_trades") or [])
        scalp_trades = list(scalp_status.get("open_trades") or []) + list(scalp_status.get("closed_trades") or [])
        scalp_underlyings = list(
            dict.fromkeys(
                str(t.get("underlying") or "").strip() for t in scalp_trades if str(t.get("underlying") or "").strip()
            )
        )
        scalp_name = "Scalp Session"
        if scalp_underlyings:
            scalp_name = "Scalp — " + ", ".join(scalp_underlyings[:3])

    today_pnl = paper_pnl_val + live_pnl_val + scalp_pnl_val

    # Best/worst across persisted runs + currently running engines/scalp session
    best_run = None
    worst_run = None
    total_backtests = len(runs)
    recent_transactions: list[dict] = []
    recent_seen: set[tuple] = set()

    def _consider_leader(candidate: dict | None):
        nonlocal best_run, worst_run
        if not isinstance(candidate, dict):
            return
        pnl = round(float(candidate.get("pnl") or 0), 2)
        candidate["pnl"] = pnl
        if best_run is None or pnl > float(best_run.get("pnl") or 0):
            best_run = candidate
        if worst_run is None or pnl < float(worst_run.get("pnl") or 0):
            worst_run = candidate

    def _add_recent_trade(trade: dict, run_name: str, mode: str):
        if not isinstance(trade, dict):
            return
        symbol = trade.get("symbol") or " ".join(
            str(part) for part in (trade.get("underlying"), trade.get("strike"), trade.get("option_type")) if part
        )
        time_value = trade.get("exit_time") or trade.get("entry_time") or ""
        record = {
            "time": time_value,
            "run_name": run_name,
            "mode": mode,
            "symbol": symbol or "—",
            "transaction_type": str(trade.get("transaction_type") or "TRADE").upper(),
            "entry_time": trade.get("entry_time") or "",
            "exit_time": trade.get("exit_time") or "",
            "entry_price": float(
                trade.get("entry_premium") or trade.get("entry_price") or trade.get("current_premium") or 0
            ),
            "exit_price": float(
                trade.get("exit_premium") or trade.get("exit_price") or trade.get("current_premium") or 0
            ),
            "quantity": trade.get("lots") or trade.get("quantity") or "—",
            "pnl": float(trade.get("pnl") or 0),
            "reason": trade.get("exit_reason") or trade.get("reason") or "—",
        }
        dedupe_key = (
            record["mode"],
            record["symbol"],
            record["transaction_type"],
            str(record["entry_time"]),
            str(record["exit_time"]),
            round(record["entry_price"], 2),
            round(record["exit_price"], 2),
            str(record["quantity"]),
            round(record["pnl"], 2),
            str(record["reason"]),
        )
        if dedupe_key in recent_seen:
            return
        recent_seen.add(dedupe_key)
        recent_transactions.append(record)

    for status in paper_statuses:
        _consider_leader(
            {
                "kind": "engine",
                "mode": "paper",
                "run_id": str(status.get("run_id") or status.get("strategy_name") or ""),
                "name": status.get("strategy_name") or status.get("run_id") or "Paper Strategy",
                "pnl": status.get("total_pnl") or 0,
            }
        )
        for trade in status.get("closed_trades", []) or []:
            _add_recent_trade(trade, status.get("strategy_name") or "Paper Run", "paper")
    for status in live_statuses:
        _consider_leader(
            {
                "kind": "engine",
                "mode": "auto",
                "run_id": str(status.get("run_id") or status.get("strategy_name") or ""),
                "name": status.get("strategy_name") or status.get("run_id") or "Live Strategy",
                "pnl": status.get("total_pnl") or 0,
            }
        )
        for trade in status.get("closed_trades", []) or []:
            _add_recent_trade(trade, status.get("strategy_name") or "Live Run", "live")

    if scalp_running and isinstance(scalp_status, dict):
        _consider_leader(
            {
                "kind": "scalp",
                "mode": "scalp",
                "name": scalp_name or "Scalp Session",
                "pnl": scalp_status.get("total_pnl") or 0,
            }
        )

    if runs:
        for r in runs:
            pnl = r.get("total_pnl", 0)
            _consider_leader(
                {
                    "kind": "run",
                    "id": r.get("id"),
                    "mode": str(r.get("mode") or "backtest"),
                    "run_id": str(r.get("run_name") or ""),
                    "name": r.get("run_name", "") or f"Run #{r.get('id')}",
                    "pnl": pnl,
                }
            )
            if r.get("mode") not in ("paper", "live"):
                continue
            for trade in r.get("trades", []) or []:
                _add_recent_trade(
                    trade,
                    r.get("run_name") or r.get("strategy_name") or f"Run #{r.get('id')}",
                    str(r.get("mode") or "paper"),
                )
        recent_transactions.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
        recent_transactions = recent_transactions[:10]

    return {
        "strategy_count": len(real_strats),
        "backtest_count": total_backtests,
        "paper_running": paper_running,
        "live_running": live_running,
        "scalp_running": scalp_running,
        "paper_strategy": ", ".join(s.get("strategy_name", "") for s in paper_statuses) if paper_statuses else "",
        "live_strategy": ", ".join(s.get("strategy_name", "") for s in live_statuses) if live_statuses else "",
        "scalp_strategy": scalp_name,
        "today_pnl": round(today_pnl, 2),
        "paper_pnl": round(paper_pnl_val, 2),
        "live_pnl": round(live_pnl_val, 2),
        "scalp_pnl": round(scalp_pnl_val, 2),
        "paper_trades": paper_trades_val,
        "live_trades": live_trades_val,
        "scalp_trades": scalp_trades_val,
        "best_run": best_run,
        "worst_run": worst_run,
        "recent_transactions": recent_transactions,
    }


# ── Strategy Validation ──────────────────────────────────────────
@app.post("/api/validate-strategy")
async def validate_strategy(request: Request):
    """Deep validation of strategy before deployment"""
    token = _get_session_token(request)
    if not await _validate_session_async(token):
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
    if not await _validate_session_async(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = {"funds": None, "positions": [], "unrealized_pnl": 0, "total_value": 0, "errors": []}
    user, broker_client, source = await _request_broker_context(request)
    if not broker_client:
        result["errors"].append(_broker_not_configured_message(user, source))
        return result
    # Funds
    try:
        funds = await asyncio.to_thread(broker_client.get_funds)
        result["funds"] = funds
        if isinstance(funds, dict):
            result["total_value"] = float(funds.get("availabelBalance", funds.get("available_balance", 0)))
    except Exception as e:
        result["errors"].append(f"Funds: {str(e)}")

    # Positions + unrealized P&L
    try:
        positions = await asyncio.to_thread(broker_client.get_positions)
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
async def get_strategy_versions(sid: int, request: Request):
    strategy = await _db_mod.get_strategy(_request_user_id(request), sid)
    if strategy:
        return {"versions": strategy.get("versions", [])}
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
        "live_running": _any_running(live_engines),
    }


@app.post("/api/save-state")
async def save_state(request: Request):
    """Persist all running engine states to disk (called by deploy script before restart)."""
    if request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "localhost only"})
    saved = []
    for owner_id, run_id, engine in _iter_registry_items(live_engines):
        if engine.running:
            engine._save_state()
            saved.append(f"live:{owner_id}:{run_id}")
    for owner_id, run_id, engine in _iter_registry_items(paper_engines):
        if engine.running:
            engine._save_state()
            saved.append(f"paper:{owner_id}:{run_id}")
    return {"status": "ok", "saved": saved}


@app.get("/api/token-status")
async def token_status():
    """Check Dhan API token expiry"""
    return config.get_token_expiry()


@app.post("/api/refresh-token")
async def refresh_token(request: Request):
    """Force-refresh the current broker token for this user or the admin fallback."""
    try:
        user, broker_client, source = await _request_broker_context(request)
        if not broker_client:
            return {
                "status": "not_configured",
                "message": _broker_not_configured_message(user, source),
            }

        new_tok = await asyncio.to_thread(broker_client.refresh_access_token, force=True)
        if new_tok:
            fresh_user = await _db_mod.get_user_by_id(user["id"]) if user else None
            return {
                "status": "ok",
                "message": "Token refreshed successfully",
                "source": source,
                "broker": _broker_profile_payload(fresh_user or user),
            }

        if source == "user":
            if _user_broker_auto_refresh_ready(user):
                message = "User broker token refresh failed. Re-save your Dhan broker credentials and try again."
            else:
                message = "Save Dhan PIN and TOTP Secret in Account Settings to enable per-user token refresh."
            return {"status": "error", "message": message, "source": source}

        return {"status": "error", "message": "Token generation failed — check TOTP secret", "source": source}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Broker Connection Validation ──────────────────────────────────
@app.post("/api/broker/check")
async def check_broker(request: Request):
    """Check if broker connection is active and valid"""
    try:
        user, broker_client, source = await _request_broker_context(request)
        if not broker_client:
            return {
                "status": "not_configured",
                "broker": "Dhan",
                "message": _broker_not_configured_message(user, source),
            }

        auto_refresh_ready = _user_broker_auto_refresh_ready(user)

        # Test connection by fetching account funds
        funds = await asyncio.to_thread(broker_client.get_funds)
        available_balance = float(funds.get("availabelBalance", 0) or 0) if isinstance(funds, dict) else 0.0

        if funds and isinstance(funds, dict):
            try:
                market_ok = await asyncio.to_thread(_probe_market_data_connection, broker_client)
            except Exception as probe_error:
                probe_msg = str(probe_error)
                if _looks_like_broker_auth_error(probe_msg):
                    if auto_refresh_ready:
                        message = "Market-data auth failed even after auto-refresh. Re-save your Dhan credentials."
                    else:
                        message = "Market-data auth failed. Save Dhan PIN and TOTP Secret in Account Settings for auto-refresh."
                    return {
                        "status": "auth_error",
                        "broker": "Dhan",
                        "message": message,
                        "source": source,
                        "available_balance": available_balance,
                        "funds": funds,
                        "market_data_ok": False,
                        "auto_refresh_ready": auto_refresh_ready,
                    }
                _logger.warning("[BrokerCheck] Market-data probe failed after funds load: %s", probe_msg)
                market_ok = False

            return {
                "status": "connected",
                "broker": "Dhan",
                "message": "Broker connection active",
                "source": source,
                "available_balance": available_balance,
                "funds": funds,
                "market_data_ok": market_ok,
                "auto_refresh_ready": auto_refresh_ready,
            }
        else:
            # No data returned
            return {"status": "error", "broker": "Dhan", "message": "Invalid response from broker API"}

    except Exception as e:
        error_msg = str(e)
        if _looks_like_broker_auth_error(error_msg):
            auto_refresh_ready = _user_broker_auto_refresh_ready(user if "user" in locals() else None)
            detail = (
                "Invalid broker credentials or expired token."
                if auto_refresh_ready
                else "Invalid broker credentials or expired token. Save Dhan PIN and TOTP Secret for auto-refresh."
            )
            return {
                "status": "auth_error",
                "broker": "Dhan",
                "message": detail,
                "source": source if "source" in locals() else "missing",
                "auto_refresh_ready": auto_refresh_ready,
            }
        elif "401" in error_msg or "Unauthorized" in error_msg:
            return {"status": "error", "broker": "Dhan", "message": "Invalid API credentials (401 Unauthorized)"}
        elif "403" in error_msg or "Forbidden" in error_msg:
            return {"status": "error", "broker": "Dhan", "message": "Access forbidden - check API permissions (403)"}
        elif "timeout" in error_msg.lower():
            return {"status": "error", "broker": "Dhan", "message": "Connection timeout - network issue"}
        else:
            return {"status": "error", "broker": "Dhan", "message": f"Connection error: {error_msg[:100]}"}


@app.get("/api/broker/trades")
async def get_broker_trades(request: Request):
    """Fetch executed trades from Dhan broker account"""
    try:
        user, broker_client, source = await _request_broker_context(request)
        if not broker_client:
            return {
                "status": "not_configured",
                "message": _broker_not_configured_message(user, source),
                "trades": [],
            }

        # Fetch trades from Dhan API
        trades_result = await asyncio.to_thread(broker_client.get_trades)
        trades = trades_result if isinstance(trades_result, list) else []

        # Auto-persist daily trade summary for portfolio history
        if trades:
            try:
                await _persist_daily_trades(trades, _request_user_id(request))
            except Exception as pe:
                print(f"[TRADE_HISTORY] Persist error: {pe}")

        return {"status": "success", "broker": "Dhan", "source": source, "count": len(trades), "trades": trades}

    except Exception as e:
        error_msg = str(e)
        return {
            "status": "error",
            "broker": "Dhan",
            "message": f"Failed to fetch trades: {error_msg[:100]}",
            "trades": [],
        }


def _backfill_trade_history(
    from_date: str = "2024-01-01",
    force: bool = False,
    user_id: int | None = None,
    broker_client: DhanClient | None = None,
):
    """Fetch historical trades from Dhan and backfill a user's persisted trade history.

    Args:
        from_date: Start date in YYYY-MM-DD format.
        force: If True, overwrite existing dates with fresh data from Dhan.
        user_id: Trade-history owner. Defaults to the configured admin user.
    """
    import time as _time

    try:
        client = broker_client or dhan
        owner_id = int(user_id or _default_history_user_id_sync())
        history = _db_mod.list_trade_history_sync(owner_id)
        today_str = _ist_date_str()
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
            result = client.get_trade_history(from_date, today_str, page)

            # Handle rate-limit: retry with exponential backoff
            if result == client.RATE_LIMITED:
                retried = False
                for attempt in range(1, RATE_LIMIT_RETRIES + 1):
                    wait = 2**attempt  # 2, 4, 8 seconds
                    print(f"[BACKFILL] Rate limited on page {page}, retry {attempt}/{RATE_LIMIT_RETRIES} after {wait}s")
                    _time.sleep(wait)
                    result = client.get_trade_history(from_date, today_str, page)
                    if result != client.RATE_LIMITED:
                        retried = True
                        break
                if not retried and result == client.RATE_LIMITED:
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

        # De-duplicate by exchange trade id (or a strict fill fingerprint fallback)
        unique_trades = _dedupe_trade_fills(all_trades)
        if len(unique_trades) < len(all_trades):
            print(f"[BACKFILL] De-duplicated: {len(all_trades)} → {len(unique_trades)} unique trades")
        all_trades = unique_trades

        daily_entries = _summarize_real_trade_history(all_trades, source="historical_fifo", carry_inventory=True)
        if force:
            _db_mod.clear_trade_history_sync(owner_id)

        new_dates = 0
        updated_entries = {}
        for date_str, entry in sorted(daily_entries.items()):
            if date_str == today_str and not force:
                continue
            if not force and date_str in existing_dates:
                existing_entry = history.get(date_str)
                if not _trade_history_entry_needs_refresh(existing_entry, trade_date=date_str, today_str=today_str):
                    continue
            if entry and (
                entry.get("trades", 0) > 0
                or entry.get("charges", 0) > 0
                or entry.get("brokerage", 0) > 0
                or entry.get("pnl", 0) != 0
            ):
                history[date_str] = entry
                updated_entries[date_str] = entry
                new_dates += 1

        if updated_entries:
            for date_str, entry in updated_entries.items():
                _db_mod.upsert_trade_history_entry_sync(owner_id, date_str, entry)
            print(f"[BACKFILL] {'Refreshed' if force else 'Added'} {new_dates} dates in SQLite trade history")
        else:
            print("[BACKFILL] No new dates to add (all existing)")

        return new_dates
    except Exception as e:
        print(f"[BACKFILL] Error: {e}")
        import traceback

        traceback.print_exc()
        return 0


@app.get("/api/portfolio/backfill")
async def portfolio_backfill(request: Request, force: bool = False):
    """Manually trigger historical trade backfill from Dhan.

    Args:
        force: If true, re-fetch ALL trades and overwrite existing data.
    """
    try:
        user, broker_client, source = await _request_broker_context(request)
        if not broker_client:
            return {"status": "not_configured", "message": _broker_not_configured_message(user, source)}
        count = await asyncio.to_thread(
            _backfill_trade_history,
            "2024-01-01",
            force,
            _request_user_id(request),
            broker_client,
        )
        return {"status": "success", "new_dates": count, "force": force}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/backfill/status")
async def backfill_status():
    """Return current background backfill state (polled by frontend)."""
    return _backfill_state


@app.get("/api/portfolio/history")
async def get_portfolio_history(request: Request):
    """Return historical real and paper P&L with daily/monthly/yearly aggregates."""
    try:
        user_id = _request_user_id(request)
        real_history = await _db_mod.list_trade_history(user_id)
        if _trade_history_needs_repair(user_id, real_history):
            _trade_history_repair_attempts[user_id] = time.monotonic()
            try:
                _, broker_client, _ = await _request_broker_context(request)
                if broker_client:
                    refresh_from_date = _trade_history_refresh_start(real_history, "2024-01-01")
                    await asyncio.to_thread(_backfill_trade_history, refresh_from_date, False, user_id, broker_client)
                    real_history = await _db_mod.list_trade_history(user_id)
            except Exception as repair_error:
                print(f"[PORTFOLIO] Trade-history repair skipped: {repair_error}")
        runs = await _db_mod.list_runs(user_id)
        daily, monthly, yearly = _aggregate_portfolio_history(real_history, runs)
        return {"status": "success", "daily": daily, "monthly": monthly, "yearly": yearly}
    except Exception as e:
        print(f"[PORTFOLIO] History error: {e}")
        return {"status": "error", "message": str(e), "daily": {}, "monthly": {}, "yearly": {}}


@app.post("/api/broker/connect")
async def connect_broker(request: Request):
    """Establish and validate broker connection"""
    try:
        user, broker_client, source = await _request_broker_context(request)
        if not broker_client:
            return {
                "status": "not_configured",
                "broker": "Dhan",
                "message": _broker_not_configured_message(user, source),
            }

        # Test connection by attempting to fetch account funds
        funds = await asyncio.to_thread(broker_client.get_funds)

        if funds and isinstance(funds, dict):
            # Successfully connected and validated
            return {
                "status": "connected",
                "broker": "Dhan",
                "message": "Successfully connected to Dhan broker",
                "source": source,
                "available_balance": funds.get("availabelBalance", 0),
                "client_id": broker_client.client_id,
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
INTRADAY_MAX_DAYS = MAX_INTRADAY_HISTORY_DAYS
ROLLING_OPTION_CHUNK_DAYS = 30
ROLLING_EXPIRY_SELECTIONS = {
    "current_week": ("WEEK", 0),
    "next_week": ("WEEK", 1),
    "current_month": ("MONTH", 0),
    "next_month": ("MONTH", 1),
}


def _fetch_data(
    instrument: str, from_date: str, to_date: str, segment: str = "indices", candle_interval: str = "5"
) -> pd.DataFrame:
    """
    Fetch OHLCV candles from Dhan at the requested raw interval.
    Mixed/derived strategy timeframes are handled later inside indicator computation;
    this function should return the raw candles exactly as fetched from Dhan.
    """
    inst_info = INSTRUMENT_MAP.get(instrument)
    if not inst_info:
        raise Exception(f"Unknown instrument: {instrument}. Not found in instrument map.")

    from datetime import datetime as dt
    from datetime import timedelta

    from_dt = dt.strptime(from_date, "%Y-%m-%d")
    to_dt = dt.strptime(to_date, "%Y-%m-%d")
    day_span = (to_dt - from_dt).days

    # Auto-detect: if range exceeds Dhan intraday history window, use daily candles.
    use_daily = day_span > INTRADAY_MAX_DAYS
    effective_interval = "D" if use_daily else str(candle_interval)

    if use_daily:
        print(
            f"[DATA] ⚠️  Date range is {day_span} days (>{INTRADAY_MAX_DAYS}d). "
            f"Auto-switching to DAILY candles for full coverage."
        )

    print(
        f"[DATA] Instrument={instrument} ({inst_info['name']}), DhanID={inst_info['dhan_id']}, "
        f"Segment={inst_info['dhan_seg']}, Interval={'Daily' if use_daily else f'{effective_interval}m raw'}, "
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

    # Intraday candles — chunk into 90-day windows
    # Dhan rate limit: ~10 requests/second. We add delay + retry on 429.
    import time as _time

    CHUNK_DAYS = INTRADAY_CHUNK_DAYS
    RATE_LIMIT_DELAY = 0.5  # seconds between API calls
    MAX_RETRIES = 3  # retry on 429 rate-limit errors
    all_dfs = []
    chunk_start = from_dt
    chunk_num = 0
    last_error = None

    while chunk_start <= to_dt:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), to_dt)
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
        f"[DATA] ✅ Total: {len(df)} {'daily' if use_daily else f'{effective_interval}m raw'} candles across {chunk_num} chunks, "
        f"{df.index[0]} → {df.index[-1]}"
    )
    return df


def _format_rolling_strike(offset_steps: int) -> str:
    if offset_steps == 0:
        return "ATM"
    sign = "+" if offset_steps > 0 else "-"
    return f"ATM{sign}{abs(offset_steps)}"


_OPTION_HISTORY_CACHE_DIR = os.getenv(
    "ALGOFORGE_OPTION_HISTORY_CACHE_DIR", os.path.join(_HERE, "data", "option_history_cache")
)
_OPTION_REAL_DATA_MAX_DAYS = 730


def _option_history_cache_path(history_key: str) -> str:
    digest = hashlib.sha256(history_key.encode("utf-8")).hexdigest()
    os.makedirs(_OPTION_HISTORY_CACHE_DIR, exist_ok=True)
    return os.path.join(_OPTION_HISTORY_CACHE_DIR, f"{digest}.csv")


def _load_option_history_cache(history_key: str) -> pd.DataFrame:
    path = _option_history_cache_path(history_key)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df[~df.index.duplicated(keep="first")]
    except Exception as exc:
        print(f"[BACKTEST] ⚠️  Failed to read option cache {path}: {exc}")
        return pd.DataFrame()


def _save_option_history_cache(history_key: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    path = _option_history_cache_path(history_key)
    try:
        cache_df = df.sort_index()
        cache_df.to_csv(path, index_label="timestamp")
    except Exception as exc:
        print(f"[BACKTEST] ⚠️  Failed to write option cache {path}: {exc}")


def _resolve_rolling_strike_alias(leg: dict, strike_step: int, max_offset: int) -> tuple[str | None, str | None]:
    strike_type = str(leg.get("strike_type", "atm") or "atm").lower()
    strike_value = float(leg.get("strike_value", 0) or 0)
    option_type = str(leg.get("option_type", "CE") or "CE").upper()

    if strike_type == "atm":
        return "ATM", None

    if strike_type in ("otm", "itm"):
        offset_steps = round_half_up(abs(strike_value) / strike_step) if strike_step > 0 else 0
        if offset_steps == 0:
            return "ATM", None
        signed_steps = offset_steps if strike_type == "otm" else -offset_steps
        if option_type == "PE":
            signed_steps *= -1
        if abs(signed_steps) > max_offset:
            return None, f"rolling options support up to ATM±{max_offset}, requested {strike_type} {offset_steps}"
        return _format_rolling_strike(signed_steps), None

    if strike_type == "spot_price":
        offset_steps = round_half_up(strike_value / strike_step) if strike_step > 0 else 0
        if abs(offset_steps) > max_offset:
            return None, f"rolling options support up to ATM±{max_offset}, requested spot offset {offset_steps}"
        return _format_rolling_strike(offset_steps), None

    if strike_type == "strike_price":
        return None, "fixed strike backtests are not representable on Dhan rolling options API"
    if strike_type in ("premium_near", "premium_above", "premium_below"):
        return None, "premium-target strike selection is not representable on Dhan rolling options API"
    return None, f"unsupported strike type '{strike_type}' for historical option pricing"


def _resample_option_history(df: pd.DataFrame, timeframe_minutes: int) -> pd.DataFrame:
    if df is None or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    agg_map = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in df.columns:
        agg_map["volume"] = "sum"
    for field in ("oi", "iv", "strike", "spot"):
        if field in df.columns:
            agg_map[field] = "last"

    return (
        df.sort_index()
        .resample(
            f"{timeframe_minutes}min",
            label="left",
            closed="left",
            origin="start_day",
            offset="15min",
        )
        .agg(agg_map)
        .dropna(subset=["open"])
    )


def _fetch_backtest_option_histories(strategy_config: dict, tf_spec, from_date: str, to_date: str) -> dict:
    legs = strategy_config.get("legs") or []
    option_legs = [leg for leg in legs if leg.get("option_type") in ("CE", "PE")]
    allow_synthetic = bool(strategy_config.get("allow_synthetic_option_fallback", False))
    pricing_info = {
        "historical_legs": 0,
        "synthetic_legs": len(option_legs),
        "allow_synthetic": allow_synthetic,
        "warnings": [],
        "errors": [],
    }
    if not option_legs:
        return pricing_info

    if tf_spec.requested <= 0:
        target = pricing_info["warnings"] if allow_synthetic else pricing_info["errors"]
        target.append("Invalid timeframe for option history fetch.")
        return pricing_info

    inst_info = INSTRUMENT_MAP.get(strategy_config.get("instrument", ""))
    if not inst_info:
        target = pricing_info["warnings"] if allow_synthetic else pricing_info["errors"]
        target.append("Unknown instrument for option history fetch.")
        return pricing_info

    if str(tf_spec.requested).upper() == "D":
        target = pricing_info["warnings"] if allow_synthetic else pricing_info["errors"]
        target.append("Daily option candles are not available on Dhan rolling options API.")
        return pricing_info

    option_exchange_segment = "BSE_FNO" if strategy_config.get("instrument") == "1" else "NSE_FNO"
    option_instrument_type = "OPTIDX" if inst_info["dhan_type"] == "INDEX" else "OPTSTK"
    strike_step = get_strike_step(strategy_config.get("instrument", "26000"))
    max_offset = 10 if option_instrument_type == "OPTIDX" else 3

    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    requested_days = max(1, (to_dt - from_dt).days + 1)
    history_cache = {}

    if requested_days >= _OPTION_REAL_DATA_MAX_DAYS:
        pricing_info["allow_synthetic"] = True
        pricing_info["warnings"].append(
            f"Date range is {requested_days} days (>= {_OPTION_REAL_DATA_MAX_DAYS}); using synthetic option pricing by rule."
        )
        strategy_config["_option_history"] = {}
        return pricing_info

    import time as _time

    for leg_index, leg in enumerate(legs):
        if leg.get("option_type") not in ("CE", "PE"):
            continue

        expiry_selection = str(leg.get("expiry") or "current_week")
        expiry_params = ROLLING_EXPIRY_SELECTIONS.get(expiry_selection)
        if not expiry_params:
            target = pricing_info["warnings"] if allow_synthetic else pricing_info["errors"]
            target.append(
                f"Leg {leg_index + 1}: expiry '{expiry_selection}' is not supported for rolling option history."
            )
            continue

        strike_alias, reason = _resolve_rolling_strike_alias(leg, strike_step, max_offset)
        if not strike_alias:
            target = pricing_info["warnings"] if allow_synthetic else pricing_info["errors"]
            target.append(f"Leg {leg_index + 1}: {reason}.")
            continue

        expiry_flag, expiry_code = expiry_params
        option_side = "CALL" if str(leg.get("option_type", "CE")).upper() == "CE" else "PUT"
        history_key = (
            f"{inst_info['dhan_id']}|{option_exchange_segment}|{option_instrument_type}|"
            f"{expiry_flag}|{expiry_code}|{strike_alias}|{option_side}|{tf_spec.fetch}"
        )

        if history_key not in history_cache:
            cached_raw = _load_option_history_cache(history_key)
            all_dfs = [cached_raw] if cached_raw is not None and not cached_raw.empty else []
            chunk_start = from_dt
            last_error = None
            cache_covers_range = (
                cached_raw is not None
                and not cached_raw.empty
                and cached_raw.index.min() <= from_dt
                and cached_raw.index.max() >= (to_dt + timedelta(hours=23, minutes=59))
            )
            if not cache_covers_range:
                while chunk_start <= to_dt:
                    chunk_end_exclusive = min(
                        chunk_start + timedelta(days=ROLLING_OPTION_CHUNK_DAYS), to_dt + timedelta(days=1)
                    )
                    try:
                        df_chunk = dhan.get_rolling_option_data(
                            security_id=inst_info["dhan_id"],
                            exchange_segment=option_exchange_segment,
                            instrument_type=option_instrument_type,
                            expiry_flag=expiry_flag,
                            expiry_code=expiry_code,
                            strike=strike_alias,
                            option_type=option_side,
                            from_date=chunk_start.strftime("%Y-%m-%d"),
                            to_date=chunk_end_exclusive.strftime("%Y-%m-%d"),
                            interval=str(tf_spec.fetch),
                        )
                        if df_chunk is not None and not df_chunk.empty:
                            all_dfs.append(df_chunk)
                    except Exception as exc:
                        last_error = str(exc)
                        break
                    _time.sleep(0.25)
                    chunk_start = chunk_end_exclusive

            if all_dfs:
                df_hist_raw = pd.concat(all_dfs).sort_index()
                df_hist_raw = df_hist_raw[~df_hist_raw.index.duplicated(keep="first")]
                _save_option_history_cache(history_key, df_hist_raw)
                coverage_end = to_dt + timedelta(days=1)
                df_hist_raw = df_hist_raw[(df_hist_raw.index >= from_dt) & (df_hist_raw.index < coverage_end)]
                df_hist_exec = df_hist_raw
                if tf_spec.derived:
                    df_hist_exec = _resample_option_history(df_hist_raw, tf_spec.requested)
                history_cache[history_key] = {
                    "raw": df_hist_raw,
                    "execution": df_hist_exec,
                }
            else:
                history_cache[history_key] = pd.DataFrame()
                warning = (
                    f"Leg {leg_index + 1}: no rolling option data returned for {strike_alias} {option_side} "
                    f"({expiry_selection})."
                )
                if last_error:
                    warning += f" Last error: {last_error}"
                target = pricing_info["warnings"] if allow_synthetic else pricing_info["errors"]
                target.append(warning)

        df_hist = history_cache.get(history_key)
        if isinstance(df_hist, dict):
            df_hist = df_hist.get("execution")
        if df_hist is None or df_hist.empty:
            continue

        leg["_bt_option_history_key"] = history_key
        leg["_bt_option_pricing"] = "historical"
        leg["_bt_option_history_label"] = strike_alias
        pricing_info["historical_legs"] += 1

    pricing_info["synthetic_legs"] = max(0, len(option_legs) - pricing_info["historical_legs"])
    strategy_config["_option_history"] = history_cache
    return pricing_info


# ── Backtest ──────────────────────────────────────────────────────
@app.post("/api/backtest")
async def api_run_backtest(payload: StrategyPayload, request: Request):
    try:
        from_date = payload.from_date or config.DEFAULT_FROM
        to_date = payload.to_date or config.DEFAULT_TO

        try:
            tf_spec = resolve_strategy_timeframe(payload.indicators)
        except ValueError as tf_err:
            return {"status": "error", "message": str(tf_err)}
        candle_interval = str(tf_spec.fetch)

        print(f"\n{'=' * 60}")
        print(f"[BACKTEST] Run: {payload.run_name}")
        print(f"[BACKTEST] Instrument: {payload.instrument}, Segment: {payload.segment}")
        print(f"[BACKTEST] Timeframe: {describe_timeframe(tf_spec)}")
        print(f"[BACKTEST] Indicators: {payload.indicators}")
        print(f"[BACKTEST] Entry conditions: {payload.entry_conditions}")
        print(f"[BACKTEST] Exit conditions: {payload.exit_conditions}")
        print(f"[BACKTEST] Legs: {payload.legs}")
        print(f"{'=' * 60}")

        # 1. Fetch data with segment-aware routing + fallback
        print(f"[BACKTEST] Fetching data from {from_date} to {to_date}...")
        try:
            df_raw = await asyncio.to_thread(
                _fetch_data,
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
        timeframe_warning = derived_timeframe_warning(tf_spec)
        from datetime import datetime as _dtw

        _from_dt = _dtw.strptime(from_date, "%Y-%m-%d")
        _to_dt = _dtw.strptime(to_date, "%Y-%m-%d")
        _day_span = (_to_dt - _from_dt).days
        if _day_span > INTRADAY_MAX_DAYS:
            data_range_warning = (
                f"📊 Date range is {_day_span} days — automatically using DAILY candles "
                f"for full {from_date} → {to_date} coverage. "
                f"(Dhan intraday history is limited to about 5 years. Daily candles go back further.)"
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
        strategy_config["timeframe_minutes"] = tf_spec.requested
        strategy_config["fetch_timeframe_minutes"] = tf_spec.fetch
        requested_days = max(1, (_to_dt - _from_dt).days + 1)
        strategy_config["allow_synthetic_option_fallback"] = requested_days >= _OPTION_REAL_DATA_MAX_DAYS
        option_pricing = _fetch_backtest_option_histories(strategy_config, tf_spec, from_date, to_date)
        if option_pricing["errors"]:
            error_msg = "Historical option data unavailable for this backtest:\n- " + "\n- ".join(
                option_pricing["errors"]
            )
            print(f"[BACKTEST] ❌ {error_msg}")
            return {
                "status": "error",
                "message": error_msg,
                "option_pricing": option_pricing,
            }
        if option_pricing["historical_legs"] > 0:
            print(
                f"[BACKTEST] Option pricing: stored/historical candles for {option_pricing['historical_legs']} leg(s)"
            )
        elif any((leg or {}).get("option_type") in ("CE", "PE") for leg in (payload.legs or [])):
            if strategy_config["allow_synthetic_option_fallback"]:
                print(
                    f"[BACKTEST] ⚠️  Option pricing: synthetic-only by range rule "
                    f"({requested_days} days >= {_OPTION_REAL_DATA_MAX_DAYS})"
                )
            else:
                print("[BACKTEST] ⚠️  Option pricing: no usable historical option data")
        for warning in option_pricing["warnings"]:
            print(f"[BACKTEST] ⚠️  {warning}")

        # 3. Run backtest
        print("[BACKTEST] Running backtest engine...")
        try:
            results = await asyncio.to_thread(
                run_backtest,
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
            run_entry = {
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
                "max_daily_loss": getattr(payload, "max_daily_loss", 0),
                "initial_capital": getattr(payload, "initial_capital", config.DEFAULT_CAPITAL),
                "combined_sl_rupees": getattr(payload, "combined_sl_rupees", 0),
                "combined_target_rupees": getattr(payload, "combined_target_rupees", 0),
                "combined_sqoff_time": getattr(payload, "combined_sqoff_time", "15:20") or "15:20",
                "fee_pct": getattr(payload, "fee_pct", 0.0),
                "trailing_sl_pct": getattr(payload, "trailing_sl_pct", 0.0),
                "execution_profile": getattr(payload, "execution_profile", "auto"),
                "spread_bps": getattr(payload, "spread_bps", 0.0),
                "entry_slippage_bps": getattr(payload, "entry_slippage_bps", 0.0),
                "exit_slippage_bps": getattr(payload, "exit_slippage_bps", 0.0),
                "entry_delay_candles": getattr(payload, "entry_delay_candles", 0),
                "signal_exit_delay_candles": getattr(payload, "signal_exit_delay_candles", 0),
                "enforce_capital": getattr(payload, "enforce_capital", False),
                "capital_buffer_pct": getattr(payload, "capital_buffer_pct", 0.0),
                "sell_option_margin_per_lot": getattr(payload, "sell_option_margin_per_lot", 0.0),
                "deploy_config": getattr(payload, "deploy_config", None),
                "option_pricing": {
                    "historical_legs": option_pricing["historical_legs"],
                    "synthetic_legs": option_pricing["synthetic_legs"],
                },
                "option_pricing_warnings": option_pricing["warnings"],
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
            saved_run = await _db_mod.create_run_record(_request_user_id(request), run_entry)
            results["run_id"] = saved_run["id"]
            print(f"[BACKTEST] Saved as Run #{saved_run['id']}")

        if data_range_warning:
            results["data_range_warning"] = data_range_warning
        if timeframe_warning:
            results["timeframe_warning"] = timeframe_warning
        if option_pricing["warnings"]:
            results["option_pricing_warnings"] = option_pricing["warnings"]
        results["option_pricing"] = {
            "historical_legs": option_pricing["historical_legs"],
            "synthetic_legs": option_pricing["synthetic_legs"],
        }
        results["timeframe_info"] = {
            "requested_minutes": tf_spec.requested,
            "fetch_minutes": tf_spec.fetch,
            "derived": tf_spec.derived,
            "all_frames": list(tf_spec.all_frames),
        }

        return results

    except Exception as e:
        import traceback

        error_msg = f"Backtest failed: {str(e)}"
        print(f"[BACKTEST] FATAL ERROR: {error_msg}")
        traceback.print_exc()
        return {"status": "error", "message": error_msg, "details": str(e)}


# ── Live Engine ───────────────────────────────────────────────────
@app.post("/api/live/start")
async def live_start(req: LiveStartRequest, request: Request):
    """Start live auto-trading with full strategy configuration."""
    user_id = _request_user_id(request)
    user = getattr(request.state, "current_user", None) or await _auth_mod.get_current_user(request)
    broker_client, broker_source = _resolve_user_broker_client(user, allow_admin_fallback=True)
    if not broker_client:
        return {"status": "error", "message": _broker_not_configured_message(user, broker_source)}
    live_bucket = _registry_bucket(live_engines, user_id)
    live_task_bucket = _registry_bucket(_live_tasks, user_id)
    stopped_engines = _load_stopped_engines(user_id)
    try:
        tf_spec = resolve_strategy_timeframe((req.strategy_config or {}).get("indicators", req.indicators))
    except ValueError as tf_err:
        return {"status": "error", "message": str(tf_err)}
    # Build strategy dict from the request
    strategy_dict = {}
    if req.strategy_config:
        strategy_dict = dict(req.strategy_config)
    else:
        strategy_dict = {
            "strategy_id": int(req.strategy_id or 0),
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
            "initial_capital": req.initial_capital,
            "execution_profile": req.execution_profile,
            "enforce_capital": req.enforce_capital,
            "capital_buffer_pct": req.capital_buffer_pct,
            "sell_option_margin_per_lot": req.sell_option_margin_per_lot,
            "poll_interval": 10,
        }
    strategy_dict["strategy_id"] = int(strategy_dict.get("strategy_id") or req.strategy_id or 0)
    strategy_dict["timeframe_minutes"] = tf_spec.requested
    strategy_dict["_user_id"] = user_id
    strategy_dict["fetch_timeframe_minutes"] = tf_spec.fetch

    deploy_config = req.deploy_config or strategy_dict.get("deploy_config", {})

    # Generate run_id from strategy name
    run_id = strategy_dict.get("run_name", "live") or "live"

    # Clear any stopped snapshot for this run_id
    stopped_engines.pop(run_id, None)
    _save_stopped_engines(user_id)

    # If an engine with same run_id exists, save its results before replacing
    old_engine = live_bucket.get(run_id)
    if old_engine:
        try:
            old_status = old_engine.get_status()
            if old_engine.running:
                old_engine.stop()
                task = live_task_bucket.pop(run_id, None)
                if task and not task.done():
                    task.cancel()
            await _save_live_run_to_history(old_status, explicit_user_id=getattr(old_engine, "_user_id", None))
        except Exception as e:
            print(f"[LIVE] Failed to save old engine {run_id}: {e}")
        live_bucket.pop(run_id, None)

    # Create a new engine instance for this strategy
    engine = LiveEngine(broker_client, run_id=run_id, state_dir=_engine_state_dir(user_id))
    engine.configure(
        strategy=strategy_dict,
        entry_conditions=req.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
        exit_conditions=req.exit_conditions or DEFAULT_EXIT_CONDITIONS,
        deploy_config=deploy_config,
    )
    engine._user_id = user_id

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
    # Preserve historical closed trades so "Completed Trades" panel shows past results
    engine.closed_trades = engine._load_trade_history() if hasattr(engine, "_load_trade_history") else []
    engine.in_trade = False
    engine.trades_today = 0

    _alert_state[_alert_state_key(user_id, run_id)] = {"in_trade": False, "closed_count": 0}

    async def broadcast(event: dict):
        await _broadcast_user_ws_json(user_id, {"source": "live", "run_id": run_id, **event})
        _check_trade_alerts(run_id, "Auto", event, user_id=user_id)
        # Save each closed trade to the user's run history for the Results page.
        if event.get("type") == "exit" and event.get("trade"):
            await _save_single_trade_to_history(event["trade"], "live", run_name=run_id, explicit_user_id=user_id)

    # Store engine and start task
    live_bucket[run_id] = engine
    live_task_bucket[run_id] = asyncio.create_task(engine.start(callback=broadcast))

    # Persist config + state immediately so it survives server restarts
    engine.session_date = date.today()
    engine._save_state()

    alerter.alert("Engine Started", f"Strategy: {run_id}\nMode: Auto (LIVE)", level="info")
    return {"status": "started", "run_id": run_id, "message": "Auto trading started with REAL orders"}


@app.post("/api/live/stop")
async def live_stop(request: Request):
    user_id = _request_user_id(request)
    live_bucket = _registry_bucket(live_engines, user_id)
    live_task_bucket = _registry_bucket(_live_tasks, user_id)
    stopped_engines = _load_stopped_engines(user_id)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    run_id = body.get("run_id", "")

    # If no run_id, stop the first (or only) running engine
    if not run_id:
        running = [rid for rid, e in live_bucket.items() if e.running]
        if running:
            run_id = running[0]
        else:
            return {"status": "not_running"}

    engine = live_bucket.get(run_id)
    if not engine:
        return {"status": "not_found", "run_id": run_id}

    # Capture results BEFORE stopping
    status_before = engine.get_status()

    engine.stop()
    task = live_task_bucket.pop(run_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    live_bucket.pop(run_id, None)

    # Delete state file so engine doesn't auto-restore on next startup
    engine._delete_state_file()

    # Persist live run to the user's history (same as paper)
    await _save_live_run_to_history(status_before, explicit_user_id=getattr(engine, "_user_id", None))

    # Keep snapshot on Live page so panel persists after stop
    status_before["running"] = False
    status_before["run_id"] = run_id
    status_before["mode"] = "auto"
    stopped_engines[run_id] = status_before
    _save_stopped_engines(user_id)
    _alert_state.pop(_alert_state_key(user_id, run_id), None)

    pnl = round(status_before.get("total_pnl", 0), 2)
    trades = len(status_before.get("closed_trades", []))
    alerter.alert(
        "Engine Stopped",
        f"Strategy: {run_id}\nMode: Auto (LIVE)\nTrades: {trades}\nTotal P&L: \u20b9{pnl:.2f}",
        level="warn",
    )

    return {"status": "stopped", "run_id": run_id}


@app.get("/api/live/status")
async def live_status(request: Request, run_id: str = ""):
    """Get live engine status. If run_id empty, returns first running engine."""
    user_id = _request_user_id(request)
    live_bucket = _registry_bucket(live_engines, user_id)
    if run_id and run_id in live_bucket:
        return live_bucket[run_id].get_status()
    # Return first running engine's status
    for rid, engine in live_bucket.items():
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
async def live_debug(request: Request, run_id: str = ""):
    """Deep diagnostic of live engine state — call when trades aren't triggering."""
    user_id = _request_user_id(request)
    live_bucket = _registry_bucket(live_engines, user_id)
    engine = None
    if run_id and run_id in live_bucket:
        engine = live_bucket[run_id]
    else:
        for e in live_bucket.values():
            if e.running:
                engine = e
                break
    if not engine:
        return {"error": "No live engine running", "engines": list(live_bucket.keys())}
    return engine.debug_engine_state()


@app.get("/api/live/trades/csv")
async def export_live_trades_csv(request: Request, run_id: str = ""):
    """Export live auto-trading trades to CSV"""
    import csv as csv_mod
    import io

    live_bucket = _registry_bucket(live_engines, _request_user_id(request))
    engine = live_bucket.get(run_id) if run_id else None
    if not engine:
        # Find first engine with trades
        for e in live_bucket.values():
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
async def paper_start(payload: StrategyPayload, request: Request):
    """Start paper trading with real live market data"""
    _crash_log = os.path.join(_HERE, "crash.log")
    with open(_crash_log, "a") as _f:
        _f.write(f"\n[PAPER] paper_start ENTERED at {datetime.now()}\n")
        _f.write(f"[PAPER] payload.instrument={payload.instrument}, run_name={payload.run_name}\n")
    try:
        return await _paper_start_impl(payload, _request_user_id(request))
    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        msg = f"[PAPER] paper_start crashed: {e}\n{tb}"
        print(msg, flush=True)
        _logger.error("[PAPER] paper_start crashed: %s\n%s", e, tb)
        with open(_crash_log, "a") as _f:
            _f.write(f"\n{'=' * 60}\n{msg}\n")
        raise


async def _paper_start_impl(payload: StrategyPayload, user_id: int):
    paper_bucket = _registry_bucket(paper_engines, user_id)
    paper_task_bucket = _registry_bucket(_paper_tasks, user_id)
    stopped_engines = _load_stopped_engines(user_id)
    try:
        tf_spec = resolve_strategy_timeframe(payload.indicators)
    except ValueError as tf_err:
        return {"status": "error", "message": str(tf_err)}
    # Configure strategy — pass ALL fields needed for SL/TP/strike logic
    strategy_dict = {
        "strategy_id": int(payload.strategy_id or 0),
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
        "initial_capital": payload.initial_capital,
        "execution_profile": payload.execution_profile,
        "spread_bps": payload.spread_bps,
        "entry_slippage_bps": payload.entry_slippage_bps,
        "exit_slippage_bps": payload.exit_slippage_bps,
        "enforce_capital": payload.enforce_capital,
        "capital_buffer_pct": payload.capital_buffer_pct,
        "sell_option_margin_per_lot": payload.sell_option_margin_per_lot,
        "max_daily_loss": payload.max_daily_loss,
        "combined_sqoff_time": payload.combined_sqoff_time,
        "timeframe_minutes": tf_spec.requested,
        "fetch_timeframe_minutes": tf_spec.fetch,
    }
    strategy_dict["_user_id"] = user_id

    # Generate run_id from strategy name
    run_id = strategy_dict.get("run_name", "paper") or "paper"

    # Clear any stopped snapshot for this run_id
    stopped_engines.pop(run_id, None)
    _save_stopped_engines(user_id)

    # If an engine with same run_id exists, save its results before replacing
    old_engine = paper_bucket.get(run_id)
    if old_engine:
        try:
            old_status = old_engine.get_status()
            if old_engine.running:
                old_engine.stop()
                task = paper_task_bucket.pop(run_id, None)
                if task and not task.done():
                    task.cancel()
            await _save_paper_run_to_history(old_status, explicit_user_id=getattr(old_engine, "_user_id", None))
        except Exception as e:
            print(f"[PAPER] Failed to save old engine {run_id}: {e}")
        paper_bucket.pop(run_id, None)

    # Create a new engine instance for this strategy
    engine = PaperTradingEngine(dhan, run_id=run_id, state_dir=_engine_state_dir(user_id))
    engine.configure(
        strategy=strategy_dict,
        entry_conditions=payload.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
        exit_conditions=payload.exit_conditions or DEFAULT_EXIT_CONDITIONS,
    )
    engine._user_id = user_id

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
    # Preserve historical closed trades so "Completed Trades" panel shows past results
    engine.closed_trades = engine._load_trade_history() if hasattr(engine, "_load_trade_history") else []
    engine.in_trade = False
    engine.trades_today = 0

    # Broadcast updates to WebSocket clients + Telegram alerts
    _alert_state[_alert_state_key(user_id, run_id)] = {"in_trade": False, "closed_count": 0}

    async def broadcast(event: dict):
        await _broadcast_user_ws_json(user_id, {"source": "paper", "run_id": run_id, **event})
        _check_trade_alerts(run_id, "Paper", event, user_id=user_id)
        # Save each closed trade to the user's run history for the Results page.
        if event.get("type") == "exit" and event.get("trade"):
            await _save_single_trade_to_history(event["trade"], "paper", run_name=run_id, explicit_user_id=user_id)

    # Store engine and start task
    paper_bucket[run_id] = engine
    paper_task_bucket[run_id] = asyncio.create_task(engine.start(callback=broadcast))

    alerter.alert("Engine Started", f"Strategy: {run_id}\nMode: Paper", level="info")
    return {"status": "started", "run_id": run_id, "message": "Paper trading started with LIVE market data"}


@app.post("/api/paper/stop")
async def paper_stop(request: Request):
    """Stop paper trading and persist results to runs.json"""
    user_id = _request_user_id(request)
    paper_bucket = _registry_bucket(paper_engines, user_id)
    paper_task_bucket = _registry_bucket(_paper_tasks, user_id)
    stopped_engines = _load_stopped_engines(user_id)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    run_id = body.get("run_id", "")

    # If no run_id, stop the first (or only) running engine
    if not run_id:
        running = [rid for rid, e in paper_bucket.items() if e.running]
        if running:
            run_id = running[0]
        else:
            return {"status": "not_running"}

    engine = paper_bucket.get(run_id)
    if not engine:
        return {"status": "not_found", "run_id": run_id}

    # Capture results BEFORE stopping (stop() may close positions)
    status_before = engine.get_status()

    engine.stop()

    task = paper_task_bucket.pop(run_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    paper_bucket.pop(run_id, None)

    # Delete state file so engine doesn't auto-restore on next startup
    engine._delete_state_file()

    # Save paper run to the user's history so it persists across restarts.
    await _save_paper_run_to_history(status_before, explicit_user_id=getattr(engine, "_user_id", None))

    # Keep snapshot on Live page so panel persists after stop
    status_before["running"] = False
    status_before["run_id"] = run_id
    status_before["mode"] = "paper"
    stopped_engines[run_id] = status_before
    _save_stopped_engines(user_id)
    _alert_state.pop(_alert_state_key(user_id, run_id), None)

    pnl = round(status_before.get("total_pnl", 0), 2)
    trades = len(status_before.get("closed_trades", []))
    alerter.alert(
        "Engine Stopped", f"Strategy: {run_id}\nMode: Paper\nTrades: {trades}\nTotal P&L: \u20b9{pnl:.2f}", level="warn"
    )

    return {"status": "stopped", "run_id": run_id}


@app.post("/api/paper/exit-position")
async def paper_exit_position(request: Request):
    """Force-exit an open position in a running paper engine."""
    user_id = _request_user_id(request)
    paper_bucket = _registry_bucket(paper_engines, user_id)
    body = await request.json()
    run_id = body.get("run_id", "")
    pos_index = body.get("position_index", 0)

    engine = paper_bucket.get(run_id)
    if not engine:
        # Try first running engine
        for rid, eng in paper_bucket.items():
            if eng.running:
                engine = eng
                run_id = rid
                break
    if not engine or not engine.running:
        return {"status": "error", "message": "No running paper engine found"}

    if pos_index >= len(engine.positions):
        return {"status": "error", "message": f"Position index {pos_index} out of range"}

    pos = engine.positions[pos_index]
    current_premium = pos.get("current_premium", pos.get("entry_premium", 0))
    engine._close_position(pos, "MANUAL_EXIT", current_premium)
    return {"status": "ok", "message": f"Position {pos.get('trading_symbol', pos.get('symbol', ''))} exited manually"}


@app.post("/api/live/exit-position")
async def live_exit_position(request: Request):
    """Force-exit an open position in a running live engine."""
    user_id = _request_user_id(request)
    live_bucket = _registry_bucket(live_engines, user_id)
    body = await request.json()
    run_id = body.get("run_id", "")
    pos_index = body.get("position_index", 0)

    engine = live_bucket.get(run_id)
    if not engine:
        for rid, eng in live_bucket.items():
            if eng.running:
                engine = eng
                run_id = rid
                break
    if not engine or not engine.running:
        return {"status": "error", "message": "No running live engine found"}

    if pos_index >= len(engine.positions):
        return {"status": "error", "message": f"Position index {pos_index} out of range"}

    pos = engine.positions[pos_index]
    current_premium = pos.get("current_premium", pos.get("entry_premium", 0))
    await engine._exit_position(pos, "MANUAL_EXIT", current_premium)
    return {"status": "ok", "message": f"Position {pos.get('trading_symbol', pos.get('symbol', ''))} exit order placed"}


def _history_trade_signature(trade: dict) -> dict | None:
    if not isinstance(trade, dict):
        return None
    return {
        "symbol": trade.get("symbol") or trade.get("trading_symbol") or "",
        "transaction_type": trade.get("transaction_type") or trade.get("side") or "",
        "option_type": trade.get("option_type") or "",
        "strike": trade.get("strike"),
        "entry_time": str(trade.get("entry_time") or ""),
        "exit_time": str(trade.get("exit_time") or ""),
        "entry_premium": round(float(trade.get("entry_premium") or trade.get("entry_price") or 0), 4),
        "exit_premium": round(float(trade.get("exit_premium") or trade.get("exit_price") or 0), 4),
        "quantity": trade.get("quantity") or trade.get("lots") or "",
        "pnl": round(float(trade.get("pnl") or 0), 4),
        "reason": trade.get("exit_reason") or trade.get("reason") or "",
    }


def _history_trade_counter(trades: list[dict]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for trade in trades or []:
        sig = _history_trade_signature(trade)
        if sig:
            counter[json.dumps(sig, sort_keys=True, default=str)] += 1
    return counter


def _history_run_signature(run: dict) -> tuple:
    mode = str(run.get("mode") or "")
    run_name = str(run.get("run_name") or run.get("strategy_name") or "")
    trades = run.get("trades") or []
    total_pnl = round(float(run.get("total_pnl") or 0), 2)
    normalized = [sig for trade in trades if (sig := _history_trade_signature(trade))]
    normalized.sort(
        key=lambda item: (
            item["entry_time"],
            item["exit_time"],
            item["symbol"],
            item["transaction_type"],
        )
    )
    return (
        mode,
        run_name,
        int(run.get("trade_count") or len(normalized)),
        total_pnl,
        json.dumps(normalized, sort_keys=True, default=str),
    )


async def _save_single_trade_to_history(
    trade: dict, mode: str, run_name: str = "", explicit_user_id: int | None = None
) -> None:
    """Save a single closed trade (paper/live) to user-scoped run history in real time."""
    try:
        user_id = await _resolve_history_user_id(explicit_user_id, trade)
        pnl = round(trade.get("pnl", 0), 2)
        instrument = trade.get("instrument", trade.get("symbol", ""))
        side = trade.get("side", trade.get("trade_side", ""))
        label = mode.title()
        name = run_name or f"{label} {instrument} {side}"
        run_entry = {
            "mode": mode,
            "run_name": name,
            "instrument": instrument,
            "status": "completed",
            "started_at": str(trade.get("entry_time", "")),
            "stopped_at": str(trade.get("exit_time", "")),
            "trade_count": 1,
            "total_pnl": pnl,
            "stats": {
                "total_trades": 1,
                "winning_trades": 1 if pnl > 0 else 0,
                "losing_trades": 1 if pnl <= 0 else 0,
                "win_rate": 100.0 if pnl > 0 else 0.0,
                "total_pnl": pnl,
            },
            "trades": [trade],
            "created_at": str(datetime.now()),
        }
        target_sig = _history_run_signature(run_entry)
        runs = await _db_mod.list_runs(user_id)
        if any(_history_run_signature(r) == target_sig for r in runs):
            print(f"[{mode.upper()}] Identical single-trade history already exists — skipping duplicate save")
            return
        saved = await _db_mod.create_run_record(user_id, run_entry)
        print(f"[{mode.upper()}] Saved trade to history as Run #{saved['id']}: {instrument} {side} P&L=₹{pnl}")
    except Exception as e:
        print(f"[{mode.upper()}] Failed to save trade to history: {e}")


async def _save_paper_run_to_history(status: dict, explicit_user_id: int | None = None):
    """Save a completed paper trading run to user-scoped history."""
    try:
        closed = status.get("closed_trades", [])
        if not closed:
            print("[PAPER] No closed trades — skipping history save")
            return

        user_id = await _resolve_history_user_id(explicit_user_id, status)
        run_name = status.get("strategy_name", "Paper Run")
        runs = await _db_mod.list_runs(user_id)
        existing = Counter()
        for run in runs:
            trade_count = int(run.get("trade_count") or len(run.get("trades") or []))
            if run.get("mode") != "paper" or run.get("run_name") != run_name or trade_count != 1:
                continue
            existing += _history_trade_counter(run.get("trades") or [])
        closed_sigs = _history_trade_counter(closed)
        if closed_sigs and all(existing[key] >= count for key, count in closed_sigs.items()):
            print(f"[PAPER] All {len(closed)} trades already saved individually — skipping bulk save")
            return

        total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
        winners = [t for t in closed if t.get("pnl", 0) > 0]
        losers = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = round(len(winners) / len(closed) * 100, 2) if closed else 0

        paper_run = {
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

        target_sig = _history_run_signature(paper_run)
        if any(_history_run_signature(r) == target_sig for r in runs):
            print("[PAPER] Identical completed run already exists — skipping duplicate history save")
            return

        saved = await _db_mod.create_run_record(user_id, paper_run)
        print(f"[PAPER] Saved run #{saved['id']} to history: {len(closed)} trades, P&L=₹{total_pnl}")
    except Exception as e:
        print(f"[PAPER] Failed to save run to history: {e}")


async def _save_scalp_run_to_history(eng, explicit_user_id: int | None = None) -> None:
    """Persist a completed scalp session to user-scoped run history."""
    try:
        status = eng.get_status()
        closed = status.get("closed_trades", [])
        if not closed:
            print("[SCALP] No closed trades — skipping history save")
            return

        user_id = await _resolve_history_user_id(explicit_user_id, status)
        total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
        winners = [t for t in closed if t.get("pnl", 0) > 0]
        losers = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = round(len(winners) / len(closed) * 100, 2) if closed else 0

        underlyings = list(dict.fromkeys(t.get("underlying", "") for t in closed if t.get("underlying")))
        run_name = "Scalp — " + ", ".join(underlyings) if underlyings else "Scalp Session"

        scalp_run = {
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

        saved = await _db_mod.create_run_record(user_id, scalp_run)
        print(f"[SCALP] Saved run #{saved['id']} to history: {len(closed)} trades, P&L=₹{total_pnl}")
    except Exception as e:
        print(f"[SCALP] Failed to save run to history: {e}")


async def _save_live_run_to_history(status: dict, explicit_user_id: int | None = None):
    """Save a completed live (auto) trading run to user-scoped history."""
    try:
        closed = status.get("closed_trades", [])
        if not closed:
            print("[LIVE] No closed trades — skipping history save")
            return

        user_id = await _resolve_history_user_id(explicit_user_id, status)
        run_name = status.get("strategy_name", "Live Run")
        runs = await _db_mod.list_runs(user_id)
        existing = Counter()
        for run in runs:
            trade_count = int(run.get("trade_count") or len(run.get("trades") or []))
            if run.get("mode") != "live" or run.get("run_name") != run_name or trade_count != 1:
                continue
            existing += _history_trade_counter(run.get("trades") or [])
        closed_sigs = _history_trade_counter(closed)
        if closed_sigs and all(existing[key] >= count for key, count in closed_sigs.items()):
            print(f"[LIVE] All {len(closed)} trades already saved individually — skipping bulk save")
            return

        total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)
        winners = [t for t in closed if t.get("pnl", 0) > 0]
        losers = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = round(len(winners) / len(closed) * 100, 2) if closed else 0

        live_run = {
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

        target_sig = _history_run_signature(live_run)
        if any(_history_run_signature(r) == target_sig for r in runs):
            print("[LIVE] Identical completed run already exists — skipping duplicate history save")
            return

        saved = await _db_mod.create_run_record(user_id, live_run)
        print(f"[LIVE] Saved run #{saved['id']} to history: {len(closed)} trades, P&L=₹{total_pnl}")
    except Exception as e:
        print(f"[LIVE] Failed to save run to history: {e}")


@app.get("/api/paper/status")
async def paper_status(request: Request, run_id: str = ""):
    """Get paper trading status. If run_id empty, returns first running engine."""
    user_id = _request_user_id(request)
    paper_bucket = _registry_bucket(paper_engines, user_id)
    if run_id and run_id in paper_bucket:
        return paper_bucket[run_id].get_status()

    # Return first running engine's status
    for rid, engine in paper_bucket.items():
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
        runs = await _db_mod.list_runs(_request_user_id(request))
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
async def engines_all(request: Request):
    """Return status of the current user's running engines for the Live page."""
    user_id = _request_user_id(request)
    engines = []
    stopped_engines = _load_stopped_engines(user_id)
    strategy_rows = await _db_mod.list_strategies(user_id)
    strategy_folder_map: dict[str, str] = {}
    strategy_by_id: dict[int, dict] = {}
    strategy_name_matches: dict[str, list[dict]] = defaultdict(list)
    for strategy in strategy_rows:
        strategy_name = str(strategy.get("run_name") or strategy.get("name") or "").strip().casefold()
        strategy_id = int(strategy.get("id") or 0)
        if strategy_id:
            strategy_by_id[strategy_id] = strategy
        if strategy_name and not strategy.get("_placeholder"):
            strategy_name_matches[strategy_name].append(strategy)
            strategy_folder_map[strategy_name] = str(strategy.get("folder") or "").strip() or "Intraday"

    def _attach_strategy_folder(status: dict) -> dict:
        if not isinstance(status, dict):
            return status
        strategy_payload = status.get("strategy") if isinstance(status.get("strategy"), dict) else None
        strategy_id = int(status.get("strategy_id") or (strategy_payload or {}).get("strategy_id") or 0)
        explicit_folder = str(status.get("folder") or (strategy_payload or {}).get("folder") or "").strip()
        strategy_name = str(
            status.get("strategy_name")
            or (strategy_payload or {}).get("run_name")
            or (strategy_payload or {}).get("name")
            or ""
        ).strip()
        matched_strategy = strategy_by_id.get(strategy_id) if strategy_id else None
        if not matched_strategy and strategy_name:
            matches = list(strategy_name_matches.get(strategy_name.casefold(), []))
            if explicit_folder:
                folder_key = (explicit_folder or "Intraday").strip().casefold()
                folder_matches = [
                    s for s in matches if (str(s.get("folder") or "").strip() or "Intraday").casefold() == folder_key
                ]
                if len(folder_matches) == 1:
                    matched_strategy = folder_matches[0]
            if not matched_strategy and len(matches) == 1:
                matched_strategy = matches[0]
        resolved_folder = explicit_folder
        if matched_strategy:
            resolved_folder = str(matched_strategy.get("folder") or "").strip() or "Intraday"
            status["strategy_id"] = int(matched_strategy.get("id") or strategy_id or 0)
            if strategy_payload is not None:
                strategy_payload.setdefault("strategy_id", status["strategy_id"])
        elif not resolved_folder and strategy_name:
            resolved_folder = strategy_folder_map.get(strategy_name.casefold(), "")
        if resolved_folder:
            status["folder"] = resolved_folder
            if strategy_payload is not None and not strategy_payload.get("folder"):
                strategy_payload["folder"] = resolved_folder
        return status

    # Add all paper engines
    for run_id, engine in _registry_bucket(paper_engines, user_id).items():
        if engine.running:
            st = _attach_strategy_folder(engine.get_status())
            st["run_id"] = run_id
            st["mode"] = "paper"
            engines.append(st)

    # Add all live engines
    for run_id, engine in _registry_bucket(live_engines, user_id).items():
        if engine.running:
            st = _attach_strategy_folder(engine.get_status())
            st["run_id"] = run_id
            st["mode"] = "auto"
            engines.append(st)

    # Add stopped engine snapshots (persisted panels)
    active_ids = {e["run_id"] for e in engines}
    for run_id, snapshot in stopped_engines.items():
        if run_id not in active_ids:
            engines.append(_attach_strategy_folder(snapshot))
            active_ids.add(run_id)

    # Fallback for migrated/admin sessions: if today's persisted engine state
    # exists but restore/stopped snapshots are absent, synthesize idle panels so
    # the Live page does not appear blank.
    if not engines:
        for snapshot in _state_file_snapshots(user_id):
            run_id = snapshot.get("run_id")
            if run_id and run_id not in active_ids:
                engines.append(_attach_strategy_folder(snapshot))
                active_ids.add(run_id)

    return {"engines": engines, "count": len(engines)}


@app.post("/api/engines/dismiss")
async def engines_dismiss(request: Request):
    """Remove a stopped engine snapshot from the Live page."""
    user_id = _request_user_id(request)
    stopped_engines = _load_stopped_engines(user_id)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    run_id = body.get("run_id", "")
    if run_id and run_id in stopped_engines:
        stopped_engines.pop(run_id)
        _save_stopped_engines(user_id)
        return {"status": "dismissed", "run_id": run_id}
    return {"status": "not_found", "run_id": run_id}


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
    # Authenticate WebSocket via session cookie (DB-backed)
    token = ws.cookies.get("algoforge_session", "")
    session = await _validate_session_async(token)
    if not session:
        await ws.close(code=4001, reason="Unauthorized")
        return
    user_id = int(session["user_id"])
    await ws.accept()
    _user_ws_clients(user_id).append(ws)

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
            scalp_engine = _scalp_engines.get(int(user_id))
            if _HAS_SCALP and scalp_engine is not None:
                try:
                    scalp_data = scalp_engine.get_status()
                except Exception:
                    pass

            payload = {"type": "status", "_ts": time.time()}

            if scalp_data is not None:
                payload["scalp"] = scalp_data

            # Engine status — every ~5s (20 × 250ms) to avoid waste
            engine_tick += 1
            if engine_tick >= 20:
                engine_tick = 0
                paper_sts = {
                    rid: e.get_status() for rid, e in _registry_bucket(paper_engines, user_id).items() if e.running
                }
                live_sts = {
                    rid: e.get_status() for rid, e in _registry_bucket(live_engines, user_id).items() if e.running
                }
                payload["paper_engines"] = paper_sts
                payload["live_engines"] = live_sts
                payload["paper_running"] = any(s.get("running") for s in paper_sts.values())
                payload["live_running"] = any(s.get("running") for s in live_sts.values())

            await ws.send_bytes(_ws_serialize(payload))
    except (WebSocketDisconnect, Exception):
        if ws in _user_ws_clients(user_id):
            _user_ws_clients(user_id).remove(ws)


# ── Orders / Positions / Funds ────────────────────────────────────
@app.post("/api/orders/place")
async def place_order(req: OrderRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit("place_order", ip, max_calls=3, window_sec=5)  # Max 3 orders per 5s per IP
    user, broker_client, source = await _request_broker_context(request)
    if not broker_client:
        raise HTTPException(status_code=400, detail=_broker_not_configured_message(user, source))
    try:
        return broker_client.place_order(
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
async def get_orders(request: Request):
    try:
        user, broker_client, source = await _request_broker_context(request)
        if not broker_client:
            return {"status": "not_configured", "message": _broker_not_configured_message(user, source), "data": []}
        orders = broker_client.get_order_book()
        return {"status": "success", "data": orders if isinstance(orders, list) else []}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100], "data": []}


@app.get("/api/positions")
async def get_positions(request: Request):
    try:
        user, broker_client, source = await _request_broker_context(request)
        if not broker_client:
            return {"status": "not_configured", "message": _broker_not_configured_message(user, source), "data": []}
        positions = broker_client.get_positions()
        return {"status": "success", "data": positions if isinstance(positions, list) else []}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100], "data": []}


@app.get("/api/funds")
async def get_funds(request: Request):
    user, broker_client, source = await _request_broker_context(request)
    if not broker_client:
        raise HTTPException(status_code=400, detail=_broker_not_configured_message(user, source))
    try:
        return broker_client.get_funds()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str, request: Request):
    user, broker_client, source = await _request_broker_context(request)
    if not broker_client:
        raise HTTPException(status_code=400, detail=_broker_not_configured_message(user, source))
    try:
        return broker_client.cancel_order(order_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Strategy CRUD ─────────────────────────────────────────────────
STRAT_FILE = "strategies.json"
RUNS_FILE = "runs.json"


async def _persist_daily_trades(trades: list, user_id: int):
    """Auto-save today's real Dhan trade P&L summary to SQLite.

    Only overwrites existing entry if the new data has MORE trade legs
    (i.e., more complete data from later in the day).
    """
    if not trades:
        return
    today_str = _ist_date_str()
    entry = _summarize_real_trade_fills(trades)
    if not entry:
        return
    trade_legs = entry.get("trade_legs", 0)

    # Only overwrite if new data has more trade legs (more complete)
    existing = await _db_mod.get_trade_history_entry(user_id, today_str) or {}
    existing_legs = existing.get("trade_legs", existing.get("trades", 0))
    if existing_legs > trade_legs or (
        str(existing.get("source") or "") == "historical_fifo" and existing_legs >= trade_legs
    ):
        print(f"[TRADE_HISTORY] Skipping update — existing has {existing_legs} legs vs new {trade_legs}")
        return

    # Preserve historical cost splits when live get_trades() still has zero charges.
    if entry.get("total_costs", 0) == 0 and existing.get("total_costs", 0) > 0:
        entry["charges"] = existing.get("charges", 0)
        entry["brokerage"] = existing.get("brokerage", 0)
        entry["total_costs"] = existing.get("total_costs", 0)
        entry["net_pnl"] = round(float(entry.get("pnl", 0) or 0) - float(entry["total_costs"] or 0), 2)
        # Also preserve per-trade costs where possible.
        for detail in entry.get("details", []):
            for old_detail in existing.get("details", []):
                if detail["symbol"] != old_detail.get("symbol"):
                    continue
                if detail.get("charges", 0) == 0:
                    detail["charges"] = old_detail.get("charges", 0)
                if detail.get("brokerage", 0) == 0:
                    detail["brokerage"] = old_detail.get("brokerage", 0)
                detail["total_costs"] = round(
                    float(detail.get("charges", 0) or 0) + float(detail.get("brokerage", 0) or 0),
                    2,
                )
                break

    await _db_mod.upsert_trade_history_entry(user_id, today_str, entry)
    print(
        "[TRADE_HISTORY] Saved "
        f"{today_str}: {entry.get('trades', 0)} trades ({trade_legs} fills), "
        f"P&L=₹{float(entry.get('pnl', 0) or 0):.2f}, "
        f"costs=₹{float(entry.get('total_costs', 0) or 0):.2f}"
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
async def get_strategies(request: Request):
    return await _db_mod.list_strategies(_request_user_id(request))


@app.post("/api/strategies")
async def save_strategy(strategy: dict, request: Request):
    now = str(datetime.now())
    strategy = {
        **strategy,
        "created_at": strategy.get("created_at") or now,
        "updated_at": strategy.get("updated_at") or now,
        "version": int(strategy.get("version", 1) or 1),
        "versions": strategy.get("versions") or [{"version": 1, "saved_at": now, "changes": "Initial save"}],
    }
    return await _db_mod.create_strategy_record(_request_user_id(request), strategy)


@app.post("/api/strategies/folders")
async def create_strategy_folder(body: dict, request: Request):
    folder = (body.get("folder") or "").strip()
    if not folder:
        raise HTTPException(status_code=400, detail="Folder name required")
    # Create a placeholder strategy so the folder persists
    now = str(datetime.now())
    placeholder = {
        "run_name": "",
        "folder": folder,
        "instrument": "",
        "legs": [],
        "entry_conditions": [],
        "exit_conditions": [],
        "created_at": now,
        "updated_at": now,
        "version": 1,
        "versions": [{"version": 1, "saved_at": now, "changes": "Folder created"}],
        "_placeholder": True,
    }
    result = await _db_mod.create_strategy_record(_request_user_id(request), placeholder)
    return {"status": "ok", "folder": folder, "id": result.get("id") if isinstance(result, dict) else None}


@app.delete("/api/strategies/{sid}")
async def delete_strategy(sid: int, request: Request):
    deleted = await _db_mod.delete_strategy_record(_request_user_id(request), sid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {"deleted": sid}


@app.put("/api/strategies/{sid}")
async def update_strategy(sid: int, updates: dict, request: Request):
    user_id = _request_user_id(request)
    strategy = await _db_mod.get_strategy(user_id, sid)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    ver = int(strategy.get("version", 1) or 1) + 1
    versions = list(strategy.get("versions", []))
    versions.append(
        {
            "version": ver,
            "saved_at": str(datetime.now()),
            "changes": updates.get("_change_note", f"Updated to v{ver}"),
        }
    )
    if len(versions) > 20:
        versions = versions[-20:]
    updates.pop("_change_note", None)
    strategy.update(updates)
    strategy["version"] = ver
    strategy["versions"] = versions
    strategy["updated_at"] = str(datetime.now())
    await _db_mod.replace_strategy_record(user_id, sid, strategy)
    return {"updated": sid}


# ── Backtest Runs CRUD ────────────────────────────────────────────
@app.get("/api/runs")
async def get_runs(request: Request):
    runs = await _db_mod.list_runs(_request_user_id(request))
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
    user_id = _request_user_id(request)
    body = await request.json()
    ids = body.get("ids", [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    deleted = await _db_mod.bulk_delete_run_records(user_id, ids)
    return {"deleted": deleted}


@app.post("/api/runs/cleanup-empty")
async def cleanup_empty_runs(request: Request):
    """Remove all 0-trade paper/live runs for the current user."""
    user_id = _request_user_id(request)
    removed = await _db_mod.cleanup_empty_runs(user_id)
    remaining = len(await _db_mod.list_runs(user_id))
    return {"removed": removed, "remaining": remaining}


@app.get("/api/runs/{rid}")
async def get_run(rid: int, request: Request):
    run = await _db_mod.get_run(_request_user_id(request), rid)
    if run:
        return run
    raise HTTPException(status_code=404, detail="Run not found")


@app.delete("/api/runs/{rid}")
async def delete_run(rid: int, request: Request):
    deleted = await _db_mod.delete_run_record(_request_user_id(request), rid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"deleted": rid}


@app.put("/api/runs/{rid}")
async def update_run(rid: int, request: Request):
    """Update run metadata (run_name, folder)."""
    user_id = _request_user_id(request)
    body = await request.json()
    run = await _db_mod.get_run(user_id, rid)
    if run:
        if "run_name" in body:
            run["run_name"] = str(body["run_name"]).strip()
        if "folder" in body:
            run["folder"] = str(body["folder"]).strip()
        await _db_mod.replace_run_record(user_id, rid, run)
        return {"updated": rid, "run_name": run.get("run_name"), "folder": run.get("folder")}
    raise HTTPException(status_code=404, detail="Run not found")


@app.get("/api/runs/{rid}/csv")
async def export_run_csv(rid: int, request: Request):
    """Export backtest trades to CSV"""
    import csv
    import io

    run = await _db_mod.get_run(_request_user_id(request), rid)
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


# ── Scalp Trades CRUD (SQLite-backed) ────────────────────────────
@app.get("/api/scalp/trades")
async def get_scalp_trades(request: Request):
    """Return all persisted closed scalp trades for the current user."""
    return await _db_mod.list_scalp_trades(_request_user_id(request))


@app.post("/api/scalp/trades/bulk-delete")
async def bulk_delete_scalp_trades(request: Request):
    """Bulk-delete scalp trades by trade_id list."""
    user_id = _request_user_id(request)
    body = await request.json()
    ids = body.get("ids", [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    deleted = await _db_mod.bulk_delete_scalp_trades(user_id, ids)
    eng = _scalp_engines.get(int(user_id))
    if eng is not None:
        id_set = {int(tid) for tid in ids}
        eng.closed_trades = [t for t in eng.closed_trades if t.get("trade_id") not in id_set]
    _notify_scalp_ws()
    return {"deleted": deleted}


@app.delete("/api/scalp/trades/{tid}")
async def delete_scalp_trade(tid: int, request: Request):
    """Delete a single persisted scalp trade by trade_id."""
    user_id = _request_user_id(request)
    await _db_mod.delete_scalp_trade(user_id, tid)
    eng = _scalp_engines.get(int(user_id))
    if eng is not None:
        eng.closed_trades = [t for t in eng.closed_trades if t.get("trade_id") != tid]
    _notify_scalp_ws()
    return {"deleted": tid}


# ── Scalp Engine (live session, in-memory) ───────────────────────


def _get_scalp_engine(user_id: int | None = None, broker_client: DhanClient | None = None):
    if not _HAS_SCALP:
        raise HTTPException(status_code=503, detail="scalp.py not available")
    owner_id = int(user_id or 0)
    if owner_id <= 0:
        raise HTTPException(status_code=400, detail="Missing scalp engine user context")
    eng = _scalp_engines.get(owner_id)
    if eng is None:

        async def _persist_closed_trade_async(owner_id: int, trade_dict: dict):
            try:
                await _db_mod.create_scalp_trade(owner_id, trade_dict)
            except Exception as e:
                print(f"[SCALP] Failed to persist closed trade for user {owner_id}: {e}")
            finally:
                _notify_scalp_ws()

        def _persist_closed_trade(trade_dict):
            if owner_id:
                asyncio.create_task(_persist_closed_trade_async(owner_id, trade_dict))
            else:
                print("[SCALP] Skipping closed-trade persistence — no owner user_id available")
            # Telegram alert for every scalp exit (manual, target, SL, sqoff)
            pnl = trade_dict.get("pnl", 0)
            sym = (
                f"{trade_dict.get('underlying', '?')} {trade_dict.get('strike', '')}{trade_dict.get('option_type', '')}"
            )
            reason = trade_dict.get("exit_reason", "unknown")
            entry_p = trade_dict.get("entry_premium", 0)
            exit_p = trade_dict.get("exit_premium", 0)
            pnl_sign = "+" if pnl >= 0 else ""
            level = "info" if pnl >= 0 else "error"
            alerter.alert(
                f"Scalp Exit [{reason}]",
                f"Symbol: {sym}\n"
                f"Entry: \u20b9{entry_p:.2f} \u2192 Exit: \u20b9{exit_p:.2f}\n"
                f"P&L: {pnl_sign}\u20b9{pnl:.2f}",
                level=level,
            )

        eng = _ScalpEngineClass(broker_client or dhan, _market_feed, on_trade_close=_persist_closed_trade)
        eng._user_id = owner_id
        eng._trade_counter = max(eng._trade_counter, _db_mod.get_max_scalp_trade_id_sync(owner_id))
        _scalp_engines[owner_id] = eng
    elif broker_client is not None:
        eng.dhan = broker_client
    return eng


@app.get("/api/scalp/status")
async def get_scalp_status(request: Request):
    user_id = _request_user_id(request)
    eng = _get_scalp_engine(user_id)
    status = eng.get_status()
    file_trades = await _db_mod.list_scalp_trades(user_id)
    status["file_trades"] = list(reversed(file_trades))
    return status


@app.post("/api/scalp/start")
async def start_scalp_engine(request: Request):
    eng = _get_scalp_engine(_request_user_id(request))
    eng.start()
    _notify_scalp_ws()
    return {"status": "started"}


@app.post("/api/scalp/stop")
async def stop_scalp_engine(request: Request):
    user_id = _request_user_id(request)
    eng = _get_scalp_engine(user_id)
    await _save_scalp_run_to_history(eng, explicit_user_id=user_id)
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
    entry_limit_price: float = 0.0
    entry_limit_max: float = 0.0


_scalp_entry_locks: Dict[int, asyncio.Lock] = {}
_last_scalp_entry_ts: Dict[int, float] = {}


def _get_scalp_entry_lock(user_id: int) -> asyncio.Lock:
    return _scalp_entry_locks.setdefault(int(user_id), asyncio.Lock())


@app.post("/api/scalp/entry")
async def scalp_entry(req: ScalpEntryReq, request: Request):
    user_id = _request_user_id(request)
    lock = _get_scalp_entry_lock(user_id)
    async with lock:
        # Cooldown guard INSIDE lock to prevent race condition
        now = asyncio.get_event_loop().time()
        last_ts = _last_scalp_entry_ts.get(user_id, 0.0)
        if now - last_ts < 2.0:
            return {"status": "error", "message": "Duplicate entry blocked — please wait 2 seconds between entries"}
        _last_scalp_entry_ts[user_id] = now
        broker_client = None
        if str(req.mode or "live").lower() == "live":
            user, broker_client, source = await _request_broker_context(request)
            if not broker_client:
                return {"status": "error", "message": _broker_not_configured_message(user, source)}
        eng = _get_scalp_engine(user_id, broker_client=broker_client)
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
                entry_limit_price=req.entry_limit_price,
                entry_limit_max=req.entry_limit_max,
            )
            if result.get("status") == "error":
                alerter.alert(
                    "Scalp Entry Failed",
                    f"Symbol: {req.underlying} {req.strike}{req.option_type}\nMode: {req.mode}\nError: {result.get('message', 'unknown')}",
                )
            else:
                trade_info = result.get("trade", {})
                entry_p = trade_info.get("entry_premium", 0)
                is_pending = trade_info.get("status") == "pending"
                if is_pending:
                    alerter.alert(
                        "Scalp Stop-Limit Pending",
                        f"Symbol: {req.underlying} {req.strike}{req.option_type}\n"
                        f"Side: {req.transaction_type} | Lots: {req.lots}\n"
                        f"Trigger: ₹{req.entry_limit_price:.2f}–₹{req.entry_limit_max:.2f} | Mode: {req.mode}",
                        level="info",
                    )
                else:
                    alerter.alert(
                        "Scalp Entry",
                        f"Symbol: {req.underlying} {req.strike}{req.option_type}\n"
                        f"Side: {req.transaction_type} | Lots: {req.lots}\n"
                        f"Entry: \u20b9{entry_p:.2f} | Mode: {req.mode}",
                        level="info",
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
async def scalp_exit(trade_id: int, request: Request):
    eng = _get_scalp_engine(_request_user_id(request))
    try:
        result = await eng.exit_trade(trade_id, reason="manual")
        if result.get("status") == "error":
            alerter.alert("Scalp Exit Failed", f"Trade ID: {trade_id}\nError: {result.get('message', 'unknown')}")
        _notify_scalp_ws()
        return result
    except Exception as e:
        alerter.alert("Scalp Exit Error", f"Trade ID: {trade_id}\nError: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/scalp/kill-all")
async def scalp_kill_all(request: Request):
    eng = _get_scalp_engine(_request_user_id(request))
    try:
        result = await eng.kill_all_trades()
        closed = result.get("closed", 0)
        if closed > 0:
            alerter.alert("Scalp Kill All", f"Emergency exit: {closed} trade(s) closed", level="warning")
        _notify_scalp_ws()
        return result
    except Exception as e:
        alerter.alert("Scalp Kill All Error", f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ScalpTargetsReq(BaseModel):
    target_premium: Optional[float] = None
    sl_premium: Optional[float] = None
    target_rupees: Optional[float] = None
    sl_rupees: Optional[float] = None
    sqoff_time: Optional[str] = None


@app.put("/api/scalp/trades/{trade_id}/targets")
async def update_scalp_targets(trade_id: int, req: ScalpTargetsReq, request: Request):
    eng = _get_scalp_engine(_request_user_id(request))
    result = await eng.update_trade_targets(trade_id, **{k: v for k, v in req.dict().items() if v is not None})
    _notify_scalp_ws()
    return result


@app.get("/api/option-ltp")
async def get_option_ltp(request: Request, underlying: str, strike: int, expiry: str, option_type: str):
    """Get live LTP for a specific option contract."""
    _, broker_client, _ = await _request_broker_context(request)
    if not broker_client:
        return {"status": "error", "message": "Broker not configured"}
    try:
        ltp = broker_client.get_option_ltp(underlying, strike, expiry, option_type)
        return {"status": "ok", "ltp": ltp}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/paper/trades/csv")
async def export_paper_trades_csv(request: Request, run_id: str = ""):
    """Export paper trading trades to CSV"""
    import csv
    import io

    paper_bucket = _registry_bucket(paper_engines, _request_user_id(request))
    engine = paper_bucket.get(run_id) if run_id else None
    if not engine:
        # Find first engine with trades
        for e in paper_bucket.values():
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


def _ticker_json_response(payload: dict) -> JSONResponse:
    """Return ticker payloads with explicit no-store headers.

    The topbar ticker is time-sensitive and should never be served from a stale
    browser/intermediate cache after deploys or after-hours fallbacks.
    """

    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


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


def _get_prev_close(preferred_client=None):
    """Get previous trading-day close for indices. Cached per day.

    Prefer Dhan daily candles when a broker client is available; fall back to
    yfinance only if Dhan history cannot be fetched.
    """
    from datetime import date

    today = date.today()
    if _prev_close_cache["date"] == str(today) and _prev_close_cache["data"]:
        return _prev_close_cache["data"]

    result = {}

    if preferred_client and preferred_client._is_configured():
        try:
            from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            to_date = today.strftime("%Y-%m-%d")
            index_specs = {"nifty": "13", "sensex": "51"}
            for key, security_id in index_specs.items():
                df = preferred_client.get_historical_data(
                    security_id=security_id,
                    exchange_segment="IDX_I",
                    instrument_type="INDEX",
                    from_date=from_date,
                    to_date=to_date,
                    candle_type="D",
                )
                if df is None or df.empty:
                    continue
                df = df.sort_index()
                latest_close = float(df["close"].iloc[-1])
                latest_bar = df.index[-1]
                latest_bar_date = latest_bar.date() if hasattr(latest_bar, "date") else today
                if len(df) >= 2 and latest_bar_date >= today:
                    prev_close = float(df["close"].iloc[-2])
                else:
                    prev_close = latest_close
                result[key] = prev_close
                result[f"{key}_ltp"] = latest_close
            if result:
                _prev_close_cache["data"] = result
                _prev_close_cache["date"] = str(today)
                print(f"[TICKER] Prev close from Dhan daily candles (cached for today): {result}")
                return result
        except Exception as e:
            print(f"[TICKER] Dhan prev close fetch failed: {e}")

    try:
        import yfinance as yf

        for sym, key in [("^NSEI", "nifty"), ("^BSESN", "sensex")]:
            hist = yf.Ticker(sym).history(period="5d")
            hist = hist.dropna(subset=["Close"])
            if hist.empty:
                continue
            latest_close = float(hist["Close"].iloc[-1])
            latest_bar = hist.index[-1]
            latest_bar_date = latest_bar.date() if hasattr(latest_bar, "date") else today
            if len(hist) >= 2 and latest_bar_date >= today:
                prev_close = float(hist["Close"].iloc[-2])
            else:
                prev_close = latest_close
            result[key] = prev_close
            result[f"{key}_ltp"] = latest_close
        _prev_close_cache["data"] = result
        _prev_close_cache["date"] = str(today)
        print(f"[TICKER] Prev close from yfinance (cached for today): {result}")
        return result
    except Exception as e:
        print(f"[TICKER] Prev close fetch failed: {e}")
        return {}


def _is_cash_market_closed_ist() -> bool:
    """True outside normal Indian cash-market hours (used for after-hours ticker fallback)."""

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:
        return True
    current = now.time()
    return current < dt_time(9, 15) or current > dt_time(15, 30)


def _historical_price_snapshot(
    client,
    *,
    security_id: int | str,
    exchange_segment: str,
    instrument_type: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    """Return the latest close and change from Dhan historical candles."""

    try:
        df = client.get_historical_data(
            security_id=str(security_id),
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date,
            candle_type="D",
        )
    except Exception as e:
        print(f"[TICKER] Historical snapshot failed for {security_id} ({exchange_segment}/{instrument_type}): {e}")
        return {"price": 0.0, "change": 0.0, "pct": 0.0}

    if df is None or df.empty or "close" not in df:
        return {"price": 0.0, "change": 0.0, "pct": 0.0}

    df = df.sort_index()
    latest_close = float(df["close"].iloc[-1] or 0)
    if latest_close <= 0:
        return {"price": 0.0, "change": 0.0, "pct": 0.0}
    prev_close = float(df["close"].iloc[-2] or latest_close) if len(df) >= 2 else latest_close
    change = round(latest_close - prev_close, 2)
    pct = round(((latest_close - prev_close) / prev_close) * 100, 2) if prev_close > 0 else 0.0
    return {"price": round(latest_close, 2), "change": change, "pct": pct}


def _build_historical_ticker_payload(ticker_client, ticker_source: str, *, ce_sid=None, pe_sid=None) -> dict | None:
    """Build a topbar ticker payload from Dhan historical candles."""

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    index_specs = {
        "nifty": ("13", "IDX_I", "INDEX"),
        "banknifty": ("25", "IDX_I", "INDEX"),
        "midcpnifty": ("49", "IDX_I", "INDEX"),
        "sensex": ("51", "IDX_I", "INDEX"),
    }
    snapshots = {
        key: _historical_price_snapshot(
            ticker_client,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date,
        )
        for key, (security_id, exchange_segment, instrument_type) in index_specs.items()
    }
    if snapshots["nifty"]["price"] <= 0:
        return None

    atm_ce = {"price": 0.0, "change": 0.0, "pct": 0.0}
    atm_pe = {"price": 0.0, "change": 0.0, "pct": 0.0}
    if ce_sid:
        atm_ce = _historical_price_snapshot(
            ticker_client,
            security_id=ce_sid,
            exchange_segment="NSE_FNO",
            instrument_type="OPTIDX",
            from_date=from_date,
            to_date=to_date,
        )
    if pe_sid:
        atm_pe = _historical_price_snapshot(
            ticker_client,
            security_id=pe_sid,
            exchange_segment="NSE_FNO",
            instrument_type="OPTIDX",
            from_date=from_date,
            to_date=to_date,
        )

    vix_data = _fetch_nse_vix()
    vix_ltp = float(vix_data.get("price", 0) or 0)
    vix_prev = float(vix_data.get("prev_close", 0) or 0)
    v_chg = round(vix_ltp - vix_prev, 2) if vix_prev > 0 else 0
    v_pct = round(((vix_ltp - vix_prev) / vix_prev) * 100, 2) if vix_prev > 0 else 0

    return {
        "status": "ok",
        "source": "dhan_historical",
        "broker_source": ticker_source,
        "nifty": snapshots["nifty"],
        "banknifty": snapshots["banknifty"],
        "midcpnifty": snapshots["midcpnifty"],
        "sensex": snapshots["sensex"],
        "vix": {"price": round(vix_ltp, 2), "change": v_chg, "pct": v_pct},
        "atmCE": atm_ce,
        "atmPE": atm_pe,
    }


@app.get("/api/ticker")
async def get_ticker(request: Request):
    """Fetch live index + ATM prices — Dhan OHLC (single call), change% from yfinance prev close"""
    global _ticker_cache

    # Return cached data if still valid
    if _ticker_cache["data"] and (time.time() - _ticker_cache["timestamp"]) < _ticker_cache["ttl"]:
        return _ticker_json_response(_ticker_cache["data"])

    # ── PRIMARY: Dhan OHLC API (one call for LTP + ATM CE/PE) ──
    broker_client = None
    try:
        _, broker_client, _ = await _request_broker_context(request)
    except Exception as e:
        print(f"[TICKER] Broker context unavailable: {e}")

    ticker_clients = []
    if broker_client and broker_client._is_configured():
        ticker_clients.append(("user", broker_client))
    if dhan._is_configured() and dhan is not broker_client:
        ticker_clients.append(("global", dhan))

    market_closed = _is_cash_market_closed_ist()

    for ticker_source, ticker_client in ticker_clients:
        try:
            print(f"[TICKER] Fetching from Dhan OHLC API ({ticker_source})...")

            # Resolve ATM option security IDs FIRST (no API call)
            ce_sid, pe_sid, atm_strike = None, None, 0
            try:
                ScripMaster.ensure_loaded()
                expiry = ScripMaster.get_nearest_expiry("NIFTY")
                if expiry:
                    last_nifty = 0
                    if _ticker_cache["data"]:
                        last_nifty = _ticker_cache["data"].get("nifty", {}).get("price", 0)
                    if last_nifty <= 0 and market_closed:
                        prev = _get_prev_close(ticker_client)
                        last_nifty = prev.get("nifty_ltp", 0) or prev.get("nifty", 0)
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

            all_data = ticker_client.get_ohlc_multi(segments)

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

                # Dhan's after-hours prev-close can flatten change to 0.00.
                # Outside market hours, prefer yfinance previous close for NIFTY/SENSEX.
                prev = (
                    _get_prev_close(ticker_client)
                    if (_is_cash_market_closed_ist() or (nifty_ltp > 0 and n_chg == 0 and n_pct == 0))
                    else {}
                )

                def _chg_yf(ltp, key, fallback_chg=0, fallback_pct=0):
                    pc = prev.get(key, 0)
                    if pc > 0 and ltp > 0:
                        return round(ltp - pc, 2), round(((ltp - pc) / pc) * 100, 2)
                    return fallback_chg, fallback_pct

                if prev:
                    if _is_cash_market_closed_ist():
                        n_chg, n_pct = _chg_yf(nifty_ltp, "nifty", n_chg, n_pct)
                        s_chg, s_pct = _chg_yf(sensex_ltp, "sensex", s_chg, s_pct)
                    else:
                        if nifty_ltp > 0 and n_chg == 0 and n_pct == 0:
                            n_chg, n_pct = _chg_yf(nifty_ltp, "nifty", n_chg, n_pct)
                        if sensex_ltp > 0 and s_chg == 0 and s_pct == 0:
                            s_chg, s_pct = _chg_yf(sensex_ltp, "sensex", s_chg, s_pct)

                # VIX from NSE India (yfinance ^INDIAVIX delisted)
                vix_data = _fetch_nse_vix()
                vix_ltp = vix_data["price"]
                vix_prev = vix_data["prev_close"]
                v_chg = round(vix_ltp - vix_prev, 2) if vix_prev > 0 else 0
                v_pct = round(((vix_ltp - vix_prev) / vix_prev) * 100, 2) if vix_prev > 0 else 0

                result = {
                    "status": "ok",
                    "source": "dhan",
                    "broker_source": ticker_source,
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
                return _ticker_json_response(result)
            else:
                historical_result = _build_historical_ticker_payload(
                    ticker_client,
                    ticker_source,
                    ce_sid=ce_sid,
                    pe_sid=pe_sid,
                )
                if historical_result:
                    _ticker_cache["data"] = historical_result
                    _ticker_cache["timestamp"] = time.time()
                    print(
                        f"[TICKER] Historical fallback via {ticker_source}: "
                        f"NIFTY={historical_result['nifty']['price']}, "
                        f"SENSEX={historical_result['sensex']['price']}"
                    )
                    return _ticker_json_response(historical_result)
                print(f"[TICKER] Dhan returned 0 for NIFTY via {ticker_source} — trying next source...")
        except Exception as e:
            print(f"[TICKER] Dhan API failed via {ticker_source}: {type(e).__name__}: {str(e)[:100]}")
            historical_result = _build_historical_ticker_payload(
                ticker_client,
                ticker_source,
                ce_sid=ce_sid if "ce_sid" in locals() else None,
                pe_sid=pe_sid if "pe_sid" in locals() else None,
            )
            if historical_result:
                _ticker_cache["data"] = historical_result
                _ticker_cache["timestamp"] = time.time()
                print(
                    f"[TICKER] Historical fallback after error via {ticker_source}: "
                    f"NIFTY={historical_result['nifty']['price']}, "
                    f"SENSEX={historical_result['sensex']['price']}"
                )
                return _ticker_json_response(historical_result)

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
            return _ticker_json_response(result)

        print("[TICKER] yfinance also returned no data")
    except Exception as yf_err:
        print(f"[TICKER] yfinance fallback failed: {yf_err}")

    if _ticker_cache["data"]:
        stale = dict(_ticker_cache["data"])
        stale["stale"] = True
        print("[TICKER] Serving stale cached topbar data")
        return _ticker_json_response(stale)

    return _ticker_json_response({"status": "error", "msg": "No price data available from any source"})


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
        today = _ist_date_str()
        future = [e for e in expiries if e >= today]
        return {"status": "ok", "symbol": symbol, "expiries": future}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def _refresh_recent_charges(history: dict, user_id: int, broker_client: DhanClient | None = None):
    """Re-fetch today & yesterday from Dhan historical API to fill in charges.

    The live get_trades() endpoint doesn't return charge fields (stt, sebiTax etc).
    Once those trades appear in get_trade_history(), we can update charges.
    """
    import time as _time

    try:
        client = broker_client or dhan
        today = _now_ist()
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

        result = client.get_trade_history(from_date, to_date, 0)
        if not isinstance(result, list) or not result:
            print(f"📊 [CHARGES] No historical data available yet for {from_date} to {to_date}")
            return

        # Paginate to get all trades
        all_trades = list(result)
        page = 1
        while len(result) >= 20:  # Dhan page size
            _time.sleep(0.3)
            result = client.get_trade_history(from_date, to_date, page)
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
            entry = _summarize_real_trade_fills(day_trades)
            if entry and entry.get("charges", 0) > 0:
                history[date_str] = entry
                updated += 1
                print(
                    "📊 [CHARGES] Updated "
                    f"{date_str}: charges=₹{float(entry.get('charges', 0) or 0):.2f}, "
                    f"P&L=₹{float(entry.get('pnl', 0) or 0):.2f} "
                    f"({entry.get('trades', 0)} trades, {entry.get('trade_legs', 0)} fills)"
                )

        if updated > 0:
            for date_str in trades_by_date:
                entry = history.get(date_str)
                if entry:
                    _db_mod.upsert_trade_history_entry_sync(user_id, date_str, entry)
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


async def _bootstrap_token_renewal():
    """Refresh the startup token without blocking app readiness."""
    global _token_renewal_task
    try:
        await asyncio.to_thread(_generate_startup_token_once)
    except Exception as e:
        _logger.warning(f"[TokenManager] Startup token bootstrap failed: {e}")
    finally:
        if _token_renewal_task is None or _token_renewal_task.done():
            _token_renewal_task = asyncio.create_task(token_renewal_loop())
            print("🔄 [TokenManager] Background token renewal scheduled (every 12h)")


async def _backfill_in_background():
    """Run the blocking backfill in a thread so the event loop stays free."""
    global _backfill_state
    _backfill_state["status"] = "running"
    _backfill_state["message"] = "Fetching historical trades from Dhan..."
    loop = asyncio.get_event_loop()
    try:
        admin = await _get_preferred_admin_user()
        if not admin:
            raise RuntimeError("No admin user available for startup trade-history backfill")
        admin_id = int(admin["id"])
        broker_client, source = _resolve_user_broker_client(admin, allow_admin_fallback=True)
        if not broker_client:
            raise RuntimeError(_broker_not_configured_message(admin, source))
        history = await _db_mod.list_trade_history(admin_id)
        force = len(history) <= 2
        refresh_from_date = "2024-01-01" if force else _trade_history_refresh_start(history, "2024-01-01")
        if force:
            _backfill_state["message"] = "First-run: full backfill in progress..."
            print("📊 [BACKFILL] Auto-backfilling trade history from Dhan (force)...")
        elif refresh_from_date != "2024-01-01":
            _backfill_state["message"] = f"Refreshing trade history from {refresh_from_date}..."
        count = await loop.run_in_executor(
            None,
            lambda: _backfill_trade_history(
                refresh_from_date,
                force=force,
                user_id=admin_id,
                broker_client=broker_client,
            ),
        )
        if force:
            print(f"📊 [BACKFILL] Done — loaded {count} days of historical trades")
        else:
            loaded = await _db_mod.list_trade_history(admin_id)
            print(f"📊 [TRADE_HISTORY] {len(loaded)} days of trade data ({count} refreshed)")
        _backfill_state.update({"status": "done", "message": "Trade history up to date.", "new_dates": count})
    except Exception as e:
        print(f"📊 [BACKFILL] Startup backfill failed: {e}")
        _backfill_state.update({"status": "error", "message": str(e)})


# ── Prometheus instrumentation (must run before app starts) ────
if _PROMETHEUS_ENABLED:
    _PFI(app).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    _logger.info("[Prometheus] Metrics exposed at /metrics")


@app.on_event("startup")
async def _init_database():
    """Initialize SQLite database and auto-create admin user if needed."""
    await _db_mod.init_db()
    await _db_mod.cleanup_expired_sessions()
    admin = await _get_preferred_admin_user()
    if not admin:
        pin = _get_bootstrap_admin_password()
        if not pin:
            raise RuntimeError(
                "No admin account exists. Set ALGOFORGE_PIN or ALGOFORGE_PASSWORD for first-run bootstrap."
            )
        hashed = _auth_mod.hash_password(pin)
        uid = await _db_mod.create_user(config.ADMIN_USERNAME, hashed, role="admin")
        print(f"🔐 [Auth] Created admin user '{config.ADMIN_USERNAME}' (id={uid})")
    else:
        print(f"🔐 [Auth] Admin user '{admin['username']}' exists (id={admin['id']})")


@app.on_event("startup")
async def _start_token_renewal():
    if _SKIP_STARTUP_JOBS:
        print("🧪 [Startup] Skipping network-heavy startup jobs (ALGOFORGE_SKIP_STARTUP_JOBS=1)")
        return
    if config.AUTO_TOKEN_ENABLED:
        asyncio.create_task(_bootstrap_token_renewal())
        print("🔄 [TokenManager] Startup token bootstrap running in background")
    if _market_feed:
        print(f"⚡ [MarketFeed] WebSocket feed ready (dhanhq {'available' if HAS_DHAN_FEED else 'NOT available'})")
    # ── Pre-cache Scrip Master in background (non-blocking) ────
    asyncio.create_task(_prefetch_scrip_master())

    # Auto-backfill trade history — runs in a thread so startup returns instantly
    asyncio.create_task(_backfill_in_background())

    # Cleanup 0-trade paper/live entries left by prior deploys/restarts
    removed = await _db_mod.cleanup_empty_runs()
    if removed:
        print(f"🧹 [STARTUP] Removed {removed} empty 0-trade runs from history")

    # ── Auto-restore live engines from persisted state ────────
    asyncio.create_task(_restore_live_engines())

    # ── Auto-restore paper engines from persisted state ────────
    asyncio.create_task(_restore_paper_engines())


async def _restore_live_engines():
    """Scan for live_state_*.json files and re-start engines that were running."""
    import json as _json
    from datetime import date as date_type

    today = str(date_type.today())
    restored = 0

    for user_id, state_dir, fname, fpath in _iter_user_state_files("live_state_"):
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
            live_bucket = _registry_bucket(live_engines, user_id)
            live_task_bucket = _registry_bucket(_live_tasks, user_id)

            # Skip if an engine with this run_id already exists
            if run_id in live_bucket:
                print(f"🔄 [Restore] Engine '{run_id}' already running — skipping")
                continue

            # Reconstruct engine with full config
            user = await _db_mod.get_user_by_id(user_id)
            broker_client, broker_source = _resolve_user_broker_client(user, allow_admin_fallback=True)
            if not broker_client:
                print(
                    f"🔄 [Restore] Skipping live restore for user {user_id} / {fname}: "
                    f"{_broker_not_configured_message(user, broker_source)}"
                )
                continue

            engine = LiveEngine(broker_client, run_id=run_id, state_dir=state_dir)
            engine.configure(
                strategy=strategy,
                entry_conditions=entry_conditions or DEFAULT_ENTRY_CONDITIONS,
                exit_conditions=exit_conditions or DEFAULT_EXIT_CONDITIONS,
                deploy_config=deploy_config,
            )
            engine._user_id = int(strategy.get("_user_id") or user_id)

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

            async def broadcast(event: dict, _rid=run_id, _user_id=getattr(engine, "_user_id", None)):
                await _broadcast_user_ws_json(_user_id, {"source": "live", "run_id": _rid, **event})
                if event.get("type") == "exit" and event.get("trade"):
                    await _save_single_trade_to_history(
                        event["trade"],
                        "live",
                        run_name=_rid,
                        explicit_user_id=_user_id,
                    )

            live_bucket[run_id] = engine
            live_task_bucket[run_id] = asyncio.create_task(engine.start(callback=broadcast))
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

    today = str(date_type.today())
    restored = 0

    for user_id, state_dir, fname, fpath in _iter_user_state_files("paper_state_"):
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
            paper_bucket = _registry_bucket(paper_engines, user_id)
            paper_task_bucket = _registry_bucket(_paper_tasks, user_id)

            # Skip if already running
            if run_id in paper_bucket:
                print(f"🔄 [Restore] Paper engine '{run_id}' already running — skipping")
                continue

            engine = PaperTradingEngine(dhan, run_id=run_id, state_dir=state_dir)
            engine.configure(
                strategy=strategy,
                entry_conditions=entry_conditions or DEFAULT_ENTRY_CONDITIONS,
                exit_conditions=exit_conditions or DEFAULT_EXIT_CONDITIONS,
            )
            engine._user_id = int(strategy.get("_user_id") or user_id)

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

            async def broadcast(event: dict, _rid=run_id, _user_id=getattr(engine, "_user_id", None)):
                await _broadcast_user_ws_json(_user_id, {"source": "paper", "run_id": _rid, **event})
                if event.get("type") == "exit" and event.get("trade"):
                    await _save_single_trade_to_history(
                        event["trade"],
                        "paper",
                        run_name=_rid,
                        explicit_user_id=_user_id,
                    )

            paper_bucket[run_id] = engine
            paper_task_bucket[run_id] = asyncio.create_task(engine.start(callback=broadcast))
            restored += 1
            print(f"✅ [Restore] Paper engine '{run_id}' restored and started")

        except Exception as e:
            print(f"❌ [Restore] Failed to restore paper {fname}: {e}")

    if restored:
        print(f"🔄 [Restore] {restored} paper engine(s) auto-restored from saved state")


@app.on_event("shutdown")
async def _shutdown_cleanup():
    """Save all running engine results and clean up."""
    # Save all running scalp engines
    for owner_id, engine in list(_scalp_engines.items()):
        try:
            await _save_scalp_run_to_history(engine, explicit_user_id=owner_id)
            engine.stop()
            print(f"🛑 [Shutdown] Saved scalp engine: {owner_id}")
        except Exception as e:
            print(f"🛑 [Shutdown] Failed to save scalp engine {owner_id}: {e}")
    # Save all running paper engines
    for owner_id, run_id, engine in list(_iter_registry_items(paper_engines)):
        try:
            status = engine.get_status()
            if engine.running:
                engine.stop()
            await _save_paper_run_to_history(status, explicit_user_id=getattr(engine, "_user_id", None))
            print(f"🛑 [Shutdown] Saved paper engine: {owner_id}:{run_id}")
        except Exception as e:
            print(f"🛑 [Shutdown] Failed to save paper engine {owner_id}:{run_id}: {e}")
    # Save all running live engines (state file for auto-restore + runs.json for history)
    for owner_id, run_id, engine in list(_iter_registry_items(live_engines)):
        try:
            status = engine.get_status()
            if engine.running:
                engine.stop()  # stop() calls _save_state() internally
            await _save_live_run_to_history(status, explicit_user_id=getattr(engine, "_user_id", None))
            print(f"🛑 [Shutdown] Saved live engine: {owner_id}:{run_id}")
        except Exception as e:
            print(f"🛑 [Shutdown] Failed to save live engine {owner_id}:{run_id}: {e}")
    shutdown_feed()
    await alerter.shutdown()
    await _db_mod.close_db()
    print("🛑 [Shutdown] MarketFeed + DB closed")


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
