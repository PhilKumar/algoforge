"""
engine/market_feed.py — Real-time WebSocket Market Feed + Candle Aggregator

Uses Dhan's MarketFeed WebSocket (wss://api-feed.dhan.co) to get tick-level
market data and aggregates into OHLCV candles of ANY timeframe.

Architecture:
  ┌────────────────┐      ticks       ┌─────────────────┐   candle_close   ┌──────────────┐
  │ Dhan WebSocket │  ──────────────► │ CandleAggregator│ ────────────────►│ Paper / Live │
  │ (background    │   LTP updates    │ (any timeframe)  │   + indicators   │   Engine     │
  │  thread)       │                  │  1m, 3m, 5m...  │                  │              │
  └────────────────┘                  └─────────────────┘                  └──────────────┘

Does NOT affect backtesting at all — this is a pure live-data module.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

# Phase 4: Fast JSON deserialization for WebSocket tick payloads
try:
    import orjson as _json_mod

    _json_loads = _json_mod.loads  # ~5-10x faster than stdlib json.loads
    _json_dumps = _json_mod.dumps
    _FAST_JSON = True
except ImportError:
    import json as _json_mod

    _json_loads = _json_mod.loads
    _json_dumps = _json_mod.dumps
    _FAST_JSON = False

# IST timezone (UTC+5:30) — Dhan operates on Indian market time
IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    """Return current time in IST (naive, for slot/log use)."""
    return datetime.now(IST).replace(tzinfo=None)


import pandas as pd

import config
from broker.dhan import DhanClient, ScripMaster

# Try importing dhanhq MarketFeed, fall back gracefully
try:
    try:
        from dhanhq.marketfeed import MarketFeed  # v2.2.0+
    except ImportError:
        from dhanhq.marketfeed import DhanFeed as MarketFeed  # v2.0.x
    HAS_DHAN_FEED = True
    # v2.0.x DhanFeed doesn't have Ticker/Quote/Full constants — define fallbacks
    _TICKER = getattr(MarketFeed, "Ticker", 15)
    _QUOTE = getattr(MarketFeed, "Quote", 17)
    _FULL = getattr(MarketFeed, "Full", 21)
except ImportError:
    HAS_DHAN_FEED = False
    _TICKER = 15
    _QUOTE = 17
    _FULL = 21
    MarketFeed = None
    print("[FEED] ⚠ dhanhq not installed — WebSocket feed unavailable, REST polling fallback active")


# ════════════════════════════════════════════════════════════════
#  Candle Aggregator — aggregates ticks into OHLCV candles
# ════════════════════════════════════════════════════════════════


class CandleAggregator:
    """
    Aggregates streaming LTP ticks into OHLCV candles of arbitrary timeframe.

    Usage:
        agg = CandleAggregator(timeframe_minutes=5)
        agg.on_candle_close = my_callback   # called with (candle_df, latest_candle)
        agg.feed_tick(price=24500.5, volume=100, ts=datetime.now())
    """

    def __init__(self, timeframe_minutes: int = 5, max_candles: int = 500):
        self.tf = timeframe_minutes
        self.max_candles = max_candles

        # Current forming candle
        self._current: Optional[dict] = None
        self._current_slot: Optional[datetime] = None

        # Completed candles
        self.candles: List[dict] = []

        # Callbacks: fired when a candle closes (with full DataFrame + latest)
        self.on_candle_close: List[Callable] = []

    def _get_slot(self, ts: datetime) -> datetime:
        """Get the candle slot start time for a given timestamp."""
        minute = ts.minute
        slot_minute = (minute // self.tf) * self.tf
        return ts.replace(minute=slot_minute, second=0, microsecond=0)

    def feed_tick(self, price: float, volume: int = 0, ts: datetime = None):
        """Feed a tick into the aggregator. Call this on every LTP update."""
        if ts is None:
            ts = _now_ist()

        slot = self._get_slot(ts)

        # New candle slot?
        if self._current_slot is None or slot > self._current_slot:
            # Close previous candle
            if self._current is not None:
                self._close_candle()

            # Start new candle
            self._current_slot = slot
            self._current = {
                "timestamp": slot,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
            }
        else:
            # Update forming candle
            self._current["high"] = max(self._current["high"], price)
            self._current["low"] = min(self._current["low"], price)
            self._current["close"] = price
            self._current["volume"] += volume

    def _close_candle(self):
        """Close the current candle and fire callback."""
        if self._current is None:
            return

        candle = self._current.copy()
        self.candles.append(candle)

        # Trim to max
        if len(self.candles) > self.max_candles:
            self.candles = self.candles[-self.max_candles :]

        # Fire all callbacks
        if self.on_candle_close:
            df = self.to_dataframe()
            for cb in self.on_candle_close:
                try:
                    cb(df, candle)
                except Exception as e:
                    print(f"[FEED] Candle close callback error: {e}")

    def force_close(self):
        """Force-close the current forming candle (e.g. at EOD)."""
        if self._current is not None:
            self._close_candle()
            self._current = None
            self._current_slot = None

    def get_current(self) -> Optional[dict]:
        """Get the currently forming (incomplete) candle."""
        return self._current.copy() if self._current else None

    def to_dataframe(self) -> pd.DataFrame:
        """Convert completed candles to DataFrame with timestamp index."""
        if not self.candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(self.candles)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df


# ════════════════════════════════════════════════════════════════
#  DhanContext wrapper — dhanhq MarketFeed needs a context object
# ════════════════════════════════════════════════════════════════


class _DhanContext:
    """Minimal context object that dhanhq.MarketFeed expects."""

    def __init__(self, client_id: str, access_token: str):
        self._cid = client_id
        self._token = access_token

    def get_client_id(self) -> str:
        return self._cid

    def get_access_token(self) -> str:
        return self._token


# ════════════════════════════════════════════════════════════════
#  LiveMarketFeed — manages WebSocket connection + subscriptions
# ════════════════════════════════════════════════════════════════


class LiveMarketFeed:
    """
    Wrapper around Dhan's MarketFeed WebSocket.

    Provides:
    - Streaming LTP for any subscribed instrument (index + options)
    - Tick-driven candle aggregation (any timeframe)
    - Thread-safe LTP cache
    - Auto-reconnect

    Usage:
        feed = LiveMarketFeed()
        feed.set_candle_callback(timeframe=5, callback=my_func)
        feed.subscribe_index("26000")       # NIFTY
        feed.subscribe_option("NIFTY", 25000, "2026-03-05", "PE")
        feed.start()

        # Later:
        ltp = feed.get_ltp(security_id)   # instant, from cache
    """

    # Map instrument_id → (dhan_security_id, exchange_segment_code)
    INDEX_MAP = {
        "26000": ("13", 0),  # NIFTY 50 on IDX
        "26009": ("25", 0),  # BANK NIFTY on IDX
        "26017": ("27", 0),  # FIN NIFTY on IDX
        "26037": ("442", 0),  # MIDCAP NIFTY on IDX
        "1": ("1", 4),  # SENSEX on BSE
    }

    def __init__(self, dhan: DhanClient = None):
        self.dhan = dhan or DhanClient()
        self._lock = threading.Lock()

        # LTP cache: security_id (int) → {"ltp": float, "time": datetime, ...}
        self._ltp_cache: Dict[int, dict] = {}

        # Subscriptions: list of (exchange_code, security_id, request_type)
        self._subscriptions: List[Tuple[int, int, int]] = []

        # Candle aggregators (keyed by instrument label, e.g. "NIFTY_IDX")
        self._aggregators: Dict[str, CandleAggregator] = {}

        # Index security IDs we need to feed into aggregators
        self._index_sec_ids: Dict[int, str] = {}  # sec_id → label
        # Option security IDs for LTP tracking
        self._option_sec_ids: Dict[int, str] = {}  # sec_id → label

        # Tick callbacks: called on EVERY tick (for live premium tracking)
        self._tick_callbacks: List[Callable] = []

        # WebSocket thread
        self._ws_thread: Optional[threading.Thread] = None
        self._feed: Optional[MarketFeed] = None
        self._running = False

        # Historical data bootstrap (for indicators that need history)
        self._bootstrap_done = False

    # ── Subscription Management ───────────────────────────────

    def subscribe_index(self, instrument_id: str, label: str = None):
        """Subscribe to index LTP (NIFTY, BANKNIFTY, etc.)."""
        info = self.INDEX_MAP.get(instrument_id)
        if not info:
            print(f"[FEED] Unknown instrument: {instrument_id}")
            return
        sec_id, exchange = info
        sec_id_int = int(sec_id)
        if label is None:
            label = f"IDX_{instrument_id}"

        self._index_sec_ids[sec_id_int] = label
        # Subscribe as Ticker (type 15) for speed — just LTP
        self._subscriptions.append((exchange, sec_id_int, _TICKER))
        print(f"[FEED] Subscribed index: {label} (sec_id={sec_id}, exchange={exchange})")

    def subscribe_option(
        self, underlying: str, strike: int, expiry: str, option_type: str, label: str = None
    ) -> Optional[int]:
        """Subscribe to an option contract's LTP. Returns security_id or None."""
        sec_id_str = ScripMaster.lookup(underlying, strike, expiry, option_type)
        if not sec_id_str:
            print(f"[FEED] Cannot subscribe: {underlying} {strike} {option_type} {expiry} — not in ScripMaster")
            return None

        sec_id = int(sec_id_str)
        exchange = 8 if underlying == "SENSEX" else 2  # BSE_FNO or NSE_FNO

        if label is None:
            label = f"{underlying}_{strike}_{option_type}"

        sub_type = _TICKER
        self._option_sec_ids[sec_id] = label
        self._subscriptions.append((exchange, sec_id, sub_type))

        # Dynamically subscribe on the live WebSocket if already running
        if self._running and self._feed:
            try:
                self._feed.subscribe_symbols([(exchange, str(sec_id), sub_type)])
                print(f"[FEED] ✅ Live-subscribed option: {label} (sec_id={sec_id})")
            except Exception as e:
                print(f"[FEED] ⚠ Live subscribe failed ({e}), will get LTP via REST fallback")
        else:
            print(f"[FEED] Queued option: {label} (sec_id={sec_id})")
        return sec_id

    def unsubscribe_option(self, sec_id: int):
        """Remove an option from LTP tracking."""
        with self._lock:
            self._option_sec_ids.pop(sec_id, None)
            self._ltp_cache.pop(sec_id, None)

    # ── Candle Aggregation ────────────────────────────────────

    def set_candle_config(
        self, instrument_id: str, timeframe: int, callback: Callable, history_df: pd.DataFrame = None
    ):
        """
        Set up candle aggregation for an index instrument.

        Args:
            instrument_id: e.g. "26000" for NIFTY
            timeframe: candle width in minutes (1, 3, 5, 7, etc.)
            callback: called with (df, latest_candle) on each candle close
            history_df: optional historical candles to seed the aggregator
        """
        info = self.INDEX_MAP.get(instrument_id)
        if not info:
            return
        sec_id_int = int(info[0])
        label = self._index_sec_ids.get(sec_id_int, f"IDX_{instrument_id}")

        # Use composite key so same instrument with same timeframe shares aggregator
        agg_key = f"{label}_{timeframe}m"

        if agg_key in self._aggregators:
            # Aggregator exists — just add the callback
            agg = self._aggregators[agg_key]
            if callback not in agg.on_candle_close:
                agg.on_candle_close.append(callback)
            print(f"[FEED] Added callback to existing aggregator: {agg_key}")
            return

        agg = CandleAggregator(timeframe_minutes=timeframe, max_candles=500)
        agg.on_candle_close = [callback]

        # Pre-seed with historical candles if provided
        if history_df is not None and not history_df.empty:
            for ts, row in history_df.iterrows():
                agg.candles.append(
                    {
                        "timestamp": ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(row.get("volume", 0)),
                    }
                )
            if agg.candles:
                agg.candles = agg.candles[-agg.max_candles :]

        self._aggregators[agg_key] = agg
        print(f"[FEED] Candle aggregator set: {agg_key}")

    def remove_candle_callback(self, instrument_id: str, timeframe: int, callback: Callable):
        """Remove a specific callback from a candle aggregator."""
        info = self.INDEX_MAP.get(instrument_id)
        if not info:
            return
        sec_id_int = int(info[0])
        label = self._index_sec_ids.get(sec_id_int, f"IDX_{instrument_id}")
        agg_key = f"{label}_{timeframe}m"
        agg = self._aggregators.get(agg_key)
        if agg and callback in agg.on_candle_close:
            agg.on_candle_close.remove(callback)
            print(f"[FEED] Removed callback from aggregator: {agg_key}")

    # ── LTP Access ────────────────────────────────────────────

    def get_ltp(self, sec_id: int) -> float:
        """Get cached LTP for a security. Returns 0 if not available."""
        with self._lock:
            entry = self._ltp_cache.get(sec_id)
            return float(entry["ltp"]) if entry else 0.0

    def get_all_ltp(self) -> dict:
        """Get copy of all cached LTPs."""
        with self._lock:
            return {k: v.copy() for k, v in self._ltp_cache.items()}

    # ── Tick Callbacks ────────────────────────────────────────

    def add_tick_callback(self, cb: Callable):
        """Register a callback for every tick. cb(security_id, ltp, timestamp)"""
        self._tick_callbacks.append(cb)

    # ── WebSocket Lifecycle ───────────────────────────────────

    def start(self):
        """Start the WebSocket feed in a background thread."""
        if not HAS_DHAN_FEED:
            print("[FEED] ⚠ dhanhq MarketFeed not available — cannot start WebSocket")
            return False

        if self._running:
            print("[FEED] Already running")
            return True

        if not self._subscriptions:
            print("[FEED] No subscriptions — nothing to stream")
            return False

        self._running = True

        # Build instrument list for MarketFeed
        instruments = [(ex, str(sid), rtype) for ex, sid, rtype in self._subscriptions]

        # Get fresh token
        token = self.dhan.access_token
        client_id = config.DHAN_CLIENT_ID

        ctx = _DhanContext(client_id, token)
        self._feed = MarketFeed(
            dhan_context=ctx,
            instruments=instruments,
            version="v2",
            on_connect=self._on_connect,
            on_message=self._on_message,
            on_close=self._on_close,
            on_error=self._on_error,
        )

        self._ws_thread = self._feed.start()  # starts in daemon thread
        print(f"[FEED] ✅ WebSocket started — {len(instruments)} instruments subscribed")
        return True

    def stop(self):
        """Stop the WebSocket feed."""
        self._running = False
        if self._feed:
            try:
                self._feed.close_connection()
            except Exception as e:
                print(f"[FEED] Close error: {e}")
            self._feed = None

        # Force-close all aggregators
        for agg in self._aggregators.values():
            agg.force_close()

        print("[FEED] 🛑 WebSocket stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── WebSocket Callbacks ───────────────────────────────────

    def _on_connect(self, ws):
        print(f"[FEED] ✅ WebSocket connected at {_now_ist().strftime('%H:%M:%S')}")

    def _on_close(self, ws):
        print(f"[FEED] ⚠ WebSocket closed at {_now_ist().strftime('%H:%M:%S')}")
        if self._running:
            print("[FEED] Will auto-reconnect on next tick...")

    def _on_error(self, ws, error):
        print(f"[FEED] ❌ WebSocket error: {error}")

    def _on_message(self, ws, data):
        """Process incoming tick data from Dhan WebSocket."""
        if not data or not isinstance(data, dict):
            return

        # Extract security_id and LTP from the tick
        sec_id = data.get("security_id")
        ltp_raw = data.get("LTP", data.get("ltp"))
        if sec_id is None or ltp_raw is None:
            return

        try:
            sec_id = int(sec_id)
            ltp = float(ltp_raw)
        except (ValueError, TypeError):
            return

        if ltp <= 0:
            return

        now = _now_ist()

        # Update LTP cache (thread-safe)
        with self._lock:
            self._ltp_cache[sec_id] = {
                "ltp": ltp,
                "time": now,
                "type": data.get("type", "Ticker Data"),
            }

        # Feed into candle aggregators (for index instruments)
        # Aggregator keys are "{label}_{timeframe}m" — feed all matching this label
        label = self._index_sec_ids.get(sec_id)
        if label:
            vol = int(data.get("volume", 0))
            for agg_key, agg in self._aggregators.items():
                if agg_key.startswith(label + "_"):
                    agg.feed_tick(price=ltp, volume=vol, ts=now)

        # Fire tick callbacks
        for cb in self._tick_callbacks:
            try:
                cb(sec_id, ltp, now)
            except Exception as e:
                print(f"[FEED] Tick callback error: {e}")

    # ── Bootstrap with Historical Data ────────────────────────

    def bootstrap_history(self, instrument_id: str, timeframe: int, days: int = 7) -> pd.DataFrame:
        """
        Fetch historical candle data via REST API to seed indicators.
        Returns the DataFrame for external use too.

        For non-standard timeframes (3m, 7m etc.), fetches 1m candles
        and resamples to the target timeframe.
        """

        try:
            # Lazy import
            inst_map_fn = None
            try:
                from app import INSTRUMENT_MAP

                inst_map_fn = INSTRUMENT_MAP
            except ImportError:
                pass

            info = self.INDEX_MAP.get(instrument_id, {})
            dhan_id = info[0] if info else "13"
            dhan_seg = "IDX_I"
            dhan_type = "INDEX"

            if inst_map_fn:
                iinfo = inst_map_fn.get(instrument_id, {})
                dhan_id = iinfo.get("dhan_id", dhan_id)
                dhan_seg = iinfo.get("dhan_seg", dhan_seg)
                dhan_type = iinfo.get("dhan_type", dhan_type)

            from_date = (_now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
            to_date = _now_ist().strftime("%Y-%m-%d")

            # Dhan supports: 1, 5, 15, 25, 60
            # For non-standard TFs, fetch 1m and resample
            standard_tfs = {1, 5, 15, 25, 60}
            if timeframe in standard_tfs:
                fetch_tf = str(timeframe)
            else:
                fetch_tf = "1"  # fetch 1m, will resample

            df_raw = self.dhan.get_historical_data(
                security_id=dhan_id,
                exchange_segment=dhan_seg,
                instrument_type=dhan_type,
                from_date=from_date,
                to_date=to_date,
                candle_type=fetch_tf,
            )

            if df_raw.empty:
                print(f"[FEED] Bootstrap returned empty data for {instrument_id}")
                return df_raw

            # Resample if needed
            if timeframe not in standard_tfs and fetch_tf == "1":
                df_raw = self._resample(df_raw, timeframe)

            self._bootstrap_done = True
            print(f"[FEED] Bootstrap: {len(df_raw)} candles @ {timeframe}m for instrument {instrument_id}")
            return df_raw

        except Exception as e:
            print(f"[FEED] Bootstrap error: {e}")
            return pd.DataFrame()

    @staticmethod
    def _resample(df: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
        """Resample a 1m DataFrame to a custom timeframe."""
        rule = f"{tf_minutes}min"
        resampled = (
            df.resample(rule, label="left", closed="left")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["open"])
        )
        return resampled


# ════════════════════════════════════════════════════════════════
#  Singleton Feed Manager (one per app)
# ════════════════════════════════════════════════════════════════

_global_feed: Optional[LiveMarketFeed] = None


def get_market_feed(dhan: DhanClient = None) -> LiveMarketFeed:
    """Get or create the global LiveMarketFeed instance."""
    global _global_feed
    if _global_feed is None:
        _global_feed = LiveMarketFeed(dhan)
    return _global_feed


def shutdown_feed():
    """Shutdown the global feed."""
    global _global_feed
    if _global_feed:
        _global_feed.stop()
        _global_feed = None
