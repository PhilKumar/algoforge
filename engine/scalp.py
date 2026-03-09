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
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from broker.dhan import ScripMaster

IST = timezone(timedelta(hours=5, minutes=30))

_SCALP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scalp_trades.json")

# How many consecutive exit-order failures before we stop retrying and alert
_MAX_EXIT_RETRIES = 3


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

        # Store raw pct values so backfill can recompute targets after
        # entry_premium is confirmed (it may be 0 at construction time).
        self._target_pct = target_pct
        self._sl_pct = sl_pct

        # Compute absolute target/SL premiums if only % given
        self.target_premium = target_premium
        self.sl_premium = sl_premium

        if not self.target_premium and target_pct > 0 and entry_premium > 0:
            if transaction_type == "BUY":
                self.target_premium = round(entry_premium * (1 + target_pct / 100), 2)
            else:
                self.target_premium = round(entry_premium * (1 - target_pct / 100), 2)

        if not self.sl_premium and sl_pct > 0 and entry_premium > 0:
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
        self.status: str = "open"

        # Exit retry counter — incremented on each failed broker exit call
        self._exit_retries: int = 0

    def _compute_pnl(self, current_prem: float) -> float:
        mult = 1 if self.transaction_type == "BUY" else -1
        return mult * (current_prem - self.entry_premium) * self.quantity

    def check_exit(self, current_prem: float) -> Optional[str]:
        """Returns exit reason string if an exit rule is triggered, else None."""
        now = _now_ist()

        # Grace period: don't auto-exit within 3 seconds of entry.
        # Protects against: (1) stale LTP from rate-limited API responses,
        # (2) instant SL triggers before real market data arrives.
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
            "pnl": round(self._compute_pnl(self.current_premium), 2),
            "status": self.status,
            "mode": self.mode,
            "exit_retries": self._exit_retries,
        }


