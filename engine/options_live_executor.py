"""One broker-facing executor for the option strategies that had none.

Fib Boundary grew its own executor first, and everything learned there is
here: an order is not a fill, a rejection and an unknown outcome are different
answers, a stop belongs INSIDE the entry so the exchange retires it, and the
last inch to the exchange stays closed until the whole lifecycle is proven.

The rest of the option strategies -- High Entry, Gap Carry, Candle Entry,
Supertrend -- each accepted `mode: "live"` and quietly traded paper before
2026-08-30. They share this one executor instead of growing four more, so a
fix to the money path is a fix everywhere rather than three near-misses.

Fib Boundary is deliberately NOT migrated onto this. It is the strategy
closest to real money and its own executor is tested against its own ladder;
moving it for tidiness would risk the one path that is nearly ready. When this
module has been proven live, that consolidation is the natural next step.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# THE ONE SWITCH. Every real order in this module goes through
# `_availability_guard`, and it refuses while this is False. Like Fib
# Boundary's own flag it is a module constant and not a config value or an
# environment variable, so nothing can drift it open at runtime.
OPTIONS_LIVE_EXECUTION_ENABLED = False


class ExecutionRefused(RuntimeError):
    """An order was decided but the executor would not send it."""


class OrderRejected(ExecutionRefused):
    """Dhan examined the order and said no. Nothing is working at the broker,
    so unlike an unknown outcome this is safe to retry on a later trigger."""


class OptionsPaperExecutor:
    """Records what would have been sent and sends nothing anywhere."""

    mode = "paper"
    is_live = False

    def __init__(self, tag: str = "PF") -> None:
        self.tag = str(tag)

    def buy(self, *, when, strike, expiry, option_type, quantity, premium, stop_price=None) -> dict:
        receipt = {
            "order_id": f"paper-{self.tag}-{when.strftime('%H%M%S')}-{int(strike)}{option_type}",
            "mode": "paper",
        }
        if stop_price:
            receipt["bracket_order_id"] = receipt["order_id"]
        return receipt

    def sell(self, *, when, strike, expiry, option_type, quantity) -> dict:
        return {
            "order_id": f"paper-{self.tag}-exit-{when.strftime('%H%M%S')}-{int(strike)}{option_type}",
            "mode": "paper",
            "status": "FILLED",
        }

    def amend_bracket_target(self, *, order_id, price) -> dict:
        return {"order_id": str(order_id), "amended": True, "mode": "paper"}

    def cancel_bracket(self, *, order_id) -> dict:
        return {"order_id": str(order_id), "cancelled": True, "mode": "paper"}


class OptionsLiveExecutor:
    """The real-money path, armed deliberately and closed by default."""

    mode = "live"
    is_live = True

    def __init__(
        self,
        broker: Any,
        symbol: str,
        *,
        armed: bool = False,
        product_type: str = "MARGIN",
        tag: str = "PF",
    ) -> None:
        self.broker = broker
        self.symbol = symbol
        self.armed = bool(armed)
        # One product for every leg. A SELL under a different product than its
        # BUY does not net at the broker; it opens a short. INTRADAY is squared
        # off by Dhan at ~15:20, so anything meant to be held asks for MARGIN.
        self.product_type = str(product_type).upper()
        self.tag = str(tag)[:20]

    # ── guards ──────────────────────────────────────────────────────────────

    def _availability_guard(self) -> None:
        if not OPTIONS_LIVE_EXECUTION_ENABLED:
            raise ExecutionRefused(
                "Live execution for this strategy is built but disabled until its fills, partial fills "
                "and restart reconciliation are proven against Dhan. Use Paper or Backtest."
            )

    def _guard(self) -> None:
        self._availability_guard()
        if not self.armed:
            raise ExecutionRefused(
                "Live execution is built but not armed. Watch a paper run first, then arm live "
                "deliberately -- no config value or environment variable opens it."
            )

    def _verify(self, order_id, *, max_wait_sec: int = 20) -> dict:
        """Ask Dhan what became of the order. UNKNOWN is an answer too."""
        verify = getattr(self.broker, "verify_order_fill", None)
        if not order_id or verify is None:
            return {"status": "UNKNOWN", "message": "no order id or no verifier on this broker"}
        try:
            return verify(str(order_id), max_wait_sec=max_wait_sec)
        except Exception as exc:  # a status fetch that dies is an unknown, not a fill
            return {"status": "UNKNOWN", "message": str(exc)}

    @staticmethod
    def _expiry_str(expiry) -> str:
        return expiry.isoformat() if isinstance(expiry, (date, datetime)) else str(expiry)

    # ── orders ──────────────────────────────────────────────────────────────

    def buy(self, *, when, strike, expiry, option_type, quantity, premium, stop_price=None) -> dict:
        """Buy one leg, and carry its stop into the same order when given one.

        With `stop_price` this is a Dhan Super Order: entry, target and stop as
        one, with the exchange cancelling whichever leg the other does not
        take. That is the only way a stop cannot outlive the leg it protects.

        The target leg goes in at 0. These strategies name their exit from
        index geometry as the campaign develops, and inventing a premium at
        entry to fill the field would put a sell in the market that no rule
        asked for. `amend_bracket_target` names it when it is real.
        """
        self._guard()
        bracketed = False
        if stop_price:
            order = self.broker.place_super_order(
                underlying=self.symbol,
                strike_price=int(strike),
                option_type=str(option_type),
                expiry=self._expiry_str(expiry),
                transaction_type="BUY",
                quantity=int(quantity),
                target_price=0.0,
                stop_loss_price=float(stop_price),
                order_type="MARKET",
                product_type=self.product_type,
                tag=f"{self.tag}_SO",
            )
            bracketed = True
        else:
            order = self.broker.place_option_order(
                underlying=self.symbol,
                strike_price=float(strike),
                option_type=str(option_type),
                expiry=self._expiry_str(expiry),
                transaction_type="BUY",
                quantity=int(quantity),
                product_type=self.product_type,
                tag=f"{self.tag}_BUY",
            )
        order_id = order.get("orderId") if isinstance(order, dict) else getattr(order, "order_id", None)
        # The acknowledgement is not the fill. What the book carries has to be
        # what Dhan actually traded, or every rupee downstream is an estimate.
        outcome = self._verify(order_id)
        status = str(outcome.get("status") or "UNKNOWN").upper()
        if status == "FILLED":
            avg = float(outcome.get("avg_price") or 0.0)
            filled = int(outcome.get("filled_qty") or 0)
            receipt = {
                "order_id": str(order_id),
                "mode": "live",
                "traded_premium": avg if avg > 0 else None,
                "traded_quantity": filled if 0 < filled <= int(quantity) else int(quantity),
            }
            if bracketed:
                receipt["bracket_order_id"] = str(order_id)
            return receipt
        if status in ("REJECTED", "CANCELLED") and int(outcome.get("filled_qty") or 0) == 0:
            raise OrderRejected(f"Dhan {status.lower()} the buy: {outcome.get('message') or 'no reason given'}")
        raise RuntimeError(
            f"buy outcome unresolved (status={status}, filled={outcome.get('filled_qty') or 0}): "
            f"{outcome.get('message') or 'no detail'}"
        )

    def sell(self, *, when, strike, expiry, option_type, quantity) -> dict:
        """Close one leg at market, and say plainly what became of it."""
        self._availability_guard()
        order = self.broker.place_option_order(
            underlying=self.symbol,
            strike_price=float(strike),
            option_type=str(option_type),
            expiry=self._expiry_str(expiry),
            transaction_type="SELL",
            quantity=int(quantity),
            product_type=self.product_type,
            tag=f"{self.tag}_EXIT",
        )
        order_id = order.get("orderId") if isinstance(order, dict) else getattr(order, "order_id", None)
        outcome = self._verify(order_id)
        status = str(outcome.get("status") or "UNKNOWN").upper()
        if status == "FILLED":
            avg = float(outcome.get("avg_price") or 0.0)
            return {
                "order_id": str(order_id),
                "mode": "live",
                "status": "FILLED",
                "avg_price": avg if avg > 0 else None,
            }
        if status in ("REJECTED", "CANCELLED") and int(outcome.get("filled_qty") or 0) == 0:
            return {"order_id": str(order_id), "mode": "live", "status": "REJECTED", "avg_price": None}
        # Money may be in motion. The caller must freeze rather than guess.
        return {
            "order_id": str(order_id),
            "mode": "live",
            "status": "UNKNOWN",
            "avg_price": None,
            "message": str(outcome.get("message") or ""),
        }

    def amend_bracket_target(self, *, order_id, price) -> dict:
        """Name a bracket's target now that it can honestly be measured."""
        self._availability_guard()
        if float(price) <= 0:
            raise ExecutionRefused("a bracket target needs a positive price")
        result = self.broker.modify_super_order(str(order_id), "TARGET_LEG", target_price=float(price))
        return {"order_id": str(order_id), "amended": True, "raw": result, "mode": "live"}

    def cancel_bracket(self, *, order_id) -> dict:
        """Release a bracket's remaining legs so the leg can be sold.

        A cancel that comes back TRADED means one of the legs already sold the
        position. That is reported, not swallowed: the caller books the leg at
        that price instead of selling it a second time.
        """
        self._availability_guard()
        result = self.broker.cancel_super_order(str(order_id), "ENTRY_LEG")
        raw = str((result or {}).get("orderStatus") or "").upper()
        if raw in ("TRADED", "CLOSED", "FILLED", "COMPLETE"):
            avg = 0.0
            for key in ("averagePrice", "averageTradedPrice", "price"):
                try:
                    avg = float((result or {}).get(key) or 0.0)
                except (TypeError, ValueError):
                    avg = 0.0
                if avg > 0:
                    break
            return {
                "order_id": str(order_id),
                "cancelled": False,
                "traded": True,
                "avg_price": avg if avg > 0 else None,
                "mode": "live",
            }
        return {"order_id": str(order_id), "cancelled": True, "raw": result, "mode": "live"}


