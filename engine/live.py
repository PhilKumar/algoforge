"""
engine/live.py — Live Trading Engine
Polls Dhan market feed every minute, evaluates conditions,
places orders via Dhan API when signals trigger.
"""

import asyncio
import time as time_module
from datetime import datetime, time
from typing import List, Dict, Any, Optional
import pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# UPDATED: Using the new dynamic indicator parser
from engine.indicators import compute_dynamic_indicators
from engine.backtest  import eval_condition_group, DEFAULT_ENTRY_CONDITIONS, DEFAULT_EXIT_CONDITIONS
from broker.dhan import DhanClient
import config

class LiveEngine:
    def __init__(self, dhan: DhanClient = None):
        self.dhan           = dhan or DhanClient()
        self.running        = False
        self.in_trade       = False
        self.entry_price    = 0.0
        self.entry_time     = None
        self.peak_price     = 0.0   # for trailing SL
        self.trades_today   = 0
        self.daily_pnl      = 0.0   # realized P&L today
        self.last_date      = None
        self.candle_buffer  = []   # rolling window of candles
        self.log            = []   # trade log
        self.status_msg     = "Idle"
        self.current_ltp    = 0.0
        self.order_id       = None

        # Strategy config (overridable)
        self.entry_conditions = DEFAULT_ENTRY_CONDITIONS
        self.exit_conditions  = DEFAULT_EXIT_CONDITIONS
        self.strategy_cfg = {
            "max_trades_per_day": config.MAX_TRADES_PER_DAY,
            "market_open":        time(9, 15),
            "market_close":       time(15, 25),
            "lots":               4,
            "lot_size":           25,
            "stoploss_pct":       10,
            "symbol":             "NIFTY",
            "security_id":        "13",
            "exchange":           "IDX_I",
            "indicators":         [] # List of strings like ["EMA_14_5m"]
        }

    def configure(self, entry_conditions: list, exit_conditions: list, strategy_cfg: dict):
        self.entry_conditions = entry_conditions
        self.exit_conditions  = exit_conditions
        self.strategy_cfg.update(strategy_cfg)

    def _fetch_recent_candles(self, n_candles: int = 150) -> pd.DataFrame:
        from datetime import timedelta
        today    = datetime.now().strftime("%Y-%m-%d")
        from_dt  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        # Use configured instrument details instead of hardcoded values
        instrument = self.strategy_cfg.get("instrument", "26000")
        try:
            from app import INSTRUMENT_MAP
            inst_info = INSTRUMENT_MAP.get(instrument, {})
            sec_id = inst_info.get("dhan_id", self.strategy_cfg.get("security_id", "13"))
            seg = inst_info.get("dhan_seg", self.strategy_cfg.get("exchange", "IDX_I"))
            inst_type = inst_info.get("dhan_type", "INDEX")
        except ImportError:
            sec_id = self.strategy_cfg.get("security_id", "13")
            seg = self.strategy_cfg.get("exchange", "IDX_I")
            inst_type = "INDEX"

        # Extract timeframe from indicators (e.g., EMA_14_5m -> 5)
        candle_type = "5"
        for ind in self.strategy_cfg.get("indicators", []):
            if "_" in ind and ind.endswith("m"):
                parts = ind.split("_")
                for p in parts:
                    if p.endswith("m") and p[:-1].isdigit():
                        candle_type = p[:-1]
                        break

        df = self.dhan.get_historical_data(
            security_id      = sec_id,
            exchange_segment = seg,
            instrument_type  = inst_type,
            from_date        = from_dt,
            to_date          = today,
            candle_type      = candle_type,
        )
        return df.tail(n_candles)

    async def start(self, on_update=None):
        self.running = True
        self.status_msg = "Running"
        self._emit(on_update, {"type": "log", "msg": "Live engine started"})

        market_open  = self.strategy_cfg["market_open"]
        market_close = self.strategy_cfg["market_close"]
        
        # Ensure market_open/close are time objects (handle string inputs)
        if isinstance(market_open, str):
            h, m = map(int, market_open.split(':'))
            market_open = time(h, m)
        if isinstance(market_close, str):
            h, m = map(int, market_close.split(':'))
            market_close = time(h, m)

        while self.running:
            now  = datetime.now()
            cur  = now.time()
            date = now.date()

            if date != self.last_date:
                self.trades_today = 0
                self.daily_pnl    = 0.0
                self.last_date    = date
                self.in_trade     = False
                self._emit(on_update, {"type": "log", "msg": f"New day: {date}"})

            if not (market_open <= cur < market_close):
                wait_msg = "Waiting for market open..." if cur < market_open else "Market closed."
                self.status_msg = wait_msg
                self._emit(on_update, {"type": "log", "msg": wait_msg})
                await asyncio.sleep(30)
                continue

            try:
                df_raw = self._fetch_recent_candles(150)
                
                # UPDATED: Get indicators from the current live configuration
                ui_indicators = self.strategy_cfg.get("indicators", [])
                
                # Calculate exactly what the user selected in the UI
                df = compute_dynamic_indicators(df_raw, ui_indicators)

            except Exception as e:
                err_msg = f"Data fetch error: {e}"
                self.status_msg = err_msg
                self._emit(on_update, {"type": "error", "msg": err_msg})
                await asyncio.sleep(config.POLL_INTERVAL_SEC)
                continue

            if df.empty:
                await asyncio.sleep(config.POLL_INTERVAL_SEC)
                continue

            prev_row = df.iloc[-2] if len(df) >= 2 else None
            row = df.iloc[-1]
            self.current_ltp = float(row["close"])
            self._emit(on_update, {"type": "ltp", "ltp": self.current_ltp, "time": str(now)})

            if self.in_trade:
                # Update peak price for trailing SL
                self.peak_price = max(self.peak_price, self.current_ltp)
                sl_price = self.entry_price * (1 - self.strategy_cfg["stoploss_pct"] / 100)
                # Trailing stop loss
                trail_pct = float(self.strategy_cfg.get("trail_pct", 0) or 0)
                if trail_pct > 0:
                    trail_sl = self.peak_price * (1 - trail_pct / 100)
                    sl_price = max(sl_price, trail_sl)  # trailing SL is tighter
                exit_signal = eval_condition_group(row, self.exit_conditions, prev_row)
                hit_sl      = self.current_ltp <= sl_price
                eod         = cur >= time(15, 20)

                if hit_sl or exit_signal or eod:
                    reason = "TrailingSL" if (trail_pct > 0 and hit_sl and self.peak_price > self.entry_price) else ("StopLoss" if hit_sl else ("EOD" if eod else "Signal"))
                    await self._exit_trade(row, reason, on_update)
            else:
                max_hit    = self.trades_today >= self.strategy_cfg["max_trades_per_day"]
                max_loss   = float(self.strategy_cfg.get("max_daily_loss", 0) or 0)
                loss_hit   = max_loss > 0 and self.daily_pnl <= -max_loss
                time_ok    = True  # Allow entries throughout market hours
                entry_sig  = eval_condition_group(row, self.entry_conditions, prev_row)

                if not max_hit and not loss_hit and time_ok and entry_sig:
                    await self._enter_trade(row, on_update)

            self.status_msg = f"Monitoring — LTP: ₹{self.current_ltp:,.2f}"
            await asyncio.sleep(config.POLL_INTERVAL_SEC)

    def stop(self):
        self.running    = False
        self.status_msg = "Stopped"

    async def _enter_trade(self, row, on_update):
        self._emit(on_update, {
            "type":  "signal_entry",
            "msg":   f"Entry signal at ₹{row['close']:.2f}",
            "price": float(row["close"]),
            "time":  str(datetime.now()),
        })

        try:
            result = self.dhan.place_order(
                security_id      = self.strategy_cfg["security_id"],
                exchange_segment = self.strategy_cfg["exchange"],
                transaction_type = "BUY",
                quantity         = self.strategy_cfg["lots"] * self.strategy_cfg["lot_size"],
                order_type       = "MARKET",
                product_type     = "INTRADAY",
                tag              = "AlgoForge_ENTRY",
            )
            self.order_id   = result.get("orderId")
            self.in_trade   = True
            self.entry_price = float(row["close"])
            self.peak_price  = self.entry_price  # initialize peak for trailing SL
            self.entry_time  = datetime.now()
            self._emit(on_update, {
                "type":     "order_placed",
                "msg":      f"BUY order placed | OrderID: {self.order_id}",
                "order_id": self.order_id,
            })
        except Exception as e:
            self._emit(on_update, {"type": "error", "msg": f"Order failed: {e}"})

    async def _exit_trade(self, row, reason: str, on_update):
        exit_price = float(row["close"])
        pnl = (exit_price - self.entry_price) * self.strategy_cfg["lots"] * self.strategy_cfg["lot_size"]

        self._emit(on_update, {
            "type":    "signal_exit",
            "msg":     f"Exit ({reason}) at ₹{exit_price:.2f} | P&L: ₹{pnl:,.2f}",
            "price":   exit_price,
            "pnl":     pnl,
            "reason":  reason,
            "time":    str(datetime.now()),
        })

        try:
            result = self.dhan.place_order(
                security_id      = self.strategy_cfg["security_id"],
                exchange_segment = self.strategy_cfg["exchange"],
                transaction_type = "SELL",
                quantity         = self.strategy_cfg["lots"] * self.strategy_cfg["lot_size"],
                order_type       = "MARKET",
                product_type     = "INTRADAY",
                tag              = "AlgoForge_EXIT",
            )
            trade_log = {
                "entry_time":  str(self.entry_time),
                "exit_time":   str(datetime.now()),
                "entry_price": self.entry_price,
                "exit_price":  exit_price,
                "pnl":         round(pnl, 2),
                "reason":      reason,
            }
            self.log.append(trade_log)
            self.trades_today += 1
            self.daily_pnl += round(pnl, 2)
            self.in_trade = False
            self.order_id = None
        except Exception as e:
            self._emit(on_update, {"type": "error", "msg": f"Exit order failed: {e}"})

    def _emit(self, callback, event: dict):
        if callback:
            try:
                # Check if callback is a coroutine function and handle accordingly
                if asyncio.iscoroutinefunction(callback):
                    # Callback is async — schedule it but don't await here
                    asyncio.create_task(callback(event))
                else:
                    callback(event)
            except Exception as e:
                print(f"[EMIT ERROR] {e}")
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{event.get('type','?').upper()}] {event.get('msg', event)}")

    def get_status(self) -> dict:
        return {
            "running":       self.running,
            "status_msg":    self.status_msg,
            "in_trade":      self.in_trade,
            "entry_price":   self.entry_price,
            "current_ltp":   self.current_ltp,
            "trades_today":  self.trades_today,
            "trade_log":     self.log,
            "unrealized_pnl": round(
                (self.current_ltp - self.entry_price) * self.strategy_cfg["lots"] * self.strategy_cfg["lot_size"]
                if self.in_trade else 0, 2
            ),
        }