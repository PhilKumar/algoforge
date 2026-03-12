"""
broker/dhan.py — Dhan HQ API Wrapper
Handles: historical data, order placement, order status, positions,
         scrip master download & option security ID lookup.
Docs: https://dhanhq.co/docs/latest/
"""

import asyncio
import csv
import json
import os
import sys
import threading
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import httpx
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

# ══════════════════════════════════════════════════════════════
#  Token Auto-Refresh on DH-906 "Invalid Token"
# ══════════════════════════════════════════════════════════════
_token_refresh_lock = threading.Lock()
_last_token_refresh: float = 0.0


def _is_invalid_token_response(resp) -> bool:
    """Check if an HTTP response indicates an invalid/expired token.
    Dhan uses two error formats:
      1. Order endpoints:  {"errorCode": "DH-906", "errorMessage": "Invalid Token"}
      2. Data endpoints:   {"data": {"808": "Authentication Failed - ..."}}
    """
    if resp.status_code != 400:
        return False
    try:
        body = resp.json() if hasattr(resp, "json") else {}
        # Format 1: Order/trade endpoints
        if body.get("errorCode") == "DH-906":
            return True
        # Format 2: Data/funds/trades endpoints
        data = body.get("data")
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, str) and ("Authentication Failed" in v or "Token invalid" in v):
                    return True
        return False
    except Exception:
        return False


def _try_refresh_token() -> bool:
    """Attempt to regenerate the Dhan access token via TOTP.
    Returns True if a new token was obtained. Thread-safe with a cooldown."""
    global _last_token_refresh
    with _token_refresh_lock:
        # Cooldown: don't retry within 30s of last refresh attempt
        if _time.time() - _last_token_refresh < 30:
            return False
        _last_token_refresh = _time.time()
    try:
        from token_manager import auto_generate_token

        new_tok = auto_generate_token()
        if new_tok:
            _dhan_log.info("[DHAN] ✅ Token auto-refreshed after invalid token error")
            return True
        _dhan_log.warning("[DHAN] Token refresh failed — auto_generate_token returned None")
        return False
    except Exception as e:
        _dhan_log.error(f"[DHAN] Token refresh error: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  TTL Response Cache (for LTP, positions, funds)
# ══════════════════════════════════════════════════════════════
class _TTLCache:
    """Simple in-memory cache with per-key TTL expiry."""

    def __init__(self):
        self._store: Dict[str, tuple] = {}  # key -> (value, expire_timestamp)

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and _time.time() < entry[1]:
            return entry[0]
        self._store.pop(key, None)
        return None

    def set(self, key: str, value, ttl_sec: float = 5.0):
        self._store[key] = (value, _time.time() + ttl_sec)

    def clear(self):
        self._store.clear()


_api_cache = _TTLCache()


# ══════════════════════════════════════════════════════════════
#  Exchange Tick Size Rounding
# ══════════════════════════════════════════════════════════════
def round_to_tick(price: float, tick_size: float = 0.05) -> float:
    """Round a price to the nearest valid exchange tick size.

    NSE F&O tick size is ₹0.05. Sending 250.12 gets rejected with
    EXCH:16283 — this function snaps it to 250.10 or 250.15.

    Uses integer arithmetic internally to avoid floating-point drift
    (e.g. 250.15000000000003).
    """
    if price <= 0 or tick_size <= 0:
        return price
    # Scale to integer domain, round, scale back
    multiplier = round(1 / tick_size)  # 20 for 0.05 tick
    return round(round(price * multiplier) / multiplier, 2)


# ══════════════════════════════════════════════════════════════
#  Persistent HTTP Session (keeps TCP+TLS warm to Dhan servers)
# ══════════════════════════════════════════════════════════════
_http_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=4,  # 4 parallel connection pools
    pool_maxsize=10,  # 10 connections per pool
    max_retries=0,  # we handle retries ourselves
)
_http_session.mount("https://", _adapter)
_http_session.mount("http://", _adapter)


# ══════════════════════════════════════════════════════════════
#  Async HTTP Client (httpx) — true non-blocking with warm TLS
#  Eliminates asyncio.to_thread overhead on the order hot-path.
# ══════════════════════════════════════════════════════════════
_async_transport = httpx.AsyncHTTPTransport(
    retries=0,  # we handle retries ourselves
    http2=True,  # HTTP/2 multiplexing — one TLS conn, many streams
    limits=httpx.Limits(
        max_connections=20,
        max_keepalive_connections=10,
        keepalive_expiry=60,
    ),
)
_async_client: httpx.AsyncClient | None = None


def _get_async_client() -> httpx.AsyncClient:
    """Lazy singleton — created on first use inside a running event loop."""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(
            transport=_async_transport,
            timeout=httpx.Timeout(10.0, connect=3.0),
            http2=True,
        )
    return _async_client


async def _async_request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict,
    json_data: dict = None,
    timeout: float = 10.0,
    max_retries: int = 3,
    base_delay: float = 0.3,
) -> httpx.Response:
    """
    Async HTTP request with exponential backoff on transient failures.
    Uses httpx.AsyncClient for true non-blocking I/O (no thread pool).
    Auto-refreshes token on DH-906 Invalid Token errors.
    """
    client = _get_async_client()
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = await client.request(
                method,
                url,
                headers=headers,
                json=json_data,
                timeout=timeout,
            )
            if _is_invalid_token_response(resp):
                _dhan_log.warning(f"[DHAN-ASYNC] {method} {url} → Invalid Token (400), refreshing...")
                refreshed = await asyncio.to_thread(_try_refresh_token)
                if refreshed:
                    headers = {**headers, "access-token": config.DHAN_ACCESS_TOKEN}
                    continue
            if resp.status_code not in _RETRYABLE_STATUSES:
                return resp
            last_exc = Exception(f"Dhan API {resp.status_code}: {resp.text[:200]}")
            _dhan_log.warning(f"[DHAN-ASYNC] {method} {url} → {resp.status_code} (attempt {attempt + 1}/{max_retries})")
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            last_exc = e
            _dhan_log.warning(f"[DHAN-ASYNC] {method} {url} network error (attempt {attempt + 1}/{max_retries}): {e}")
        if attempt < max_retries - 1:
            await asyncio.sleep(base_delay * (2**attempt))  # 0.3s, 0.6s, 1.2s
    raise last_exc