def build_executor(
    broker: Any,
    symbol: str,
    *,
    mode: str,
    armed: bool = True,
    product_type: str = "MARGIN",
    tag: str = "PF",
) -> Any:
    """Paper or live, chosen in one place so no route can get it wrong."""
    if str(mode).lower() == "live":
        return OptionsLiveExecutor(broker, symbol, armed=armed, product_type=product_type, tag=tag)
    return OptionsPaperExecutor(tag=tag)


def reconcile_live_orders(broker: Any, *, symbol: str, legs: list[dict]) -> dict:
    """Ask Dhan what happened to these legs while the process was down.

    BLOCKING -- call it through `asyncio.to_thread`.

    `legs` are what the engine BELIEVES it holds, each
    ``{order_id, bracket_order_id, strike, expiry, option_type, quantity}``.
    What comes back says which of them the broker has already sold, and
    whether the account agrees about the rest.

    Conservative on purpose, and asymmetric on purpose:

    * a bracket that TRADED means one of its legs sold the position, so the
      engine must book that leg rather than sell it again;
    * the account holding LESS than the engine expects is the dangerous
      direction -- something closed that the engine still thinks it owns --
      so it is reported as `short_by` and the caller freezes;
    * holding MORE is only noted. The Dhan account is shared across every
      strategy here, and a leg belonging to a different one is not this
      engine's business to close.
    """
    notes: list[str] = []
    settled: dict[str, float] = {}
    for leg in legs:
        bracket = str(leg.get("bracket_order_id") or "")
        if not bracket or bracket.startswith("paper-"):
            continue
        try:
            status = broker.get_order_status(bracket) or {}
        except Exception as exc:  # a status fetch that dies is not an answer
            notes.append(f"bracket {bracket} could not be checked at Dhan: {exc}")
            continue
        raw = str(status.get("orderStatus") or status.get("status") or "UNKNOWN").upper()
        if raw in ("TRADED", "FILLED", "COMPLETE", "CLOSED"):
            price = 0.0
            for key in ("averagePrice", "averageTradedPrice", "price"):
                try:
                    price = float(status.get(key) or 0.0)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    break
            settled[str(leg.get("order_id") or bracket)] = price
            notes.append(f"bracket {bracket} TRADED while the app was down; its leg is booked")
        elif raw == "UNKNOWN":
            notes.append(f"bracket {bracket} status UNKNOWN at Dhan; left standing")

    expected: dict[tuple, int] = {}
    for leg in legs:
        if str(leg.get("order_id") or "") in settled:
            continue
        if not leg.get("order_id") or str(leg["order_id"]).startswith("paper-"):
            continue
        key = (float(leg["strike"]), str(leg["expiry"]), str(leg["option_type"]))
        expected[key] = expected.get(key, 0) + int(leg.get("quantity") or 0)
    short_by: dict[str, int] = {}
    if expected:
        try:
            from broker.dhan import ScripMaster

            held: dict[str, int] = {}
            for pos in broker.get_positions() or []:
                sec = str(pos.get("securityId") or "")
                try:
                    qty = int(float(pos.get("netQty") or 0))
                except (TypeError, ValueError):
                    qty = 0
                if sec:
                    held[sec] = held.get(sec, 0) + qty
            for (strike, expiry, option_type), qty in expected.items():
                security_id = str(ScripMaster.lookup(symbol, strike, expiry, option_type))
                have = held.get(security_id, 0)
                label = f"{int(strike)}{option_type} {expiry}"
                if have < qty:
                    short_by[label] = qty - have
                    notes.append(f"Dhan holds {have} of the {qty} this engine expects on {label}")
                elif have > qty:
                    notes.append(f"Dhan holds {have} on {label}, more than this engine's {qty} -- not touched")
        except Exception as exc:
            # No position check is not the same as a clean one. Say so, and
            # let the caller decide; it must not read as "everything agrees".
            notes.append(f"could not compare the Dhan position book: {exc}")
            short_by["__unchecked__"] = 0
    return {"notes": notes, "settled": settled, "short_by": short_by}


__all__ = [
    "OPTIONS_LIVE_EXECUTION_ENABLED",
    "ExecutionRefused",
    "OptionsLiveExecutor",
    "OptionsPaperExecutor",
    "OrderRejected",
    "build_executor",
    "reconcile_live_orders",
]
