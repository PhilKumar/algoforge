from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import requests

from broker.dhan import SCRIP_CACHE_DIR, SCRIP_MASTER_URL, DhanClient

IST = ZoneInfo("Asia/Kolkata")
_REQUEST_TIMEOUT_SEC = 30
_SNAPSHOT_TTL_SEC = 20
_SCRIP_CACHE_FILE = os.path.join(SCRIP_CACHE_DIR, "nifty50_equities.json")

NIFTY_50_CONSTITUENTS = [
    {"symbol": "ADANIENT", "name": "Adani Enterprises Ltd.", "industry": "Metals & Mining"},
    {"symbol": "ADANIPORTS", "name": "Adani Ports and Special Economic Zone Ltd.", "industry": "Services"},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals Enterprise Ltd.", "industry": "Healthcare"},
    {"symbol": "ASIANPAINT", "name": "Asian Paints Ltd.", "industry": "Consumer Durables"},
    {"symbol": "AXISBANK", "name": "Axis Bank Ltd.", "industry": "Financial Services"},
    {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto Ltd.", "industry": "Automobile and Auto Components"},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance Ltd.", "industry": "Financial Services"},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv Ltd.", "industry": "Financial Services"},
    {"symbol": "BEL", "name": "Bharat Electronics Ltd.", "industry": "Capital Goods"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel Ltd.", "industry": "Telecommunication"},
    {"symbol": "CIPLA", "name": "Cipla Ltd.", "industry": "Healthcare"},
    {"symbol": "COALINDIA", "name": "Coal India Ltd.", "industry": "Oil Gas & Consumable Fuels"},
    {"symbol": "DRREDDY", "name": "Dr. Reddy's Laboratories Ltd.", "industry": "Healthcare"},
    {"symbol": "EICHERMOT", "name": "Eicher Motors Ltd.", "industry": "Automobile and Auto Components"},
    {"symbol": "ETERNAL", "name": "Eternal Ltd.", "industry": "Consumer Services"},
    {"symbol": "GRASIM", "name": "Grasim Industries Ltd.", "industry": "Construction Materials"},
    {"symbol": "HCLTECH", "name": "HCL Technologies Ltd.", "industry": "Information Technology"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "industry": "Financial Services"},
    {"symbol": "HDFCLIFE", "name": "HDFC Life Insurance Company Ltd.", "industry": "Financial Services"},
    {"symbol": "HINDALCO", "name": "Hindalco Industries Ltd.", "industry": "Metals & Mining"},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever Ltd.", "industry": "Fast Moving Consumer Goods"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank Ltd.", "industry": "Financial Services"},
    {"symbol": "ITC", "name": "ITC Ltd.", "industry": "Fast Moving Consumer Goods"},
    {"symbol": "INFY", "name": "Infosys Ltd.", "industry": "Information Technology"},
    {"symbol": "INDIGO", "name": "InterGlobe Aviation Ltd.", "industry": "Services"},
    {"symbol": "JSWSTEEL", "name": "JSW Steel Ltd.", "industry": "Metals & Mining"},
    {"symbol": "JIOFIN", "name": "Jio Financial Services Ltd.", "industry": "Financial Services"},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank Ltd.", "industry": "Financial Services"},
    {"symbol": "LT", "name": "Larsen & Toubro Ltd.", "industry": "Construction"},
    {"symbol": "M&M", "name": "Mahindra & Mahindra Ltd.", "industry": "Automobile and Auto Components"},
    {"symbol": "MARUTI", "name": "Maruti Suzuki India Ltd.", "industry": "Automobile and Auto Components"},
    {"symbol": "MAXHEALTH", "name": "Max Healthcare Institute Ltd.", "industry": "Healthcare"},
    {"symbol": "NTPC", "name": "NTPC Ltd.", "industry": "Power"},
    {"symbol": "NESTLEIND", "name": "Nestle India Ltd.", "industry": "Fast Moving Consumer Goods"},
    {"symbol": "ONGC", "name": "Oil & Natural Gas Corporation Ltd.", "industry": "Oil Gas & Consumable Fuels"},
    {"symbol": "POWERGRID", "name": "Power Grid Corporation of India Ltd.", "industry": "Power"},
    {"symbol": "RELIANCE", "name": "Reliance Industries Ltd.", "industry": "Oil Gas & Consumable Fuels"},
    {"symbol": "SBILIFE", "name": "SBI Life Insurance Company Ltd.", "industry": "Financial Services"},
    {"symbol": "SHRIRAMFIN", "name": "Shriram Finance Ltd.", "industry": "Financial Services"},
    {"symbol": "SBIN", "name": "State Bank of India", "industry": "Financial Services"},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical Industries Ltd.", "industry": "Healthcare"},
    {"symbol": "TCS", "name": "Tata Consultancy Services Ltd.", "industry": "Information Technology"},
    {"symbol": "TATACONSUM", "name": "Tata Consumer Products Ltd.", "industry": "Fast Moving Consumer Goods"},
    {"symbol": "TMPV", "name": "Tata Motors Passenger Vehicles Ltd.", "industry": "Automobile and Auto Components"},
    {"symbol": "TATASTEEL", "name": "Tata Steel Ltd.", "industry": "Metals & Mining"},
    {"symbol": "TECHM", "name": "Tech Mahindra Ltd.", "industry": "Information Technology"},
    {"symbol": "TITAN", "name": "Titan Company Ltd.", "industry": "Consumer Durables"},
    {"symbol": "TRENT", "name": "Trent Ltd.", "industry": "Consumer Services"},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement Ltd.", "industry": "Construction Materials"},
    {"symbol": "WIPRO", "name": "Wipro Ltd.", "industry": "Information Technology"},
]

