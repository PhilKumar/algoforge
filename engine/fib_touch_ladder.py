"""engine/fib_touch_ladder.py -- the swing-anchored touch ladder.

Phil's locked spec, 2026-08-06.  This supersedes the typed-mother fib-boundary
engines (:mod:`engine.cascade_fib_boundary` and
:class:`engine.cascade_options.FibBoundaryPaper`) for the console's Fib
Boundary tab.  Those two are left in the tree, untouched and still tested, so
the old behaviour can be restored by pointing the routes back at them.

Five things differ from the engines it replaces, every one of them Phil's call:

1. **The fib is anchored on a SWING, not on the mother candle's own high/low.**
   The mother names *where to start looking*; the anchors come from the first
   involvement on each side.  A one-minute candle's own 8-point range made a
   ladder so tight that L16 sat inside the same minute's noise.

2. **Levels are the halving ladder** ``(2, 3, 4, 6, 8, 12, 16)`` -- one
   half-step inserted inside each doubling gap -- instead of ``(4, 8)``.

3. **A buy happens the moment price TOUCHES a level.**  The two-red-close
   recovery mechanic is gone.  This reverses a measured finding recorded in
   ``engine/cascade_fib_geometry.py`` (buying the line rather than the recovery
   "turned the 15m result from profit into loss when it was measured both
   ways") -- but that was measured on 5m/15m/1H mothers at L4/L8 depth, which
   is a slower animal than a 1m mother with a 0.25 target.

4. **One lot per rung**, so the position grows 1, 2, 3, 4 ... and a **rupee cap
   on the whole ladder** (not per rung) ends it.  At roughly Rs 13,000 a NIFTY
   lot, Rs 75,000 funds about five rungs; the rest of the ladder is priced and
   drawn but marked UNFUNDED rather than silently skipped.

5. **The nearest expiry at least ``min_dte`` days out**, which is the current
   week on a symbol that has weeklies and the near monthly on one that does
   not.  NSE lists weeklies for NIFTY only; BANKNIFTY, FINNIFTY and MIDCPNIFTY
   are monthly-only, and SENSEX carries BSE's own weekly chain.  One rule
   covers all five because it asks the expiry list rather than the symbol.

Geometry is measured on the index and only the index; premium moves P&L and
never decides anything.  That rule is the whole cascade stack's and it holds
here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

__all__ = [
    "HALVING_LEVELS",
    "INVOLVEMENT_CANDLES",
    "SYMBOL_TERMS",
    "SymbolTerms",
    "symbol_terms",
    "FibTouchError",
    "SwingAnchor",
    "TouchRung",
    "TouchFill",
    "FibTouchConfig",
    "FibTouchLadder",
    "GEOMETRY_TIMEFRAMES",
    "TIMEFRAME_MINUTES",
    "ExecutionRefused",
    "PaperExecutor",
    "LiveExecutor",
    "find_swing_anchor",
    "find_trendline",
    "Trendline",
    "level_price",
    "select_expiry",
    "atm_strike",
]


# Phil's ladder: the doubling levels with one half-step folded into each gap.
# L2 -> L3 -> L4 -> L6 -> L8 -> L12 -> L16.
HALVING_LEVELS: tuple[int, ...] = (2, 3, 4, 6, 8, 12, 16)

# "2 layers specifically 2 or more green candles" -- Phil, off a 25 Mar chart
# where a 3-bar pivot took the low 273 points lower and two sessions late.  The
# same constant already governs engine/candle_recovery.py; it is repeated rather
# than imported so this module stays free of that engine's option plumbing.
INVOLVEMENT_CANDLES = 2

# The charts a mother candle may be read on. Entries are always watched on 1m.
GEOMETRY_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h")

# Minutes per bar, used to validate that a typed mother is a real candle open.
TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}


class FibTouchError(ValueError):
    """The ladder cannot be built from what was supplied."""


@dataclass(frozen=True)
class SymbolTerms:
    """What an index trades as, and what can honestly be said about it.

    Every number was read off Dhan's own scrip master (cached 2026-08-05), not
    assumed from the symbol's name.  They are FALLBACKS: the routes ask the live
    scrip master first, because a lot size is wrong the moment an exchange
    changes it and wrong silently.
    """

    symbol: str
    label: str
    lot_size: int
    strike_step: float
    # NSE withdrew the weeklies on BANKNIFTY, FINNIFTY and MIDCPNIFTY, so their
    # chains list only monthlies and "current week" cannot mean anything there.
    # `select_expiry` needs no special case -- it reads the chain -- but the
    # console has to say which rule the user is actually getting.
    has_weeklies: bool
    # Can a BACKTEST price this symbol's legs?  NIFTY and BankNifty have Upstox
    # premium keys; SENSEX has its own path in tools/fib_space_premium.py.
    # FINNIFTY and MIDCPNIFTY have neither, so a replay of them is geometry
    # with no rupees -- which the console must say rather than print a zero.
    backtestable: bool
    note: str = ""


SYMBOL_TERMS: dict[str, SymbolTerms] = {
    "NIFTY": SymbolTerms("NIFTY", "NIFTY 50", 65, 50.0, True, True),
    "BANKNIFTY": SymbolTerms(
        "BANKNIFTY", "NIFTY Bank", 30, 100.0, False, True, "Monthly expiries only -- NSE withdrew the weeklies."
    ),
    "FINNIFTY": SymbolTerms(
        "FINNIFTY", "NIFTY Financial", 60, 50.0, False, False, "Monthly only, and no premium history: paper only."
    ),
    "MIDCPNIFTY": SymbolTerms(
        "MIDCPNIFTY", "NIFTY Midcap", 120, 25.0, False, False, "Monthly only, and no premium history: paper only."
    ),
    "SENSEX": SymbolTerms(
        "SENSEX", "BSE SENSEX", 20, 100.0, True, True, "Weekly, but prints in only 34% of minutes -- fills land late."
    ),
}


def symbol_terms(symbol: str) -> SymbolTerms:
    key = str(symbol or "").strip().upper()
    terms = SYMBOL_TERMS.get(key)
    if terms is None:
        raise FibTouchError(f"Unknown symbol {key!r}. Known: {', '.join(sorted(SYMBOL_TERMS))}.")
    return terms


class Bar(Protocol):
    """The handful of fields this engine reads off a candle."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


