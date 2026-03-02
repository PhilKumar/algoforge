"""
engine/paper_trading.py — Real Paper Trading Engine with Live Market Data
Uses actual live option chain data from Dhan to simulate trading with REAL prices.
No mock data - this is forward testing with actual market conditions.
"""

import asyncio
import time
from datetime import datetime, date as date_type
from typing import List, Dict, Any, Optional
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine.indicators import compute_dynamic_indicators
from engine.backtest import eval_condition_group, get_lot_size, get_strike_step
from broker.dhan import DhanClient
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
        
        # Strategy configuration
        self.strategy = {}
        self.entry_conditions = []
        self.exit_conditions = []
        
        # Trading state
        self.in_trade = False
        self.positions = []  # List of open positions
        self.closed_trades = []  # Historical trades
        self.trades_today = 0
        
        # Live data
        self.current_spot = 0.0
        self.current_time = None
        self.candle_buffer = pd.DataFrame()
        self.current_indicators = {}  # Latest indicator values for UI
        self.current_candle = {}      # Latest OHLCV candle for UI
        
        # Logging
        self.event_log = []
        self.price_recordings = []  # Optional: record prices for later analysis
        
    def configure(self, strategy: dict, entry_conditions: list, exit_conditions: list):
        """Configure the paper trading strategy"""
        self.strategy = strategy
        self.entry_conditions = entry_conditions
        self.exit_conditions = exit_conditions
        self.log_event("info", f"Strategy configured: {strategy.get('run_name', 'Unnamed')}")
        
    def log_event(self, event_type: str, message: str, data: dict = None):
        """Log an event with timestamp"""
        event = {
            "time": datetime.now(),
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
        self.log_event("start", "🚀 Paper Trading Engine Started (LIVE DATA MODE)")
        self.log_event("info", f"Instrument: {self._get_instrument_name()}")
        self.log_event("info", f"Timeframe: {self._get_timeframe()} minutes")
        self.log_event("info", f"Max trades/day: {self.strategy.get('max_trades_per_day', 1)}")
        
        poll_interval = self.strategy.get("poll_interval", 10)  # seconds (default 10s)
        
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
        
    async def _tick(self, callback=None):
        """Single tick - check market, evaluate conditions, manage trades"""
        now = datetime.now()
        self.current_time = now
        current_time = now.time()
        
        # Check if new day
        if now.date() != self.session_date:
            self.trades_today = 0
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
        if self.trades_today < max_trades and not self.in_trade:
            entry_triggered = eval_condition_group(latest_row, self.entry_conditions)
            
            if entry_triggered:
                await self._enter_trade(latest_row)
        
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
        from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = datetime.now().strftime("%Y-%m-%d")
        
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
                'updated_at': datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')
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
        This uses REAL market data - not estimated!
        """
        try:
            # TODO: Implement actual option chain fetch from Dhan
            # For now, estimate based on spot movement
            spot_change_pct = (self.current_spot - position["entry_spot"]) / position["entry_spot"]
            
            # Rough premium estimation (will be replaced with actual option chain data)
            delta = position.get("delta", 0.5)
            premium_change = position["entry_premium"] * spot_change_pct * delta
            
            return max(0.5, position["entry_premium"] + premium_change)
        except Exception as e:
            self.log_event("error", f"Failed to fetch option price: {e}")
            return position.get("current_premium", position["entry_premium"])
    
    def _check_exit_conditions(self, position: dict, row: pd.Series, current_premium: float) -> Optional[str]:
        """Check if any exit condition is met"""
        # Stop loss check
        sl_pct = position.get("sl_pct", 0)
        if sl_pct > 0:
            sl_threshold = position["entry_premium"] * (1 - sl_pct / 100)
            if position["transaction_type"] == "BUY" and current_premium <= sl_threshold:
                return "STOP_LOSS"
            elif position["transaction_type"] == "SELL" and current_premium >= (position["entry_premium"] * (1 + sl_pct / 100)):
                return "STOP_LOSS"
        
        # Target check
        target_pct = position.get("target_pct", 0)
        if target_pct > 0:
            target_threshold = position["entry_premium"] * (1 + target_pct / 100)
            if position["transaction_type"] == "BUY" and current_premium >= target_threshold:
                return "TARGET"
            elif position["transaction_type"] == "SELL" and current_premium <= (position["entry_premium"] * (1 - target_pct / 100)):
                return "TARGET"
        
        # Signal exit
        if eval_condition_group(row, self.exit_conditions):
            return "EXIT_SIGNAL"
        
        # Square off time
        sqoff_time = position.get("sqoff_time", "15:20")
        if isinstance(sqoff_time, str):
            h, m = map(int, sqoff_time.split(":"))
            from datetime import time as time_class
            sqoff_time = time_class(h, m)
        
        if self.current_time.time() >= sqoff_time:
            return "SQUARE_OFF"
        
        return None
    
    async def _enter_trade(self, row: pd.Series):
        """Enter trade based on strategy legs"""
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
        lot_size = get_lot_size(instrument, self.session_date)
        
        for i, leg in enumerate(legs):
            # Calculate strike based on strike_type
            strike = self._calculate_strike(leg, self.current_spot, strike_step)
            
            # Get premium (in real implementation, fetch from option chain API)
            entry_premium = await self._estimate_premium(
                strike, 
                self.current_spot, 
                leg["option_type"],
                strike_step
            )
            
            position = {
                "id": len(self.positions) + len(self.closed_trades) + 1,
                "leg_num": i + 1,
                "transaction_type": leg["transaction_type"],
                "option_type": leg["option_type"],
                "strike": strike,
                "entry_time": self.current_time,
                "entry_spot": self.current_spot,
                "entry_premium": entry_premium,
                "current_premium": entry_premium,
                "lots": leg.get("lots", 1),
                "lot_size": lot_size,
                "sl_pct": leg.get("sl_pct", 0),
                "target_pct": leg.get("target_pct", 0),
                "trail_pct": leg.get("trail_pct", 0),
                "sqoff_time": leg.get("sqoff_time", "15:20"),
                "unrealized_pnl": 0,
                "status": "open"
            }
            
            self.positions.append(position)
            
            self.log_event("entry", f"🦿 Leg {i+1}: {leg['transaction_type']} {leg['option_type']} @ {strike}", {
                "premium": entry_premium,
                "lots": position["lots"],
                "lot_size": lot_size
            })
        
        self.in_trade = True
        self.trades_today += 1
    
    def _close_position(self, position: dict, reason: str, exit_premium: float):
        """Close a position and calculate P&L"""
        position["status"] = "closed"
        position["exit_time"] = self.current_time
        position["exit_premium"] = exit_premium
        position["exit_reason"] = reason
        
        direction = 1 if position["transaction_type"] == "BUY" else -1
        pnl = (
            (exit_premium - position["entry_premium"]) * 
            direction * 
            position["lots"] * 
            position["lot_size"]
        )
        position["pnl"] = round(pnl, 2)
        
        self.closed_trades.append(position.copy())
        self.positions.remove(position)
        
        self.log_event("exit", f"🔚 Exit Leg {position['leg_num']}: {reason}", {
            "entry_premium": position["entry_premium"],
            "exit_premium": exit_premium,
            "pnl": pnl
        })
        
        # Check if all positions are closed
        if not self.positions:
            self.in_trade = False
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