# ══════════════════════════════════════════════════════════════
#  Exponential Backoff + Circuit Breaker
# ══════════════════════════════════════════════════════════════
import logging as _log

_dhan_log = _log.getLogger("algoforge.dhan")

# Retryable HTTP status codes (rate-limit / transient server errors)
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Global rate limiter for /v2/marketfeed/* endpoints (shared across all callers)
_mf_last_call: float = 0.0
_MF_MIN_INTERVAL: float = 1.0  # seconds between marketfeed REST calls
_mf_throttle_enabled: bool = False  # activated by ScalpEngine when a trade is open


def enable_marketfeed_throttle(on: bool = True):
    """Toggle the global marketfeed rate-limiter (called by ScalpEngine)."""
    global _mf_throttle_enabled
    _mf_throttle_enabled = on


def _throttle_marketfeed():
    """Block until at least _MF_MIN_INTERVAL has elapsed since the last marketfeed call.
    Only active when a live scalp trade is open."""
    if not _mf_throttle_enabled:
        return
    global _mf_last_call
    now = _time.monotonic()
    wait = _MF_MIN_INTERVAL - (now - _mf_last_call)
    if wait > 0:
        _time.sleep(wait)
    _mf_last_call = _time.monotonic()


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict,
    json: dict = None,
    timeout: int = 30,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> "requests.Response":
    """
    Execute an HTTP request with exponential backoff on transient failures.
    Auto-refreshes token on DH-906 Invalid Token errors.
    Raises the last exception if all retries are exhausted.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = _http_session.request(method, url, headers=headers, json=json, timeout=timeout)
            if _is_invalid_token_response(resp):
                _dhan_log.warning(f"[DHAN] {method} {url} → Invalid Token (400), refreshing...")
                if _try_refresh_token():
                    headers = {**headers, "access-token": config.DHAN_ACCESS_TOKEN}
                    continue
            if resp.status_code not in _RETRYABLE_STATUSES:
                return resp
            # Retryable status — treat as transient
            last_exc = Exception(f"Dhan API {resp.status_code}: {resp.text[:200]}")
            _dhan_log.warning(f"[DHAN] {method} {url} → {resp.status_code} (attempt {attempt + 1}/{max_retries})")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            _dhan_log.warning(f"[DHAN] {method} {url} network error (attempt {attempt + 1}/{max_retries}): {e}")
        if attempt < max_retries - 1:
            delay = base_delay * (2**attempt)  # 1s, 2s, 4s …
            _time.sleep(delay)
    raise last_exc


class _CircuitBreaker:
    """
    Simple circuit breaker: opens after `failure_threshold` consecutive
    failures; resets after `recovery_timeout` seconds.
    """

    CLOSED = "closed"
    OPEN = "open"

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self._threshold = failure_threshold
        self._timeout = recovery_timeout
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = 0.0

    def call_allowed(self) -> bool:
        if self._state == self.CLOSED:
            return True
        # Half-open: allow one probe after timeout
        if _time.time() - self._opened_at >= self._timeout:
            return True
        return False

    def record_success(self):
        self._failures = 0
        self._state = self.CLOSED

    def record_failure(self):
        self._failures += 1
        if self._failures >= self._threshold:
            if self._state != self.OPEN:
                _dhan_log.error(f"[DHAN] Circuit breaker OPEN after {self._failures} consecutive failures")
            self._state = self.OPEN
            self._opened_at = _time.time()

    @property
    def state(self) -> str:
        return self._state


_circuit_breaker = _CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)


# ══════════════════════════════════════════════════════════════
#  SCRIP MASTER — Option Security ID Lookup
# ══════════════════════════════════════════════════════════════
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".scrip_cache")

# Map frontend instrument ID → underlying symbol name in scrip master
UNDERLYING_MAP = {
    "26000": "NIFTY",
    "26009": "BANKNIFTY",
    "26017": "FINNIFTY",
    "26037": "MIDCPNIFTY",
    "1": "SENSEX",
}


class ScripMaster:
    """
    Download, cache, and lookup Dhan instrument security IDs for option contracts.
    Downloads the full scrip master CSV (~150MB), filters for index options only,
    and caches the result to a local JSON file (~2-5MB).
    """

    _options_cache: Dict[str, str] = {}  # key -> security_id
    _expiry_cache: Dict[str, list] = {}  # symbol -> [expiry_dates]
    _lot_cache: Dict[str, int] = {}  # key -> lot_size
    _loaded_date: Optional[str] = None
    _loading = False

    @classmethod
    def ensure_loaded(cls) -> bool:
        """Ensure scrip master is loaded for today. Returns True if loaded."""
        today = datetime.now().strftime("%Y-%m-%d")
        if cls._loaded_date == today and cls._options_cache:
            return True

        # Try disk cache first
        cache_file = os.path.join(SCRIP_CACHE_DIR, f"options_{today}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    data = json.load(f)
                cls._options_cache = data.get("options", {})
                cls._expiry_cache = data.get("expiries", {})
                cls._lot_cache = data.get("lots", {})
                cls._loaded_date = today
                print(f"[SCRIP] ✅ Loaded {len(cls._options_cache)} options from disk cache")
                return True
            except Exception as e:
                print(f"[SCRIP] Cache read error: {e}")

        # Download fresh
        return cls._download_and_filter(cache_file, today)

    @classmethod
    def _download_and_filter(cls, cache_file: str, today: str) -> bool:
        """Download scrip master CSV and extract option contracts."""
        if cls._loading:
            print("[SCRIP] Download already in progress...")
            return False
        cls._loading = True

        print("[SCRIP] 📥 Downloading Dhan scrip master (this may take 30-60 seconds)...")
        os.makedirs(SCRIP_CACHE_DIR, exist_ok=True)

        try:
            resp = _http_session.get(SCRIP_MASTER_URL, stream=True, timeout=180)
            resp.raise_for_status()
        except Exception as e:
            print(f"[SCRIP] ❌ Download failed: {e}")
            cls._loading = False
            return False

        options = {}
        expiries: Dict[str, set] = {}
        lots = {}
        # Map trading symbol prefix → canonical name used as key
        supported_prefixes = {
            "NIFTY": "NIFTY",
            "BANKNIFTY": "BANKNIFTY",
            "FINNIFTY": "FINNIFTY",
            "MIDCPNIFTY": "MIDCPNIFTY",
            "SENSEX": "SENSEX",
        }
        count = 0

        try:
            # Stream line-by-line to keep memory low
            # Force decode to str if bytes are returned
            def _lines():
                for raw in resp.iter_lines():
                    if isinstance(raw, bytes):
                        yield raw.decode("utf-8", errors="replace")
                    else:
                        yield raw

            lines_iter = _lines()
            header_line = next(lines_iter)
            # Parse header with proper CSV handling
            header = next(csv.reader([header_line]))
            col_map = {name.strip(): i for i, name in enumerate(header)}

            idx_sec = col_map.get("SEM_SMST_SECURITY_ID", -1)
            idx_inst = col_map.get("SEM_INSTRUMENT_NAME", -1)
            idx_stk = col_map.get("SEM_STRIKE_PRICE", -1)
            idx_exp = col_map.get("SEM_EXPIRY_DATE", -1)
            idx_opt = col_map.get("SEM_OPTION_TYPE", -1)
            idx_exch = col_map.get("SEM_EXM_EXCH_ID", -1)
            idx_lot = col_map.get("SEM_LOT_UNITS", -1)
            idx_tsym = col_map.get("SEM_TRADING_SYMBOL", -1)

            max_idx = max(idx_sec, idx_inst, idx_stk, idx_exp, idx_opt, idx_tsym)

            for line_text in lines_iter:
                if not line_text or not line_text.strip():
                    continue
                try:
                    row = next(csv.reader([line_text]))
                except Exception:  # nosec B112 — skip malformed CSV rows
                    continue
                if len(row) <= max_idx:
                    continue

                inst_name = row[idx_inst].strip() if idx_inst >= 0 else ""
                if inst_name != "OPTIDX":
                    continue

                # Extract symbol from SEM_TRADING_SYMBOL (e.g., "NIFTY-Mar2026-24000-CE")
                trading_sym = row[idx_tsym].strip() if idx_tsym >= 0 else ""
                sym_prefix = trading_sym.split("-")[0] if "-" in trading_sym else ""
                symbol = supported_prefixes.get(sym_prefix, "")
                if not symbol:
                    continue

                # Only NSE for most, BSE for SENSEX
                exch = row[idx_exch].strip() if idx_exch >= 0 else ""
                if symbol == "SENSEX" and exch != "BSE":
                    continue
                if symbol != "SENSEX" and exch != "NSE":
                    continue

                sec_id = row[idx_sec].strip() if idx_sec >= 0 else ""
                strike_raw = row[idx_stk].strip() if idx_stk >= 0 else ""
                expiry_raw = row[idx_exp].strip() if idx_exp >= 0 else ""
                opt_type = row[idx_opt].strip() if idx_opt >= 0 else ""
                lot_raw = row[idx_lot].strip() if idx_lot >= 0 else ""

                if not sec_id or not strike_raw or not opt_type:
                    continue

                # Normalize strike (remove trailing .00000)
                try:
                    strike_f = float(strike_raw)
                    strike_key = str(int(strike_f)) if strike_f == int(strike_f) else strike_raw
                except ValueError:
                    continue

                # Normalize expiry: extract date part from "YYYY-MM-DD HH:MM:SS"
                expiry = expiry_raw.strip().split(" ")[0] if " " in expiry_raw else expiry_raw.strip()

                key = f"{symbol}_{strike_key}_{expiry}_{opt_type}"
                options[key] = sec_id

                # Lot size (convert "25.0" → 25)
                try:
                    lot_val = int(float(lot_raw))
                    lots[f"{symbol}_{expiry}"] = lot_val
                except:
                    pass

                # Expiry tracking
                if symbol not in expiries:
                    expiries[symbol] = set()
                expiries[symbol].add(expiry)

                count += 1

        except StopIteration:
            pass
        except Exception as e:
            print(f"[SCRIP] Parse error: {e}")

        # Convert sets → sorted lists for JSON
        expiries_serial = {k: sorted(list(v)) for k, v in expiries.items()}

        cls._options_cache = options
        cls._expiry_cache = expiries_serial
        cls._lot_cache = lots
        cls._loaded_date = today
        cls._loading = False

        # Save to disk cache
        try:
            with open(cache_file, "w") as f:
                json.dump({"options": options, "expiries": expiries_serial, "lots": lots}, f)
            print(f"[SCRIP] ✅ Cached {count} option contracts for {list(expiries_serial.keys())}")
        except Exception as e:
            print(f"[SCRIP] Cache write error: {e}")

        # Clean up old cache files
        try:
            for old_file in Path(SCRIP_CACHE_DIR).glob("options_*.json"):
                if old_file.name != f"options_{today}.json":
                    old_file.unlink()
        except:
            pass

        return count > 0

    @classmethod
    def lookup(cls, symbol: str, strike: int, expiry: str, option_type: str) -> str:
        """Look up security ID for a specific option contract."""
        cls.ensure_loaded()
        key = f"{symbol}_{strike}_{expiry}_{option_type}"
        sec_id = cls._options_cache.get(key, "")
        if not sec_id:
            # Try with trailing .0 in strike (some scrip masters store decimals)
            alt_key = f"{symbol}_{strike}.0_{expiry}_{option_type}"
            sec_id = cls._options_cache.get(alt_key, "")
        if not sec_id:
            print(f"[SCRIP] ⚠ Security ID not found: {key}")
        return sec_id

    @classmethod
    def get_nearest_expiry(cls, symbol: str, from_date: str = None) -> str:
        """Get nearest weekly expiry for a symbol (>= today)."""
        cls.ensure_loaded()
        expiries = cls._expiry_cache.get(symbol, [])
        ref_date = from_date or datetime.now().strftime("%Y-%m-%d")
        future = [e for e in expiries if e >= ref_date]
        return future[0] if future else ""

    @classmethod
    def get_expiries(cls, symbol: str) -> list:
        """Get all available expiry dates for a symbol."""
        cls.ensure_loaded()
        return cls._expiry_cache.get(symbol, [])

    @classmethod
    def get_lot_size(cls, symbol: str, expiry: str) -> int:
        """Get lot size from scrip master."""
        cls.ensure_loaded()
        lot = cls._lot_cache.get(f"{symbol}_{expiry}", 0)
        if lot > 0:
            return lot
        # Fallback defaults (current as of Jan 2026)
        defaults = {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 50, "SENSEX": 20}
        return defaults.get(symbol, 65)

    @classmethod
    def instrument_to_symbol(cls, instrument_id: str) -> str:
        """Convert frontend instrument ID to underlying symbol."""
        return UNDERLYING_MAP.get(instrument_id, "NIFTY")


class DhanClient:
    def __init__(self, client_id: str = None, access_token: str = None):
        self.client_id = client_id or config.DHAN_CLIENT_ID
        self._fixed_token = access_token  # None means "use config dynamically"
        self.base_url = config.DHAN_BASE_URL
        self.data_url = config.DHAN_DATA_URL

    @property
    def access_token(self) -> str:
        """Always return the current token from config (updated by token_manager)."""
        return self._fixed_token or config.DHAN_ACCESS_TOKEN

    @property
    def headers(self) -> dict:
        """Build headers dynamically so token changes are always picked up."""
        return {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _is_configured(self) -> bool:
        return (
            self.client_id != "YOUR_CLIENT_ID_HERE"
            and self.access_token != "YOUR_ACCESS_TOKEN_HERE"
            and self.access_token != "PASTE_YOUR_NEW_TOKEN_HERE"
            and len(self.access_token) > 20
        )

    # ──────────────────────────────────────────────────────────
    # Historical Data
    # ──────────────────────────────────────────────────────────
    def get_historical_data(
        self,
        security_id: str,
        exchange_segment: str,  # IDX_I, NSE_EQ, BSE_EQ, NSE_FNO
        instrument_type: str,  # INDEX, EQUITY, FUTIDX, OPTIDX
        expiry_code: int = 0,  # 0=current week, 1=next week, 2=next month
        from_date: str = None,
        to_date: str = None,
        candle_type: str = "1",  # Supported: 1, 5, 15, 25, 60 (minutes), D (daily) - NO 3 or 30!
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles from Dhan Charts API v2

        Supported candle intervals:
        - Intraday: 1, 5, 15, 25, 60 minutes
        - Daily: D

        Note: 3-minute and 30-minute candles are NOT supported by Dhan API!
        """

        if not self._is_configured():
            raise ConnectionError("Dhan credentials not set. Edit config.py with your client_id and access_token.")

        # Validate candle_type
        valid_intervals = ["1", "5", "15", "25", "60", "D"]
        if candle_type not in valid_intervals:
            raise ValueError(
                f"Invalid candle_type '{candle_type}'. Dhan API only supports: {', '.join(valid_intervals)}"
            )

        if not from_date:
            from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")

        print(
            f"[DHAN] Fetching: secId={security_id}, seg={exchange_segment}, "
            f"type={instrument_type}, candle={candle_type}, {from_date} → {to_date}"
        )

        if candle_type == "D":
            # ── Daily / Historical candles ──
            endpoint = f"{self.data_url}/charts/historical"
            payload = {
                "securityId": str(security_id),
                "exchangeSegment": exchange_segment,
                "instrument": instrument_type,
                "expiryCode": expiry_code,
                "fromDate": from_date,
                "toDate": to_date,
            }
        else:
            # ── Intraday candles (1m, 5m, 15m, 25m, 60m) ──
            endpoint = f"{self.data_url}/charts/intraday"
            payload = {
                "securityId": str(security_id),
                "exchangeSegment": exchange_segment,
                "instrument": instrument_type,
                "interval": str(candle_type),
                "fromDate": from_date,
                "toDate": to_date,
            }

        import logging as _log

        _dlog = _log.getLogger("algoforge.dhan")
        _safe = {
            k: v for k, v in payload.items() if k not in ("access-token", "accessToken", "token", "password", "secret")
        }
        _dlog.debug(f"[DHAN] POST {endpoint} payload_keys={list(_safe.keys())}")

        if not _circuit_breaker.call_allowed():
            raise Exception("Dhan API circuit breaker is OPEN — skipping candle fetch")
        try:
            resp = _request_with_retry("POST", endpoint, headers=self.headers, json=payload, timeout=30)
        except Exception as e:
            _circuit_breaker.record_failure()
            raise
        if resp.status_code != 200:
            error_text = resp.text[:500]
            _circuit_breaker.record_failure()
            _dhan_log.warning(f"[DHAN] POST {endpoint} status={resp.status_code} err={error_text}")
            raise Exception(f"Dhan API error {resp.status_code}: {error_text}")
        _circuit_breaker.record_success()

        data = resp.json()

        # Dhan v2 returns data directly as arrays (not wrapped in "data" key always)
        # Handle both formats
        if "open" in data:
            ohlcv = data
        elif "data" in data:
            ohlcv = data["data"]
        else:
            raise Exception(f"Unexpected Dhan response format: {list(data.keys())}")

        # Handle timestamp format — could be epoch seconds or milliseconds
        timestamps = ohlcv.get("start_Time", ohlcv.get("timestamp", ohlcv.get("start_time", [])))
        if not timestamps:
            raise Exception(f"No timestamp field found in response. Keys: {list(ohlcv.keys())}")

        # Auto-detect epoch format (seconds vs milliseconds)
        first_ts = timestamps[0] if timestamps else 0
        unit = "ms" if first_ts > 1e12 else "s"

        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(timestamps, unit=unit) + pd.Timedelta(hours=5, minutes=30),
                "open": [float(x) for x in ohlcv["open"]],
                "high": [float(x) for x in ohlcv["high"]],
                "low": [float(x) for x in ohlcv["low"]],
                "close": [float(x) for x in ohlcv["close"]],
                "volume": [int(x) for x in ohlcv.get("volume", [0] * len(ohlcv["open"]))],
            }
        )
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)

        print(f"[DHAN] ✅ Got {len(df)} candles: {df.index[0]} → {df.index[-1]}")
        return df

    def get_nifty_daily(self, from_date: str, to_date: str) -> pd.DataFrame:
        """Convenience: NIFTY daily OHLCV — uses security_id=13 for NIFTY 50"""
        return self.get_historical_data(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date=from_date,
            to_date=to_date,
            candle_type="D",
        )

    def get_nifty_intraday(self, from_date: str, to_date: str, interval: str = "5") -> pd.DataFrame:
        """Convenience: NIFTY 5-min intraday candles"""
        return self.get_historical_data(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date=from_date,
            to_date=to_date,
            candle_type=interval,
        )

    # ──────────────────────────────────────────────────────────
    # Order Management
    # ──────────────────────────────────────────────────────────
    def place_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,  # BUY or SELL
        quantity: int,
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
        price: float = 0,
        trigger_price: float = 0,
        validity: str = "DAY",
        tag: str = "AlgoForge",
    ) -> dict:
        """Place an order via Dhan API"""
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not set. Edit config.py first.")

        payload = {
            "dhanClientId": self.client_id,
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": validity,
            "securityId": str(security_id),
            "quantity": int(quantity),
            "price": round_to_tick(float(price)) if price else 0.0,
            "triggerPrice": round_to_tick(float(trigger_price)) if trigger_price else 0.0,
            "correlationId": (tag or "")[:25],
        }
        print(f"[DHAN] Order payload: {payload}")

        if not _circuit_breaker.call_allowed():
            raise Exception(
                f"Dhan API circuit breaker is OPEN — broker unavailable, retrying in {_circuit_breaker._timeout:.0f}s"
            )
        try:
            resp = _request_with_retry(
                "POST",
                f"{self.base_url}/v2/orders",
                headers=self.headers,
                json=payload,
                timeout=10,
            )
        except Exception as e:
            _circuit_breaker.record_failure()
            raise
        if resp.status_code not in (200, 201):
            _circuit_breaker.record_failure()
            raise Exception(f"Order placement failed {resp.status_code}: {resp.text}")
        _circuit_breaker.record_success()
        return resp.json()

    def place_option_order(
        self,
        underlying: str,  # "NIFTY", "BANKNIFTY", etc.
        strike_price: int,
        option_type: str,  # CE or PE
        expiry: str,  # "2026-02-20"
        transaction_type: str,  # BUY or SELL
        quantity: int,
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
        price: float = 0,
        trigger_price: float = 0,
        tag: str = "AlgoForge",
    ) -> dict:
        """
        Place an options order using real security_id from scrip master.
        Looks up the correct Dhan security ID for the specific
        strike + expiry + option_type contract.
        """
        # Look up real security_id from scrip master
        security_id = ScripMaster.lookup(underlying, strike_price, expiry, option_type)
        if not security_id:
            raise Exception(
                f"Cannot find security ID for {underlying} {strike_price}{option_type} "
                f"expiry {expiry}. Scrip master may not be loaded or contract doesn't exist."
            )

        print(
            f"[DHAN] Option order: {transaction_type} {underlying} "
            f"{strike_price}{option_type} exp={expiry} qty={quantity} "
            f"type={order_type} product={product_type} secId={security_id}"
        )

        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"

        return self.place_order(
            security_id=security_id,
            exchange_segment=exchange_seg,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
        )

    def place_sl_order(
        self,
        underlying: str,
        strike_price: int,
        option_type: str,
        expiry: str,
        transaction_type: str,  # opposite of entry: BUY→SELL, SELL→BUY
        quantity: int,
        trigger_price: float,
        price: float = 0,
        product_type: str = "INTRADAY",
        order_type: str = "SL",  # SL or SL-M
        tag: str = "AlgoForge_SL",
    ) -> dict:
        """
        Place a stop-loss order for an option position.
        SL = Stop Loss Limit (needs both trigger and limit price)
        SL-M = Stop Loss Market (only trigger, market execution)
        """
        security_id = ScripMaster.lookup(underlying, strike_price, expiry, option_type)
        if not security_id:
            raise Exception(f"Cannot find security ID for SL order: {underlying} {strike_price}{option_type}")

        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"

        # For SL-M, price should be 0 (market execution on trigger)
        if order_type == "SL-M":
            price = 0

        print(
            f"[DHAN] SL Order: {transaction_type} {underlying} {strike_price}{option_type} "
            f"trigger=₹{trigger_price} price=₹{price} type={order_type}"
        )

        return self.place_order(
            security_id=security_id,
            exchange_segment=exchange_seg,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
        )

    def get_order_book(self) -> list:
        """Get all orders for the day"""
        resp = _http_session.get(
            f"{self.base_url}/v2/orders",
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"Order book failed: {resp.text}")
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("data", []) if isinstance(data, dict) else []

    def get_trades(self, from_date: str = None, to_date: str = None) -> list:
        """Get all executed trades (trade book)

        Note: Dhan API may only support current day trades by default.
        For historical trades across multiple days, you may need to call this
        endpoint multiple times or check Dhan's reports/statements API.
        """
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not configured")

        try:
            # Dhan /v2/trades endpoint gets today's trades by default
            # For historical data, we might need additional parameters
            resp = _http_session.get(
                f"{self.base_url}/v2/trades",
                headers=self.headers,
                timeout=10,
            )

            print(f"[DHAN] get_trades status: {resp.status_code}")

            if resp.status_code == 401:
                raise Exception("Unauthorized - Invalid access token or client ID")
            elif resp.status_code == 403:
                raise Exception("Forbidden - Access token may have expired")
            elif resp.status_code != 200:
                raise Exception(f"Trade book API returned {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            print(f"[DHAN] get_trades response type: {type(data).__name__}")

            # Return trades array - handle both possible response formats
            if isinstance(data, list):
                trades = data
            elif isinstance(data, dict) and "data" in data:
                trades = data["data"]
            else:
                trades = data if isinstance(data, list) else []

            print(f"[DHAN] ✅ Retrieved {len(trades)} trades")
            return trades

        except requests.exceptions.Timeout:
            raise Exception("Connection timeout - please check your internet")
        except requests.exceptions.ConnectionError:
            raise Exception("Connection error - unable to reach Dhan servers")

    # Sentinel returned by get_trade_history on rate-limit (distinct from empty [])
    RATE_LIMITED = "__RATE_LIMITED__"

    def get_trade_history(self, from_date: str, to_date: str, page: int = 0) -> list | str:
        """Get historical trade book for a date range.

        Uses Dhan Statement API: GET /v2/trades/{from-date}/{to-date}/{page}
        Returns paginated results - pass page=0 as default.
        Returns RATE_LIMITED sentinel string if rate-limited (caller should retry).

        Args:
            from_date: Start date in YYYY-MM-DD format
            to_date: End date in YYYY-MM-DD format
            page: Page number (0-indexed)
        """
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not configured")

        try:
            resp = _http_session.get(
                f"{self.base_url}/v2/trades/{from_date}/{to_date}/{page}",
                headers=self.headers,
                timeout=15,
            )

            print(f"[DHAN] get_trade_history {from_date} to {to_date} page={page} status: {resp.status_code}")

            if resp.status_code == 429:
                print(f"[DHAN] Rate limited on page {page}")
                return self.RATE_LIMITED

            if resp.status_code != 200:
                print(f"[DHAN] Trade history error: {resp.text[:200]}")
                return []

            data = resp.json()
            # Detect rate-limit error returned as 200 with error JSON
            if isinstance(data, dict) and data.get("errorCode") == "DH-904":
                print(f"[DHAN] Rate limited (DH-904) on page {page}")
                return self.RATE_LIMITED

            trades = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
            print(f"[DHAN] ✅ Retrieved {len(trades)} historical trades")
            return trades

        except Exception as e:
            print(f"[DHAN] Trade history error: {e}")
            return []

    def cancel_order(self, order_id: str) -> dict:
        resp = _http_session.delete(
            f"{self.base_url}/v2/orders/{order_id}",
            headers=self.headers,
            timeout=10,
        )
        return resp.json()

    def get_positions(self) -> list:
        """Get current open positions"""
        resp = _http_session.get(
            f"{self.base_url}/v2/positions",
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"Positions fetch failed: {resp.text}")
        return resp.json().get("data", [])

    def get_funds(self) -> dict:
        """Get available margin/funds"""
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not configured")

        try:
            resp = _http_session.get(
                f"{self.base_url}/v2/fundlimit",
                headers=self.headers,
                timeout=10,
            )

            print(f"[DHAN] get_funds status: {resp.status_code}")

            if resp.status_code == 401:
                raise Exception("Unauthorized - Invalid access token or client ID")
            elif resp.status_code == 403:
                raise Exception("Forbidden - Access token may have expired")
            elif resp.status_code != 200:
                raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            print(f"[DHAN] get_funds response keys: {list(data.keys())}")

            # Dhan API returns different formats - handle both
            if "data" in data:
                return data["data"]
            elif "availabelBalance" in data or "sodLimit" in data:
                return data
            else:
                print(f"[DHAN] Unexpected response format: {data}")
                return data

        except requests.exceptions.Timeout:
            raise Exception("Connection timeout - please check your internet")
        except requests.exceptions.ConnectionError:
            raise Exception("Connection error - unable to reach Dhan servers")

    def get_ltp(self, security_ids: list, exchange_segment: str = "NSE_EQ") -> dict:
        """Get Last Traded Price for given securities"""
        # Dhan API requires security IDs as integers
        int_ids = [int(sid) for sid in security_ids]
        payload = {
            "NSE_EQ": int_ids if exchange_segment == "NSE_EQ" else [],
            "NSE_FNO": int_ids if exchange_segment == "NSE_FNO" else [],
            "BSE_FNO": int_ids if exchange_segment == "BSE_FNO" else [],
            "IDX_I": int_ids if exchange_segment == "IDX_I" else [],
        }
        _throttle_marketfeed()
        resp = _request_with_retry(
            "POST",
            f"{self.base_url}/v2/marketfeed/ltp",
            headers=self.headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"LTP fetch failed: {resp.text}")
        return resp.json().get("data", {})

    def get_ltp_multi(self, segments: dict) -> dict:
        """Get LTP across multiple exchange segments in ONE API call.
        segments: {"IDX_I": [13, 51, 25], "NSE_FNO": [54880, 54881]}
        Returns raw data dict with all segments.
        """
        payload = {
            "NSE_EQ": [int(s) for s in segments.get("NSE_EQ", [])],
            "NSE_FNO": [int(s) for s in segments.get("NSE_FNO", [])],
            "BSE_FNO": [int(s) for s in segments.get("BSE_FNO", [])],
            "IDX_I": [int(s) for s in segments.get("IDX_I", [])],
        }
        _throttle_marketfeed()
        resp = _request_with_retry(
            "POST",
            f"{self.base_url}/v2/marketfeed/ltp",
            headers=self.headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"LTP fetch failed: {resp.text}")
        return resp.json().get("data", {})

    def get_ohlc_multi(self, segments: dict) -> dict:
        """Get OHLC + LTP across multiple exchange segments in ONE API call.
        Uses /v2/marketfeed/ohlc — returns last_price + ohlc{open,close,high,low}.
        ohlc.close = previous day's closing price.
        """
        payload = {
            "NSE_EQ": [int(s) for s in segments.get("NSE_EQ", [])],
            "NSE_FNO": [int(s) for s in segments.get("NSE_FNO", [])],
            "BSE_FNO": [int(s) for s in segments.get("BSE_FNO", [])],
            "IDX_I": [int(s) for s in segments.get("IDX_I", [])],
        }
        _throttle_marketfeed()
        resp = _request_with_retry(
            "POST",
            f"{self.base_url}/v2/marketfeed/ohlc",
            headers=self.headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"OHLC fetch failed: {resp.text}")
        return resp.json().get("data", {})

    def get_quote_multi(self, segments: dict) -> dict:
        """Get full market quote (LTP + OHLC + net_change + volume + OI) in ONE call.
        Uses /v2/marketfeed/quote — includes net_change (absolute change from prev close).
        """
        payload = {
            "NSE_EQ": [int(s) for s in segments.get("NSE_EQ", [])],
            "NSE_FNO": [int(s) for s in segments.get("NSE_FNO", [])],
            "BSE_FNO": [int(s) for s in segments.get("BSE_FNO", [])],
            "IDX_I": [int(s) for s in segments.get("IDX_I", [])],
        }
        _throttle_marketfeed()
        resp = _request_with_retry(
            "POST",
            f"{self.base_url}/v2/marketfeed/quote",
            headers=self.headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"Quote fetch failed: {resp.text}")
        return resp.json().get("data", {})

    def get_option_ltp(self, underlying: str, strike: int, expiry: str, option_type: str) -> float:
        """
        Get LTP for a specific option contract.
        Returns the last traded price, or 0 if not found.
        """
        security_id = ScripMaster.lookup(underlying, strike, expiry, option_type)
        if not security_id:
            print(f"[DHAN] Cannot get LTP — no security ID for {underlying} {strike}{option_type}")
            return 0.0

        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"
        try:
            data = self.get_ltp([security_id], exchange_segment=exchange_seg)
            # Dhan response: {"NSE_FNO": {"54880": {"last_price": 15.6}}}
            if isinstance(data, dict):
                seg_data = data.get(exchange_seg, {})
                if isinstance(seg_data, dict):
                    sid_data = seg_data.get(str(security_id), seg_data.get(int(security_id), {}))
                    if isinstance(sid_data, dict):
                        return float(sid_data.get("last_price", sid_data.get("ltp", 0)))
                    elif isinstance(sid_data, (int, float)):
                        return float(sid_data)
                # Fallback: iterate all nested values
                for key, val in data.items():
                    if isinstance(val, dict):
                        for k2, v2 in val.items():
                            if isinstance(v2, dict):
                                return float(v2.get("last_price", v2.get("ltp", 0)))
                            elif isinstance(v2, (int, float)):
                                return float(v2)
            return 0.0
        except Exception as e:
            print(f"[DHAN] Option LTP fetch failed: {e}")
            return 0.0

    def modify_order(
        self,
        order_id: str,
        order_type: str = None,
        quantity: int = None,
        price: float = None,
        trigger_price: float = None,
    ) -> dict:
        """Modify an existing order"""
        payload = {"dhanClientId": self.client_id, "orderId": order_id}
        if order_type:
            payload["orderType"] = order_type
        if quantity:
            payload["quantity"] = quantity
        if price is not None:
            payload["price"] = round_to_tick(float(price)) if price else 0.0
        if trigger_price is not None:
            payload["triggerPrice"] = round_to_tick(float(trigger_price)) if trigger_price else 0.0

        resp = _http_session.put(
            f"{self.base_url}/v2/orders/{order_id}",
            json=payload,
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"Order modify failed: {resp.text}")
        return resp.json()

    def get_order_status(self, order_id: str) -> dict:
        """Get status of a specific order"""
        try:
            resp = _http_session.get(
                f"{self.base_url}/v2/orders/{order_id}",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code != 200:
                return {"orderStatus": "UNKNOWN"}
            return resp.json()
        except:
            return {"orderStatus": "UNKNOWN"}

    # ──────────────────────────────────────────────────────────
    # Order Verification (#7) — poll until filled or timeout
    # ──────────────────────────────────────────────────────────
    def verify_order_fill(self, order_id: str, max_wait_sec: int = 15, poll_interval: float = 1.5) -> dict:
        """Poll order status until filled, rejected, or timeout.
        Returns dict with: order_id, status, filled_qty, avg_price, message.
        """
        import time as _t

        start = _t.time()
        last_status = {}
        while _t.time() - start < max_wait_sec:
            status = self.get_order_status(order_id)
            last_status = status
            os = status.get("orderStatus", status.get("status", "UNKNOWN")).upper()
            # Terminal states
            if os in ("TRADED", "FILLED", "COMPLETE"):
                return {
                    "order_id": order_id,
                    "status": "FILLED",
                    "filled_qty": status.get("filledQty", status.get("tradedQuantity", status.get("quantity", 0))),
                    "avg_price": status.get("averagePrice", status.get("price", 0)),
                    "message": "Order filled successfully",
                }
            if os in ("REJECTED", "CANCELLED"):
                return {
                    "order_id": order_id,
                    "status": os,
                    "filled_qty": 0,
                    "avg_price": 0,
                    "message": status.get("rejectionReason", status.get("omsErrorDescription", f"Order {os}")),
                }
            _t.sleep(poll_interval)
        # Timeout
        return {
            "order_id": order_id,
            "status": "TIMEOUT",
            "filled_qty": last_status.get("filledQty", 0),
            "avg_price": last_status.get("averagePrice", 0),
            "message": f"Order not filled within {max_wait_sec}s. Last status: {last_status.get('orderStatus', 'UNKNOWN')}",
        }

    # ──────────────────────────────────────────────────────────
    # Cached versions of frequent API calls (#12)
    # ──────────────────────────────────────────────────────────
    def get_positions_cached(self, ttl: float = 5.0) -> list:
        """Get positions with TTL cache (avoid hammering API)."""
        cached = _api_cache.get("positions")
        if cached is not None:
            return cached
        result = self.get_positions()
        _api_cache.set("positions", result, ttl)
        return result

    def get_funds_cached(self, ttl: float = 10.0) -> dict:
        """Get funds with TTL cache."""
        cached = _api_cache.get("funds")
        if cached is not None:
            return cached
        result = self.get_funds()
        _api_cache.set("funds", result, ttl)
        return result

    def get_ltp_cached(self, security_ids: list, exchange_segment: str = "NSE_EQ", ttl: float = 3.0) -> dict:
        """Get LTP with TTL cache."""
        key = f"ltp:{exchange_segment}:{','.join(str(s) for s in security_ids)}"
        cached = _api_cache.get(key)
        if cached is not None:
            return cached
        result = self.get_ltp(security_ids, exchange_segment)
        _api_cache.set(key, result, ttl)
        return result

    # ──────────────────────────────────────────────────────────
    # True-async hot-path methods (httpx — zero thread overhead)
    # ──────────────────────────────────────────────────────────
    async def async_place_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
        price: float = 0,
        trigger_price: float = 0,
        validity: str = "DAY",
        tag: str = "AlgoForge",
    ) -> dict:
        """Place an order via httpx (true async — no thread pool)."""
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not set.")
        payload = {
            "dhanClientId": self.client_id,
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": validity,
            "securityId": str(security_id),
            "quantity": int(quantity),
            "price": round_to_tick(float(price)) if price else 0.0,
            "triggerPrice": round_to_tick(float(trigger_price)) if trigger_price else 0.0,
            "correlationId": (tag or "")[:25],
        }
        print(f"[DHAN] Async order payload: {payload}")
        if not _circuit_breaker.call_allowed():
            raise Exception("Dhan API circuit breaker OPEN")
        try:
            resp = await _async_request_with_retry(
                "POST",
                f"{self.base_url}/v2/orders",
                headers=self.headers,
                json_data=payload,
                timeout=10.0,
            )
        except Exception:
            _circuit_breaker.record_failure()
            raise
        if resp.status_code not in (200, 201):
            _circuit_breaker.record_failure()
            raise Exception(f"Order placement failed {resp.status_code}: {resp.text}")
        _circuit_breaker.record_success()
        return resp.json()

    async def async_place_option_order(
        self,
        underlying: str,
        strike_price: int,
        option_type: str,
        expiry: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
        price: float = 0,
        trigger_price: float = 0,
        tag: str = "AlgoForge",
    ) -> dict:
        """Place an options order via httpx (true async)."""
        security_id = ScripMaster.lookup(underlying, strike_price, expiry, option_type)
        if not security_id:
            raise Exception(f"Cannot find security ID for {underlying} {strike_price}{option_type} expiry {expiry}.")
        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"
        return await self.async_place_order(
            security_id=security_id,
            exchange_segment=exchange_seg,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
        )

    async def async_place_sl_order(
        self,
        underlying: str,
        strike_price: int,
        option_type: str,
        expiry: str,
        transaction_type: str,
        quantity: int,
        trigger_price: float,
        price: float = 0,
        product_type: str = "INTRADAY",
        order_type: str = "SL",
        tag: str = "AlgoForge_SL",
    ) -> dict:
        """Place a stop-loss order via httpx (true async)."""
        security_id = ScripMaster.lookup(underlying, strike_price, expiry, option_type)
        if not security_id:
            raise Exception(f"Cannot find security ID for SL: {underlying} {strike_price}{option_type}")
        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"
        if order_type == "SL-M":
            price = 0
        return await self.async_place_order(
            security_id=security_id,
            exchange_segment=exchange_seg,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
        )

    async def async_cancel_order(self, order_id: str) -> dict:
        """Cancel an order via httpx (true async)."""
        resp = await _async_request_with_retry(
            "DELETE",
            f"{self.base_url}/v2/orders/{order_id}",
            headers=self.headers,
            timeout=10.0,
        )
        return resp.json()

    async def async_get_ltp(self, security_ids: list, exchange_segment: str = "NSE_EQ") -> dict:
        """Get LTP via httpx (true async)."""
        int_ids = [int(sid) for sid in security_ids]
        payload = {
            "NSE_EQ": int_ids if exchange_segment == "NSE_EQ" else [],
            "NSE_FNO": int_ids if exchange_segment == "NSE_FNO" else [],
            "BSE_FNO": int_ids if exchange_segment == "BSE_FNO" else [],
            "IDX_I": int_ids if exchange_segment == "IDX_I" else [],
        }
        resp = await _async_request_with_retry(
            "POST",
            f"{self.base_url}/v2/marketfeed/ltp",
            headers=self.headers,
            json_data=payload,
            timeout=10.0,
        )
        if resp.status_code != 200:
            raise Exception(f"LTP fetch failed: {resp.text}")
        return resp.json().get("data", {})

    async def async_get_option_ltp(self, underlying: str, strike: int, expiry: str, option_type: str) -> float:
        """Get single option LTP via httpx (true async)."""
        security_id = ScripMaster.lookup(underlying, strike, expiry, option_type)
        if not security_id:
            return 0.0
        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"
        try:
            data = await self.async_get_ltp([security_id], exchange_segment=exchange_seg)
            if isinstance(data, dict):
                seg_data = data.get(exchange_seg, {})
                if isinstance(seg_data, dict):
                    sid_data = seg_data.get(str(security_id), seg_data.get(int(security_id), {}))
                    if isinstance(sid_data, dict):
                        return float(sid_data.get("last_price", sid_data.get("ltp", 0)))
                    elif isinstance(sid_data, (int, float)):
                        return float(sid_data)
            return 0.0
        except Exception:
            return 0.0

    # ──────────────────────────────────────────────────────────
    # Thread-offloaded async wrappers (for non-hot-path calls)
    # ──────────────────────────────────────────────────────────
    async def async_get_historical_data(self, *a, **kw) -> pd.DataFrame:
        return await asyncio.to_thread(self.get_historical_data, *a, **kw)

    async def async_get_positions(self) -> list:
        return await asyncio.to_thread(self.get_positions_cached)

    async def async_get_funds(self) -> dict:
        return await asyncio.to_thread(self.get_funds_cached)

    async def async_get_order_book(self) -> list:
        return await asyncio.to_thread(self.get_order_book)

    async def async_verify_order_fill(self, order_id: str, max_wait_sec: int = 15) -> dict:
        return await asyncio.to_thread(self.verify_order_fill, order_id, max_wait_sec)