class ScalpEngine:
    """
    Manages all active scalp trades.
    • Runs a background monitoring loop.
    • Uses _market_feed LTP cache for zero-latency price checks.
    • Falls back to REST `get_option_ltp` every 2s if no WS feed.
    """

    def __init__(self, dhan_client, market_feed=None):
        self.dhan = dhan_client
        self.feed = market_feed  # LiveMarketFeed instance or None

        self.open_trades: Dict[int, ScalpTrade] = {}
        self.closed_trades: list = []
        self.event_log: list = []
        self._trade_counter: int = 0
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._ws_subs: Dict[int, str] = {}  # trade_id → ws_sec_id

        # Set of trade_ids currently being closed — prevents race-condition
        # double-exits when monitor loop and manual exit fire simultaneously.
        self._closing: set = set()

        # Load persisted closed trades from disk
        self._load_trades()

    # ── Persistence ──────────────────────────────────────────────

    def _load_trades(self):
        """Load closed trades from scalp_trades.json on startup."""
        if os.path.exists(_SCALP_FILE):
            try:
                with open(_SCALP_FILE, "r") as f:
                    data = json.load(f)
                self.closed_trades = data if isinstance(data, list) else []
                # Restore trade counter from highest trade_id
                if self.closed_trades:
                    max_id = max(t.get("trade_id", 0) for t in self.closed_trades)
                    self._trade_counter = max_id
                print(
                    f"[SCALP] Loaded {len(self.closed_trades)} closed trades from disk (counter={self._trade_counter})"
                )
            except Exception as e:
                print(f"[SCALP] Failed to load trades: {e}")
                self.closed_trades = []

    def _save_trades(self):
        """Persist closed trades to scalp_trades.json (fire-and-forget, non-blocking)."""
        data = list(self.closed_trades)  # snapshot
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._save_trades_sync, data)
        except RuntimeError:
            # No running loop (e.g. shutdown) — write synchronously
            self._save_trades_sync(data)

    @staticmethod
    def _save_trades_sync(data: list):
        """Synchronous disk write — runs in thread pool, never blocks the event loop."""
        try:
            tmp = _SCALP_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, _SCALP_FILE)
        except Exception as e:
            print(f"[SCALP] Failed to save trades: {e}")

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
    ) -> Dict[str, Any]:
        """Place a broker order (or simulate in paper mode) and register the scalp trade."""
        # Map common aliases to Dhan API values
        _pt_map = {"MIS": "INTRADAY", "NRML": "MARGIN"}
        product_type = _pt_map.get(product_type, product_type)
        quantity = lots * lot_size

        if mode == "paper":
            # Paper mode: no real order — snapshot current LTP as entry price.
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
                    await asyncio.sleep(0.3)
        else:
            # Place real broker order via thread pool — never block the event loop
            try:
                result = await asyncio.to_thread(
                    self.dhan.place_option_order,
                    underlying=underlying,
                    strike_price=strike,
                    option_type=option_type,
                    expiry=expiry,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    order_type=order_type,
                    product_type=product_type,
                    tag="AF_SCALP",
                )
                order_id = result.get("orderId", "")
            except Exception as e:
                return {"status": "error", "message": str(e)}

            # Use LTP as immediate estimated entry price — don't block on fill verify
            entry_premium = self.dhan.get_option_ltp(underlying, strike, expiry, option_type) or 0.0

        # ── Validate & auto-fix SL/Target vs entry price ────────
        # For BUY: SL must be BELOW entry, Target must be ABOVE entry.
        # For SELL: SL must be ABOVE entry, Target must be BELOW entry.
        # If swapped (e.g. user sets SL=100 for a BUY at 43), auto-swap and warn.
        warnings = []
        if entry_premium > 0 and sl_premium > 0 and target_premium > 0:
            if transaction_type == "BUY" and sl_premium > entry_premium and target_premium > entry_premium:
                # SL is above entry — user likely swapped SL and Target
                if sl_premium > target_premium:
                    sl_premium, target_premium = target_premium, sl_premium
                    warnings.append(
                        f"Auto-swapped Target/SL: SL was ₹{target_premium} (above entry ₹{entry_premium:.2f}). "
                        f"Now Target=₹{target_premium} SL=₹{sl_premium}"
                    )
            elif transaction_type == "SELL" and sl_premium < entry_premium and target_premium < entry_premium:
                if sl_premium < target_premium:
                    sl_premium, target_premium = target_premium, sl_premium
                    warnings.append(f"Auto-swapped Target/SL for SELL: Now Target=₹{target_premium} SL=₹{sl_premium}")
        # Single-value sanity: SL alone on wrong side
        if entry_premium > 0 and sl_premium > 0 and target_premium == 0:
            if transaction_type == "BUY" and sl_premium >= entry_premium:
                warnings.append(
                    f"⚠️ SL premium ₹{sl_premium} >= entry ₹{entry_premium:.2f} for BUY — "
                    f"SL will trigger immediately. Did you mean SL rupees (₹ loss) instead?"
                )
            elif transaction_type == "SELL" and sl_premium <= entry_premium:
                warnings.append(
                    f"⚠️ SL premium ₹{sl_premium} <= entry ₹{entry_premium:.2f} for SELL — SL will trigger immediately."
                )
        for w in warnings:
            self._log("warn", w)

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

        mode_label = "[PAPER] " if mode == "paper" else ""
        self._log(
            "entry",
            f"{mode_label}✅ SCALP ENTER: {transaction_type} {underlying} {strike}{option_type} "
            f"@ ₹{entry_premium:.2f} | orderId={order_id} "
            f"| target=₹{trade.target_premium or 'none'} SL=₹{trade.sl_premium or 'none'}",
        )

        if not self._running:
            self.start()

        # Fire-and-forget: verify fill price in background, update trade when confirmed
        if order_id and order_id != "PAPER":
            asyncio.create_task(self._verify_fill_bg(self._trade_counter, order_id))

        return {"status": "ok", "trade_id": self._trade_counter, "trade": trade.to_dict()}

    async def _verify_fill_bg(self, trade_id: int, order_id: str):
        """Background task: verify broker fill price and update trade entry price."""
        try:
            fill = await asyncio.to_thread(self.dhan.verify_order_fill, order_id, 15, 1.5)
            trade = self.open_trades.get(trade_id)
            if not trade:
                return  # already closed
            if fill.get("status") == "FILLED" and fill.get("avg_price"):
                actual = float(fill["avg_price"])
                old_entry = trade.entry_premium
                trade.entry_premium = actual
                # Recompute pct-based targets with confirmed fill price
                if not trade.target_premium and trade._target_pct > 0:
                    mult = 1 if trade.transaction_type == "BUY" else -1
                    trade.target_premium = round(actual * (1 + mult * trade._target_pct / 100), 2)
                if not trade.sl_premium and trade._sl_pct > 0:
                    mult = -1 if trade.transaction_type == "BUY" else 1
                    trade.sl_premium = round(actual * (1 + mult * trade._sl_pct / 100), 2)
                self._log(
                    "info",
                    f"📌 Entry fill verified: ₹{actual:.2f} (was ₹{old_entry:.2f}) "
                    f"| target=₹{trade.target_premium or 'none'} SL=₹{trade.sl_premium or 'none'} "
                    f"(orderId={order_id})",
                )
            else:
                self._log("warn", f"⚠️ Entry fill not confirmed for trade {trade_id}: {fill.get('message', '')}")
        except Exception as e:
            self._log("error", f"Fill verification error for trade {trade_id}: {e}")

    async def exit_trade(self, trade_id: int, reason: str = "manual") -> Dict[str, Any]:
        """Manually exit an open scalp trade."""
        trade = self.open_trades.get(trade_id)
        if not trade:
            return {"status": "error", "message": f"Trade {trade_id} not found or already closed"}
        await self._close_trade(trade, reason)
        return {"status": "ok", "trade": trade.to_dict()}

    async def update_trade_targets(self, trade_id: int, **kwargs) -> Dict[str, Any]:
        """Update target/SL for an open trade (e.g. after looking at the chart)."""
        trade = self.open_trades.get(trade_id)
        if not trade:
            return {"status": "error", "message": f"Trade {trade_id} not found"}
        for attr in ("target_premium", "sl_premium", "target_rupees", "sl_rupees", "sqoff_time"):
            if attr in kwargs and kwargs[attr] is not None:
                setattr(trade, attr, kwargs[attr])
        self._log("info", f"🎯 Trade {trade_id} targets updated: {kwargs}")
        return {"status": "ok", "trade": trade.to_dict()}

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "open_trades": [t.to_dict() for t in self.open_trades.values()],
            "closed_trades": list(reversed(self.closed_trades)),
            "event_log": list(reversed(self.event_log[-100:])),
            "total_pnl": round(sum(t.get("pnl", 0) for t in self.closed_trades), 2),
        }

    # ── Internal monitoring ───────────────────────────────────────

    async def _monitor_loop(self):
        """Poll/WS prices every ~1s and trigger auto-exits."""
        while self._running:
            try:
                trades = list(self.open_trades.items())
                if not trades:
                    await asyncio.sleep(1)
                    continue

                # Batch-fetch all LTPs in ONE non-blocking call
                price_map = await self._fetch_all_ltps(trades)

                for tid, trade in trades:
                    # Skip if this trade is already being closed
                    if tid in self._closing:
                        continue

                    current_prem = price_map.get(tid, 0.0)
                    got_fresh_ltp = current_prem > 0
                    if got_fresh_ltp:
                        # Backfill entry price if it was 0 at entry time
                        if trade.entry_premium == 0:
                            trade.entry_premium = current_prem
                            if not trade.target_premium and trade._target_pct > 0:
                                mult = 1 if trade.transaction_type == "BUY" else -1
                                trade.target_premium = round(current_prem * (1 + mult * trade._target_pct / 100), 2)
                            if not trade.sl_premium and trade._sl_pct > 0:
                                mult = -1 if trade.transaction_type == "BUY" else 1
                                trade.sl_premium = round(current_prem * (1 + mult * trade._sl_pct / 100), 2)
                            self._log(
                                "info",
                                f"📌 Trade {tid} entry price backfilled @ ₹{current_prem:.2f} "
                                f"| target=₹{trade.target_premium or 'none'} SL=₹{trade.sl_premium or 'none'}",
                            )
                        trade.current_premium = current_prem

                    # Only check exits when we have a FRESH LTP from this tick.
                    # If the API was rate-limited or errored, skip exit check
                    # to avoid false triggers on stale current_premium.
                    if not got_fresh_ltp:
                        continue

                    reason = trade.check_exit(trade.current_premium)
                    if reason:
                        # Re-check trade is still open before closing
                        if tid not in self.open_trades:
                            continue
                        await self._close_trade(trade, reason)
            except Exception as e:
                self._log("error", f"Monitor error: {e}")
            await asyncio.sleep(1)

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
            # Queue for batch REST fetch
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
        """
        Place exit order and move trade to closed_trades.

        Safety guarantees:
          1. Atomic _closing set prevents double-exit from monitor + manual exit racing.
          2. Exit order uses LIMIT with 5% slippage — avoids Dhan's bad MARKET→LIMIT conversion.
          3. Exit order is placed via asyncio.to_thread — never blocks the event loop.
          4. If broker API fails, trade stays in open_trades and retries on next monitor tick.
             After _MAX_EXIT_RETRIES consecutive failures, trade is force-closed with last LTP
             and a critical alert is logged so the operator can manually verify Dhan.
          5. Uses .pop() instead of del — safe against any remaining race conditions.
        """
        tid = trade.trade_id

        # ── Guard 1: already closed ──────────────────────────────
        if trade.status == "closed" or tid not in self.open_trades:
            return

        # ── Guard 2: atomic double-close prevention ───────────────
        if tid in self._closing:
            self._log("info", f"⚠️ Trade {tid} exit already in progress ({reason}), skipping duplicate")
            return
        self._closing.add(tid)

        try:
            exit_txn = "SELL" if trade.transaction_type == "BUY" else "BUY"
            exit_order_id = ""

            if trade.mode == "paper":
                exit_order_id = "PAPER"
            else:
                # ── LIMIT order with aggressive slippage (not MARKET) ──────
                # Dhan converts F&O MARKET orders to LIMIT with a conservative
                # buffer for SELLs, causing them to hang as PENDING.
                # Slippage widens on each retry: 5% → 10% → 15%.
                try:
                    ltp = await asyncio.to_thread(self._get_ltp, trade, tid)
                except Exception:
                    ltp = 0.0
                ltp = ltp or trade.current_premium
                slippage = 0.05 + (trade._exit_retries * 0.05)  # 5%, 10%, 15%
                if exit_txn == "SELL":
                    exit_price = round(max(0.05, ltp * (1 - slippage)), 2)
                else:
                    exit_price = round(ltp * (1 + slippage), 2)

                self._log(
                    "info",
                    f"🚀 Placing exit [{reason}]: {exit_txn} LIMIT @ ₹{exit_price} "
                    f"(LTP=₹{ltp:.2f}, slip={slippage:.0%}) | trade={tid} retry={trade._exit_retries}",
                )

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
                        price=exit_price,
                        tag=f"AF_SCALP_EXIT_{reason.upper()[:8]}",
                    )
                    exit_order_id = result.get("orderId", "")
                    if not exit_order_id:
                        raise ValueError(f"Broker returned no orderId. Response: {result}")

                except Exception as broker_err:
                    trade._exit_retries += 1
                    alert = (
                        f"🚨🚨🚨 EXIT ORDER FAILED (attempt {trade._exit_retries}/{_MAX_EXIT_RETRIES}) "
                        f"for trade {tid} [{reason}] — {broker_err} "
                        f"| {trade.underlying} {trade.strike}{trade.option_type} "
                        f"| POSITION STILL OPEN AT DHAN — CHECK BROKER DASHBOARD"
                    )
                    self._log("error", alert)

                    if trade._exit_retries < _MAX_EXIT_RETRIES:
                        # Leave trade in open_trades — monitor loop will retry on next tick
                        return

                    # Max retries exceeded — force-close in system state with loud alert
                    self._log(
                        "error",
                        f"🚨🚨🚨 MAX EXIT RETRIES EXCEEDED for trade {tid}. "
                        f"Force-closing in system state. MANUALLY VERIFY DHAN POSITION. "
                        f"Last LTP=₹{ltp:.2f}",
                    )
                    exit_order_id = "FAILED_CHECK_DHAN"

        except Exception as unexpected_err:
            self._log("error", f"Unexpected error in _close_trade for trade {tid}: {unexpected_err}")
            self._closing.discard(tid)
            return

        # ── Verify fill price ────────────────────────────────────
        exit_prem = 0.0
        if exit_order_id and exit_order_id not in ("PAPER", "FAILED_CHECK_DHAN"):
            try:
                fill = await asyncio.to_thread(self.dhan.verify_order_fill, exit_order_id, 15, 1.5)
                fill_status = fill.get("status", "")
                if fill_status == "FILLED" and fill.get("avg_price"):
                    exit_prem = float(fill["avg_price"])
                    self._log("info", f"📌 Exit fill verified: ₹{exit_prem:.2f} (orderId={exit_order_id})")
                elif fill_status in ("REJECTED", "CANCELLED"):
                    # Order was rejected — increment retry counter and leave trade open
                    trade._exit_retries += 1
                    self._log(
                        "error",
                        f"🚨 Exit order {exit_order_id} was {fill_status}: {fill.get('message', '')} "
                        f"| trade {tid} (retry {trade._exit_retries}/{_MAX_EXIT_RETRIES}) "
                        f"— will retry on next tick",
                    )
                    if trade._exit_retries < _MAX_EXIT_RETRIES:
                        self._closing.discard(tid)
                        return  # keep trade open for retry
                    self._log("error", f"🚨🚨🚨 MAX RETRIES after {fill_status} for trade {tid}. Force-closing.")
                    exit_order_id = "FAILED_CHECK_DHAN"
                elif fill_status == "TIMEOUT":
                    # Order is stuck PENDING at Dhan — cancel it and retry with wider slippage
                    self._log(
                        "warn",
                        f"⚠️ Exit order {exit_order_id} still PENDING after 15s — cancelling and retrying",
                    )
                    try:
                        await asyncio.to_thread(self.dhan.cancel_order, exit_order_id)
                        self._log("info", f"Cancelled pending exit order {exit_order_id}")
                    except Exception as cancel_err:
                        self._log("error", f"Failed to cancel pending order {exit_order_id}: {cancel_err}")
                    trade._exit_retries += 1
                    if trade._exit_retries < _MAX_EXIT_RETRIES:
                        self._closing.discard(tid)
                        return  # keep trade open — next tick will retry with wider slippage
                    self._log("error", f"🚨🚨🚨 MAX RETRIES after TIMEOUT for trade {tid}. Force-closing.")
                    exit_order_id = "FAILED_CHECK_DHAN"
                else:
                    self._log(
                        "warn",
                        f"⚠️ Exit fill unconfirmed (status={fill_status}) for trade {tid}. "
                        f"Falling back to LTP. Msg: {fill.get('message', '')}",
                    )
            except Exception as verify_err:
                self._log("error", f"Exit fill verification error for trade {tid}: {verify_err}")

        # Fallback to LTP if verify_order_fill didn't return a price
        if not exit_prem:
            exit_prem = self._get_ltp(trade, tid) or trade.current_premium

        pnl = trade._compute_pnl(exit_prem)

        trade.exit_time = _now_ist()
        trade.exit_premium = exit_prem
        trade.exit_reason = reason
        trade.exit_order_id = exit_order_id
        trade.pnl = round(pnl, 2)
        trade.status = "closed"
        trade.current_premium = exit_prem

        self.closed_trades.append(trade.to_dict())
        self._save_trades()  # persist to disk
        self.open_trades.pop(tid, None)  # safe pop — no KeyError on race
        self._closing.discard(tid)

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