def _is_green(bar: Bar) -> bool:
    # Strict: a doji and the exchange's flat 15:30 settlement stub close level,
    # and neither is involvement by anybody.
    return float(bar.close) > float(bar.open)


def _is_red(bar: Bar) -> bool:
    return float(bar.close) < float(bar.open)


# ── geometry ──────────────────────────────────────────────────────


def level_price(side: str, anchor_high: float, anchor_low: float, level: float) -> float:
    """Index price of a fib level, on the side the option trades.

    CE ladders DOWN below the swing low (buy the fall, target the bounce);
    PE ladders UP above the swing high (buy the rise, target the drop).  Level 1
    is always the far anchor, so level 2 is one full swing beyond it.
    """
    if anchor_high <= anchor_low:
        raise FibTouchError("anchor high must exceed anchor low")
    span = float(anchor_high) - float(anchor_low)
    if str(side).upper() == "CE":
        return float(anchor_high) - float(level) * span
    return float(anchor_low) + float(level) * span


@dataclass(frozen=True)
class SwingAnchor:
    """The two prices the whole ladder is measured from."""

    high: float
    low: float
    high_timestamp: datetime
    low_timestamp: datetime
    # The bar at which BOTH anchors were knowable.  Nothing may trade before
    # it: the involvement that freezes the near anchor is only visible once its
    # run of candles has closed, so replaying from the mother would be reading
    # the future.
    confirmed_at: datetime
    involvement_candles: int

    @property
    def span(self) -> float:
        return self.high - self.low


def find_swing_anchor(
    candles: Sequence[Bar],
    mother_timestamp: datetime,
    side: str,
    *,
    lookback_bars: int = 240,
    involvement: int = INVOLVEMENT_CANDLES,
) -> Optional[SwingAnchor]:
    """Anchor the fib on the counter-swing that FOLLOWS the mother.

    Both anchors sit after the mother, and they are found in order. Phil,
    2026-08-06, correcting a chart that had used the mother's own high: "the fib
    has to be drawn from the swing low to the high but it takes the mother
    candle high -- wrong."

    For CE the mother marks a top, so:
      * the LOW freezes at the first BUYER involvement -- ``involvement``
        consecutive green closes -- and is the lowest low from the mother up to
        and including that run;
      * the HIGH is then the highest high printed from that low onward, frozen
        when the buying runs out at the first SELLER involvement.

    PE is the exact mirror, greens and reds swapped, highs and lows swapped.

    The mother's own bar can no longer supply either anchor, which is the whole
    point of the correction. Returns ``None`` until BOTH have frozen: until then
    the swing has no width and the ladder has no geometry.
    """
    working = str(side).upper()
    if working not in {"CE", "PE"}:
        raise FibTouchError("side must be CE or PE")
    if lookback_bars <= 0:
        raise FibTouchError("lookback_bars must be positive")
    if involvement <= 0:
        raise FibTouchError("involvement must be positive")

    ordered = sorted(candles, key=lambda row: row.timestamp)
    mother_index = next((i for i, row in enumerate(ordered) if row.timestamp == mother_timestamp), None)
    if mother_index is None:
        return None
    window = ordered[mother_index : mother_index + lookback_bars + 1]

    # ── stage 1: the near anchor, frozen by the first involvement ──
    near_price: Optional[float] = None
    near_timestamp: Optional[datetime] = None
    near_frozen_at: Optional[int] = None
    run = 0
    for offset, row in enumerate(window):
        if working == "CE":
            if near_price is None or float(row.low) < near_price:
                near_price, near_timestamp = float(row.low), row.timestamp
            run = run + 1 if _is_green(row) else 0
        else:
            if near_price is None or float(row.high) > near_price:
                near_price, near_timestamp = float(row.high), row.timestamp
            run = run + 1 if _is_red(row) else 0
        if run >= involvement:
            near_frozen_at = offset
            break
    if near_frozen_at is None or near_price is None or near_timestamp is None:
        return None

    # ── stage 2: the far anchor, the top of the move that involvement began ──
    far_price: Optional[float] = None
    far_timestamp: Optional[datetime] = None
    run = 0
    for row in window[near_frozen_at:]:
        if working == "CE":
            if far_price is None or float(row.high) > far_price:
                far_price, far_timestamp = float(row.high), row.timestamp
            run = run + 1 if _is_red(row) else 0
        else:
            if far_price is None or float(row.low) < far_price:
                far_price, far_timestamp = float(row.low), row.timestamp
            run = run + 1 if _is_green(row) else 0
        if run >= involvement:
            if far_price is None or far_timestamp is None:
                return None
            high, high_ts = (far_price, far_timestamp) if working == "CE" else (near_price, near_timestamp)
            low, low_ts = (near_price, near_timestamp) if working == "CE" else (far_price, far_timestamp)
            if high <= low:
                # A swing with no width cannot carry a ladder; say so rather
                # than divide by zero three calls downstream.
                return None
            return SwingAnchor(
                high=round(float(high), 2),
                low=round(float(low), 2),
                high_timestamp=high_ts,
                low_timestamp=low_ts,
                confirmed_at=row.timestamp,
                involvement_candles=involvement,
            )
    return None


