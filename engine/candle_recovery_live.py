"""The order book that stands between High Entry's replay and real money.

High Entry does not accumulate state the way the fib ladder does: every poll
REPLAYS the whole campaign from bars and rebuilds its trade list. That is
exactly right for paper -- the rules are pure over their inputs -- and it is
the one thing that cannot be pointed at a broker unchanged. A replay that
produced a different answer would not "change its mind"; it would orphan real
orders that are already working.

So the replay keeps deciding, and this keeps the orders. Every trade the
engine reports carries a stable identity -- the campaign, its trade number and
the bar that armed it -- and this book records, once, what was actually sent
for that identity. A trade already ordered is never ordered again. A trade
that DISAPPEARS from a later replay, because the vendor revised a candle under
us, is never quietly sold: the book freezes and asks for a human, because an
order nobody can explain is not one a program should be closing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def trade_key(campaign_id: str, trade: Any) -> str:
    """One trade's identity across replays.

    The trade NUMBER alone is not enough: a revised candle can renumber the
    list. The bar that armed the trade is a fact about the market, so the pair
    survives anything a replay can legitimately do.
    """
    armed = getattr(trade, "armed_at", None)
    stamp = armed.isoformat() if isinstance(armed, datetime) else str(armed)
    return f"{campaign_id}#{int(getattr(trade, 'trade_no', 0))}@{stamp}"


class RecoveryOrderBook:
    """What was really sent for each trade the replay decided on."""

    def __init__(self, executor: Any, *, stop_pct: float = 0.70) -> None:
        self.executor = executor
        # The broker-side stop is a NET, not High Entry's stop rule. The rule
        # is an INDEX level (`sl_level`, marked at the fill and never trailed)
        # and the engine still owns it. What rests at Dhan is a premium a long
        # option only reaches in a collapse, there for the minutes when this
        # process cannot act at all.
        self.stop_pct = float(stop_pct)
        # key -> {order_id, bracket_order_id, strike, expiry, quantity,
        #         option_type, entry_premium, exit_order_id, exit_price, state}
        self.orders: dict[str, dict] = {}
        # Keys this book holds orders for that the latest replay no longer
        # reports. Nothing automatic happens while this is non-empty.
        self.orphans: list[str] = []
        self.log: list[dict] = []

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def frozen(self) -> bool:
        """True when the book and the replay disagree about what exists."""
        return bool(self.orphans)

    @property
    def is_live(self) -> bool:
        return bool(getattr(self.executor, "is_live", False))

    def _note(self, when: datetime, event: str, **fields) -> None:
        self.log.append({"at": when.isoformat(), "event": event, **fields})
        del self.log[:-400]

    # ── the one entry point ─────────────────────────────────────────────────

    def sync(self, campaign_id: str, trades: list, *, symbol: str, side: str, when: datetime) -> dict:
        """Bring the broker into line with what the replay now says.

        Order matters: orphans are detected FIRST, because a book that has lost
        track of an order must not go on to open another one.
        """
        seen = {trade_key(campaign_id, t): t for t in trades}
        mine = {k: v for k, v in self.orders.items() if k.startswith(f"{campaign_id}#")}
        for key in mine:
            if key not in seen and key not in self.orphans and self.orders[key].get("state") == "OPEN":
                self.orphans.append(key)
                self._note(when, "orphaned_order", key=key, order_id=self.orders[key].get("order_id"))
        if self.frozen:
            return {"frozen": True, "orphans": list(self.orphans), "entries": 0, "exits": 0}

        entries = exits = 0
        for key, trade in seen.items():
            record = self.orders.get(key)
            if record is None:
                if self._should_enter(trade):
                    if self._enter(key, trade, symbol=symbol, side=side, when=when):
                        entries += 1
                continue
            if record.get("state") == "OPEN" and getattr(trade, "exit_time", None) is not None:
                if self._exit(key, trade, side=side, when=when):
                    exits += 1
        return {"frozen": False, "orphans": [], "entries": entries, "exits": exits}

    # ── entries ─────────────────────────────────────────────────────────────

    @staticmethod
    def _should_enter(trade: Any) -> bool:
        """Only a trade the replay says is FILLED AND STILL OPEN gets an order.

        A closed trade discovered for the first time is history the book missed
        -- the app was down for its whole life -- and buying it now would be
        opening a position the rules have already exited.
        """
        return (
            getattr(trade, "entry_time", None) is not None
            and getattr(trade, "exit_time", None) is None
            and getattr(trade, "strike", None) is not None
            and int(getattr(trade, "quantity", 0) or 0) > 0
        )

    def _enter(self, key: str, trade: Any, *, symbol: str, side: str, when: datetime) -> bool:
        premium = float(getattr(trade, "entry_premium", 0) or 0)
        stop = max(0.05, round(premium * (1.0 - self.stop_pct), 2)) if premium > 0 else None
        try:
            receipt = self.executor.buy(
                when=when,
                strike=int(trade.strike),
                expiry=trade.expiry,
                option_type=side,
                quantity=int(trade.quantity),
                premium=premium,
                stop_price=stop,
            )
        except Exception as exc:
            # Refused, rejected or unresolved -- all three mean this book must
            # NOT record an order it may not have. A refusal can be retried on
            # the next poll; an unknown is frozen by the caller.
            self._note(when, "entry_failed", key=key, detail=str(exc))
            return False
        self.orders[key] = {
            "order_id": str(receipt.get("order_id") or ""),
            "bracket_order_id": str(receipt.get("bracket_order_id") or "") or None,
            "strike": int(trade.strike),
            "expiry": trade.expiry.isoformat() if hasattr(trade.expiry, "isoformat") else str(trade.expiry),
            "option_type": str(side),
            "quantity": int(receipt.get("traded_quantity") or trade.quantity),
            "entry_premium": float(receipt.get("traded_premium") or premium),
            "symbol": str(symbol),
            "state": "OPEN",
        }
        self._note(
            when,
            "entered",
            key=key,
            order_id=self.orders[key]["order_id"],
            premium=self.orders[key]["entry_premium"],
            stop=stop,
        )
        return True

    # ── exits ───────────────────────────────────────────────────────────────

    def _exit(self, key: str, trade: Any, *, side: str, when: datetime) -> bool:
        record = self.orders[key]
        bracket = record.get("bracket_order_id")
        if bracket:
            # The bracket's own legs have to come off before the leg is sold on
            # the open market, or a stop still working at Dhan would sell a
            # position that is already gone.
            try:
                outcome = self.executor.cancel_bracket(order_id=bracket)
            except Exception as exc:
                self._note(when, "bracket_release_failed", key=key, detail=str(exc))
                return False
            if isinstance(outcome, dict) and outcome.get("traded"):
                record["state"] = "CLOSED"
                record["exit_price"] = outcome.get("avg_price")
                record["exit_reason"] = "bracket_leg"
                self._note(when, "closed_by_bracket", key=key, price=outcome.get("avg_price"))
                return True
            record["bracket_order_id"] = None
        try:
            receipt = self.executor.sell(
                when=when,
                strike=record["strike"],
                expiry=record["expiry"],
                option_type=side,
                quantity=int(record["quantity"]),
            )
        except Exception as exc:
            self._note(when, "exit_failed", key=key, detail=str(exc))
            return False
        status = str((receipt or {}).get("status") or "UNKNOWN").upper()
        if status == "FILLED":
            record["state"] = "CLOSED"
            record["exit_order_id"] = str(receipt.get("order_id") or "")
            record["exit_price"] = receipt.get("avg_price")
            record["exit_reason"] = getattr(trade, "exit_reason", None)
            self._note(when, "exited", key=key, price=receipt.get("avg_price"), reason=record["exit_reason"])
            return True
        if status == "REJECTED":
            # Nothing is working at the broker; the next poll tries again.
            self._note(when, "exit_rejected", key=key)
            return False
        # UNKNOWN. Money may be in motion, so this book stops deciding
        # anything until a human has looked at the broker.
        record["state"] = "EXIT_UNKNOWN"
        self.orphans.append(key)
        self._note(when, "exit_unknown", key=key, order_id=receipt.get("order_id"))
        return False

    # ── the human's way back in ─────────────────────────────────────────────

    def clear_freeze(self, when: datetime) -> int:
        """Accept the broker book as it stands and start deciding again.

        Deliberate, and never automatic: whoever calls this is saying they have
        looked at Dhan and know what is really held.
        """
        count = len(self.orphans)
        for key in self.orphans:
            record = self.orders.get(key)
            if record is not None:
                record["state"] = "RECONCILED"
        self.orphans = []
        self._note(when, "freeze_cleared", count=count)
        return count

    # ── persistence ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"orders": {k: dict(v) for k, v in self.orders.items()}, "orphans": list(self.orphans)}

    def load(self, raw: Optional[dict]) -> None:
        if not raw:
            return
        self.orders = {str(k): dict(v) for k, v in (raw.get("orders") or {}).items()}
        self.orphans = [str(k) for k in (raw.get("orphans") or [])]

    def open_orders(self) -> list[dict]:
        return [dict(v) for v in self.orders.values() if v.get("state") == "OPEN"]


__all__ = ["RecoveryOrderBook", "trade_key"]
