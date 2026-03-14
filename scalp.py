"""
engine/scalp.py — Scalp Mode Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hybrid manual/auto trading:
  • Manual entry  → click BUY/SELL → broker order placed immediately
  • Auto exit     → exits when premium target, SL, or sqoff time is hit
  • OR auto entry → runs entry conditions, but user can also exit manually

Completely isolated from LiveEngine and PaperTradingEngine.
Does NOT touch any existing code.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from broker.dhan import ScripMaster, enable_marketfeed_throttle

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist():
    return datetime.now(IST).replace(tzinfo=None)


class ScalpTrade:
    """Represents a single open scalp position."""

    def __init__(
        self,
        trade_id: int,
        underlying: str,
        strike: int,
        option_type: str,  # CE or PE
        expiry: str,
        transaction_type: str,  # BUY or SELL
        lots: int,
        lot_size: int,
        entry_premium: float,
        # Exit rules (all optional — at least one should be set)
        target_premium: float = 0.0,  # absolute option price to exit at
        sl_premium: float = 0.0,  # absolute SL option price
        target_pct: float = 0.0,  # % gain target on entry premium
        sl_pct: float = 0.0,  # % loss SL on entry premium
        target_rupees: float = 0.0,  # fixed ₹ profit target (across all lots)
        sl_rupees: float = 0.0,  # fixed ₹ loss SL
        sqoff_time: str = "15:20",  # HH:MM auto square-off
        order_id: str = "",
        entry_time: Optional[datetime] = None,
        mode: str = "live",  # "live" or "paper"
        # Stop-limit entry: wait for premium to enter [limit_price, limit_max] before placing order
        entry_limit_price: float = 0.0,
        entry_limit_max: float = 0.0,
    ):
        self.trade_id = trade_id
        self.mode = mode
        self.underlying = underlying
        self.strike = strike
        self.option_type = option_type
        self.expiry = expiry
        self.transaction_type = transaction_type
        self.lots = lots
        self.lot_size = lot_size
        self.quantity = lots * lot_size
        self.entry_premium = entry_premium
        self.current_premium = entry_premium

        # Stop-limit entry fields
        self.entry_limit_price = entry_limit_price
        self.entry_limit_max = entry_limit_max

        # Store pct values for deferred computation (pending trades have no entry price yet)
        self.target_pct = target_pct
        self.sl_pct = sl_pct

        # Compute absolute target/SL premiums if only % given
        self.target_premium = target_premium
        self.sl_premium = sl_premium

        if entry_premium > 0:
            if not self.target_premium and target_pct > 0:
                if transaction_type == "BUY":
                    self.target_premium = round(entry_premium * (1 + target_pct / 100), 2)
                else:
                    self.target_premium = round(entry_premium * (1 - target_pct / 100), 2)

            if not self.sl_premium and sl_pct > 0:
                if transaction_type == "BUY":
                    self.sl_premium = round(entry_premium * (1 - sl_pct / 100), 2)
                else:
                    self.sl_premium = round(entry_premium * (1 + sl_pct / 100), 2)

        self.target_rupees = target_rupees
        self.sl_rupees = sl_rupees
        self.sqoff_time = sqoff_time
        self.order_id = order_id
        self.entry_time = entry_time or _now_ist()
        self.exit_time: Optional[datetime] = None
        self.exit_premium: float = 0.0
        self.exit_reason: str = ""
        self.exit_order_id: str = ""
        self.pnl: float = 0.0
        # Broker-side SL/TP order IDs (safety net — placed after entry fills)
        self.broker_sl_order_id: str = ""
        self.broker_tp_order_id: str = ""
        # Pending = waiting for stop-limit trigger; open = actively trading
        self.status: str = "pending" if (entry_limit_price > 0 and entry_limit_max > 0) else "open"

    def _compute_pnl(self, current_prem: float) -> float:
        mult = 1 if self.transaction_type == "BUY" else -1
        return mult * (current_prem - self.entry_premium) * self.quantity

    def check_exit(self, current_prem: float) -> Optional[str]:
        """Returns exit reason string if an exit rule is triggered, else None."""
        now = _now_ist()

        # Don't auto-exit until entry price is known (backfill pending)
        if self.entry_premium <= 0:
            return None

        # Grace period: don't auto-exit within 3 seconds of entry.
        elapsed = (now - self.entry_time).total_seconds()
        if elapsed < 3:
            return None

        pnl = self._compute_pnl(current_prem)

        # Square-off time
        try:
            parts = self.sqoff_time.split(":")
            sq_h, sq_m = int(parts[0]), int(parts[1])
            if now.hour > sq_h or (now.hour == sq_h and now.minute >= sq_m):
                return "sqoff_time"
        except Exception:
            pass

        if self.transaction_type == "BUY":
            # Target: price reached or exceeded
            if self.target_premium > 0 and current_prem >= self.target_premium:
                return "target_hit"
            # SL: price dropped to or below
            if self.sl_premium > 0 and current_prem <= self.sl_premium:
                return "sl_hit"
        else:  # SELL
            if self.target_premium > 0 and current_prem <= self.target_premium:
                return "target_hit"
            if self.sl_premium > 0 and current_prem >= self.sl_premium:
                return "sl_hit"

        # ₹ targets
        if self.target_rupees > 0 and pnl >= self.target_rupees:
            return "target_rupees_hit"
        if self.sl_rupees > 0 and pnl <= -self.sl_rupees:
            return "sl_rupees_hit"

        return None

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "underlying": self.underlying,
            "symbol": f"{self.underlying} {self.strike}{self.option_type} {self.expiry}",
            "strike": self.strike,
            "option_type": self.option_type,
            "expiry": self.expiry,
            "transaction_type": self.transaction_type,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "quantity": self.quantity,
            "entry_premium": self.entry_premium,
            "current_premium": self.current_premium,
            "target_premium": self.target_premium,
            "sl_premium": self.sl_premium,
            "target_rupees": self.target_rupees,
            "sl_rupees": self.sl_rupees,
            "sqoff_time": self.sqoff_time,
            "order_id": self.order_id,
            "entry_time": str(self.entry_time),
            "exit_time": str(self.exit_time) if self.exit_time else None,
            "exit_premium": self.exit_premium,
            "exit_reason": self.exit_reason,
            "exit_order_id": self.exit_order_id,
            "pnl": round(self._compute_pnl(self.current_premium), 2) if self.status != "pending" else 0.0,
            "status": self.status,
            "mode": self.mode,
            "entry_limit_price": self.entry_limit_price,
            "entry_limit_max": self.entry_limit_max,
            "broker_sl_order_id": self.broker_sl_order_id,
            "broker_tp_order_id": self.broker_tp_order_id,
        }


class ScalpEngine:
    """
    Manages all active scalp trades.
    • Runs a background monitoring loop.
    • Uses _market_feed LTP cache for zero-latency price checks.
    • Falls back to REST `get_option_ltp` every 2s if no WS feed.
    """

    def __init__(self, dhan_client, market_feed=None, on_trade_close=None):
        self.dhan = dhan_client
        self.feed = market_feed  # LiveMarketFeed instance or None
        self.on_trade_close = on_trade_close  # callback(trade_dict) for persistence

        self.open_trades: Dict[int, ScalpTrade] = {}
        self.closed_trades: list = []
        self.event_log: list = []
        self._trade_counter: int = 0
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._ws_subs: Dict[int, str] = {}  # trade_id → ws_sec_id

    # ── Public API ───────────────────────────────────────────────

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._monitor_loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def enter_trade(
        self,
        underlying: str,
        strike: int,
        option_type: str,
        expiry: str,
        transaction_type: str,
        lots: int,
        lot_size: int,
        target_premium: float = 0.0,
        sl_premium: float = 0.0,
        target_pct: float = 0.0,
        sl_pct: float = 0.0,
        target_rupees: float = 0.0,
        sl_rupees: float = 0.0,
        sqoff_time: str = "15:20",
        product_type: str = "INTRADAY",
        order_type: str = "MARKET",
        mode: str = "live",  # "live" or "paper"
        entry_limit_price: float = 0.0,
        entry_limit_max: float = 0.0,
    ) -> Dict[str, Any]:
        """Place a broker order (or simulate in paper mode) and register the scalp trade.
        If entry_limit_price and entry_limit_max are set, the trade goes into 'pending' state
        and waits for the premium to enter [limit_price, limit_max] before placing the order."""
        quantity = lots * lot_size

        # ── Stop-limit entry: create pending trade, no order yet ──
        if entry_limit_price > 0 and entry_limit_max > 0:
            # Ensure min <= max
            lo = min(entry_limit_price, entry_limit_max)
            hi = max(entry_limit_price, entry_limit_max)
            self._trade_counter += 1
            trade = ScalpTrade(
                trade_id=self._trade_counter,
                underlying=underlying,
                strike=strike,
                option_type=option_type,
                expiry=expiry,
                transaction_type=transaction_type,
                lots=lots,
                lot_size=lot_size,
                entry_premium=0.0,  # unknown until triggered
                target_premium=target_premium,
                sl_premium=sl_premium,
                target_pct=target_pct,
                sl_pct=sl_pct,
                target_rupees=target_rupees,
                sl_rupees=sl_rupees,
                sqoff_time=sqoff_time,
                mode=mode,
                entry_limit_price=lo,
                entry_limit_max=hi,
            )
            self.open_trades[self._trade_counter] = trade

            # Subscribe to WS feed for LTP monitoring
            if self.feed:
                try:
                    ws_sec_id = self.feed.subscribe_option(underlying, strike, expiry, option_type)
                    if ws_sec_id:
                        self._ws_subs[self._trade_counter] = ws_sec_id
                except Exception:
                    pass

            mode_label = "[PAPER] " if mode == "paper" else ""
            self._log(
                "info",
                f"{mode_label}⏳ STOP-LIMIT PENDING: {transaction_type} {underlying} {strike}{option_type} "
                f"| trigger range ₹{lo:.2f}–₹{hi:.2f} "
                f"| target=₹{target_premium or 'none'} SL=₹{sl_premium or 'none'}",
            )

            if not self._running:
                self.start()

            return {"status": "ok", "trade_id": self._trade_counter, "trade": trade.to_dict()}

        # ── Immediate (market) entry ──
        if mode == "paper":
            # Paper mode: no real order — snapshot current LTP as entry price.
            # Run LTP fetch off the event loop so it never blocks concurrent entries.
            order_id = "PAPER"
            entry_premium = 0.0
            for _attempt in range(3):
                try:
                    ltp = await asyncio.to_thread(self.dhan.get_option_ltp, underlying, strike, expiry, option_type)
                    if ltp and ltp > 0:
                        entry_premium = float(ltp)
                        break
                except Exception:
                    pass
                if _attempt < 2:
                    await asyncio.sleep(0.3)  # brief pause between retries
        else:
            # Place real broker order
            try:
                result = self.dhan.place_option_order(
                    underlying=underlying,
                    strike_price=strike,
                    option_type=option_type,
                    expiry=expiry,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    order_type=order_type,
                    product_type="MARGIN" if product_type == "NRML" else product_type,
                    tag="AF_SCALP",
                )
                order_id = result.get("orderId", "")
                # Dhan may accept the API call (200) but reject on exchange side.
                # Check for rejection signals in the response.
                order_status = str(result.get("orderStatus", result.get("status", ""))).upper()
                if order_status in ("REJECTED", "CANCELLED", "FAILED"):
                    reason = result.get("remarks", result.get("message", result.get("rejectedReason", "Unknown")))
                    return {"status": "error", "message": f"Order rejected by broker: {reason}"}
                if not order_id:
                    return {"status": "error", "message": f"No orderId returned: {result}"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

            # Verify order was accepted — poll order status once after brief delay
            try:
                await asyncio.sleep(0.5)
                order_book = await asyncio.to_thread(self.dhan.get_order_book)
                for o in order_book:
                    if str(o.get("orderId", "")) == str(order_id):
                        o_status = str(o.get("orderStatus", "")).upper()
                        if o_status in ("REJECTED", "CANCELLED"):
                            reason = o.get("rejectedReason", o.get("remarks", "Unknown"))
                            self._log("error", f"❌ Order {order_id} was {o_status}: {reason}")
                            return {"status": "error", "message": f"Order {o_status}: {reason}"}
                        break
            except Exception as e:
                self._log("error", f"Order verification failed: {e}")
                # Continue — the order might still be valid

            # Get fill premium
            entry_premium = self.dhan.get_option_ltp(underlying, strike, expiry, option_type) or 0.0

        self._trade_counter += 1
        trade = ScalpTrade(
            trade_id=self._trade_counter,
            underlying=underlying,
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            transaction_type=transaction_type,
            lots=lots,
            lot_size=lot_size,
            entry_premium=entry_premium,
            target_premium=target_premium,
            sl_premium=sl_premium,
            target_pct=target_pct,
            sl_pct=sl_pct,
            target_rupees=target_rupees,
            sl_rupees=sl_rupees,
            sqoff_time=sqoff_time,
            order_id=order_id,
            mode=mode,
        )
        self.open_trades[self._trade_counter] = trade

        # Subscribe to WS feed if available
        if self.feed:
            try:
                ws_sec_id = self.feed.subscribe_option(underlying, strike, expiry, option_type)
                if ws_sec_id:
                    self._ws_subs[self._trade_counter] = ws_sec_id
            except Exception:
                pass

        # Enable broker-level marketfeed throttle while scalp trades are open
        if mode == "live":
            enable_marketfeed_throttle(True)

        mode_label = "[PAPER] " if mode == "paper" else ""
        self._log(
            "entry",
            f"{mode_label}✅ SCALP ENTER: {transaction_type} {underlying} {strike}{option_type} "
            f"@ ₹{entry_premium:.2f} | orderId={order_id} "
            f"| target=₹{trade.target_premium or 'none'} SL=₹{trade.sl_premium or 'none'}",
        )

        # Place broker-side SL/TP safety-net orders (live mode only)
        if mode == "live" and entry_premium > 0:
            await self._place_broker_sl_tp(trade)

        if not self._running:
            self.start()

        return {"status": "ok", "trade_id": self._trade_counter, "trade": trade.to_dict()}

    async def exit_trade(self, trade_id: int, reason: str = "manual") -> Dict[str, Any]:
        """Manually exit an open scalp trade."""
        trade = self.open_trades.get(trade_id)
        if not trade:
            return {"status": "error", "message": f"Trade {trade_id} not found or already closed"}
        await self._close_trade(trade, reason)
        return {"status": "ok", "trade": trade.to_dict()}

    async def kill_all_trades(self) -> Dict[str, Any]:
        """Emergency exit ALL open trades immediately."""
        trades_to_close = list(self.open_trades.values())
        if not trades_to_close:
            return {"status": "ok", "closed": 0, "message": "No open trades"}
        self._log("info", f"🔴 KILL ALL — closing {len(trades_to_close)} trade(s)...")
        closed = 0
        for trade in trades_to_close:
            try:
                await self._close_trade(trade, "kill")
                closed += 1
            except Exception as e:
                self._log("error", f"Kill failed for trade {trade.trade_id}: {e}")
        return {"status": "ok", "closed": closed}

    async def update_trade_targets(self, trade_id: int, **kwargs) -> Dict[str, Any]:
        """Update target/SL for an open trade (e.g. after looking at the chart)."""
        trade = self.open_trades.get(trade_id)
        if not trade:
            return {"status": "error", "message": f"Trade {trade_id} not found"}
        for attr in ("target_premium", "sl_premium", "target_rupees", "sl_rupees", "sqoff_time"):
            if attr in kwargs and kwargs[attr] is not None:
                setattr(trade, attr, kwargs[attr])
        # Sync broker-side SL/TP orders (live mode only)
        if trade.mode == "live" and ("sl_premium" in kwargs or "target_premium" in kwargs):
            await self._modify_broker_sl_tp(trade, **kwargs)
        self._log("info", f"🎯 Trade {trade_id} targets updated: {kwargs}")
        return {"status": "ok", "trade": trade.to_dict()}

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "open_trades": [t.to_dict() for t in self.open_trades.values()],
            "closed_trades": list(reversed(self.closed_trades[-50:])),
            "event_log": list(reversed(self.event_log[-100:])),
            "total_pnl": round(sum(t.get("pnl", 0) for t in self.closed_trades), 2),
        }

    # ── Internal monitoring ───────────────────────────────────────

    async def _monitor_loop(self):
        """Poll/WS prices every ~1s and trigger auto-exits."""
        _last_rest_call = 0.0
        _last_ws_health_check = 0.0
        while self._running:
            # Periodic WS health check — every 30s, verify feed is alive
            now_mono = asyncio.get_event_loop().time()
            if self.feed and (now_mono - _last_ws_health_check) > 30:
                _last_ws_health_check = now_mono
                try:
                    if hasattr(self.feed, "check_health") and not self.feed.check_health():
                        self._log("info", "📡 WS feed stale — triggering reconnect")
                except Exception:
                    pass
            try:
                trades = list(self.open_trades.items())
                if not trades:
                    await asyncio.sleep(1)
                    continue

                # Batch-fetch all LTPs in ONE non-blocking call
                price_map = await self._fetch_all_ltps(trades)

                for tid, trade in trades:
                    current_prem = price_map.get(tid, 0.0)
                    if current_prem <= 0:
                        continue

                    # ── Handle pending stop-limit trades ──
                    if trade.status == "pending":
                        trade.current_premium = current_prem
                        if trade.entry_limit_price <= current_prem <= trade.entry_limit_max:
                            # Premium entered the trigger range — activate!
                            await self._activate_pending_trade(trade)
                        continue  # Don't check exit for pending trades

                    # ── Handle open trades ──
                    # Backfill entry price if it was 0 at entry time
                    if trade.entry_premium == 0:
                        trade.entry_premium = current_prem
                        # Reset grace period so check_exit waits 3s from backfill
                        trade.entry_time = _now_ist()
                        if not trade.target_premium and trade.target_pct > 0:
                            mult = 1 if trade.transaction_type == "BUY" else -1
                            trade.target_premium = round(current_prem * (1 + mult * trade.target_pct / 100), 2)
                        if not trade.sl_premium and trade.sl_pct > 0:
                            mult = -1 if trade.transaction_type == "BUY" else 1
                            trade.sl_premium = round(current_prem * (1 + mult * trade.sl_pct / 100), 2)
                        self._log("info", f"📌 Trade {tid} entry price backfilled @ ₹{current_prem:.2f}")
                    trade.current_premium = current_prem
                    reason = trade.check_exit(trade.current_premium)
                    if reason:
                        if tid not in self.open_trades:
                            continue  # Already closed by manual exit during LTP fetch
                        await self._close_trade(trade, reason)
            except Exception as e:
                self._log("error", f"Monitor error: {e}")
            await asyncio.sleep(1)

    async def _activate_pending_trade(self, trade: ScalpTrade):
        """Place broker order for a pending stop-limit trade when premium enters the trigger range."""
        tid = trade.trade_id
        mode_label = "[PAPER] " if trade.mode == "paper" else ""
        self._log(
            "entry",
            f"{mode_label}🎯 STOP-LIMIT TRIGGERED: {trade.transaction_type} {trade.underlying} "
            f"{trade.strike}{trade.option_type} | LTP ₹{trade.current_premium:.2f} "
            f"in range ₹{trade.entry_limit_price:.2f}–₹{trade.entry_limit_max:.2f}",
        )

        if trade.mode == "paper":
            order_id = "PAPER"
            entry_premium = trade.current_premium
        else:
            # Place real broker order
            try:
                result = self.dhan.place_option_order(
                    underlying=trade.underlying,
                    strike_price=trade.strike,
                    option_type=trade.option_type,
                    expiry=trade.expiry,
                    transaction_type=trade.transaction_type,
                    quantity=trade.quantity,
                    order_type="MARKET",
                    product_type="INTRADAY",
                    tag="AF_SCALP_SL",
                )
                order_id = result.get("orderId", "")
                order_status = str(result.get("orderStatus", result.get("status", ""))).upper()
                if order_status in ("REJECTED", "CANCELLED", "FAILED"):
                    reason = result.get("remarks", result.get("message", result.get("rejectedReason", "Unknown")))
                    self._log("error", f"❌ Stop-limit order rejected: {reason}")
                    # Remove the pending trade
                    self.open_trades.pop(tid, None)
                    return
                if not order_id:
                    self._log("error", f"❌ No orderId returned for stop-limit: {result}")
                    self.open_trades.pop(tid, None)
                    return
            except Exception as e:
                self._log("error", f"❌ Stop-limit order placement failed: {e}")
                self.open_trades.pop(tid, None)
                return

            # Brief delay then get fill premium
            await asyncio.sleep(0.5)
            entry_premium = (
                self.dhan.get_option_ltp(trade.underlying, trade.strike, trade.expiry, trade.option_type)
                or trade.current_premium
            )

            # Enable throttle for live trades
            enable_marketfeed_throttle(True)

        # Activate the trade
        trade.status = "open"
        trade.order_id = order_id
        trade.entry_premium = entry_premium
        trade.entry_time = _now_ist()
        trade.current_premium = entry_premium

        # Compute pct-based targets now that we have an actual entry price
        if not trade.target_premium and trade.target_pct > 0:
            if trade.transaction_type == "BUY":
                trade.target_premium = round(entry_premium * (1 + trade.target_pct / 100), 2)
            else:
                trade.target_premium = round(entry_premium * (1 - trade.target_pct / 100), 2)
        if not trade.sl_premium and trade.sl_pct > 0:
            if trade.transaction_type == "BUY":
                trade.sl_premium = round(entry_premium * (1 - trade.sl_pct / 100), 2)
            else:
                trade.sl_premium = round(entry_premium * (1 + trade.sl_pct / 100), 2)

        self._log(
            "entry",
            f"{mode_label}✅ SCALP ENTER (stop-limit): {trade.transaction_type} {trade.underlying} "
            f"{trade.strike}{trade.option_type} @ ₹{entry_premium:.2f} | orderId={order_id} "
            f"| target=₹{trade.target_premium or 'none'} SL=₹{trade.sl_premium or 'none'}",
        )

        # Place broker-side SL/TP safety-net orders (live mode only)
        if trade.mode == "live":
            await self._place_broker_sl_tp(trade)

    async def _place_broker_sl_tp(self, trade: ScalpTrade):
        """Place SL and/or TP orders on the broker as a safety net (live mode only).
        These protect the position even if the server goes down."""
        if trade.mode != "live" or not trade.entry_premium:
            return
        exit_txn = "SELL" if trade.transaction_type == "BUY" else "BUY"

        # ── SL order (Stop-Loss Limit on broker) ──
        if trade.sl_premium > 0:
            try:
                if exit_txn == "SELL":
                    sl_price = round(max(0.05, trade.sl_premium * 0.95), 2)
                else:
                    sl_price = round(trade.sl_premium * 1.05, 2)
                result = await asyncio.to_thread(
                    self.dhan.place_option_order,
                    underlying=trade.underlying,
                    strike_price=trade.strike,
                    option_type=trade.option_type,
                    expiry=trade.expiry,
                    transaction_type=exit_txn,
                    quantity=trade.quantity,
                    order_type="SL",
                    product_type="INTRADAY",
                    price=sl_price,
                    trigger_price=trade.sl_premium,
                    tag="AF_SC_SL",
                )
                oid = result.get("orderId", "")
                if oid:
                    trade.broker_sl_order_id = str(oid)
                    self._log("info", f"🛡️ Broker SL placed: {exit_txn} trigger=₹{trade.sl_premium} orderId={oid}")
            except Exception as e:
                self._log("error", f"Broker SL placement failed: {e}")

        # ── TP order (Limit order on broker) ──
        if trade.target_premium > 0:
            try:
                result = await asyncio.to_thread(
                    self.dhan.place_option_order,
                    underlying=trade.underlying,
                    strike_price=trade.strike,
                    option_type=trade.option_type,
                    expiry=trade.expiry,
                    transaction_type=exit_txn,
                    quantity=trade.quantity,
                    order_type="LIMIT",
                    product_type="INTRADAY",
                    price=trade.target_premium,
                    tag="AF_SC_TP",
                )
                oid = result.get("orderId", "")
                if oid:
                    trade.broker_tp_order_id = str(oid)
                    self._log("info", f"🎯 Broker TP placed: {exit_txn} limit=₹{trade.target_premium} orderId={oid}")
            except Exception as e:
                self._log("error", f"Broker TP placement failed: {e}")

    async def _cancel_broker_orders(self, trade: ScalpTrade):
        """Cancel pending broker-side SL/TP orders (called before software-triggered exit)."""
        for label, oid_attr in [("SL", "broker_sl_order_id"), ("TP", "broker_tp_order_id")]:
            oid = getattr(trade, oid_attr, "")
            if oid:
                try:
                    await asyncio.to_thread(self.dhan.cancel_order, oid)
                    self._log("info", f"🚫 Broker {label} cancelled: orderId={oid}")
                except Exception as e:
                    self._log("error", f"Broker {label} cancel failed ({oid}): {e}")
                setattr(trade, oid_attr, "")

    async def _modify_broker_sl_tp(self, trade: ScalpTrade, **kwargs):
        """Modify broker-side SL/TP orders when user updates targets (live mode only)."""
        if trade.mode != "live":
            return
        new_sl = kwargs.get("sl_premium")
        new_tp = kwargs.get("target_premium")
        exit_txn = "SELL" if trade.transaction_type == "BUY" else "BUY"

        if new_sl is not None and trade.broker_sl_order_id:
            try:
                if exit_txn == "SELL":
                    sl_price = round(max(0.05, new_sl * 0.95), 2)
                else:
                    sl_price = round(new_sl * 1.05, 2)
                await asyncio.to_thread(
                    self.dhan.modify_order,
                    order_id=trade.broker_sl_order_id,
                    price=sl_price,
                    trigger_price=new_sl,
                )
                self._log("info", f"🛡️ Broker SL modified: trigger=₹{new_sl} orderId={trade.broker_sl_order_id}")
            except Exception as e:
                self._log("error", f"Broker SL modify failed: {e}")
        elif new_sl is not None and new_sl > 0 and not trade.broker_sl_order_id:
            # SL was 0 before, now user set one — place new SL order
            trade.sl_premium = new_sl
            await self._place_broker_sl_tp(trade)

        if new_tp is not None and trade.broker_tp_order_id:
            try:
                await asyncio.to_thread(
                    self.dhan.modify_order,
                    order_id=trade.broker_tp_order_id,
                    price=new_tp,
                )
                self._log("info", f"🎯 Broker TP modified: limit=₹{new_tp} orderId={trade.broker_tp_order_id}")
            except Exception as e:
                self._log("error", f"Broker TP modify failed: {e}")
        elif new_tp is not None and new_tp > 0 and not trade.broker_tp_order_id:
            trade.target_premium = new_tp
            await self._place_broker_sl_tp(trade)

    async def _fetch_all_ltps(self, trades: list) -> dict:
        """Fetch LTPs for all open trades in a single batched API call.
        Returns {trade_id: ltp_float}.
        WS cache is checked first; remaining trades are batched into one REST call.
        The REST call runs in a thread pool so it never blocks the event loop."""
        result = {}
        nse_ids: Dict[int, int] = {}  # trade_id -> security_id
        bse_ids: Dict[int, int] = {}  # trade_id -> security_id

        for tid, trade in trades:
            # Try WS cache first
            ws_sec_id = self._ws_subs.get(tid)
            if ws_sec_id and self.feed:
                try:
                    ltp = self.feed.get_ltp(ws_sec_id)
                    if ltp and ltp > 0:
                        result[tid] = float(ltp)
                        continue
                except Exception:
                    pass
                # WS cache returned 0 — try re-subscribing (silent recovery)
                try:
                    new_id = self.feed.subscribe_option(trade.underlying, trade.strike, trade.expiry, trade.option_type)
                    if new_id:
                        self._ws_subs[tid] = new_id
                except Exception:
                    pass
            elif self.feed and tid not in self._ws_subs:
                # Trade has no WS subscription yet — subscribe now
                try:
                    ws_sec_id = self.feed.subscribe_option(
                        trade.underlying, trade.strike, trade.expiry, trade.option_type
                    )
                    if ws_sec_id:
                        self._ws_subs[tid] = ws_sec_id
                except Exception:
                    pass
            # Queue for batch REST fetch (fallback)
            sec_id = ScripMaster.lookup(trade.underlying, trade.strike, trade.expiry, trade.option_type)
            if sec_id:
                if trade.underlying == "SENSEX":
                    bse_ids[tid] = int(sec_id)
                else:
                    nse_ids[tid] = int(sec_id)

        if nse_ids or bse_ids:
            segments: Dict[str, list] = {}
            if nse_ids:
                segments["NSE_FNO"] = list(set(nse_ids.values()))
            if bse_ids:
                segments["BSE_FNO"] = list(set(bse_ids.values()))
            try:
                data = await asyncio.to_thread(self.dhan.get_ltp_multi, segments)

                def _extract(seg_data: dict, sec_id: int) -> float:
                    for key in (str(sec_id), int(sec_id)):
                        v = seg_data.get(key, {})
                        if isinstance(v, dict):
                            return float(v.get("last_price", v.get("ltp", 0)))
                        elif isinstance(v, (int, float)):
                            return float(v)
                    return 0.0

                for tid, sec_id in nse_ids.items():
                    ltp = _extract(data.get("NSE_FNO", {}), sec_id)
                    if ltp > 0:
                        result[tid] = ltp
                for tid, sec_id in bse_ids.items():
                    ltp = _extract(data.get("BSE_FNO", {}), sec_id)
                    if ltp > 0:
                        result[tid] = ltp
            except Exception as e:
                self._log("error", f"Batch LTP fetch failed: {e}")

        return result

    def _get_ltp(self, trade: ScalpTrade, trade_id: int) -> float:
        """Synchronous LTP helper — used only for exit price snapshots."""
        ws_sec_id = self._ws_subs.get(trade_id)
        if ws_sec_id and self.feed:
            try:
                ltp = self.feed.get_ltp(ws_sec_id)
                if ltp and ltp > 0:
                    return float(ltp)
            except Exception:
                pass
        try:
            return self.dhan.get_option_ltp(trade.underlying, trade.strike, trade.expiry, trade.option_type)
        except Exception:
            return 0.0

    async def _close_trade(self, trade: ScalpTrade, reason: str):
        """Place exit order (or simulate in paper mode) and move trade to closed_trades."""
        # Guard against double-close (race between manual exit and auto-exit monitor)
        if trade.trade_id not in self.open_trades or trade.status == "closed":
            self._log("info", f"⚠️ Trade {trade.trade_id} already closed, skipping duplicate exit")
            return

        # Cancel pending stop-limit trade — no position to exit
        if trade.status == "pending":
            trade.status = "closed"
            trade.exit_time = _now_ist()
            trade.exit_reason = "cancelled"
            self.open_trades.pop(trade.trade_id, None)
            self._ws_subs.pop(trade.trade_id, None)
            self._log("info", f"🚫 STOP-LIMIT CANCELLED: {trade.underlying} {trade.strike}{trade.option_type}")
            return

        # Cancel broker-side SL/TP orders before placing exit
        if trade.broker_sl_order_id or trade.broker_tp_order_id:
            await self._cancel_broker_orders(trade)

        exit_txn = "SELL" if trade.transaction_type == "BUY" else "BUY"
        exit_order_id = ""
        if trade.mode == "paper":
            exit_order_id = "PAPER"
        else:
            try:
                # Use LIMIT order with aggressive fill price — Dhan converts
                # F&O MARKET orders to LIMIT with a bad price buffer for SELLs,
                # causing exit orders to hang as pending instead of filling.
                ltp = self._get_ltp(trade, trade.trade_id) or trade.current_premium
                if exit_txn == "SELL":
                    # Sell at 5% below LTP to guarantee immediate fill
                    exit_price = round(max(0.05, ltp * 0.95), 2)
                else:
                    # Buy at 5% above LTP to guarantee immediate fill
                    exit_price = round(ltp * 1.05, 2)
                self._log("info", f"Exit {exit_txn} LIMIT @ ₹{exit_price} (LTP=₹{ltp})")
                result = self.dhan.place_option_order(
                    underlying=trade.underlying,
                    strike_price=trade.strike,
                    option_type=trade.option_type,
                    expiry=trade.expiry,
                    transaction_type=exit_txn,
                    quantity=trade.quantity,
                    order_type="LIMIT",
                    product_type="INTRADAY",
                    price=exit_price,
                    tag=f"AF_SCALP_EXIT_{reason.upper()[:8]}",
                )
                exit_order_id = result.get("orderId", "")
            except Exception as e:
                self._log("error", f"Exit order failed for trade {trade.trade_id}: {e}")

        exit_prem = self._get_ltp(trade, trade.trade_id) or trade.current_premium
        pnl = trade._compute_pnl(exit_prem)

        trade.exit_time = _now_ist()
        trade.exit_premium = exit_prem
        trade.exit_reason = reason
        trade.exit_order_id = exit_order_id
        trade.pnl = round(pnl, 2)
        trade.status = "closed"
        trade.current_premium = exit_prem

        trade_dict = trade.to_dict()
        self.closed_trades.append(trade_dict)
        self.open_trades.pop(trade.trade_id, None)
        self._ws_subs.pop(trade.trade_id, None)

        # Persist via callback (auto-exits + manual exits all go through here)
        if self.on_trade_close:
            try:
                self.on_trade_close(trade_dict)
            except Exception as e:
                self._log("error", f"on_trade_close callback failed: {e}")

        # Disable throttle when no live trades remain
        if not any(t.mode == "live" for t in self.open_trades.values()):
            enable_marketfeed_throttle(False)

        pnl_sign = "+" if pnl >= 0 else ""
        self._log(
            "exit" if pnl >= 0 else "stop",
            f"{'✅' if pnl >= 0 else '🛑'} SCALP EXIT [{reason}]: "
            f"{trade.underlying} {trade.strike}{trade.option_type} "
            f"entry=₹{trade.entry_premium:.2f} exit=₹{exit_prem:.2f} "
            f"P&L={pnl_sign}₹{pnl:.2f}",
        )

    def _log(self, evt_type: str, message: str):
        entry = {
            "time": _now_ist().strftime("%H:%M:%S"),
            "type": evt_type,
            "message": message,
        }
        self.event_log.append(entry)
        print(f"[SCALP][{evt_type.upper()}] {message}")