_WEIGHT_OVERRIDES = {
    "HDFCBANK": 2.55,
    "RELIANCE": 2.45,
    "ICICIBANK": 2.35,
    "INFY": 2.15,
    "TCS": 2.1,
    "BHARTIARTL": 1.95,
    "ITC": 1.85,
    "LT": 1.8,
    "SBIN": 1.75,
    "HINDUNILVR": 1.7,
    "BAJFINANCE": 1.58,
    "SUNPHARMA": 1.5,
    "AXISBANK": 1.48,
    "KOTAKBANK": 1.46,
    "M&M": 1.42,
    "MARUTI": 1.38,
    "JIOFIN": 1.34,
    "ULTRACEMCO": 1.3,
    "ASIANPAINT": 1.28,
    "TITAN": 1.26,
    "ADANIPORTS": 1.24,
    "BAJAJFINSV": 1.22,
    "HCLTECH": 1.2,
    "NTPC": 1.18,
    "POWERGRID": 1.18,
    "ONGC": 1.16,
    "TATASTEEL": 1.15,
    "BEL": 1.14,
    "TATACONSUM": 1.12,
    "TRENT": 1.12,
    "INDIGO": 1.1,
    "MAXHEALTH": 1.08,
}

_CONSTITUENT_LOOKUP = {item["symbol"]: item for item in NIFTY_50_CONSTITUENTS}
_SNAPSHOT_CACHE: dict[str, Any] = {"timestamp": 0.0, "payload": None}
_SECURITY_MAP_CACHE: dict[str, Any] = {"loaded": False, "payload": {}}
_DAILY_BASELINE_CACHE: dict[str, Any] = {"key": "", "payload": {}}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _now_ist_iso() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


def _is_market_open_ist(now: datetime | None = None) -> bool:
    current = now or datetime.now(IST)
    if current.weekday() >= 5:
        return False
    current_time = current.time()
    return dt_time(9, 15) <= current_time <= dt_time(15, 30)


def _empty_snapshot(message: str, *, source: str = "unavailable", stale: bool = False) -> dict:
    items = [
        {
            "symbol": item["symbol"],
            "name": item["name"],
            "industry": item["industry"],
            "price": 0.0,
            "change": 0.0,
            "change_pct": 0.0,
            "volume": 0,
            "weight": _WEIGHT_OVERRIDES.get(item["symbol"], 1.0),
            "unavailable": True,
        }
        for item in NIFTY_50_CONSTITUENTS
    ]
    return {
        "status": "error",
        "message": message,
        "source": source,
        "stale": stale,
        "as_of": _now_ist_iso(),
        "items": items,
        "leaders": items[:5],
        "laggards": items[:5],
        "breadth": {"advancers": 0, "decliners": 0, "flat": len(items)},
    }


def _load_cached_security_map() -> dict[str, int]:
    try:
        with open(_SCRIP_CACHE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        raw_map = payload.get("security_map") or {}
        return {str(symbol): int(sec_id) for symbol, sec_id in raw_map.items() if str(sec_id).strip()}
    except Exception:
        return {}


def _save_cached_security_map(security_map: dict[str, int]) -> None:
    try:
        os.makedirs(SCRIP_CACHE_DIR, exist_ok=True)
        with open(_SCRIP_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"saved_at": _now_ist_iso(), "security_map": security_map}, f, indent=2)
    except Exception:
        pass