@dataclass(frozen=True)
class Trendline:
    """A drawn reference line. It decides nothing.

    Phil asked for CryptoForge's trendline on this chart but kept the fib on the
    swing, so this is the geometry without the authority: it is rendered, and no
    rung consults it.
    """

    start_timestamp: datetime
    start_price: float
    anchor_timestamp: datetime
    anchor_price: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_timestamp": self.start_timestamp.isoformat(),
            "start_price": round(self.start_price, 2),
            "anchor_timestamp": self.anchor_timestamp.isoformat(),
            "anchor_price": round(self.anchor_price, 2),
        }


def find_trendline(
    candles: Sequence[Bar],
    mother_timestamp: datetime,
    side: str,
    anchor: SwingAnchor,
    **_ignored: Any,
) -> Optional[Trendline]:
    """Phil's rule, in his words (2026-08-06):

        "TL has to start from Mother Candle high and touch the first top red
         candle before the swing low .. Like the swing high top red candle open"

    So: start at the mother's HIGH, and anchor on the OPEN of the topmost red
    candle sitting BETWEEN the mother and the swing low -- the last resistance
    on the way down. An earlier version searched AFTER the swing low and ported
    CryptoForge's clean-line veto; both were wrong for this chart, and the veto
    is not part of the rule he asked for.

    PE mirrors: mother LOW to the open of the lowest green candle before the
    swing high.
    """
    working = str(side).upper()
    if working not in {"CE", "PE"}:
        raise FibTouchError("side must be CE or PE")
    ordered = sorted(candles, key=lambda row: row.timestamp)
    mother = next((row for row in ordered if row.timestamp == mother_timestamp), None)
    if mother is None:
        return None
    start_price = float(mother.high) if working == "CE" else float(mother.low)
    # The window closes at the swing anchor the ladder is measured from: for a
    # CE that is the swing LOW, because the fall into it is what the line rides.
    edge = anchor.low_timestamp if working == "CE" else anchor.high_timestamp
    window = [row for row in ordered if mother.timestamp < row.timestamp <= edge]
    candidates = [row for row in window if (_is_red(row) if working == "CE" else _is_green(row))]
    if not candidates:
        return None
    # "the TOP red candle" -- highest open on a CE, lowest on a PE.
    anchor_bar = (
        max(candidates, key=lambda row: float(row.open))
        if working == "CE"
        else min(candidates, key=lambda row: float(row.open))
    )
    return Trendline(
        start_timestamp=mother.timestamp,
        start_price=round(start_price, 2),
        anchor_timestamp=anchor_bar.timestamp,
        anchor_price=round(float(anchor_bar.open), 2),
    )


# ── contract selection ────────────────────────────────────────────


def select_expiry(expiries: Iterable[date], on: date, *, min_dte: int = 4) -> date:
    """The nearest listed expiry at least ``min_dte`` days out.

    One rule serves Phil's "current week, or next week if under 4 days" and the
    monthly-only symbols at once, because it reads the expiry list instead of
    assuming a rhythm from the symbol's name.  NIFTY and SENSEX have weeklies,
    so it lands on this week or next; BANKNIFTY, FINNIFTY and MIDCPNIFTY list
    only monthlies, so it lands on the near monthly.
    """
    eligible = sorted(day for day in expiries if (day - on).days >= int(min_dte))
    if not eligible:
        raise FibTouchError(f"no expiry at least {min_dte} days after {on.isoformat()}")
    return eligible[0]


def atm_strike(spot: float, strike_step: float) -> float:
    """The listed strike nearest the index."""
    if strike_step <= 0:
        raise FibTouchError("strike_step must be positive")
    import math

    return math.floor(float(spot) / float(strike_step) + 0.5) * float(strike_step)


# ── execution ─────────────────────────────────────────────────────
#
# Paper and live differ in exactly one object.  The engine decides WHAT to buy
# and WHEN entirely on index geometry, then hands the decision to an executor;
# neither mode can drift from the other's rules, because there is only one set
# of rules.  That is what "paper must behave the same as live" has to mean in
# code -- not two engines kept in sync by hand.


