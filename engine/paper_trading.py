"""
engine/paper_trading.py — Real Paper Trading Engine with Live Market Data
Uses actual live option chain data from Dhan to simulate trading with REAL prices.
No mock data - this is forward testing with actual market conditions.
Two modes:
  1. WebSocket mode (fast): LiveMarketFeed pushes ticks → candles aggregate live
     → conditions evaluated on candle close → ~1-2 second latency
  2. REST polling mode (fallback): polls Dhan REST API every N seconds
     → conditions evaluated per poll → ~30-90 second latency"""

import asyncio
import math
import time
from datetime import datetime, date as date_type, timezone, timedelta
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
from broker.dhan import DhanClient, ScripMaster
import config


# Import INSTRUMENT_MAP lazily to avoid circular imports
def _get_instrument_map():
    from app import INSTRUMENT_MAP
    return INSTRUMENT_MAP


class PaperTradingEngine:
    """
    Paper trading engine that uses REAL live market data.
    - Fetches actual option chain prices from Dhan
    - Executes trades in paper mode (no real orders)
    - Records all trades for analysis
    - Can optionally save price data for historical backtesting later
    """
    
    def __init__(self, dhan: DhanClient = None):
        self.dhan = dhan or DhanClient()
        self.running = False
        self.session_date = None
        
        # WebSocket feed (injected from app.py — if available, use event-driven mode)
        self._feed = None          # LiveMarketFeed instance
        self._ws_mode = False      # True when using WebSocket
        self._option_sec_id = None # subscribed option security_id for LTP
        self._candle_event = None  # asyncio.Event set on each candle close
        self._latest_candle_df = None  # DataFrame from candle close callback
        self._latest_candle = None     # Last closed candle dict
        
        # Strategy configuration
        self.strategy = {}
        self.entry_conditions = []
        self.exit_conditions = []
        
        # Trading state
        self.in_trade = False
        self.positions = []  # List of open positions
        self.closed_trades = []  # Historical trades
        self.trades_today = 0
        self.daily_pnl = 0.0       # Realized P&L for today
        self.max_daily_loss = 0.0  # Will be set from strategy config
        
        # Strategy-level SL/TP (₹ amounts)
        self.strat_sl_val = 0.0   # e.g. 13000 (20% of 250*260)
        self.strat_tp_val = 0.0   # e.g. 6600
        self.trade_entry_prem = 0.0  # entry premium for strategy-level PnL calc
        
        # Live data
        self.current_spot = 0.0
        self.current_time = None
        self.candle_buffer = pd.DataFrame()
        self.current_indicators = {}  # Latest indicator values for UI
        self.current_candle = {}      # Latest OHLCV candle for UI
        self._prev_row = None         # Previous candle row for crossover detection
        
        # Logging
        self.event_log = []
        self.price_recordings = []  # Optional: record prices for later analysis
    
    def set_feed(self, feed):
        """Inject a LiveMarketFeed for WebSocket-driven mode."""
        self._feed = feed
        
    def configure(self, strategy: dict, entry_conditions: list, exit_conditions: list):
        """Configure the paper trading strategy"""
        self.strategy = strategy
        self.entry_conditions = entry_conditions
        self.exit_conditions = exit_conditions
        
        # Pre-compute strategy-level SL/TP values
        sl_pct = float(strategy.get("stoploss_pct", 0) or 0)
        sl_rupees = float(strategy.get("stoploss_rupees", 0) or 0)
        tp_pct = float(strategy.get("target_profit_pct", 0) or 0)
        tp_rupees = float(strategy.get("target_profit_rupees", 0) or 0)
        
        # These will be finalized when trade enters (needs entry premium)
        self._sl_pct = sl_pct
        self._sl_rupees = sl_rupees
        self._tp_pct = tp_pct
        self._tp_rupees = tp_rupees
        
        self.log_event("info", f"Strategy configured: {strategy.get('run_name', 'Unnamed')}")
        if sl_rupees > 0 or sl_pct > 0:
            self.log_event("info", f"Strategy SL: ₹{sl_rupees:,.0f}" if sl_rupees > 0 else f"Strategy SL: {sl_pct}%")
        if tp_rupees > 0 or tp_pct > 0:
            self.log_event("info", f"Strategy TP: ₹{tp_rupees:,.0f}" if tp_rupees > 0 else f"Strategy TP: {tp_pct}%")
        
    def log_event(self, event_type: str, message: str, data: dict = None):
        """Log an event with timestamp"""
        event = {
            "time": _now_ist(),
            "type": event_type,
            "message": message,
            "data": data or {}
        }
        self.event_log.append(event)
        timestamp = event["time"].strftime("%H:%M:%S")
        print(f"[PAPER] [{timestamp}] [{event_type.upper()}] {message}")
    
    async def _emit_callback(self, callback, event: dict):
        """Emit callback, handling both async and sync functions"""
        if not callback:
            return
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(event)
            else:
                callback(event)
        except Exception as e:
            print(f"[PAPER] Callback error: {e}")
        
    async def start(self, callback=None):
        """Start the paper trading engine"""
        self.running = True
        self.session_date = date_type.today()
        self.daily_pnl = 0.0
        self.max_daily_loss = float(self.strategy.get("max_daily_loss", 0) or 0)
        self.log_event("start", "🚀 Paper Trading Engine Started (LIVE DATA MODE)")
        self.log_event("info", f"Instrument: {self._get_instrument_name()}")
        self.log_event("info", f"Timeframe: {self._get_timeframe()} minutes")
        self.log_event("info", f"Max trades/day: {self.strategy.get('max_trades_per_day', 1)}")
        if self.max_daily_loss > 0:
            self.log_event("info", f"Max daily loss: ₹{self.max_daily_loss:,.0f}")
        
        # ── Choose mode: WebSocket (fast) or REST polling (fallback) ──
        self._ws_mode = self._feed is not None and self._feed.is_running
        
        if self._ws_mode:
            self.log_event("info", "⚡ Mode: WebSocket (event-driven, ~1-2s latency)")
            await self._run_ws_mode(callback)
        else:
            self.log_event("info", "🔄 Mode: REST polling (fallback, ~30-60s latency)")
            poll_interval = self.strategy.get("poll_interval", 10)
            while self.running:
                try:
                    await self._tick(callback)
                except Exception as e:
                    self.log_event("error", f"Tick error: {str(e)}")
                await asyncio.sleep(poll_interval)
            
    def stop(self):
        """Stop the paper trading engine"""
        self.running = False
        
        # Close all open positions
        if self.positions:
            self.log_event("warning", f"Force closing {len(self.positions)} open positions")
            for pos in self.positions:
                self._close_position(pos, "ENGINE_STOP", self.current_spot)
        
        # Final summary
        total_pnl = sum(t["pnl"] for t in self.closed_trades)
        win_trades = len([t for t in self.closed_trades if t["pnl"] > 0])
        
        self.log_event("stop", "🛑 Paper Trading Engine Stopped")
        self.log_event("info", f"Trades: {len(self.closed_trades)} | Winners: {win_trades} | Total P&L: ₹{total_pnl:,.2f}")
    
    # ── WebSocket Event-Driven Mode ───────────────────────────
    async def _run_ws_mode(self, callback=None):
        """
        WebSocket-driven loop: waits for candle-close events instead of polling.
        On each candle close:
          1. Compute indicators on the candle DataFrame
          2. If in trade: check exit conditions using feed's instant LTP
          3. If not in trade: check entry conditions
        Between candles: monitor positions every 1s using cached LTP.
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
            # Set asyncio event from the WS thread
            loop.call_soon_threadsafe(self._candle_event.set)
        
        # Configure candle aggregation on the feed
        # First bootstrap with historical data for indicator warm-up
        history_df = self._feed.bootstrap_history(instrument, timeframe, days=7)
        indicators = self.strategy.get("indicators", [])
        
        self._feed.set_candle_config(
            instrument_id=instrument,
            timeframe=timeframe,
            callback=_on_candle_close,
            history_df=history_df,
        )
        
        self.log_event("info", f"📊 Candle aggregation: {timeframe}m (including {len(history_df)} historical candles)")
        
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
                
                # Check if new day
                if now.date() != self.session_date:
                    self.trades_today = 0
                    self.daily_pnl = 0.0
                    self.session_date = now.date()
                    self.log_event("info", f"📅 New trading day: {self.session_date}")
                
                # Check market hours
                from datetime import time as time_class
                market_open = self.strategy.get("market_open", "09:15")
                market_close = self.strategy.get("market_close", "15:25")
                if isinstance(market_open, str):
                    h, m = map(int, market_open.split(":"))
                    market_open = time_class(h, m)
                if isinstance(market_close, str):
                    h, m = map(int, market_close.split(":"))
                    market_close = time_class(h, m)
                
                if not (market_open <= now.time() <= market_close):
                    await asyncio.sleep(5)
                    if callback:
                        await self._emit_callback(callback, {"type": "status", "message": "Outside market hours"})
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
                
                # ── Monitor positions (fast: every 1 second) ──
                if self.in_trade:
                    for position in list(self.positions):
                        if position["status"] == "closed":
                            continue
                        # Get LTP from feed cache (instant, no API call)
                        current_premium = self._get_premium_from_feed(position)
                        # REST fallback every ~5s if WS has no data yet
                        if current_premium <= 0:
                            rest_counter = position.get("_rest_counter", 0) + 1
                            position["_rest_counter"] = rest_counter
                            if rest_counter % 5 == 1:  # First try + every 5 iterations
                                try:
                                    symbol_name = self._get_symbol_name()
                                    current_premium = self.dhan.get_option_ltp(
                                        symbol_name, int(position["strike"]),
                                        position["expiry"], position["option_type"]
                                    ) or 0.0
                                except Exception:
                                    current_premium = 0.0
                        if current_premium > 0:
                            position["current_premium"] = current_premium
                            direction = 1 if position["transaction_type"] == "BUY" else -1
                            position["unrealized_pnl"] = (
                                (current_premium - position["entry_premium"]) *
                                direction * position["lots"] * position["lot_size"]
                            )
                        
                        # Check exit conditions against current row (last known)
                        latest_row = self.candle_buffer.iloc[-1] if not self.candle_buffer.empty else None
                        if latest_row is not None:
                            exit_triggered = self._check_exit_conditions(position, latest_row, position["current_premium"])
                            if exit_triggered:
                                self._close_position(position, exit_triggered, position["current_premium"])
                
                # ── Wait for candle close event (with timeout for position monitoring) ──
                try:
                    await asyncio.wait_for(self._candle_event.wait(), timeout=1.0)
                    self._candle_event.clear()
                except asyncio.TimeoutError:
                    # No new candle — continue position monitoring
                    if callback:
                        await self._emit_callback(callback, self.get_status())
                    continue
                
                # ── Candle closed! Evaluate conditions ──
                candle_df = self._latest_candle_df
                latest_candle = self._latest_candle
                
                if candle_df is None or candle_df.empty:
                    continue
                
                # Compute indicators on the full candle history
                df_with_indicators = compute_dynamic_indicators(candle_df, indicators)
                self.candle_buffer = df_with_indicators
                
                if df_with_indicators.empty:
                    continue
                
                latest_row = df_with_indicators.iloc[-1]
                self.current_spot = float(latest_row.get("close", self.current_spot))
                
                # Store candle + indicators for UI
                self._update_ui_data(latest_row)
                
                candle_time = latest_candle.get("timestamp", now)
                latency = (now - candle_time).total_seconds() if isinstance(candle_time, datetime) else 0
                self.log_event("candle", f"🕯️ {timeframe}m candle @ {self.current_spot:.2f} (latency: {latency:.1f}s)")
                
                # Check entry conditions
                max_trades = self.strategy.get("max_trades_per_day", 1)
                daily_loss_hit = self.max_daily_loss > 0 and self.daily_pnl <= -self.max_daily_loss
                
                if not self.in_trade and self.trades_today < max_trades and not daily_loss_hit:
                    prev_row = df_with_indicators.iloc[-2] if len(df_with_indicators) >= 2 else None
                    entry_triggered = eval_condition_group(latest_row, self.entry_conditions, prev_row)
                    
                    if entry_triggered:
                        self.log_event("signal", f"⚡ ENTRY SIGNAL at {now.strftime('%H:%M:%S')} (candle close + {latency:.1f}s)")
                        await self._enter_trade(latest_row)
                
                # Store previous row for crossover detection
                self._prev_row = latest_row
                
                # Send status update
                if callback:
                    await self._emit_callback(callback, self.get_status())
                
            except Exception as e:
                self.log_event("error", f"WS mode error: {str(e)}")
                await asyncio.sleep(1)
    
    def _get_premium_from_feed(self, position: dict) -> float:
        """Get option premium from WebSocket feed's LTP cache (instant, no API call)."""
        if not self._feed:
            return 0.0
        
        sec_id = position.get("ws_sec_id")
        if sec_id:
            ltp = self._feed.get_ltp(sec_id)
            if ltp > 0:
                return ltp
        return 0.0
    
    def _update_ui_data(self, row):
        """Store latest candle + indicator values for the live monitor UI."""
        try:
            self.current_candle = {
                'open': round(float(row.get('open', 0)), 2),
                'high': round(float(row.get('high', 0)), 2),
                'low': round(float(row.get('low', 0)), 2),
                'close': round(float(row.get('close', 0)), 2),
                'volume': int(row.get('volume', 0)),
                'updated_at': _now_ist().strftime('%Y-%m-%d %I:%M:%S %p')
            }
            ohlcv_cols = {
                'open', 'high', 'low', 'close', 'volume', 'oi', 'timestamp',
                'date', 'datetime', 'time_of_day',
                'current_open', 'current_high', 'current_low', 'current_close',
                'yesterday_open', 'yesterday_high', 'yesterday_low', 'yesterday_close',
                'cpr_type', 'pivot', 'bc', 'tc', 'cpr_range', 'cpr_width_pct',
                'cpr_is_narrow', 'supertrend_dir'
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
    
    # ── REST Polling Mode (original _tick) ────────────────────
    async def _tick(self, callback=None):
        """Single tick - check market, evaluate conditions, manage trades"""
        now = _now_ist()
        self.current_time = now
        current_time = now.time()
        
        # Check if new day
        if now.date() != self.session_date:
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.session_date = now.date()
            self.log_event("info", f"📅 New trading day: {self.session_date}")
        
        # Check market hours
        market_open = self.strategy.get("market_open", "09:15")
        market_close = self.strategy.get("market_close", "15:25")
        
        from datetime import time as time_class
        if isinstance(market_open, str):
            h, m = map(int, market_open.split(":"))
            market_open = time_class(h, m)
        if isinstance(market_close, str):
            h, m = map(int, market_close.split(":"))
            market_close = time_class(h, m)
            
        if not (market_open <= current_time <= market_close):
            if callback:
                await self._emit_callback(callback, {"type": "status", "message": "Outside market hours"})
            return
        
        # Fetch live candle data
        try:
            df = await self._fetch_live_data()
            if df.empty:
                return
                
            self.candle_buffer = df
            self.current_spot = float(df["close"].iloc[-1])
            
            if callback:
                await self._emit_callback(callback, {
                    "type": "price_update",
                    "spot": self.current_spot,
                    "time": str(now)
                })
        except Exception as e:
            self.log_event("error", f"Failed to fetch live data: {e}")
            return
        
        # Get latest candle for condition evaluation
        latest_row = df.iloc[-1]
        
        # Manage existing positions
        for position in list(self.positions):
            if position["status"] == "closed":
                continue
                
            # Fetch current option price
            current_premium = await self._get_current_premium(position)
            position["current_premium"] = current_premium
            
            # Calculate unrealized P&L
            direction = 1 if position["transaction_type"] == "BUY" else -1
            position["unrealized_pnl"] = (
                (current_premium - position["entry_premium"]) * 
                direction * 
                position["lots"] * 
                position["lot_size"]
            )
            
            # Check exit conditions
            exit_triggered = self._check_exit_conditions(position, latest_row, current_premium)
            
            if exit_triggered:
                reason = exit_triggered
                self._close_position(position, reason, current_premium)
        
        # Check entry conditions (if not in trade)
        max_trades = self.strategy.get("max_trades_per_day", 1)
        # Check max daily loss limit
        daily_loss_hit = self.max_daily_loss > 0 and self.daily_pnl <= -self.max_daily_loss
        if daily_loss_hit and not self.in_trade:
            pass  # Skip entry — daily loss limit reached
        elif self.trades_today < max_trades and not self.in_trade:
            prev_row = df.iloc[-2] if len(df) >= 2 else None
            entry_triggered = eval_condition_group(latest_row, self.entry_conditions, prev_row)
            
            if entry_triggered:
                await self._enter_trade(latest_row)
        
        # Store previous row for crossover detection in exit conditions
        self._prev_row = latest_row
        
        # Send status update
        if callback:
            await self._emit_callback(callback, self.get_status())
    
    async def _fetch_live_data(self) -> pd.DataFrame:
        """Fetch live candle data with indicators"""
        from datetime import timedelta
        
        # Validate and normalize timeframe to supported intervals
        timeframe = self._get_timeframe()
        valid_intervals = [1, 5, 15, 25, 60]
        if timeframe not in valid_intervals:
            print(f"[PAPER] Timeframe {timeframe}m not supported, using 5m")
            timeframe = 5
        
        instrument = self.strategy.get("instrument", "26000")
        
        # Fetch last 7 days to have enough data for indicators
        from_date = (_now_ist() - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = _now_ist().strftime("%Y-%m-%d")
        
        # Get instrument mapping (lazy import to avoid circular dependency)
        inst_map = _get_instrument_map()
        inst_info = inst_map.get(instrument, {})
        
        df_raw = self.dhan.get_historical_data(
            security_id=inst_info.get("dhan_id", "13"),
            exchange_segment=inst_info.get("dhan_seg", "IDX_I"),
            instrument_type=inst_info.get("dhan_type", "INDEX"),
            from_date=from_date,
            to_date=to_date,
            candle_type=str(timeframe)
        )
        
        # Apply indicators
        indicators = self.strategy.get("indicators", [])
        df = compute_dynamic_indicators(df_raw, indicators)
        
        # Store current candle + indicator values for live monitor UI
        if not df.empty:
            last = df.iloc[-1]
            self.current_candle = {
                'open':  round(float(last.get('open',  0)), 2),
                'high':  round(float(last.get('high',  0)), 2),
                'low':   round(float(last.get('low',   0)), 2),
                'close': round(float(last.get('close', 0)), 2),
                'volume': int(last.get('volume', 0)),
                'openInterest': int(last.get('oi', 0)),
                'updated_at': _now_ist().strftime('%Y-%m-%d %I:%M:%S %p')
            }
            ohlcv_cols = {
                'open','high','low','close','volume','oi','timestamp',
                'date','datetime','time_of_day',
                'current_open','current_high','current_low','current_close',
                'yesterday_open','yesterday_high','yesterday_low','yesterday_close',
                'cpr_type','pivot','bc','tc','cpr_range','cpr_width_pct','cpr_is_narrow',
                'supertrend_dir'
            }
            self.current_indicators = {}
            for col in df.columns:
                if col in ohlcv_cols:
                    continue
                try:
                    val = last[col]
                    if pd.isna(val):
                        continue
                    fval = float(val)
                    self.current_indicators[col] = round(fval, 2)
                except (TypeError, ValueError):
                    pass  # skip datetime.time, bool strings, etc.
        
        return df.tail(200)  # Keep last 200 candles
    
    async def _get_current_premium(self, position: dict) -> float:
        """
        Fetch current premium for an option position from Dhan.
        Uses REAL LTP from Dhan option chain API.
        Falls back to delta estimation if API fails.
        """
        try:
            underlying = self._get_symbol_name()
            strike = position.get("strike", 0)
            option_type = position.get("option_type", "PE")
            expiry = position.get("expiry", "")
            
            if underlying and strike and expiry:
                ltp = self.dhan.get_option_ltp(underlying, int(strike), expiry, option_type)
                if ltp > 0:
                    return ltp
        except Exception as e:
            self.log_event("warning", f"LTP fetch failed, using estimate: {e}")
        
        # Fallback: delta-based estimation
        try:
            spot_change = self.current_spot - position["entry_spot"]
            ot = position.get("option_type", "PE")
            direction = -1 if ot == "PE" else 1
            moneyness = direction * spot_change
            
            ep = position["entry_premium"]
            atm_prem = self.current_spot * 0.007
            r = ep / max(atm_prem, 1)
            delta = min(0.95, 1.0 - 1.0 / (1.0 + r ** 2.5))
            
            premium_change = abs(spot_change) * delta * (-1 if (ot == "PE" and spot_change > 0) or (ot == "CE" and spot_change < 0) else 1)
            return max(0.5, ep + premium_change)
        except Exception as e:
            self.log_event("error", f"Premium estimation failed: {e}")
            return position.get("current_premium", position["entry_premium"])
    
    def _check_exit_conditions(self, position: dict, row: pd.Series, current_premium: float) -> Optional[str]:
        """Check if any exit condition is met (strategy-level + leg-level + signal)"""
        # Update peak premium for trailing SL
        if position["transaction_type"] == "BUY":
            position["peak_premium"] = max(position.get("peak_premium", position["entry_premium"]), current_premium)
        else:
            position["peak_premium"] = min(position.get("peak_premium", position["entry_premium"]), current_premium)

        # ── Strategy-level SL/TP (₹ amounts) — checked FIRST ──
        if self.strat_sl_val > 0 or self.strat_tp_val > 0:
            ep = position["entry_premium"]
            qty = position["lots"] * position["lot_size"]
            direction = 1 if position["transaction_type"] == "BUY" else -1
            cur_pnl = (current_premium - ep) * direction * qty
            
            if self.strat_sl_val > 0 and cur_pnl <= -self.strat_sl_val:
                self.log_event("exit", f"Strategy SL hit: PnL ₹{cur_pnl:,.0f} <= -₹{self.strat_sl_val:,.0f}")
                return "STRATEGY_SL"
            if self.strat_tp_val > 0 and cur_pnl >= self.strat_tp_val:
                self.log_event("exit", f"Strategy TP hit: PnL ₹{cur_pnl:,.0f} >= ₹{self.strat_tp_val:,.0f}")
                return "STRATEGY_TP"

        # Trailing stop loss check (takes priority over static SL)
        trail_pct = position.get("trail_pct", 0)
        if trail_pct > 0:
            peak = position["peak_premium"]
            if position["transaction_type"] == "BUY":
                trail_sl = peak * (1 - trail_pct / 100)
                if current_premium <= trail_sl:
                    self.log_event("exit", f"Trailing SL hit: peak={peak:.2f} trail={trail_sl:.2f} current={current_premium:.2f}")
                    return "TRAILING_SL"
            else:  # SELL
                trail_sl = peak * (1 + trail_pct / 100)
                if current_premium >= trail_sl:
                    self.log_event("exit", f"Trailing SL hit: peak={peak:.2f} trail={trail_sl:.2f} current={current_premium:.2f}")
                    return "TRAILING_SL"

        # Static stop loss check (leg-level)
        sl_pct = position.get("sl_pct", 0)
        if sl_pct > 0:
            sl_threshold = position["entry_premium"] * (1 - sl_pct / 100)
            if position["transaction_type"] == "BUY" and current_premium <= sl_threshold:
                return "STOP_LOSS"
            elif position["transaction_type"] == "SELL" and current_premium >= (position["entry_premium"] * (1 + sl_pct / 100)):
                return "STOP_LOSS"
        
        # Target check (leg-level)
        target_pct = position.get("target_pct", 0)
        if target_pct > 0:
            target_threshold = position["entry_premium"] * (1 + target_pct / 100)
            if position["transaction_type"] == "BUY" and current_premium >= target_threshold:
                return "TARGET"
            elif position["transaction_type"] == "SELL" and current_premium <= (position["entry_premium"] * (1 - target_pct / 100)):
                return "TARGET"
        
        # Signal exit
        if eval_condition_group(row, self.exit_conditions, self._prev_row):
            return "EXIT_SIGNAL"
        
        # Square off time
        sqoff_time = self.strategy.get("combined_sqoff_time", "15:20")
        if not sqoff_time:
            sqoff_time = position.get("sqoff_time", "15:20")
        if isinstance(sqoff_time, str):
            h, m = map(int, sqoff_time.split(":"))
            from datetime import time as time_class
            sqoff_time = time_class(h, m)
        
        if self.current_time.time() >= sqoff_time:
            return "SQUARE_OFF"
        
        return None
    
    async def _enter_trade(self, row: pd.Series):
        """Enter trade based on strategy legs — uses REAL option LTP from Dhan"""
        self.log_event("signal", "✅ ENTRY CONDITIONS MET", {
            "spot": self.current_spot,
            "time": str(self.current_time)
        })
        
        legs = self.strategy.get("legs", [])
        if not legs:
            self.log_event("warning", "No legs configured - cannot enter trade")
            return
        
        instrument = self.strategy.get("instrument", "26000")
        strike_step = get_strike_step(instrument)
        
        # Use user-configured lot_size if set, else from ScripMaster
        user_lot_size = int(self.strategy.get("lot_size", 0) or 0)
        symbol = self._get_symbol_name()
        expiry = ScripMaster.get_nearest_expiry(symbol)
        
        if user_lot_size > 0:
            lot_size = user_lot_size
        else:
            lot_size = ScripMaster.get_lot_size(symbol, expiry) if expiry else get_lot_size(instrument, self.session_date)
        
        lots = int(self.strategy.get("lots", 1) or 1)
        
        self.log_event("info", f"📊 Expiry: {expiry} | Lot size: {lot_size} | Lots: {lots}")
        
        for i, leg in enumerate(legs):
            option_type = leg.get("option_type", "PE")
            strike_type = leg.get("strike_type", "atm")
            strike_value = leg.get("strike_value", 0)
            
            # Calculate strike — handle premium_near by scanning real LTP
            if strike_type == "premium_near" and expiry:
                strike = await self._find_premium_near_strike(
                    symbol, expiry, option_type, float(strike_value), 
                    self.current_spot, strike_step
                )
                self.log_event("info", f"🎯 premium_near target=₹{strike_value} → strike={strike}")
            else:
                strike = self._calculate_strike(leg, self.current_spot, strike_step)
            
            # Get REAL premium from Dhan LTP
            entry_premium = 0.0
            if expiry:
                try:
                    entry_premium = self.dhan.get_option_ltp(symbol, int(strike), expiry, option_type)
                except Exception as e:
                    self.log_event("warning", f"LTP fetch failed: {e}")
            
            if entry_premium <= 0:
                # Fallback to estimation
                entry_premium = await self._estimate_premium(
                    strike, self.current_spot, option_type, strike_step
                )
                self.log_event("warning", f"Using estimated premium: ₹{entry_premium:.2f}")
            
            leg_lots = leg.get("lots", lots)
            
            option_name = f"{symbol} {strike} {option_type}"
            
            position = {
                "id": len(self.positions) + len(self.closed_trades) + 1,
                "leg_num": i + 1,
                "symbol": option_name,
                "transaction_type": leg["transaction_type"],
                "option_type": option_type,
                "strike": strike,
                "expiry": expiry,
                "entry_time": self.current_time,
                "entry_spot": self.current_spot,
                "entry_premium": entry_premium,
                "current_premium": entry_premium,
                "lots": leg_lots,
                "lot_size": lot_size,
                "sl_pct": leg.get("sl_pct", 0),
                "target_pct": leg.get("target_pct", 0),
                "trail_pct": leg.get("trail_pct", 0),
                "sqoff_time": leg.get("sqoff_time", "15:20"),
                "unrealized_pnl": 0,
                "peak_premium": entry_premium,  # for trailing SL
                "status": "open",
                "ws_sec_id": None,  # Will be set if WebSocket mode
            }
            
            # Subscribe option to WebSocket feed for instant LTP tracking
            if self._ws_mode and self._feed and expiry:
                ws_sec_id = self._feed.subscribe_option(symbol, int(strike), expiry, option_type)
                if ws_sec_id:
                    position["ws_sec_id"] = ws_sec_id
                    self._option_sec_id = ws_sec_id
                    self.log_event("info", f"⚡ Option subscribed to WebSocket: sec_id={ws_sec_id}")
            
            self.positions.append(position)
            
            self.log_event("entry", f"📝 Leg {i+1}: {leg['transaction_type']} {symbol} {strike} {option_type} @ ₹{entry_premium:.2f}", {
                "premium": entry_premium,
                "lots": leg_lots,
                "lot_size": lot_size,
                "strike": strike,
                "expiry": expiry
            })
            
            # Set strategy-level SL/TP based on entry premium
            self.trade_entry_prem = entry_premium
            qty = leg_lots * lot_size
            if self._sl_rupees > 0:
                self.strat_sl_val = self._sl_rupees
            elif self._sl_pct > 0:
                self.strat_sl_val = entry_premium * qty * self._sl_pct / 100
            else:
                self.strat_sl_val = 0
            
            if self._tp_rupees > 0:
                self.strat_tp_val = self._tp_rupees
            elif self._tp_pct > 0:
                self.strat_tp_val = entry_premium * qty * self._tp_pct / 100
            else:
                self.strat_tp_val = 0
            
            if self.strat_sl_val > 0:
                self.log_event("info", f"🛡️ Strategy SL: ₹{self.strat_sl_val:,.0f}")
            if self.strat_tp_val > 0:
                self.log_event("info", f"🎯 Strategy TP: ₹{self.strat_tp_val:,.0f}")
        
        self.in_trade = True
        self.trades_today += 1
    
    async def _find_premium_near_strike(self, symbol: str, expiry: str, 
                                         option_type: str, target_prem: float,
                                         spot: float, strike_step: int) -> int:
        """
        Find the strike with premium closest to target using real Dhan LTP.
        Scans strikes around ATM to find the best match.
        """
        atm = round(spot / strike_step) * strike_step
        best_strike = atm
        best_diff = float('inf')
        
        # Scan ±10 strikes around ATM
        for offset in range(-10, 11):
            strike = int(atm + offset * strike_step)
            if strike <= 0:
                continue
            try:
                ltp = self.dhan.get_option_ltp(symbol, strike, expiry, option_type)
                if ltp > 0:
                    diff = abs(ltp - target_prem)
                    if diff < best_diff:
                        best_diff = diff
                        best_strike = strike
                    # Early exit if we found a very close match
                    if diff < target_prem * 0.05:
                        break
                # Small delay to avoid rate limits
                await asyncio.sleep(0.1)
            except Exception:
                continue
        
        return best_strike
    
    def _close_position(self, position: dict, reason: str, exit_premium: float):
        """Close a position and calculate P&L. Cap at SL/TP level for strategy exits."""
        position["status"] = "closed"
        position["exit_time"] = self.current_time
        position["exit_reason"] = reason
        
        direction = 1 if position["transaction_type"] == "BUY" else -1
        qty = position["lots"] * position["lot_size"]
        ep = position["entry_premium"]
        
        # Cap PnL at exact strategy SL/TP level
        if reason == "STRATEGY_TP" and self.strat_tp_val > 0:
            pnl = round(self.strat_tp_val, 2)
            exit_premium = round(ep + self.strat_tp_val / qty * direction, 2)
        elif reason == "STRATEGY_SL" and self.strat_sl_val > 0:
            pnl = -round(self.strat_sl_val, 2)
            exit_premium = round(ep - self.strat_sl_val / qty * direction, 2)
        else:
            pnl = round((exit_premium - ep) * direction * qty, 2)
        
        position["exit_premium"] = exit_premium
        position["pnl"] = pnl
        self.daily_pnl += pnl
        
        self.closed_trades.append(position.copy())
        self.positions.remove(position)
        
        self.log_event("exit", f"📊 Exit Leg {position['leg_num']}: {reason} | PnL: ₹{pnl:,.2f}", {
            "entry_premium": ep,
            "exit_premium": exit_premium,
            "pnl": pnl
        })
        
        # Check if all positions are closed
        if not self.positions:
            self.in_trade = False
            self.strat_sl_val = 0
            self.strat_tp_val = 0
            total_pnl = sum(t["pnl"] for t in self.closed_trades if t.get("exit_time") == self.current_time)
            self.log_event("info", f"✅ All legs closed. Trade P&L: ₹{total_pnl:,.2f}")
    
    def _calculate_strike(self, leg: dict, spot: float, strike_step: int) -> int:
        """Calculate strike price based on strike_type"""
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
        else:
            return int(atm)
    
    async def _estimate_premium(self, strike: int, spot: float, option_type: str, strike_step: int) -> float:
        """
        Estimate premium for an option.
        TODO: Replace with actual option chain fetch from Dhan API
        """
        atm = round(spot / strike_step) * strike_step
        moneyness = (spot - strike) if option_type == "CE" else (strike - spot)
        
        # Base ATM premium (0.5% - 0.6% of spot)
        atm_prem = spot * 0.005
        
        if moneyness > 0:  # ITM
            intrinsic = moneyness
            extrinsic = atm_prem * 0.5 * (1 - abs(moneyness) / (spot * 0.2))
            return max(1, round(intrinsic + extrinsic, 2))
        else:  # OTM
            distance_pct = abs(moneyness) / spot
            return max(1, round(atm_prem * max(0.05, (1 - distance_pct * 5)), 2))
    
    def _get_instrument_name(self) -> str:
        """Get instrument display name"""
        inst_map = {
            "26000": "NIFTY 50",
            "26009": "BANK NIFTY",
            "1": "SENSEX",
            "26017": "NIFTY FIN SVC",
            "26037": "NIFTY MIDCAP"
        }
        return inst_map.get(self.strategy.get("instrument", "26000"), "Unknown")
    
    def _get_symbol_name(self) -> str:
        """Get ScripMaster symbol name (NIFTY, BANKNIFTY, etc.)"""
        sym_map = {
            "26000": "NIFTY",
            "26009": "BANKNIFTY",
            "1": "SENSEX",
            "26017": "FINNIFTY",
            "26037": "MIDCPNIFTY"
        }
        return sym_map.get(self.strategy.get("instrument", "26000"), "NIFTY")
    
    def _get_timeframe(self) -> int:
        """Extract timeframe from indicators"""
        indicators = self.strategy.get("indicators", [])
        for ind in indicators:
            if "_" in ind and ind.endswith("m"):
                parts = ind.split("_")
                for p in parts:
                    if p.endswith("m") and p[:-1].isdigit():
                        return int(p[:-1])
        return 5  # default
    
    def get_status(self) -> dict:
        """Get current status for UI"""
        total_pnl = sum(p.get("unrealized_pnl", 0) for p in self.positions)
        total_pnl += sum(t.get("pnl", 0) for t in self.closed_trades)
        
        return {
            "running": self.running,
            "in_trade": self.in_trade,
            "current_spot": self.current_spot,
            "current_time": str(self.current_time) if self.current_time else None,
            "trades_today": self.trades_today,
            "positions": self.positions,
            "closed_trades": self.closed_trades,
            "total_pnl": round(total_pnl, 2),
            "strategy_name": self.strategy.get("run_name", "Paper Strategy"),
            "instrument": self.strategy.get("instrument", ""),
            "current_candle": self.current_candle,
            "current_indicators": self.current_indicators,
            "event_log": [
                {
                    "time": e["time"].strftime("%H:%M:%S"),
                    "type": e["type"],
                    "message": e["message"]
                }
                for e in self.event_log[-50:]  # Last 50 events
            ]
        }
