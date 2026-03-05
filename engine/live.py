"""
engine/live.py — Live Auto-Trading Engine
Places REAL orders via Dhan API.
Two modes:
  1. WebSocket mode (fast): LiveMarketFeed pushes ticks → candles aggregate
     → conditions evaluated on candle close → ~1-2 second latency
  2. REST polling mode (fallback): polls Dhan REST API every N seconds
     → conditions evaluated per poll → ~30-90 second latency"""

import asyncio
import time as time_module
from datetime import datetime, time, date as date_type, timedelta, timezone
from typing import List, Dict, Any, Optional

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    """Return current time in IST (naive datetime)."""
    return datetime.now(IST).replace(tzinfo=None)
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine.indicators import compute_dynamic_indicators
from engine.backtest import eval_condition_group, get_lot_size, get_strike_step
from broker.dhan import DhanClient, ScripMaster, UNDERLYING_MAP
import config


# Lazy import to avoid circular dependency
def _get_instrument_map():
    from app import INSTRUMENT_MAP
    return INSTRUMENT_MAP


class LiveEngine:
    """
    Live auto-trading engine.
    - Fetches real market data from Dhan
    - Evaluates entry/exit conditions
    - Places REAL option orders via Dhan API
    - Manages stop-loss orders at broker level
    - Tracks positions, P&L and order status
    """

    def __init__(self, dhan: DhanClient = None, run_id: str = None):
        self.dhan = dhan or DhanClient()
        self.running = False
        self.session_date = None
        self.mode = "auto"  # "auto" for real orders
        self.run_id = run_id  # Unique ID for multi-engine support

        # WebSocket feed (injected from app.py — if available, use event-driven mode)
        self._feed = None          # LiveMarketFeed instance
        self._ws_mode = False      # True when using WebSocket
        self._option_sec_id = None # subscribed option security_id for LTP
        self._candle_event = None  # asyncio.Event set on each candle close
        self._latest_candle_df = None  # DataFrame from candle close callback
        self._latest_candle = None     # Last closed candle dict

        # Strategy + execution config
        self.strategy: dict = {}
        self.deploy_config: dict = {}
        self.entry_conditions: list = []
        self.exit_conditions: list = []

        # Trading state
        self.in_trade = False
        self.positions: List[dict] = []        # Open positions with order info
        self.closed_trades: List[dict] = []    # Completed trades
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.max_daily_loss = 0.0

        # Strategy-level SL/TP (₹ amounts)
        self.strat_sl_val = 0.0
        self.strat_tp_val = 0.0
        self.trade_entry_prem = 0.0
        self._sl_pct = 0.0
        self._sl_rupees = 0.0
        self._tp_pct = 0.0
        self._tp_rupees = 0.0

        # Market data
        self.current_spot = 0.0
        self.current_time: Optional[datetime] = None
        self.candle_buffer = pd.DataFrame()
        self.current_candle: dict = {}
        self.current_indicators: dict = {}
        self._prev_row = None
        self._entry_signal_pending = False  # True = signal fired, enter on NEXT candle  # For crossover detection

        # Logging
        self.event_log: List[dict] = []

    def set_feed(self, feed):
        """Inject a LiveMarketFeed for WebSocket-driven mode."""
        self._feed = feed

    # ── Configuration ─────────────────────────────────────────
    def configure(self, strategy: dict, entry_conditions: list,
                  exit_conditions: list, deploy_config: dict = None):
        """Configure the live trading engine with strategy + execution settings."""
        self.strategy = strategy
        self.entry_conditions = entry_conditions
        self.exit_conditions = exit_conditions
        self.deploy_config = deploy_config or strategy.get("deploy_config", {})
        self.max_daily_loss = float(strategy.get("max_daily_loss", 0) or 0)

        # Pre-compute strategy-level SL/TP values
        self._sl_pct = float(strategy.get("stoploss_pct", 0) or 0)
        self._sl_rupees = float(strategy.get("stoploss_rupees", 0) or 0)
        self._tp_pct = float(strategy.get("target_profit_pct", 0) or 0)
        self._tp_rupees = float(strategy.get("target_profit_rupees", 0) or 0)

        self.log_event("info", f"Strategy configured: {strategy.get('run_name', 'Unnamed')}")
        self.log_event("info", f"Mode: {'Auto Trading (REAL ORDERS)' if self.deploy_config.get('order_type') == 'auto' else 'Paper Testing'}")
        self.log_event("info", f"Product: {self.deploy_config.get('product_type', 'MIS')}")
        self.log_event("info", f"Entry order: {self.deploy_config.get('entry_order', 'MARKET')}")
        if self._sl_rupees > 0 or self._sl_pct > 0:
            self.log_event("info", f"Strategy SL: ₹{self._sl_rupees:,.0f}" if self._sl_rupees > 0 else f"Strategy SL: {self._sl_pct}%")
        if self._tp_rupees > 0 or self._tp_pct > 0:
            self.log_event("info", f"Strategy TP: ₹{self._tp_rupees:,.0f}" if self._tp_rupees > 0 else f"Strategy TP: {self._tp_pct}%")

    # ── Logging ───────────────────────────────────────────────
    def log_event(self, event_type: str, message: str, data: dict = None):
        event = {
            "time": _now_ist(),
            "type": event_type,
            "message": message,
            "data": data or {}
        }
        self.event_log.append(event)
        ts = event["time"].strftime("%H:%M:%S")
        print(f"[LIVE] [{ts}] [{event_type.upper()}] {message}")

    async def _emit(self, callback, event: dict):
        if not callback:
            return
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(event)
            else:
                callback(event)
        except Exception as e:
            print(f"[LIVE] Callback error: {e}")

    # ── Main Loop ─────────────────────────────────────────────
    async def start(self, callback=None):
        """Start the live trading engine."""
        self.running = True
        self.session_date = date_type.today()
        self.daily_pnl = 0.0

        self.log_event("start", "🚀 Live Auto-Trading Engine Started (REAL ORDERS)")
        self.log_event("info", f"Instrument: {self._get_instrument_name()}")
        self.log_event("info", f"Timeframe: {self._get_timeframe()}m")
        self.log_event("info", f"Max trades/day: {self.strategy.get('max_trades_per_day', 1)}")
        if self.max_daily_loss > 0:
            self.log_event("info", f"Max daily loss: ₹{self.max_daily_loss:,.0f}")

        # Pre-load scrip master for option lookup
        self.log_event("info", "Loading scrip master for option security IDs...")
        try:
            loaded = ScripMaster.ensure_loaded()
            if loaded:
                symbol = self._get_underlying_symbol()
                expiry = ScripMaster.get_nearest_expiry(symbol)
                self.log_event("info", f"Scrip master loaded. Nearest {symbol} expiry: {expiry}")
            else:
                self.log_event("warning", "Scrip master not loaded — option orders may fail")
        except Exception as e:
            self.log_event("error", f"Scrip master load error: {e}")

        # ── Choose mode: WebSocket (fast) or REST polling (fallback) ──
        self._ws_mode = self._feed is not None and self._feed.is_running

        if self._ws_mode:
            self.log_event("info", "⚡ Mode: WebSocket (event-driven, ~1-2s latency)")
            await self._run_ws_mode(callback)
        else:
            self.log_event("info", "🔄 Mode: REST polling (fallback)")
            poll_interval = self.strategy.get("poll_interval", config.POLL_INTERVAL_SEC)
            while self.running:
                try:
                    await self._tick(callback)
                except Exception as e:
                    self.log_event("error", f"Tick error: {str(e)}")
                    import traceback
                    traceback.print_exc()
                await asyncio.sleep(poll_interval)

    def stop(self):
        """Stop the live trading engine."""
        self.running = False

        # Force close open positions
        if self.positions:
            self.log_event("warning", f"Engine stopping with {len(self.positions)} open positions!")
            for pos in self.positions:
                self.log_event("warning",
                    f"⚠ Open position: {pos.get('underlying','')} "
                    f"{pos.get('strike','')}{pos.get('option_type','')} "
                    f"Order ID: {pos.get('entry_order_id','?')}")

        total_pnl = sum(t.get("pnl", 0) for t in self.closed_trades)
        win_count = len([t for t in self.closed_trades if t.get("pnl", 0) > 0])

        self.log_event("stop", "🛑 Live Auto-Trading Engine Stopped")
        self.log_event("info",
            f"Session: {len(self.closed_trades)} trades | "
            f"Winners: {win_count} | P&L: ₹{total_pnl:,.2f}")

    # ── WebSocket Event-Driven Mode ───────────────────────────
    async def _run_ws_mode(self, callback=None):
        """
        WebSocket-driven loop — same logic as paper_trading._run_ws_mode()
        but places REAL orders on entry/exit.
        """
        from engine.indicators import compute_dynamic_indicators

        instrument = self.strategy.get("instrument", "26000")
        timeframe = self._get_timeframe()

        # Set up candle-close event (asyncio-safe from thread)
        loop = asyncio.get_event_loop()
        self._candle_event = asyncio.Event()

        def _on_candle_close(df, candle):
            """Called from WebSocket thread when a candle closes."""
            self._latest_candle_df = df
            self._latest_candle = candle
            loop.call_soon_threadsafe(self._candle_event.set)

        # Bootstrap history for indicator warm-up
        history_df = self._feed.bootstrap_history(instrument, timeframe, days=7)
        indicators = self.strategy.get("indicators", [])

        self._feed.set_candle_config(
            instrument_id=instrument,
            timeframe=timeframe,
            callback=_on_candle_close,
            history_df=history_df,
        )

        self.log_event("info", f"📊 Candle aggregation: {timeframe}m ({len(history_df)} historical candles)")

        # ── Immediately populate UI data from bootstrap history ──
        if not history_df.empty:
            try:
                df_init = compute_dynamic_indicators(history_df.copy(), indicators)
                if not df_init.empty:
                    self.candle_buffer = df_init
                    self.current_spot = float(df_init.iloc[-1].get("close", 0))
                    self._update_ui_data(df_init.iloc[-1])
                    self.log_event("info", f"📈 Initial UI data: spot={self.current_spot:.2f}")
            except Exception as e:
                self.log_event("warning", f"Bootstrap UI init failed: {e}")

        # Main event loop
        while self.running:
            try:
                now = _now_ist()
                self.current_time = now

                # New day reset
                if now.date() != self.session_date:
                    self.trades_today = 0
                    self.daily_pnl = 0.0
                    self.session_date = now.date()
                    self.log_event("info", f"📅 New trading day: {self.session_date}")

                # Market hours check
                market_open_str = self.strategy.get("market_open", "09:15")
                market_close_str = self.strategy.get("market_close", "15:25")
                if isinstance(market_open_str, str):
                    h, m = map(int, market_open_str.split(":"))
                    market_open = time(h, m)
                else:
                    market_open = market_open_str
                if isinstance(market_close_str, str):
                    h, m = map(int, market_close_str.split(":"))
                    market_close = time(h, m)
                else:
                    market_close = market_close_str

                if not (market_open <= now.time() <= market_close):
                    await asyncio.sleep(5)
                    if callback:
                        await self._emit(callback, {"type": "status", "message": "Outside market hours"})
                    continue

                # Update current spot from feed cache
                from engine.market_feed import LiveMarketFeed
                idx_info = LiveMarketFeed.INDEX_MAP.get(instrument)
                if idx_info:
                    spot = self._feed.get_ltp(int(idx_info[0]))
                    if spot > 0:
                        self.current_spot = spot
                        # Keep UI candle fresh between closes
                        if self.current_candle:
                            self.current_candle['close'] = spot
                            self.current_candle['updated_at'] = _now_ist().strftime('%Y-%m-%d %I:%M:%S %p')
                            if spot > self.current_candle.get('high', 0):
                                self.current_candle['high'] = spot
                            if spot < self.current_candle.get('low', float('inf')):
                                self.current_candle['low'] = spot

                # ── Monitor positions every 1 second ──
                if self.in_trade:
                    for pos in list(self.positions):
                        if pos.get("status") == "closed":
                            continue
                        # Get LTP from feed cache (instant)
                        current_premium = self._get_premium_from_feed(pos)
                        # REST fallback every ~5s if WS has no data yet
                        if current_premium <= 0:
                            rest_counter = pos.get("_rest_counter", 0) + 1
                            pos["_rest_counter"] = rest_counter
                            if rest_counter % 5 == 1:
                                try:
                                    current_premium = self.dhan.get_option_ltp(
                                        pos.get("underlying", ""),
                                        int(pos["strike"]),
                                        pos["expiry"], pos["option_type"]
                                    ) or 0.0
                                except Exception:
                                    current_premium = 0.0
                        if current_premium > 0:
                            pos["current_premium"] = current_premium
                            direction = 1 if pos["transaction_type"] == "BUY" else -1
                            pos["unrealized_pnl"] = round(
                                (current_premium - pos["entry_premium"]) *
                                direction * pos["lots"] * pos["lot_size"], 2
                            )

                        # Check exit conditions
                        latest_row = self.candle_buffer.iloc[-1] if not self.candle_buffer.empty else None
                        if latest_row is not None:
                            exit_reason = self._check_exit_conditions(pos, latest_row, pos["current_premium"])
                            if exit_reason:
                                await self._exit_position(pos, exit_reason, pos["current_premium"], callback)

                # ── Wait for candle close event ──
                try:
                    await asyncio.wait_for(self._candle_event.wait(), timeout=1.0)
                    self._candle_event.clear()
                except asyncio.TimeoutError:
                    # No new candle — but execute pending entry immediately
                    if self._entry_signal_pending and not self.in_trade:
                        max_trades = self.strategy.get("max_trades_per_day", 1)
                        daily_loss_hit = self.max_daily_loss > 0 and self.daily_pnl <= -self.max_daily_loss
                        if self.trades_today < max_trades and not daily_loss_hit:
                            self._entry_signal_pending = False
                            latest_row = self.candle_buffer.iloc[-1] if not self.candle_buffer.empty else None
                            if latest_row is not None:
                                self.log_event("entry", f"🚀 Executing pending entry at {_now_ist().strftime('%H:%M:%S')} (next candle open)")
                                await self._enter_trade(latest_row, callback)
                    if callback:
                        await self._emit(callback, self.get_status())
                    continue

                # ── Candle closed — evaluate conditions ──
                candle_df = self._latest_candle_df
                latest_candle = self._latest_candle

                if candle_df is None or candle_df.empty:
                    continue

                df_with_indicators = compute_dynamic_indicators(candle_df, indicators)
                self.candle_buffer = df_with_indicators

                if df_with_indicators.empty:
                    continue

                latest_row = df_with_indicators.iloc[-1]
                self.current_spot = float(latest_row.get("close", self.current_spot))

                self._update_ui_data(latest_row)

                candle_time = latest_candle.get("timestamp", now)
                latency = (now - candle_time).total_seconds() if isinstance(candle_time, datetime) else 0
                self.log_event("candle", f"🕯️ {timeframe}m candle @ {self.current_spot:.2f} (latency: {latency:.1f}s)")

                # Check entry
                max_trades = self.strategy.get("max_trades_per_day", 1)
                daily_loss_hit = self.max_daily_loss > 0 and self.daily_pnl <= -self.max_daily_loss

                if not self.in_trade and self.trades_today < max_trades and not daily_loss_hit:
                    # Execute pending signal from previous candle (enter on THIS candle's open)
                    if self._entry_signal_pending:
                        self._entry_signal_pending = False
                        self.log_event("entry", f"🚀 Executing pending entry (next candle open)")
                        await self._enter_trade(latest_row, callback)
                    else:
                        prev_row = df_with_indicators.iloc[-2] if len(df_with_indicators) >= 2 else None
                        entry_sig = eval_condition_group(latest_row, self.entry_conditions, prev_row)
                        if entry_sig:
                            self._entry_signal_pending = True
                            self.log_event("signal", f"⚡ ENTRY SIGNAL — will enter on NEXT candle open")

                self._prev_row = latest_row

                if callback:
                    await self._emit(callback, self.get_status())

            except Exception as e:
                self.log_event("error", f"WS mode error: {str(e)}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(1)

    def _get_premium_from_feed(self, pos: dict) -> float:
        """Get option premium from WebSocket feed's LTP cache (instant)."""
        if not self._feed:
            return 0.0
        sec_id = pos.get("ws_sec_id")
        if sec_id:
            ltp = self._feed.get_ltp(sec_id)
            if ltp > 0:
                return ltp
        return 0.0

    def _update_ui_data(self, row):
        """Store latest candle + indicator values for the live monitor UI."""
        try:
            self.current_candle = {
                "open": round(float(row.get("open", 0)), 2),
                "high": round(float(row.get("high", 0)), 2),
                "low": round(float(row.get("low", 0)), 2),
                "close": round(float(row.get("close", 0)), 2),
                "volume": int(row.get("volume", 0)),
                "updated_at": _now_ist().strftime("%Y-%m-%d %I:%M:%S %p"),
            }
            ohlcv_cols = {
                "open", "high", "low", "close", "volume", "oi", "timestamp",
                "date", "datetime", "time_of_day",
                "current_open", "current_high", "current_low", "current_close",
                "yesterday_open", "yesterday_high", "yesterday_low", "yesterday_close",
                "cpr_type", "pivot", "bc", "tc", "cpr_range", "cpr_width_pct",
                "cpr_is_narrow", "supertrend_dir",
            }
            self.current_indicators = {}
            for col in self.candle_buffer.columns:
                if col in ohlcv_cols:
                    continue
                try:
                    val = row[col]
                    if pd.isna(val):
                        continue
                    self.current_indicators[col] = round(float(val), 2)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

    # ── Tick ──────────────────────────────────────────────────
    async def _tick(self, callback=None):
        """Single tick — fetch data, evaluate conditions, manage trades."""
        now = _now_ist()
        self.current_time = now
        cur_time = now.time()

        # New day reset
        if now.date() != self.session_date:
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.session_date = now.date()
            self.log_event("info", f"📅 New trading day: {self.session_date}")

        # Market hours check
        market_open_str = self.strategy.get("market_open", "09:15")
        market_close_str = self.strategy.get("market_close", "15:25")
        if isinstance(market_open_str, str):
            h, m = map(int, market_open_str.split(":"))
            market_open = time(h, m)
        else:
            market_open = market_open_str
        if isinstance(market_close_str, str):
            h, m = map(int, market_close_str.split(":"))
            market_close = time(h, m)
        else:
            market_close = market_close_str

        if not (market_open <= cur_time <= market_close):
            if callback:
                await self._emit(callback, {"type": "status", "message": "Outside market hours"})
            return

        # Fetch live candle data
        try:
            df = await self._fetch_live_data()
            if df.empty:
                return
            self.candle_buffer = df
            self.current_spot = float(df["close"].iloc[-1])
            if callback:
                await self._emit(callback, {
                    "type": "price_update",
                    "spot": self.current_spot,
                    "time": str(now)
                })
        except Exception as e:
            self.log_event("error", f"Data fetch error: {e}")
            return

        latest_row = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) >= 2 else None

        # ── Manage open positions ──
        for pos in list(self.positions):
            if pos.get("status") == "closed":
                continue

            # Get current option premium
            current_premium = await self._get_current_premium(pos)
            pos["current_premium"] = current_premium

            # Calculate unrealized P&L
            direction = 1 if pos["transaction_type"] == "BUY" else -1
            pos["unrealized_pnl"] = round(
                (current_premium - pos["entry_premium"]) *
                direction * pos["lots"] * pos["lot_size"], 2
            )

            # Check exit conditions
            exit_reason = self._check_exit_conditions(pos, latest_row, current_premium)
            if exit_reason:
                await self._exit_position(pos, exit_reason, current_premium, callback)

        # ── Check entry conditions ──
        max_trades = self.strategy.get("max_trades_per_day", 1)
        daily_loss_hit = (self.max_daily_loss > 0 and
                         self.daily_pnl <= -self.max_daily_loss)

        if daily_loss_hit and not self.in_trade:
            pass  # Skip — daily loss limit hit
        elif self.trades_today < max_trades and not self.in_trade:
            # Execute pending signal from previous tick (enter on THIS candle)
            if self._entry_signal_pending:
                self._entry_signal_pending = False
                self.log_event("entry", f"🚀 Executing pending entry (next candle open)")
                await self._enter_trade(latest_row, callback)
            else:
                entry_sig = eval_condition_group(latest_row, self.entry_conditions, prev_row)
                if entry_sig:
                    self._entry_signal_pending = True
                    self.log_event("signal", f"⚡ ENTRY SIGNAL — will enter on NEXT candle open")

        self._prev_row = latest_row

        # Send status update
        if callback:
            await self._emit(callback, self.get_status())

    # ── Data Fetch ────────────────────────────────────────────
    async def _fetch_live_data(self) -> pd.DataFrame:
        """Fetch live candle data with indicators applied."""
        timeframe = self._get_timeframe()
        valid_intervals = [1, 5, 15, 25, 60]
        resample_from_1m = timeframe not in valid_intervals
        fetch_tf = 1 if resample_from_1m else timeframe

        instrument = self.strategy.get("instrument", "26000")
        from_date = (_now_ist() - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = _now_ist().strftime("%Y-%m-%d")

        inst_map = _get_instrument_map()
        inst_info = inst_map.get(instrument, {})

        df_raw = self.dhan.get_historical_data(
            security_id=inst_info.get("dhan_id", "13"),
            exchange_segment=inst_info.get("dhan_seg", "IDX_I"),
            instrument_type=inst_info.get("dhan_type", "INDEX"),
            from_date=from_date,
            to_date=to_date,
            candle_type=str(fetch_tf),
        )

        # Resample 1m candles to non-standard timeframe (e.g. 3m, 7m)
        if resample_from_1m and not df_raw.empty:
            rule = f"{timeframe}min"
            df_raw = df_raw.resample(rule).agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna(subset=["open"])

        indicators = self.strategy.get("indicators", [])
        df = compute_dynamic_indicators(df_raw, indicators)

        # Store current candle + indicators for UI
        if not df.empty:
            last = df.iloc[-1]
            self.current_candle = {
                "open": round(float(last.get("open", 0)), 2),
                "high": round(float(last.get("high", 0)), 2),
                "low": round(float(last.get("low", 0)), 2),
                "close": round(float(last.get("close", 0)), 2),
                "volume": int(last.get("volume", 0)),
                "openInterest": int(last.get("oi", 0)),
                "updated_at": _now_ist().strftime("%Y-%m-%d %I:%M:%S %p"),
            }
            ohlcv_cols = {
                "open", "high", "low", "close", "volume", "oi", "timestamp",
                "date", "datetime", "time_of_day",
                "current_open", "current_high", "current_low", "current_close",
                "yesterday_open", "yesterday_high", "yesterday_low", "yesterday_close",
                "cpr_type", "pivot", "bc", "tc", "cpr_range", "cpr_width_pct",
                "cpr_is_narrow", "supertrend_dir",
            }
            self.current_indicators = {}
            for col in df.columns:
                if col in ohlcv_cols:
                    continue
                try:
                    val = last[col]
                    if pd.isna(val):
                        continue
                    self.current_indicators[col] = round(float(val), 2)
                except (TypeError, ValueError):
                    pass

        return df.tail(200)

    # ── Option Premium ────────────────────────────────────────
    async def _get_current_premium(self, pos: dict) -> float:
        """Fetch current premium for an option position from Dhan LTP API."""
        # Try WebSocket feed first (instant, no API call)
        if self._ws_mode and self._feed:
            ws_ltp = self._get_premium_from_feed(pos)
            if ws_ltp > 0:
                return ws_ltp

        try:
            ltp = self.dhan.get_option_ltp(
                underlying=pos["underlying"],
                strike=pos["strike"],
                expiry=pos["expiry"],
                option_type=pos["option_type"],
            )
            if ltp > 0:
                return ltp
        except Exception as e:
            self.log_event("error", f"LTP fetch failed for {pos['underlying']} "
                          f"{pos['strike']}{pos['option_type']}: {e}")

        # Fallback: estimate based on spot movement
        spot_change_pct = (
            (self.current_spot - pos["entry_spot"]) / pos["entry_spot"]
            if pos["entry_spot"] else 0
        )
        delta = 0.5 if pos["option_type"] == "CE" else -0.5
        est = pos["entry_premium"] * (1 + spot_change_pct * delta * 2)
        return max(0.5, round(est, 2))

    # ── Entry ─────────────────────────────────────────────────
    async def _enter_trade(self, row: pd.Series, callback=None):
        """Enter trade: place real orders for each leg."""
        self.log_event("signal", "✅ ENTRY CONDITIONS MET", {
            "spot": self.current_spot,
            "time": str(self.current_time),
        })

        legs = self.strategy.get("legs", [])
        if not legs:
            self.log_event("warning", "No legs configured — cannot enter trade")
            return

        instrument = self.strategy.get("instrument", "26000")
        underlying = ScripMaster.instrument_to_symbol(instrument)
        strike_step = get_strike_step(instrument)
        expiry = ScripMaster.get_nearest_expiry(underlying)
        if not expiry:
            self.log_event("error", f"No expiry found for {underlying} — cannot place order")
            return

        lot_size = ScripMaster.get_lot_size(underlying, expiry)
        if lot_size == 0:
            lot_size = get_lot_size(instrument, self.session_date)

        # Deploy config for execution
        product_type = self.deploy_config.get("product_type", "MIS")
        if product_type == "NRML":
            product_type = "MARGIN"  # Dhan uses MARGIN for NRML
        entry_order_type = self.deploy_config.get("entry_order", "MARKET")
        place_leg_sl = self.deploy_config.get("place_leg_sl", "no") == "yes"
        sqoff_on_fail = self.deploy_config.get("sqoff_on_fail", "no") == "yes"

        entered_positions = []
        any_failed = False

        for i, leg in enumerate(legs):
            strike_type = leg.get("strike_type", "atm")
            strike_value = leg.get("strike_value", 0)
            opt_type = leg.get("option_type", "CE")
            txn_type = leg.get("transaction_type", "BUY")
            lots = leg.get("lots", 1)
            quantity = lots * lot_size

            # For premium-based strike types, scan real LTP to find correct strike
            if strike_type in ("premium_near", "premium_above", "premium_below") and expiry and float(strike_value or 0) > 0:
                mode = strike_type.split("_")[1]  # "near", "above", "below"
                strike = await self._find_premium_strike(
                    underlying, expiry, opt_type, float(strike_value),
                    self.current_spot, strike_step, mode=mode
                )
                self.log_event("info", f"🎯 {strike_type} target=₹{strike_value} → strike={strike}")
            else:
                strike = self._calculate_strike(leg, self.current_spot, strike_step)

            # Trading symbol for display
            trading_symbol = f"{underlying} {strike}{opt_type} {expiry}"

            self.log_event("entry",
                f"🦿 Leg {i+1}: {txn_type} {lots}x {strike}{opt_type} "
                f"({entry_order_type}, {product_type})")

            # Place the entry order
            try:
                result = self.dhan.place_option_order(
                    underlying=underlying,
                    strike_price=strike,
                    option_type=opt_type,
                    expiry=expiry,
                    transaction_type=txn_type,
                    quantity=quantity,
                    order_type=entry_order_type,
                    product_type=product_type,
                    tag=f"AF_E{i+1}_{opt_type}_{strike}",
                )
                order_id = result.get("orderId", "")
                self.log_event("order",
                    f"✅ Order placed: {txn_type} {trading_symbol} | "
                    f"OrderID: {order_id}")
            except Exception as e:
                self.log_event("error",
                    f"❌ Order FAILED for Leg {i+1}: {e}")
                any_failed = True
                if sqoff_on_fail and entered_positions:
                    self.log_event("warning",
                        "Square-off triggered due to failed entry")
                    for pos in entered_positions:
                        await self._exit_position(pos, "ENTRY_FAIL_SQOFF",
                                                   pos["entry_premium"], callback)
                    return
                continue

            # Get fill price (use LTP as estimate — real fill comes from order book)
            entry_premium = self.dhan.get_option_ltp(
                underlying, strike, expiry, opt_type
            ) or self._estimate_premium(strike, self.current_spot, opt_type, strike_step)

            position = {
                "id": len(self.positions) + len(self.closed_trades) + 1,
                "leg_num": i + 1,
                "symbol": f"{underlying} {strike} {opt_type}",
                "underlying": underlying,
                "transaction_type": txn_type,
                "option_type": opt_type,
                "strike": strike,
                "expiry": expiry,
                "entry_time": self.current_time,
                "entry_spot": self.current_spot,
                "entry_premium": entry_premium,
                "current_premium": entry_premium,
                "lots": lots,
                "lot_size": lot_size,
                "quantity": quantity,
                "sl_pct": leg.get("sl_pct", 0),
                "target_pct": leg.get("target_pct", 0),
                "sl_points": leg.get("sl_points", 0),
                "target_points": leg.get("target_points", 0),
                "sl_rupees": leg.get("sl_rupees", 0),
                "target_rupees": leg.get("target_rupees", 0),
                "trail_pct": leg.get("trail_pct", 0),
                "sqoff_time": leg.get("sqoff_time",
                              self.strategy.get("market_close", "15:20")),
                "unrealized_pnl": 0,
                "peak_premium": entry_premium,
                "entry_order_id": order_id,
                "sl_order_id": None,
                "exit_order_id": None,
                "trading_symbol": trading_symbol,
                "symbol": trading_symbol,  # For UI display
                "status": "open",
                "ws_sec_id": None,  # Will be set if WebSocket mode
            }

            # Subscribe option to WebSocket feed for instant LTP tracking
            if self._ws_mode and self._feed:
                ws_sec_id = self._feed.subscribe_option(underlying, strike, expiry, opt_type)
                if ws_sec_id:
                    position["ws_sec_id"] = ws_sec_id
                    self._option_sec_id = ws_sec_id
                    self.log_event("info", f"⚡ Option subscribed to WebSocket: sec_id={ws_sec_id}")

            # Place SL order to broker if configured
            if place_leg_sl and leg.get("sl_pct", 0) > 0:
                await self._place_sl_order(position)

            self.positions.append(position)
            entered_positions.append(position)

        if entered_positions:
            self.in_trade = True
            self.trades_today += 1

            # Compute strategy-level SL/TP based on first leg entry
            pos0 = entered_positions[0]
            ep0 = pos0["entry_premium"]
            qty0 = pos0["lots"] * pos0["lot_size"]
            self.trade_entry_prem = ep0

            if self._sl_rupees > 0:
                self.strat_sl_val = self._sl_rupees
            elif self._sl_pct > 0:
                self.strat_sl_val = ep0 * qty0 * self._sl_pct / 100
            else:
                self.strat_sl_val = 0

            if self._tp_rupees > 0:
                self.strat_tp_val = self._tp_rupees
            elif self._tp_pct > 0:
                self.strat_tp_val = ep0 * qty0 * self._tp_pct / 100
            else:
                self.strat_tp_val = 0

            if self.strat_sl_val > 0:
                self.log_event("info", f"🛡️ Strategy SL: ₹{self.strat_sl_val:,.0f}")
            if self.strat_tp_val > 0:
                self.log_event("info", f"🎯 Strategy TP: ₹{self.strat_tp_val:,.0f}")

            self.log_event("info",
                f"📊 Trade #{self.trades_today}: {len(entered_positions)} legs opened")
        else:
            self.log_event("error", "No legs could be entered")

    # ── SL Order Placement ────────────────────────────────────
    async def _place_sl_order(self, pos: dict):
        """Place a stop-loss order at the broker for a position."""
        sl_pct = pos.get("sl_pct", 0)
        if sl_pct <= 0:
            return

        opposite_txn = "SELL" if pos["transaction_type"] == "BUY" else "BUY"

        # Calculate SL trigger price
        if pos["transaction_type"] == "BUY":
            trigger = round(pos["entry_premium"] * (1 - sl_pct / 100), 2)
        else:
            trigger = round(pos["entry_premium"] * (1 + sl_pct / 100), 2)

        # SL-Limit price difference from deploy config
        sl_limit_diff = float(self.deploy_config.get("sl_limit_diff_pct", 1) or 1)
        if pos["transaction_type"] == "BUY":
            limit_price = round(trigger * (1 - sl_limit_diff / 100), 2)
        else:
            limit_price = round(trigger * (1 + sl_limit_diff / 100), 2)

        product_type = self.deploy_config.get("product_type", "MIS")
        if product_type == "NRML":
            product_type = "MARGIN"

        try:
            result = self.dhan.place_sl_order(
                underlying=pos["underlying"],
                strike_price=pos["strike"],
                option_type=pos["option_type"],
                expiry=pos["expiry"],
                transaction_type=opposite_txn,
                quantity=pos["quantity"],
                trigger_price=trigger,
                price=limit_price,
                product_type=product_type,
                order_type="SL",
                tag=f"AF_SL_{pos['option_type']}_{pos['strike']}",
            )
            pos["sl_order_id"] = result.get("orderId", "")
            self.log_event("order",
                f"🛡 SL order placed for Leg {pos['leg_num']}: "
                f"trigger=₹{trigger} limit=₹{limit_price} "
                f"OrderID: {pos['sl_order_id']}")
        except Exception as e:
            self.log_event("error",
                f"SL order failed for Leg {pos['leg_num']}: {e}")

    # ── Exit ──────────────────────────────────────────────────
    async def _exit_position(self, pos: dict, reason: str,
                              exit_premium: float, callback=None):
        """Exit a position: place exit order and cancel SL order."""
        opposite_txn = "SELL" if pos["transaction_type"] == "BUY" else "BUY"

        exit_order_type = self.deploy_config.get("exit_order", "MARKET")
        product_type = self.deploy_config.get("product_type", "MIS")
        if product_type == "NRML":
            product_type = "MARGIN"

        # Cancel existing SL order first
        if pos.get("sl_order_id"):
            try:
                self.dhan.cancel_order(pos["sl_order_id"])
                self.log_event("order",
                    f"🚫 SL order cancelled: {pos['sl_order_id']}")
            except Exception as e:
                self.log_event("warning",
                    f"SL cancel failed (may already be triggered): {e}")

        # Place exit order
        try:
            result = self.dhan.place_option_order(
                underlying=pos["underlying"],
                strike_price=pos["strike"],
                option_type=pos["option_type"],
                expiry=pos["expiry"],
                transaction_type=opposite_txn,
                quantity=pos["quantity"],
                order_type=exit_order_type,
                product_type=product_type,
                tag=f"AF_X_{pos['option_type']}_{pos['strike']}",
            )
            pos["exit_order_id"] = result.get("orderId", "")
            self.log_event("order",
                f"✅ Exit order placed: {opposite_txn} {pos['trading_symbol']} "
                f"| OrderID: {pos['exit_order_id']}")
        except Exception as e:
            self.log_event("error",
                f"❌ Exit order FAILED for Leg {pos['leg_num']}: {e}")
            return

        # Update position
        pos["status"] = "closed"
        pos["exit_time"] = self.current_time
        pos["exit_premium"] = exit_premium
        pos["exit_reason"] = reason

        direction = 1 if pos["transaction_type"] == "BUY" else -1
        pnl = round(
            (exit_premium - pos["entry_premium"]) *
            direction * pos["lots"] * pos["lot_size"], 2
        )
        pos["pnl"] = pnl
        self.daily_pnl += pnl

        self.closed_trades.append(pos.copy())
        self.positions.remove(pos)

        self.log_event("exit",
            f"🔚 Leg {pos['leg_num']} closed ({reason}): "
            f"Entry ₹{pos['entry_premium']:.2f} → "
            f"Exit ₹{exit_premium:.2f} | P&L: ₹{pnl:,.2f}")

        # Check if all legs closed
        if not self.positions:
            self.in_trade = False
            self.strat_sl_val = 0
            self.strat_tp_val = 0
            trade_pnl = sum(
                t["pnl"] for t in self.closed_trades
                if t.get("exit_time") == self.current_time
            )
            self.log_event("info",
                f"📊 All legs closed. Trade P&L: ₹{trade_pnl:,.2f} | "
                f"Daily P&L: ₹{self.daily_pnl:,.2f}")

    # ── Exit Condition Check ──────────────────────────────────
    def _check_exit_conditions(self, pos: dict, row: pd.Series,
                                current_premium: float) -> Optional[str]:
        """Check if any exit condition is met for a position."""
        # Update peak premium for trailing SL
        if pos["transaction_type"] == "BUY":
            pos["peak_premium"] = max(
                pos.get("peak_premium", pos["entry_premium"]),
                current_premium
            )
        else:
            pos["peak_premium"] = min(
                pos.get("peak_premium", pos["entry_premium"]),
                current_premium
            )

        # ── Strategy-level SL/TP (₹ amounts) — checked FIRST ──
        if self.strat_sl_val > 0 or self.strat_tp_val > 0:
            ep = pos["entry_premium"]
            qty = pos["lots"] * pos["lot_size"]
            direction = 1 if pos["transaction_type"] == "BUY" else -1
            cur_pnl = (current_premium - ep) * direction * qty

            if self.strat_sl_val > 0 and cur_pnl <= -self.strat_sl_val:
                self.log_event("exit", f"Strategy SL hit: PnL ₹{cur_pnl:,.0f} <= -₹{self.strat_sl_val:,.0f}")
                return "STRATEGY_SL"
            if self.strat_tp_val > 0 and cur_pnl >= self.strat_tp_val:
                self.log_event("exit", f"Strategy TP hit: PnL ₹{cur_pnl:,.0f} >= ₹{self.strat_tp_val:,.0f}")
                return "STRATEGY_TP"

        # Trailing stop loss
        trail_pct = pos.get("trail_pct", 0)
        if trail_pct > 0:
            peak = pos["peak_premium"]
            if pos["transaction_type"] == "BUY":
                trail_sl = peak * (1 - trail_pct / 100)
                if current_premium <= trail_sl:
                    return "TRAILING_SL"
            else:
                trail_sl = peak * (1 + trail_pct / 100)
                if current_premium >= trail_sl:
                    return "TRAILING_SL"

        # Static stop loss (engine-level — backup if broker SL not placed)
        sl_pct = pos.get("sl_pct", 0)
        if sl_pct > 0:
            if pos["transaction_type"] == "BUY":
                if current_premium <= pos["entry_premium"] * (1 - sl_pct / 100):
                    return "STOP_LOSS"
            else:
                if current_premium >= pos["entry_premium"] * (1 + sl_pct / 100):
                    return "STOP_LOSS"

        # Target profit
        target_pct = pos.get("target_pct", 0)
        if target_pct > 0:
            if pos["transaction_type"] == "BUY":
                if current_premium >= pos["entry_premium"] * (1 + target_pct / 100):
                    return "TARGET"
            else:
                if current_premium <= pos["entry_premium"] * (1 - target_pct / 100):
                    return "TARGET"

        # SL Points (absolute premium points)
        sl_points = pos.get("sl_points", 0)
        if sl_points > 0:
            ep = pos["entry_premium"]
            if pos["transaction_type"] == "BUY" and current_premium <= ep - sl_points:
                return "SL_POINTS"
            elif pos["transaction_type"] == "SELL" and current_premium >= ep + sl_points:
                return "SL_POINTS"

        # Target Points (absolute premium points)
        target_points = pos.get("target_points", 0)
        if target_points > 0:
            ep = pos["entry_premium"]
            if pos["transaction_type"] == "BUY" and current_premium >= ep + target_points:
                return "TARGET_POINTS"
            elif pos["transaction_type"] == "SELL" and current_premium <= ep - target_points:
                return "TARGET_POINTS"

        # SL ₹ Total (leg-level rupee loss)
        sl_rupees = pos.get("sl_rupees", 0)
        if sl_rupees > 0:
            qty = pos["lots"] * pos["lot_size"]
            direction = 1 if pos["transaction_type"] == "BUY" else -1
            cur_pnl = (current_premium - pos["entry_premium"]) * direction * qty
            if cur_pnl <= -sl_rupees:
                return "SL_RUPEES"

        # Target ₹ Total (leg-level rupee profit)
        target_rupees = pos.get("target_rupees", 0)
        if target_rupees > 0:
            qty = pos["lots"] * pos["lot_size"]
            direction = 1 if pos["transaction_type"] == "BUY" else -1
            cur_pnl = (current_premium - pos["entry_premium"]) * direction * qty
            if cur_pnl >= target_rupees:
                return "TARGET_RUPEES"

        # Signal exit
        if eval_condition_group(row, self.exit_conditions, self._prev_row):
            return "EXIT_SIGNAL"

        # Square-off time — check strategy-level combined_sqoff_time first
        sqoff = self.strategy.get("combined_sqoff_time", "15:20")
        if not sqoff:
            sqoff = pos.get("sqoff_time", "15:20")
        if isinstance(sqoff, str):
            h, m = map(int, sqoff.split(":"))
            sqoff = time(h, m)
        if self.current_time and self.current_time.time() >= sqoff:
            return "SQUARE_OFF"

        return None

    # ── Helpers ───────────────────────────────────────────────
    def _calculate_strike(self, leg: dict, spot: float,
                           strike_step: int) -> int:
        """Calculate strike price based on leg configuration."""
        atm = round(spot / strike_step) * strike_step
        strike_type = leg.get("strike_type", "atm")
        strike_value = leg.get("strike_value", 0)
        option_type = leg.get("option_type", "CE")

        if strike_type == "atm":
            return int(atm)
        elif strike_type == "strike_price":
            return int(round(strike_value / strike_step) * strike_step)
        elif strike_type == "otm":
            offset = int(round(strike_value / strike_step) * strike_step)
            return int(atm + offset if option_type == "CE" else atm - offset)
        elif strike_type == "itm":
            offset = int(round(strike_value / strike_step) * strike_step)
            return int(atm - offset if option_type == "CE" else atm + offset)
        elif strike_type == "spot_price":
            offset = int(round(strike_value / strike_step) * strike_step)
            return int(round((spot + offset) / strike_step) * strike_step)
        return int(atm)

    async def _find_premium_strike(self, symbol: str, expiry: str,
                                    option_type: str, target_prem: float,
                                    spot: float, strike_step: int,
                                    mode: str = "near") -> int:
        """
        Find strike whose premium matches target:
          near  -> closest to target (either side)
          above -> cheapest strike with premium >= target
          below -> most expensive strike with premium <= target
        """
        import asyncio
        atm = round(spot / strike_step) * strike_step
        candidates = []

        for offset in range(-15, 16):
            strike = int(atm + offset * strike_step)
            if strike <= 0:
                continue
            try:
                ltp = self.dhan.get_option_ltp(symbol, strike, expiry, option_type)
                if ltp and ltp > 0:
                    candidates.append((strike, ltp))
                await asyncio.sleep(0.05)
            except Exception:
                continue

        if not candidates:
            return int(atm)

        if mode == "above":
            valid = [(s, p) for s, p in candidates if p >= target_prem]
            if valid:
                best = min(valid, key=lambda x: x[1])
                self.log_event("info", f"🔍 premium_above: {len(valid)} strikes qualify ≥₹{target_prem}, selected {best[0]} (₹{best[1]:.2f})")
                return best[0]
            self.log_event("warning", f"⚠️ premium_above: no strike ≥₹{target_prem}, using closest")
            best = min(candidates, key=lambda x: abs(x[1] - target_prem))
            return best[0]
        elif mode == "below":
            valid = [(s, p) for s, p in candidates if p <= target_prem]
            if valid:
                best = max(valid, key=lambda x: x[1])
                self.log_event("info", f"🔍 premium_below: {len(valid)} strikes qualify ≤₹{target_prem}, selected {best[0]} (₹{best[1]:.2f})")
                return best[0]
            self.log_event("warning", f"⚠️ premium_below: no strike ≤₹{target_prem}, using closest")
            best = min(candidates, key=lambda x: abs(x[1] - target_prem))
            return best[0]
        else:  # near
            best = min(candidates, key=lambda x: abs(x[1] - target_prem))
            self.log_event("info", f"🔍 premium_near: selected {best[0]} (₹{best[1]:.2f}, target ₹{target_prem})")
            return best[0]

    def _estimate_premium(self, strike: int, spot: float,
                           option_type: str, strike_step: int) -> float:
        """Estimate option premium (fallback when LTP unavailable)."""
        moneyness = (spot - strike) if option_type == "CE" else (strike - spot)
        atm_prem = spot * 0.005
        if moneyness > 0:
            intrinsic = moneyness
            extrinsic = atm_prem * 0.5 * max(0, 1 - abs(moneyness) / (spot * 0.2))
            return max(1, round(intrinsic + extrinsic, 2))
        else:
            distance_pct = abs(moneyness) / spot
            return max(1, round(atm_prem * max(0.05, 1 - distance_pct * 5), 2))

    def _get_underlying_symbol(self) -> str:
        """Get underlying symbol for scrip master lookup."""
        inst = self.strategy.get("instrument", "26000")
        return UNDERLYING_MAP.get(inst, "NIFTY")

    def _get_instrument_name(self) -> str:
        names = {
            "26000": "NIFTY 50", "26009": "BANK NIFTY",
            "1": "SENSEX", "26017": "NIFTY FIN SVC",
            "26037": "NIFTY MIDCAP",
        }
        return names.get(self.strategy.get("instrument", "26000"), "Unknown")

    def _get_timeframe(self) -> int:
        """Extract candle timeframe from indicators."""
        for ind in self.strategy.get("indicators", []):
            if "_" in ind and ind.endswith("m"):
                parts = ind.split("_")
                for p in parts:
                    if p.endswith("m") and p[:-1].isdigit():
                        return int(p[:-1])
        return 5

    # ── Status ────────────────────────────────────────────────
    def get_status(self) -> dict:
        """Get current engine status for UI display."""
        total_pnl = sum(p.get("unrealized_pnl", 0) for p in self.positions)
        total_pnl += sum(t.get("pnl", 0) for t in self.closed_trades)

        # Serialize positions (convert datetime objects)
        positions_out = []
        for p in self.positions:
            out = {k: v for k, v in p.items()}
            if isinstance(out.get("entry_time"), datetime):
                out["entry_time"] = str(out["entry_time"])
            positions_out.append(out)

        closed_out = []
        for t in self.closed_trades:
            out = {k: v for k, v in t.items()}
            if isinstance(out.get("entry_time"), datetime):
                out["entry_time"] = str(out["entry_time"])
            if isinstance(out.get("exit_time"), datetime):
                out["exit_time"] = str(out["exit_time"])
            closed_out.append(out)

        return {
            "running": self.running,
            "run_id": self.run_id or "",
            "mode": self.mode,
            "in_trade": self.in_trade,
            "current_spot": self.current_spot,
            "current_time": str(self.current_time) if self.current_time else None,
            "trades_today": self.trades_today,
            "daily_pnl": round(self.daily_pnl, 2),
            "positions": positions_out,
            "closed_trades": closed_out,
            "total_pnl": round(total_pnl, 2),
            "strategy_name": self.strategy.get("run_name", "Live Strategy"),
            "instrument": self.strategy.get("instrument", ""),
            "strategy": {**self.strategy, "entry_conditions": self.entry_conditions, "exit_conditions": self.exit_conditions},
            "current_candle": self.current_candle,
            "current_indicators": self.current_indicators,
            "event_log": [
                {
                    "time": e["time"].strftime("%H:%M:%S"),
                    "type": e["type"],
                    "message": e["message"],
                }
                for e in self.event_log[-50:]
            ],
        }