class ExecutionRefused(RuntimeError):
    """An order was decided but the executor would not send it."""


class PaperExecutor:
    """Records the fill and sends nothing anywhere."""

    mode = "paper"
    is_live = False

    def buy(self, *, when, strike, expiry, option_type, quantity, lots, premium) -> dict:
        return {"order_id": f"paper-{when.strftime('%H%M%S')}-{int(strike)}{option_type}", "mode": "paper"}

    def sell_all(self, *, when, legs) -> dict:
        return {"order_id": f"paper-exit-{when.strftime('%H%M%S')}", "mode": "paper"}


class LiveExecutor:
    """The real-money path. Refuses to send until it is explicitly armed.

    The whole live route exists here so paper and live share one decision path
    and one set of rules -- but the last inch, the call that actually reaches
    the exchange, is closed.  Phil asked for the toggle and the code with live
    kept disabled until he has watched paper run and says otherwise, so arming
    is a deliberate act (`armed=True`) and never a default, a config value or
    an environment variable that could drift open.
    """

    mode = "live"
    is_live = True

    def __init__(self, broker: Any, symbol: str, *, armed: bool = False) -> None:
        self.broker = broker
        self.symbol = symbol
        self.armed = bool(armed)

    def _guard(self) -> None:
        if not self.armed:
            raise ExecutionRefused(
                "Live execution is built but not armed. Watch a paper ladder run first, "
                "then arm live deliberately -- no config value or environment variable opens it."
            )

    def buy(self, *, when, strike, expiry, option_type, quantity, lots, premium) -> dict:
        self._guard()
        order = self.broker.place_option_order(
            self.symbol,
            float(strike),
            expiry.isoformat(),
            str(option_type),
            side="BUY",
            quantity=int(quantity),
        )
        return {"order_id": getattr(order, "order_id", None) or str(order), "mode": "live"}

    def sell_all(self, *, when, legs) -> dict:
        self._guard()
        ids = []
        for leg in legs:
            order = self.broker.place_option_order(
                self.symbol,
                float(leg["strike"]),
                str(leg["expiry"]),
                str(leg["option_type"]),
                side="SELL",
                quantity=int(leg["quantity"]),
            )
            ids.append(getattr(order, "order_id", None) or str(order))
        return {"order_id": ",".join(str(i) for i in ids), "mode": "live"}


# ── configuration ─────────────────────────────────────────────────


@dataclass(frozen=True)
class FibTouchConfig:
    """Everything the ladder needs, all of it Phil's locked spec."""

    symbol: str
    side: str  # CE or PE
    mother_timestamp: datetime
    lot_size: int
    strike_step: float
    # The chart the MOTHER is read on, and so the chart the swing and every
    # level are measured from. Phil picks a 5m / 15m / 1H mother when he wants a
    # wider swing under the ladder.
    timeframe: str = "1m"
    # Touches are always watched on the finest bar available. A touch of 24,500
    # is the same touch on any chart, so reading it on a slow one only delays
    # the fill -- the timeframe belongs to the geometry, not the trigger. Same
    # split the fib-space engine uses (15m geometry, faster entries).
    entry_timeframe: str = "1m"
    levels: tuple[int, ...] = HALVING_LEVELS
    lots_per_rung: int = 1
    # A cap on the WHOLE ladder, not per rung.  Phil: "per ladder it will 75k
    # only not more than that ... one lot of nifty with premium 180 or 200 will
    # have the fund upto 13000".
    capital_cap_inr: float = 75_000.0
    target_fraction: float = 0.25
    itm_steps: int = 2
    min_dte: int = 4
    lookback_bars: int = 240
    involvement_candles: int = INVOLVEMENT_CANDLES

    def __post_init__(self) -> None:
        if str(self.side).upper() not in {"CE", "PE"}:
            raise FibTouchError("side must be CE or PE")
        if not self.levels or any(float(level) <= 0 for level in self.levels):
            raise FibTouchError("levels must be positive fib multipliers")
        if sorted(self.levels) != list(self.levels):
            raise FibTouchError("levels must be listed shallow-first")
        if self.lot_size <= 0 or self.strike_step <= 0:
            raise FibTouchError("lot_size and strike_step must be positive")
        if self.lots_per_rung <= 0:
            raise FibTouchError("lots_per_rung must be positive")
        if self.capital_cap_inr <= 0:
            raise FibTouchError("capital_cap_inr must be positive")
        if not 0 < float(self.target_fraction) <= 1:
            raise FibTouchError("target_fraction must be between 0 and 1")
        if self.itm_steps < 0:
            raise FibTouchError("itm_steps cannot be negative")
        if self.min_dte < 0:
            raise FibTouchError("min_dte cannot be negative")
        if str(self.timeframe).lower() not in GEOMETRY_TIMEFRAMES:
            raise FibTouchError(f"timeframe must be one of {', '.join(GEOMETRY_TIMEFRAMES)}")

    @property
    def working_side(self) -> str:
        return str(self.side).upper()


# ── live state ────────────────────────────────────────────────────


