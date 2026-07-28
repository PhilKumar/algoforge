"""Live cash-market execution for the Terminal Cascade, via resting orders.

The Cascade decides *price levels* from closed candles. It must not also sit and
watch for those levels to be crossed: by the time the app notices, on the next
bar close, the price has moved. So every level the engine decides becomes a real
order resting at the exchange, and the exchange does the watching.

    entry   SL stop-limit BUY, day validity, trigger at the Cascade stop
    exit    LIMIT SELL for the whole holding at the Cascade target

A limit sell at the target sells "at target or better": if the market gaps
through it, the order executes in the opening auction at the better gap price,
not at the target and not at market.

Day validity means a still-pending order must be re-placed each session. That is
the price of a true exchange-side stop, and it is worth paying: an SL order has
its margin blocked at placement, so it cannot be rejected for funds at the
moment it triggers. A GTT blocks nothing until it fires and can be rejected
exactly when it matters.

Four things this module refuses to do, each because the alternative loses money:

* **Never resubmit an order it is not certain failed.** Dhan's `place_order`
  raises `AmbiguousOrderSubmission` when a submission times out. The order may
  be live at the exchange, so retrying would double the position. An ambiguous
  submission halts the campaign for a human to reconcile.
* **Never assume a fill.** Quantity and price come from the broker's order book,
  never from the candle the engine was looking at.
* **Never act twice on one decision.** Every order carries a key derived from the
  campaign event behind it. The poll loop re-reads candles after a restart, and
  without this a replayed candle would place the same order again.
* **Never leave a position with no exit resting.** Replacing the target order is
  cancel-then-place; if the place fails after the cancel succeeded, the holding
  is unprotected and the campaign halts rather than continuing blind.

Deliberately NOT supported: **MTF.** Margin funding carries interest and pledge
mechanics the cost model does not represent, so a live MTF campaign would report
a profit it did not make. CNC only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE continuous session. Orders are refused outside it: pre-open matches on a
# different mechanism and is not where a Cascade rung belongs.
SESSION_OPEN = dt_time(9, 15)
SESSION_CLOSE = dt_time(15, 30)

RESTING = "RESTING"
FILLED = "FILLED"
PARTIAL = "PARTIAL"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
SUBMITTED = "SUBMITTED"

OPEN_STATUSES = frozenset({SUBMITTED, RESTING, PARTIAL})
DONE_STATUSES = frozenset({FILLED, CANCELLED, REJECTED})

# How Dhan reports order states, mapped onto the states this module reasons in.
_BROKER_STATUS_MAP = {
    "PENDING": RESTING,
    "OPEN": RESTING,
    "TRIGGERED": RESTING,
    "PART_TRADED": PARTIAL,
    "PARTIALLY_FILLED": PARTIAL,
    "TRADED": FILLED,
    "FILLED": FILLED,
    "COMPLETE": FILLED,
    "CANCELLED": CANCELLED,
    "CANCELED": CANCELLED,
    "EXPIRED": CANCELLED,
    "REJECTED": REJECTED,
}


class LiveExecutionError(RuntimeError):
    """Base class for live execution failures."""


class LiveExecutionHalt(LiveExecutionError):
    """The campaign must stop until a human reconciles the broker order book.

    Raised wherever continuing could duplicate a position, mis-size one, or
    leave one with no exit resting.
    """


class LiveExecutionRejected(LiveExecutionError):
    """A guardrail refused the order before anything reached the broker."""


@dataclass(frozen=True)
class LiveGuardrails:
    """Limits fixed when the campaign is armed, checked on every order."""

    max_order_inr: float
    max_campaign_inr: float
    allowed_product_types: frozenset[str] = frozenset({"CNC"})
    # Cash the Cascade will not spend, so a rung cannot consume the whole
    # balance and leave nothing for charges.
    funds_buffer_inr: float = 0.0
    # A stop-limit whose limit equals its trigger can trigger and never fill in
    # a fast move. The limit is placed this far through the trigger so the order
    # is genuinely marketable once touched. This is the live cost of a stop, and
    # it is the honest equivalent of a slippage assumption.
    stop_limit_offset_pct: float = 0.001
    # How long to wait for the broker to confirm an order is resting. This is
    # not a fill deadline: a resting order is *meant* to sit unfilled.
    accept_wait_sec: int = 10

    def __post_init__(self) -> None:
        if self.max_order_inr <= 0 or self.max_campaign_inr <= 0:
            raise ValueError("max_order_inr and max_campaign_inr must be positive")
        if self.max_order_inr > self.max_campaign_inr:
            raise ValueError("max_order_inr cannot exceed max_campaign_inr")
        if not self.allowed_product_types:
            raise ValueError("at least one product type must be allowed")
        if not 0 <= self.stop_limit_offset_pct < 0.05:
            raise ValueError("stop_limit_offset_pct must be a small non-negative fraction")


@dataclass
class RestingOrder:
    """One order the campaign has working at the broker."""

    key: str
    side: str  # BUY or SELL
    quantity: int
    limit_price: float
    trigger_price: float = 0.0  # 0 for a plain limit order
    order_id: Optional[str] = None
    status: str = SUBMITTED
    filled_qty: int = 0
    avg_price: float = 0.0
    placed_for: Optional[date] = None  # the session this day-order covers
    message: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def spent_inr(self) -> float:
        return round(self.filled_qty * self.avg_price, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "trigger_price": self.trigger_price,
            "order_id": self.order_id,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "avg_price": self.avg_price,
            "placed_for": self.placed_for.isoformat() if self.placed_for else None,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RestingOrder":
        stamp = payload.get("placed_for")
        return cls(
            key=str(payload.get("key") or ""),
            side=str(payload.get("side") or "BUY"),
            quantity=int(payload.get("quantity") or 0),
            limit_price=float(payload.get("limit_price") or 0.0),
            trigger_price=float(payload.get("trigger_price") or 0.0),
            order_id=payload.get("order_id"),
            status=str(payload.get("status") or SUBMITTED),
            filled_qty=int(payload.get("filled_qty") or 0),
            avg_price=float(payload.get("avg_price") or 0.0),
            placed_for=date.fromisoformat(stamp) if stamp else None,
            message=str(payload.get("message") or ""),
        )


@dataclass(frozen=True)
class Instrument:
    symbol: str
    security_id: str
    exchange_segment: str = "NSE_EQ"
    product_type: str = "CNC"


class PaperRestingExecutor:
    """Accepts orders and fills them only when a candle actually reaches them.

    The paper model is written as an object rather than left implied by the
    absence of a broker, so paper and live differ in one named place.
    """

    mode = "paper"

    def __init__(self) -> None:
        self.orders: dict[str, RestingOrder] = {}
        self.halted = False
        self.halt_reason = ""

    def place_stop_buy(self, key, instrument, quantity, trigger_price, *, now=None) -> RestingOrder:
        if key in self.orders:
            return self.orders[key]
        order = RestingOrder(
            key=key,
            side="BUY",
            quantity=quantity,
            limit_price=trigger_price,
            trigger_price=trigger_price,
            order_id=f"paper-{len(self.orders) + 1}",
            status=RESTING,
            placed_for=(now or datetime.now(IST)).date(),
        )
        self.orders[key] = order
        return order

    def place_limit_sell(self, key, instrument, quantity, limit_price, *, now=None) -> RestingOrder:
        if key in self.orders:
            return self.orders[key]
        order = RestingOrder(
            key=key,
            side="SELL",
            quantity=quantity,
            limit_price=limit_price,
            order_id=f"paper-{len(self.orders) + 1}",
            status=RESTING,
            placed_for=(now or datetime.now(IST)).date(),
        )
        self.orders[key] = order
        return order

    def cancel(self, key: str) -> Optional[RestingOrder]:
        order = self.orders.get(key)
        if order is not None and order.is_open:
            order.status = CANCELLED
        return order

    def sync(self, key: str) -> Optional[RestingOrder]:
        return self.orders.get(key)

    def fill(self, key: str, quantity: int, price: float) -> RestingOrder:
        """Test/replay hook: mark how much of a resting order traded."""
        order = self.orders[key]
        order.filled_qty = quantity
        order.avg_price = price
        order.status = FILLED if quantity >= order.quantity else PARTIAL
        return order

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "orders": {key: row.to_dict() for key, row in self.orders.items()}}


class DhanRestingExecutor:
    """Places and reconciles real Dhan cash-market resting orders."""

    mode = "live"

    def __init__(
        self,
        broker: Any,
        guardrails: LiveGuardrails,
        *,
        orders: Optional[dict[str, RestingOrder]] = None,
        deployed_inr: float = 0.0,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
        tag: str = "PhilForgeCascade",
    ) -> None:
        self.broker = broker
        self.guardrails = guardrails
        # Persisted with the campaign, which is what makes the duplicate guard
        # survive a restart rather than merely a request.
        self.orders: dict[str, RestingOrder] = dict(orders or {})
        self.deployed_inr = float(deployed_inr)
        self.clock = clock
        self.tag = tag
        self.halted = False
        self.halt_reason = ""

    # ── guards ────────────────────────────────────────────────────

    def _halt(self, reason: str) -> LiveExecutionHalt:
        self.halted = True
        self.halt_reason = reason
        return LiveExecutionHalt(reason)

    def _require_ready(self, now: datetime) -> None:
        if self.halted:
            raise LiveExecutionHalt(
                f"Campaign is halted and will not place further orders: {self.halt_reason} "
                "Reconcile the Dhan order book, then re-arm explicitly."
            )
        if now.weekday() >= 5 or not (SESSION_OPEN <= now.time() < SESSION_CLOSE):
            raise LiveExecutionRejected(
                f"Outside the NSE continuous session ({now:%Y-%m-%d %H:%M} IST); no order placed."
            )

    def _check_buy(self, instrument: Instrument, quantity: int, price: float) -> None:
        if instrument.product_type.upper() not in self.guardrails.allowed_product_types:
            raise LiveExecutionRejected(
                f"Product type {instrument.product_type} is not armed for live Cascade "
                f"(allowed: {', '.join(sorted(self.guardrails.allowed_product_types))})."
            )
        value = quantity * float(price or 0)
        if value > self.guardrails.max_order_inr:
            raise LiveExecutionRejected(
                f"Order value Rs {value:,.0f} exceeds the armed per-order cap "
                f"of Rs {self.guardrails.max_order_inr:,.0f}."
            )
        if self.deployed_inr + value > self.guardrails.max_campaign_inr:
            raise LiveExecutionRejected(
                f"Order would take campaign exposure to Rs {self.deployed_inr + value:,.0f}, "
                f"past the armed cap of Rs {self.guardrails.max_campaign_inr:,.0f}."
            )
        # An SL order has its margin blocked at placement, so the cash must be
        # there now -- which is exactly why this order type cannot be rejected
        # for funds at the moment it triggers.
        needed = value + self.guardrails.funds_buffer_inr
        try:
            funds = self.broker.get_funds() or {}
        except Exception as exc:
            raise LiveExecutionRejected(f"Could not read Dhan funds before a live buy: {exc}") from exc
        available = 0.0
        for name in ("availabelBalance", "availableBalance", "withdrawableBalance", "sodLimit"):
            if funds.get(name) is not None:
                available = float(funds[name] or 0)
                break
        if available < needed:
            raise LiveExecutionRejected(
                f"Insufficient Dhan funds: need Rs {needed:,.0f} (incl. buffer), available Rs {available:,.0f}."
            )

    # ── placement ─────────────────────────────────────────────────

    def _submit(self, order: RestingOrder, instrument: Instrument, order_type: str) -> RestingOrder:
        # Recorded BEFORE submission. If the process dies between here and the
        # broker's reply, the restart finds a SUBMITTED row it cannot account
        # for and halts, rather than cheerfully placing the order again.
        self.orders[order.key] = order
        try:
            response = self.broker.place_order(
                security_id=str(instrument.security_id),
                exchange_segment=str(instrument.exchange_segment),
                transaction_type=order.side,
                quantity=int(order.quantity),
                order_type=order_type,
                product_type=instrument.product_type.upper(),
                price=order.limit_price,
                trigger_price=order.trigger_price,
                validity="DAY",
                tag=self.tag,
            )
        except Exception as exc:
            raise self._halt(
                f"{order.side} {order.quantity} {instrument.symbol} was not confirmed by Dhan ({exc}). "
                "The order may have reached the exchange -- reconcile the order book before re-arming."
            ) from exc

        order_id = str((response or {}).get("orderId") or (response or {}).get("order_id") or "")
        if not order_id:
            raise self._halt(
                f"Dhan accepted the {order.side} for {instrument.symbol} but returned no order id "
                f"({response!r}). Reconcile the order book before re-arming."
            )
        order.order_id = order_id
        order.status = RESTING
        self.orders[order.key] = order
        return self.sync(order.key) or order

    def place_stop_buy(
        self, key: str, instrument: Instrument, quantity: int, trigger_price: float, *, now=None
    ) -> RestingOrder:
        if key in self.orders:
            return self.orders[key]
        stamp = now or self.clock()
        self._require_ready(stamp)
        # Buy stop: the limit sits above the trigger so a fast move through it
        # still fills instead of triggering into an unfillable limit.
        limit = round(trigger_price * (1.0 + self.guardrails.stop_limit_offset_pct), 2)
        self._check_buy(instrument, quantity, limit)
        return self._submit(
            RestingOrder(
                key=key,
                side="BUY",
                quantity=quantity,
                limit_price=limit,
                trigger_price=round(float(trigger_price), 2),
                placed_for=stamp.date(),
            ),
            instrument,
            "SL",
        )

    def place_limit_sell(
        self, key: str, instrument: Instrument, quantity: int, limit_price: float, *, now=None
    ) -> RestingOrder:
        if key in self.orders:
            return self.orders[key]
        stamp = now or self.clock()
        self._require_ready(stamp)
        return self._submit(
            RestingOrder(
                key=key,
                side="SELL",
                quantity=quantity,
                limit_price=round(float(limit_price), 2),
                placed_for=stamp.date(),
            ),
            instrument,
            "LIMIT",
        )

    # ── reconciliation ────────────────────────────────────────────

    def sync(self, key: str) -> Optional[RestingOrder]:
        """Refresh one order from the broker. This is how fills are learned."""
        order = self.orders.get(key)
        if order is None or not order.order_id or order.status in DONE_STATUSES:
            return order
        raw = self.broker.get_order_status(order.order_id) or {}
        broker_status = str(raw.get("orderStatus") or raw.get("status") or "UNKNOWN").upper()
        if broker_status == "UNKNOWN":
            # Losing sight of a live order is not the same as it being gone.
            return order
        order.filled_qty = int(float(raw.get("filledQty") or raw.get("tradedQuantity") or 0))
        order.avg_price = float(raw.get("averagePrice") or raw.get("price") or 0.0)
        order.status = _BROKER_STATUS_MAP.get(broker_status, order.status)
        order.message = str(raw.get("rejectionReason") or raw.get("omsErrorDescription") or "")
        if order.status in {FILLED, PARTIAL} and order.filled_qty > 0 and order.avg_price > 0:
            self._account(order)
        self.orders[key] = order
        return order

    def _account(self, order: RestingOrder) -> None:
        booked = getattr(order, "_booked_inr", 0.0)
        delta = order.spent_inr - booked
        if abs(delta) < 0.005:
            return
        if order.side == "BUY":
            self.deployed_inr = round(self.deployed_inr + delta, 2)
        else:
            self.deployed_inr = round(max(self.deployed_inr - delta, 0.0), 2)
        object.__setattr__(order, "_booked_inr", order.spent_inr)

    def cancel(self, key: str) -> Optional[RestingOrder]:
        order = self.orders.get(key)
        if order is None:
            return None
        # Read the broker's truth before acting on ours. An order can trade in
        # the instant between deciding to cancel it and the cancel arriving,
        # and cancelling over the top of that would lose the fill.
        order = self.sync(key) or order
        if not order.is_open or order.filled_qty > 0:
            return order
        if not order.order_id:
            raise self._halt(f"Cannot cancel {key}: it was submitted but never got an order id.")
        try:
            self.broker.cancel_order(order.order_id)
        except Exception as exc:
            raise self._halt(
                f"Cancel of {order.side} {order.quantity} (order {order.order_id}) failed ({exc}). "
                "Reconcile the order book before re-arming."
            ) from exc
        # Re-read rather than assume: an order can fill in the instant between
        # the decision to cancel it and the cancel arriving.
        refreshed = self.sync(key)
        if refreshed is not None and refreshed.status not in DONE_STATUSES:
            refreshed.status = CANCELLED
        return refreshed

    def replace_limit_sell(
        self,
        old_key: str,
        new_key: str,
        instrument: Instrument,
        quantity: int,
        limit_price: float,
        *,
        now=None,
    ) -> RestingOrder:
        """Cancel the working target order and rest a new one for the new size.

        Every buy lowers the average entry and therefore the target, so the exit
        order is replaced on each fill. Between the cancel and the new order the
        holding has no exit resting, so a failure there halts rather than
        leaving the position quietly unprotected.
        """
        previous = self.cancel(old_key)
        if previous is not None and previous.filled_qty > 0:
            # It filled while we were replacing it. The caller's quantity is now
            # stale, and inventing a new one here would be guessing.
            raise self._halt(
                f"Target order {previous.order_id} filled {previous.filled_qty} while being replaced. "
                "Reconcile the position before re-arming."
            )
        try:
            return self.place_limit_sell(new_key, instrument, quantity, limit_price, now=now)
        except Exception as exc:
            # Whatever went wrong, the shape of the problem is the same and it
            # is the most urgent one this module can report: the old exit is
            # gone and the new one never arrived.
            raise self._halt(
                f"The target order was cancelled but its replacement could not be placed ({exc}). "
                f"{quantity} {instrument.symbol} is held with no exit resting -- act on this now."
            ) from exc

    def orders_needing_replacement(self, today: date) -> list[RestingOrder]:
        """Day orders that are still open but were placed for an earlier session.

        Day validity is the cost of a true exchange-side stop: an order that has
        not triggered dies at the close and must be re-placed next morning.
        """
        return [order for order in self.orders.values() if order.is_open and order.placed_for != today]

    # ── persistence ───────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "deployed_inr": self.deployed_inr,
            "orders": {key: order.to_dict() for key, order in self.orders.items()},
        }

    def restore(self, payload: dict[str, Any]) -> "DhanRestingExecutor":
        self.orders = {key: RestingOrder.from_dict(row) for key, row in (payload.get("orders") or {}).items()}
        self.deployed_inr = float(payload.get("deployed_inr") or 0.0)
        self.halted = bool(payload.get("halted"))
        self.halt_reason = str(payload.get("halt_reason") or "")
        # A SUBMITTED row means the process died before anything recorded what
        # the broker did with that order.
        unresolved = [key for key, order in self.orders.items() if order.status == SUBMITTED]
        if unresolved:
            self.halted = True
            self.halt_reason = (
                f"{len(unresolved)} order(s) were submitted but never confirmed before a restart "
                f"({', '.join(sorted(unresolved)[:3])}). Reconcile the Dhan order book before re-arming."
            )
        return self