def _download_security_map() -> dict[str, int]:
    wanted = set(_CONSTITUENT_LOOKUP)
    security_map: dict[str, int] = {}

    resp = requests.get(
        SCRIP_MASTER_URL,
        timeout=_REQUEST_TIMEOUT_SEC,
        stream=True,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    reader = csv.DictReader((line.decode("utf-8", errors="replace") for line in resp.iter_lines() if line))
    for row in reader:
        symbol = str(row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
        if symbol not in wanted:
            continue
        if str(row.get("SEM_EXM_EXCH_ID") or "").strip().upper() != "NSE":
            continue
        if str(row.get("SEM_SEGMENT") or "").strip().upper() != "E":
            continue
        if str(row.get("SEM_INSTRUMENT_NAME") or "").strip().upper() != "EQUITY":
            continue
        if str(row.get("SEM_SERIES") or "").strip().upper() != "EQ":
            continue
        sec_id = int(float(str(row.get("SEM_SMST_SECURITY_ID") or "0").strip() or "0"))
        if sec_id > 0:
            security_map[symbol] = sec_id
        if len(security_map) == len(wanted):
            break

    if security_map:
        _save_cached_security_map(security_map)
    return security_map


def _get_security_map() -> dict[str, int]:
    if _SECURITY_MAP_CACHE.get("loaded"):
        return dict(_SECURITY_MAP_CACHE.get("payload") or {})

    cached = _load_cached_security_map()
    if len(cached) >= len(_CONSTITUENT_LOOKUP):
        _SECURITY_MAP_CACHE["loaded"] = True
        _SECURITY_MAP_CACHE["payload"] = dict(cached)
        return cached

    downloaded = {}
    try:
        downloaded = _download_security_map()
    except Exception:
        downloaded = {}

    merged = dict(cached)
    merged.update(downloaded)
    _SECURITY_MAP_CACHE["loaded"] = True
    _SECURITY_MAP_CACHE["payload"] = dict(merged)
    return merged


def _extract_quote_metrics(raw: dict | None) -> tuple[float, float, float, int]:
    raw = raw or {}
    price = _safe_float(
        raw.get("last_price") or raw.get("lastPrice") or raw.get("ltp") or raw.get("LTP"),
        0.0,
    )
    prev_close = 0.0
    ohlc = raw.get("ohlc") if isinstance(raw.get("ohlc"), dict) else {}
    if ohlc:
        prev_close = _safe_float(ohlc.get("close") or ohlc.get("prev_close") or ohlc.get("previous_close"), 0.0)
    change = _safe_float(raw.get("net_change") or raw.get("change") or raw.get("netChange"), 0.0)
    if abs(change) < 1e-9 and price > 0 and prev_close > 0:
        change = price - prev_close
    pct = _safe_float(
        raw.get("change_percentage")
        or raw.get("changePercent")
        or raw.get("percent_change")
        or raw.get("percentChange"),
        0.0,
    )
    if abs(pct) < 1e-9 and price > 0 and prev_close > 0:
        pct = (change / prev_close) * 100
    volume = int(
        round(
            _safe_float(
                raw.get("volume")
                or raw.get("day_volume")
                or raw.get("dayVolume")
                or raw.get("traded_volume")
                or raw.get("total_quantity_traded"),
                0.0,
            )
        )
    )
    return round(price, 2), round(change, 2), round(pct, 2), max(0, volume)


def _resolve_daily_baseline_from_frame(frame) -> tuple[float, float]:
    frame = frame.dropna(how="all")
    close_series = frame.get("Close") if hasattr(frame, "get") else None
    if close_series is None:
        close_series = frame.get("close") if hasattr(frame, "get") else None
    if close_series is None:
        return 0.0, 0.0
    close_series = close_series.dropna()
    if close_series.empty:
        return 0.0, 0.0

    latest_close = _safe_float(close_series.iloc[-1], 0.0)
    if latest_close <= 0:
        return 0.0, 0.0

    if len(close_series) < 2:
        return round(latest_close, 2), round(latest_close, 2)

    latest_index = close_series.index[-1]
    latest_bar_date = latest_index.date() if hasattr(latest_index, "date") else datetime.now(IST).date()
    today = datetime.now(IST).date()
    market_open = _is_market_open_ist()

    if market_open and latest_bar_date < today:
        prev_close = latest_close
    else:
        prev_close = _safe_float(close_series.iloc[-2], latest_close)

    return round(latest_close, 2), round(prev_close, 2)


def _get_daily_baselines_yfinance() -> dict[str, dict[str, float]]:
    import yfinance as yf

    symbols = [item["symbol"] for item in NIFTY_50_CONSTITUENTS]
    yf_symbols = [f"{symbol}.NS" for symbol in symbols]
    data = yf.download(
        tickers=" ".join(yf_symbols),
        period="10d",
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=True,
        group_by="ticker",
    )

    baselines: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        yf_symbol = f"{symbol}.NS"
        baseline = None
        try:
            frame = data[yf_symbol] if hasattr(data.columns, "levels") else data
            latest_close, prev_close = _resolve_daily_baseline_from_frame(frame)
            if latest_close > 0 and prev_close > 0:
                baseline = {"latest_close": latest_close, "prev_close": prev_close}
        except Exception:
            baseline = None
        if baseline:
            baselines[symbol] = baseline
    return baselines


def _get_daily_baselines_dhan(client: DhanClient, security_map: dict[str, int]) -> dict[str, dict[str, float]]:
    baselines: dict[str, dict[str, float]] = {}
    today = datetime.now(IST).date()
    from_date = (today - timedelta(days=10)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    for symbol, security_id in security_map.items():
        frame = None
        try:
            frame = client.get_historical_data(
                security_id=str(security_id),
                exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
                from_date=from_date,
                to_date=to_date,
                candle_type="D",
            )
        except Exception:
            frame = None
        if frame is None or getattr(frame, "empty", True):
            continue
        latest_close, prev_close = _resolve_daily_baseline_from_frame(frame)
        if latest_close > 0 and prev_close > 0:
            baselines[symbol] = {"latest_close": latest_close, "prev_close": prev_close}
    return baselines


def _get_daily_baselines(
    broker_client: DhanClient | None,
    security_map: dict[str, int],
) -> dict[str, dict[str, float]]:
    cache_key = f"{datetime.now(IST).date().isoformat()}:{'open' if _is_market_open_ist() else 'closed'}"
    if _DAILY_BASELINE_CACHE.get("key") == cache_key:
        return dict(_DAILY_BASELINE_CACHE.get("payload") or {})

    baselines: dict[str, dict[str, float]] = {}
    try:
        baselines = _get_daily_baselines_yfinance()
    except Exception:
        baselines = {}

    if broker_client and getattr(broker_client, "_is_configured", lambda: False)():
        missing = {symbol: sec_id for symbol, sec_id in security_map.items() if symbol not in baselines}
        if missing:
            try:
                baselines.update(_get_daily_baselines_dhan(broker_client, missing))
            except Exception:
                pass

    _DAILY_BASELINE_CACHE["key"] = cache_key
    _DAILY_BASELINE_CACHE["payload"] = dict(baselines)
    return baselines


def _ranked_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
    available = [item for item in items if not item.get("unavailable")]
    leaders = sorted(available, key=lambda item: (item["change_pct"], item["change"], item["symbol"]), reverse=True)[:6]
    laggards = sorted(available, key=lambda item: (item["change_pct"], item["change"], item["symbol"]))[:6]
    return leaders, laggards


def _finalize_payload(items: list[dict], *, source: str, message: str = "", stale: bool = False) -> dict:
    leaders, laggards = _ranked_items(items)
    advancers = sum(1 for item in items if item.get("change_pct", 0) > 0)
    decliners = sum(1 for item in items if item.get("change_pct", 0) < 0)
    flat = len(items) - advancers - decliners
    return {
        "status": "ok" if items else "error",
        "message": message,
        "source": source,
        "stale": stale,
        "as_of": _now_ist_iso(),
        "items": items,
        "leaders": leaders,
        "laggards": laggards,
        "breadth": {"advancers": advancers, "decliners": decliners, "flat": flat},
    }


def _dhan_snapshot(client: DhanClient) -> dict:
    security_map = _get_security_map()
    if not security_map:
        raise RuntimeError("No Dhan security map available for Nifty 50 equities")

    segments = {"NSE_EQ": list(security_map.values())}
    quote_data = client.get_quote_multi(segments)
    nse_quotes = quote_data.get("NSE_EQ", {}) if isinstance(quote_data, dict) else {}
    daily_baselines = _get_daily_baselines(client, security_map)

    items: list[dict] = []
    for base in NIFTY_50_CONSTITUENTS:
        symbol = base["symbol"]
        sec_id = security_map.get(symbol)
        raw = nse_quotes.get(str(sec_id)) or nse_quotes.get(sec_id) or {}
        price, change, change_pct, volume = _extract_quote_metrics(raw)
        baseline = daily_baselines.get(symbol) or {}
        baseline_price = _safe_float(baseline.get("latest_close"), 0.0)
        prev_close = _safe_float(baseline.get("prev_close"), 0.0)

        if price <= 0 and baseline_price > 0:
            price = baseline_price

        if price > 0 and prev_close > 0:
            computed_change = round(price - prev_close, 2)
            computed_pct = round((computed_change / prev_close) * 100, 2) if prev_close else 0.0
            if abs(change) < 1e-9 and abs(change_pct) < 1e-9:
                change = computed_change
                change_pct = computed_pct
            elif abs(computed_change - change) > 0.05 or abs(computed_pct - change_pct) > 0.05:
                change = computed_change
                change_pct = computed_pct

        items.append(
            {
                "symbol": symbol,
                "name": base["name"],
                "industry": base["industry"],
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "volume": volume,
                "weight": _WEIGHT_OVERRIDES.get(symbol, 1.0),
                "unavailable": price <= 0,
            }
        )
    return _finalize_payload(items, source="dhan_quote")


def _yfinance_snapshot() -> dict:
    import yfinance as yf

    symbols = [item["symbol"] for item in NIFTY_50_CONSTITUENTS]
    yf_symbols = [f"{symbol}.NS" for symbol in symbols]
    data = yf.download(
        tickers=" ".join(yf_symbols),
        period="10d",
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=True,
        group_by="ticker",
    )

    items: list[dict] = []
    for base in NIFTY_50_CONSTITUENTS:
        symbol = base["symbol"]
        yf_symbol = f"{symbol}.NS"
        price = 0.0
        change = 0.0
        change_pct = 0.0
        volume = 0
        try:
            if hasattr(data.columns, "levels"):
                frame = data[yf_symbol]
            else:
                frame = data
            frame = frame.dropna(how="all")
            if not frame.empty:
                close_series = frame.get("Close")
                volume_series = frame.get("Volume")
                if close_series is not None and len(close_series.dropna()) >= 1:
                    close_series = close_series.dropna()
                    price = _safe_float(close_series.iloc[-1], 0.0)
                    prev = _safe_float(close_series.iloc[-2], price) if len(close_series) > 1 else price
                    change = price - prev
                    change_pct = (change / prev * 100) if prev else 0.0
                if volume_series is not None and len(volume_series.dropna()) >= 1:
                    volume = int(round(_safe_float(volume_series.dropna().iloc[-1], 0.0)))
        except Exception:
            price = 0.0
        items.append(
            {
                "symbol": symbol,
                "name": base["name"],
                "industry": base["industry"],
                "price": round(price, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "volume": max(0, volume),
                "weight": _WEIGHT_OVERRIDES.get(symbol, 1.0),
                "unavailable": price <= 0,
            }
        )
    return _finalize_payload(items, source="yfinance_fallback")


def get_nifty50_market_movers_snapshot(
    broker_client: DhanClient | None = None,
    fallback_client: DhanClient | None = None,
    *,
    ttl_sec: int = _SNAPSHOT_TTL_SEC,
) -> dict:
    now = time.time()
    cached_payload = _SNAPSHOT_CACHE.get("payload")
    if cached_payload and (now - float(_SNAPSHOT_CACHE.get("timestamp") or 0.0)) < ttl_sec:
        return cached_payload

    clients = []
    for candidate in (broker_client, fallback_client):
        if candidate and getattr(candidate, "_is_configured", lambda: False)():
            clients.append(candidate)

    errors: list[str] = []
    for client in clients:
        try:
            payload = _dhan_snapshot(client)
            if any(not item.get("unavailable") for item in payload.get("items", [])):
                _SNAPSHOT_CACHE["payload"] = payload
                _SNAPSHOT_CACHE["timestamp"] = now
                return payload
        except Exception as exc:
            errors.append(f"dhan:{type(exc).__name__}")

    try:
        payload = _yfinance_snapshot()
        if any(not item.get("unavailable") for item in payload.get("items", [])):
            _SNAPSHOT_CACHE["payload"] = payload
            _SNAPSHOT_CACHE["timestamp"] = now
            return payload
    except Exception as exc:
        errors.append(f"yfinance:{type(exc).__name__}")

    if cached_payload:
        stale = dict(cached_payload)
        stale["stale"] = True
        stale["message"] = "Serving cached market movers data"
        return stale

    return _empty_snapshot(
        "Market movers data is temporarily unavailable" + (f" ({', '.join(errors)})" if errors else ""),
        source="unavailable",
    )