@dataclass
class TouchRung:
    """One fib level and what happened to it."""

    level: int
    index_price: float
    # PENDING -> FILLED, or UNFUNDED when the ladder's rupee cap stopped here.
    status: str = "PENDING"
    filled_at: Optional[datetime] = None

    @property
    def key(self) -> str:
        return f"L{self.level}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "key": self.key,
            "index_price": round(self.index_price, 2),
            "status": self.status,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
        }


@dataclass
class TouchFill:
    """One buy, carrying everything the console has to show about it."""

    buy_number: int
    level: int
    timestamp: datetime
    index_price: float
    premium: float
    lots: int
    quantity: int
    strike: float
    expiry: date
    option_type: str
    order_id: str = ""

    @property
    def funded_inr(self) -> float:
        return round(self.premium * self.quantity, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "buy_number": self.buy_number,
            "level": self.level,
            "rung_key": f"L{self.level}",
            "timestamp": self.timestamp.isoformat(),
            "index_price": round(self.index_price, 2),
            "premium": round(self.premium, 2),
            "lots": self.lots,
            "quantity": self.quantity,
            "strike": self.strike,
            "expiry": self.expiry.isoformat(),
            "option_type": self.option_type,
            "funded_inr": self.funded_inr,
            "order_id": self.order_id,
        }


# A premium source: (when, strike, expiry, option_type) -> price, or None when
# the option did not print.  A missing price is always a recorded gap here and
# never a fabricated number.
PremiumLookup = Callable[[datetime, float, date, str], Optional[float]]
# The expiry chain as it stood on a date.  Passed in so a replay sees the
# expiries that were really listed then, not today's.
ExpirySource = Callable[[date], Sequence[date]]


class FibTouchLadder:
    """Runs one swing-anchored touch ladder, candle by candle.

    Feed it closed index candles with :meth:`on_candle` and it will, in order:
    find the swing, price the ladder, buy each level the moment price touches
    it while the rupee cap allows, and close the whole basket at one target.
    The campaign ENDS at the target -- there is no re-arm and no successor.
    """

    def __init__(
        self,
        config: FibTouchConfig,
        *,
        premium_lookup: PremiumLookup,
        expiry_source: ExpirySource,
        executor: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.premium_lookup = premium_lookup
        self.expiry_source = expiry_source
        # Paper unless told otherwise: the safe mode is the one you get by
        # forgetting to choose.
        self.executor = executor if executor is not None else PaperExecutor()
        self.side = config.working_side

        # Two streams. `geometry_history` carries the mother's own timeframe and
        # is the ONLY thing the swing is measured from; `history` carries the 1m
        # bars that touches are read on. When the mother is a 1m candle the two
        # are the same series, and the engine does not care.
        self.geometry_history: list[Bar] = []
        self.history: list[Bar] = []
        self.anchor: Optional[SwingAnchor] = None
        self.rungs: list[TouchRung] = []
        self.fills: list[TouchFill] = []
        self.events: list[dict[str, Any]] = []
        self.data_gaps: list[str] = []
        self.status = "WAITING_FOR_SWING"

        # A candle that both fills and reaches the target must not do both: the
        # high that pays and the low that buys are unordered inside one bar, so
        # settling on the entry bar would be reading the future half the time.
        self._last_fill_timestamp: Optional[datetime] = None
        self.exit_timestamp: Optional[datetime] = None
        self.exit_reason: Optional[str] = None
        self.exit_index: Optional[float] = None
        self.gross_pnl: Optional[float] = None
        self.costs_total: Optional[float] = None
        self.net_pnl: Optional[float] = None
        self._exit_premiums: list[Optional[float]] = []

    # ── derived views ─────────────────────────────────────────────

    @property
    def deployed_inr(self) -> float:
        return round(sum(fill.funded_inr for fill in self.fills), 2)

    @property
    def remaining_inr(self) -> float:
        return round(max(0.0, self.config.capital_cap_inr - self.deployed_inr), 2)

    @property
    def open_lots(self) -> int:
        return sum(fill.lots for fill in self.fills)

    @property
    def open_quantity(self) -> int:
        return sum(fill.quantity for fill in self.fills)

    @property
    def average_index_entry(self) -> Optional[float]:
        quantity = self.open_quantity
        if quantity <= 0:
            return None
        return sum(fill.index_price * fill.quantity for fill in self.fills) / quantity

    @property
    def average_premium(self) -> Optional[float]:
        quantity = self.open_quantity
        if quantity <= 0:
            return None
        return sum(fill.premium * fill.quantity for fill in self.fills) / quantity

    @property
    def target_index(self) -> Optional[float]:
        """0.25 of the way back from the average entry toward the far anchor.

        Recomputed on every fill, so a deeper buy pulls the target down (CE) or
        up (PE) with the average -- which is the whole point of laddering.
        """
        average = self.average_index_entry
        if average is None or self.anchor is None:
            return None
        if self.side == "CE":
            return average + self.config.target_fraction * (self.anchor.high - average)
        return average - self.config.target_fraction * (average - self.anchor.low)

    def _beyond(self, price: float, level_price_: float) -> bool:
        """Has price reached a level, in the direction the ladder runs?"""
        return price <= level_price_ if self.side == "CE" else price >= level_price_

    def _touched(self, bar: Bar, level_price_: float) -> bool:
        """A TOUCH, not a close -- the wick is enough."""
        return float(bar.low) <= level_price_ if self.side == "CE" else float(bar.high) >= level_price_

    def _log(self, when: datetime, event: str, **payload: Any) -> None:
        self.events.append({"timestamp": when.isoformat(), "event": event, **payload})

    # ── the ladder ────────────────────────────────────────────────

    def _build_rungs(self) -> None:
        anchor = self.anchor
        if anchor is None:
            return
        self.rungs = [
            TouchRung(level=int(level), index_price=level_price(self.side, anchor.high, anchor.low, level))
            for level in self.config.levels
        ]

    def _resolve_contract(self, when: datetime, spot: float) -> tuple[float, date]:
        """Strike and expiry for a buy happening now, at this index level.

        Re-resolved per rung on purpose: as the index walks down, ATM-2 walks
        with it, so a deeper CE buy takes a lower strike.  The basket therefore
        holds several strikes, which is what Phil asked for when he checked
        whether the strike updates on each buy.
        """
        expiries = list(self.expiry_source(when.date()))
        expiry = select_expiry(expiries, when.date(), min_dte=self.config.min_dte)
        atm = atm_strike(spot, self.config.strike_step)
        offset = self.config.itm_steps * self.config.strike_step
        strike = atm - offset if self.side == "CE" else atm + offset
        return float(strike), expiry

    def _try_fill(self, bar: Bar) -> None:
        """Buy every level this candle touched, shallowest first."""
        for rung in self.rungs:
            if rung.status != "PENDING":
                continue
            if not self._touched(bar, rung.index_price):
                # Levels are ordered shallow-first, so the first untouched one
                # ends the walk: nothing deeper can have been reached.
                break
            # Fill AT the level, not at the close -- a touch is a limit order
            # resting on the line, and the line is the price it gets.
            fill_index = rung.index_price
            try:
                strike, expiry = self._resolve_contract(bar.timestamp, fill_index)
            except FibTouchError as exc:
                self.data_gaps.append(f"{bar.timestamp.isoformat()}: {exc}")
                self._log(bar.timestamp, "contract_unavailable", level=rung.level, detail=str(exc))
                break
            premium = self.premium_lookup(bar.timestamp, strike, expiry, self.side)
            if premium is None or premium <= 0:
                gap = (
                    f"no {self.config.symbol} {strike:g}{self.side} {expiry.isoformat()} print at "
                    f"{bar.timestamp.isoformat()} for L{rung.level}"
                )
                self.data_gaps.append(gap)
                self._log(bar.timestamp, "premium_missing", level=rung.level, strike=strike)
                break

            lots = self.config.lots_per_rung
            quantity = lots * self.config.lot_size
            cost = float(premium) * quantity
            if self.deployed_inr + cost > self.config.capital_cap_inr:
                # The cap ends the ladder.  Every remaining rung is marked so
                # the console can draw it as priced-but-unfunded rather than
                # leave the user wondering why a touched level did nothing.
                for remaining in self.rungs:
                    if remaining.status == "PENDING":
                        remaining.status = "UNFUNDED"
                self.status = "OPEN_CAPPED"
                self._log(
                    bar.timestamp,
                    "capital_cap_reached",
                    level=rung.level,
                    would_cost=round(cost, 2),
                    deployed=self.deployed_inr,
                    cap=self.config.capital_cap_inr,
                )
                return

            try:
                receipt = self.executor.buy(
                    when=bar.timestamp,
                    strike=strike,
                    expiry=expiry,
                    option_type=self.side,
                    quantity=quantity,
                    lots=lots,
                    premium=float(premium),
                )
            except ExecutionRefused as exc:
                # The decision was right; the executor would not send it. Say so
                # and leave the rung PENDING rather than record a phantom fill.
                self.data_gaps.append(f"L{rung.level} not sent: {exc}")
                self._log(bar.timestamp, "execution_refused", level=rung.level, detail=str(exc))
                self.status = "EXECUTION_REFUSED"
                return
            fill = TouchFill(
                buy_number=len(self.fills) + 1,
                level=rung.level,
                timestamp=bar.timestamp,
                index_price=fill_index,
                premium=float(premium),
                lots=lots,
                quantity=quantity,
                strike=strike,
                expiry=expiry,
                option_type=self.side,
                order_id=str(receipt.get("order_id") or ""),
            )
            self.fills.append(fill)
            self._last_fill_timestamp = bar.timestamp
            rung.status = "FILLED"
            rung.filled_at = bar.timestamp
            if self.status != "OPEN_CAPPED":
                self.status = "OPEN"
            self._log(
                bar.timestamp,
                "fill",
                level=rung.level,
                buy_number=fill.buy_number,
                index=round(fill_index, 2),
                premium=round(float(premium), 2),
                lots=lots,
                quantity=quantity,
                strike=strike,
                expiry=expiry.isoformat(),
                deployed=self.deployed_inr,
            )

    def _try_exit(self, bar: Bar) -> bool:
        """Close the whole basket when the index reaches the target."""
        if not self.fills:
            return False
        if self._last_fill_timestamp is not None and bar.timestamp <= self._last_fill_timestamp:
            return False
        target = self.target_index
        if target is None:
            return False
        # The target is a resting sell limit, so a wick through it is a fill.
        hit = float(bar.high) >= target if self.side == "CE" else float(bar.low) <= target
        if not hit:
            return False
        prices: list[Optional[float]] = []
        for fill in self.fills:
            price = self.premium_lookup(bar.timestamp, fill.strike, fill.expiry, self.side)
            if price is None:
                # A deep ITM leg goes quiet exactly when it is worth most.  Its
                # intrinsic value against the index is a floor nobody disputes
                # and it UNDERSTATES the exit rather than inventing a price.
                intrinsic = (
                    max(float(bar.close) - fill.strike, 0.0)
                    if self.side == "CE"
                    else max(fill.strike - float(bar.close), 0.0)
                )
                if intrinsic > 0:
                    price = intrinsic
                    self.data_gaps.append(
                        f"L{fill.level} exit priced at intrinsic Rs {intrinsic:,.2f} "
                        f"(no print at {bar.timestamp.isoformat()}); understates profit"
                    )
            prices.append(price)
        try:
            self.executor.sell_all(
                when=bar.timestamp,
                legs=[
                    {
                        "strike": fill.strike,
                        "expiry": fill.expiry.isoformat(),
                        "option_type": fill.option_type,
                        "quantity": fill.quantity,
                    }
                    for fill in self.fills
                ],
            )
        except ExecutionRefused as exc:
            # Refusing an EXIT leaves real money exposed, so it is loud: the
            # target stays live and the campaign does not pretend to be closed.
            self.data_gaps.append(f"exit not sent: {exc}")
            self._log(bar.timestamp, "exit_refused", detail=str(exc))
            self.status = "EXIT_REFUSED"
            return False
        self._exit_premiums = prices
        self.exit_timestamp = bar.timestamp
        self.exit_index = target
        self.exit_reason = "target"
        self.status = "CLOSED"
        self._settle(prices)
        self._log(bar.timestamp, "target", target=round(target, 2), net=self.net_pnl)
        return True

    def _settle(self, exit_prices: Sequence[Optional[float]]) -> None:
        if not self.fills or any(price is None for price in exit_prices):
            return
        self.gross_pnl = round(
            sum(
                (float(exit_price) - fill.premium) * fill.quantity for exit_price, fill in zip(exit_prices, self.fills)
            ),
            2,
        )
        self.costs_total = round(self._costs(exit_prices), 2)
        self.net_pnl = round(self.gross_pnl - self.costs_total, 2)

    def _costs(self, exit_prices: Sequence[Optional[float]]) -> float:
        """Statutory round costs, charged per contract the basket holds."""
        from cascade_costs import (
            OptionCostFill,
            calculate_nifty_option_basket_round_costs,
        )

        grouped: dict[tuple[float, date], list[tuple[TouchFill, float]]] = {}
        for fill, exit_price in zip(self.fills, exit_prices):
            grouped.setdefault((fill.strike, fill.expiry), []).append((fill, float(exit_price)))
        total = 0.0
        for rows in grouped.values():
            quantity = sum(fill.quantity for fill, _ in rows)
            lots = sum(fill.lots for fill, _ in rows)
            sell_price = sum(price * fill.quantity for fill, price in rows) / quantity
            total += calculate_nifty_option_basket_round_costs(
                buys=[OptionCostFill(price=fill.premium, quantity=fill.quantity, lots=fill.lots) for fill, _ in rows],
                sell_price=sell_price,
                sell_quantity=quantity,
                sell_lots=lots,
            ).total
        return total

    def _try_expiry_exit(self, bar: Bar) -> bool:
        """Settle at intrinsic on the expiry the basket's nearest leg carries.

        With no stop loss, expiry is the only thing besides the target that can
        end this campaign -- so it has to be exact, not approximate.
        """
        if not self.fills:
            return False
        from datetime import time as dt_time

        expiry = min(fill.expiry for fill in self.fills)
        if bar.timestamp.date() < expiry:
            return False
        if bar.timestamp.date() == expiry and bar.timestamp.time() < dt_time(15, 15):
            return False
        prices = [
            (
                max(float(bar.close) - fill.strike, 0.0)
                if self.side == "CE"
                else max(fill.strike - float(bar.close), 0.0)
            )
            for fill in self.fills
        ]
        self._exit_premiums = list(prices)
        self.exit_timestamp = bar.timestamp
        self.exit_index = float(bar.close)
        self.exit_reason = "expiry_square_off"
        self.status = "EXPIRED"
        self._settle(prices)
        self._log(bar.timestamp, "expiry_exit", expiry=expiry.isoformat(), net=self.net_pnl)
        return True

    # ── entry point ───────────────────────────────────────────────

    def on_geometry_candle(self, bar: Bar) -> None:
        """Feed one CLOSED candle of the MOTHER's timeframe.

        Only the swing is read from this stream. Nothing trades here: a 1H bar
        closing tells you where the ladder sits, not that a level was touched.
        """
        if self.anchor is not None or self.status in {"CLOSED", "EXPIRED", "KILLED"}:
            return
        self.geometry_history.append(bar)
        anchor = find_swing_anchor(
            self.geometry_history,
            self.config.mother_timestamp,
            self.side,
            lookback_bars=self.config.lookback_bars,
            involvement=self.config.involvement_candles,
        )
        if anchor is None:
            return
        self.anchor = anchor
        self._build_rungs()
        self.status = "ARMED"
        self._log(
            anchor.confirmed_at,
            "swing_anchored",
            timeframe=self.config.timeframe,
            high=anchor.high,
            low=anchor.low,
            span=round(anchor.span, 2),
            levels=[rung.as_dict() for rung in self.rungs],
        )

    def on_candle(self, bar: Bar) -> None:
        """Advance the campaign by one CLOSED 1-minute index candle."""
        if self.status in {"CLOSED", "EXPIRED", "KILLED"}:
            return
        self.history.append(bar)
        # A 1m mother needs no separate stream -- the entry bars ARE the
        # geometry bars, so feed them through as well.
        if self.config.timeframe == self.config.entry_timeframe:
            self.on_geometry_candle(bar)
        if self.anchor is None:
            return
        # The bar that confirmed the swing is the earliest one that may trade;
        # anything before it was not knowable when it printed.
        if bar.timestamp < self.anchor.confirmed_at:
            return

        self._try_fill(bar)
        if self._try_exit(bar):
            return
        self._try_expiry_exit(bar)

    def kill_and_close(self, bar: Bar) -> bool:
        """Close the campaign by hand, at whatever the basket is worth now.

        Returns False when a leg cannot be priced at all -- the caller must not
        report a closed round it could not value, so the ladder keeps running
        and the user can try again on the next quote.
        """
        if self.status in {"CLOSED", "EXPIRED", "KILLED"}:
            return True
        if not self.fills:
            self.status = "KILLED"
            self.exit_timestamp = bar.timestamp
            self.exit_reason = "killed"
            self._log(bar.timestamp, "killed", open_lots=0)
            return True
        prices: list[Optional[float]] = []
        for fill in self.fills:
            price = self.premium_lookup(bar.timestamp, fill.strike, fill.expiry, self.side)
            if price is None:
                intrinsic = (
                    max(float(bar.close) - fill.strike, 0.0)
                    if self.side == "CE"
                    else max(fill.strike - float(bar.close), 0.0)
                )
                price = intrinsic if intrinsic > 0 else None
            if price is None:
                return False
            prices.append(price)
        self._exit_premiums = prices
        self.exit_timestamp = bar.timestamp
        self.exit_index = float(bar.close)
        self.exit_reason = "killed"
        self.status = "KILLED"
        self._settle(prices)
        self._log(bar.timestamp, "killed", net=self.net_pnl, open_lots=self.open_lots)
        return True

    def run(self, candles: Iterable[Bar]) -> "FibTouchLadder":
        for bar in sorted(candles, key=lambda row: row.timestamp):
            self.on_candle(bar)
            if self.status in {"CLOSED", "EXPIRED", "KILLED"}:
                break
        return self

    # ── serialisation for the console ─────────────────────────────

    def get_status(self) -> dict[str, Any]:
        anchor = self.anchor
        return {
            "symbol": self.config.symbol,
            "side": self.side,
            "timeframe": self.config.timeframe,
            "entry_timeframe": self.config.entry_timeframe,
            "mode": getattr(self.executor, "mode", "paper"),
            "is_live": bool(getattr(self.executor, "is_live", False)),
            "armed": bool(getattr(self.executor, "armed", False)),
            "status": self.status,
            "mother_timestamp": self.config.mother_timestamp.isoformat(),
            "anchor": (
                {
                    "high": anchor.high,
                    "low": anchor.low,
                    "span": round(anchor.span, 2),
                    "high_timestamp": anchor.high_timestamp.isoformat(),
                    "low_timestamp": anchor.low_timestamp.isoformat(),
                    "confirmed_at": anchor.confirmed_at.isoformat(),
                    "involvement_candles": anchor.involvement_candles,
                }
                if anchor
                else None
            ),
            "levels": [rung.as_dict() for rung in self.rungs],
            "fills": [fill.as_dict() for fill in self.fills],
            "lot_size": self.config.lot_size,
            "strike_step": self.config.strike_step,
            "itm_steps": self.config.itm_steps,
            "min_dte": self.config.min_dte,
            "capital_cap_inr": self.config.capital_cap_inr,
            "deployed_inr": self.deployed_inr,
            "remaining_inr": self.remaining_inr,
            "open_lots": self.open_lots,
            "open_quantity": self.open_quantity,
            "average_index_entry": (
                round(self.average_index_entry, 2) if self.average_index_entry is not None else None
            ),
            "average_premium": (round(self.average_premium, 2) if self.average_premium is not None else None),
            "target_index": round(self.target_index, 2) if self.target_index is not None else None,
            "target_fraction": self.config.target_fraction,
            "exit_timestamp": self.exit_timestamp.isoformat() if self.exit_timestamp else None,
            "exit_reason": self.exit_reason,
            "exit_index": round(self.exit_index, 2) if self.exit_index is not None else None,
            "exit_premiums": [round(p, 2) if p is not None else None for p in self._exit_premiums],
            "gross_pnl": self.gross_pnl,
            "costs_total": self.costs_total,
            "net_pnl": self.net_pnl,
            "events": list(self.events),
            "data_gaps": list(self.data_gaps),
        }
