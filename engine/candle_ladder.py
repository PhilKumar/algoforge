"""The two-red recovery buy, escalating up the timeframes.

Phil's rule, in his words:

    "if starts at 1m wait for 2 red candles close, put the stop buy order on the
    close of the 1st/previous red candle. Once buy triggered, mark the ultimate
    low after the mother candle and move to 5min TF and wait for 2 red candle
    close and do the same thing..."

So one mother candle produces a ladder of up to four buys, each read on a slower
chart than the last: 1m, then 5m, then 15m, then 1H.  Each rung is bigger than
the one before (1, 2, 3, 4 lots), and the whole basket leaves together on the
first target or at expiry.  There is no re-arm: one mother is one trade.

Two decisions in here are worth stating out loud, because they change every
number this produces:

**The stop sits on the FIRST of the two reds**, which is the higher close.  The
older 1H engine used the second, and the difference is not cosmetic: on a pair
closing 102 then 99, this waits for a recovery back to 102 rather than buying
the moment 99 prints.  Later reds trail the stop down -- a third qualifying red
re-arms at the second one's close -- so the rule stays "one red back from the
newest", never "wherever it first armed".

**The recorded low gates the next rung** (`require_new_low`, on by default).
Without it a 1m fill and a 5m fill can both fire on one downswing, which is not
a ladder so much as the same trade bought twice.  Requiring the market to make
a genuinely new low first matches the fib cascade's own new-low invariant.  It
is a flag because it is an interpretation of "mark the ultimate low", not
something Phil spelled out.

This module owns geometry and paper fills, and nothing else: no broker, no
strike selection, no expiry calendar.  Whoever calls it supplies the contract
and a way to price it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from cascade_costs import OptionCostFill, calculate_nifty_option_basket_round_costs

# Slowest-last, because that is the direction a campaign climbs.
LADDER_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h")

# Minutes per bar, used only to order events by the moment each candle CLOSED.
TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}

# The lot on each successive rung, deepest last.
DEFAULT_LOTS: tuple[int, ...] = (1, 2, 3, 4)


class LadderError(ValueError):
    """The ladder cannot be built from the timeframes given."""


def ladder_from(timeframe: str, depth: int = 4) -> tuple[str, ...]:
    """The timeframes a campaign starting here will climb through.

    A 15m start has only 15m, 1h above it, so it ladders two deep however many
    lots were asked for -- the chart runs out before the schedule does.
    """
    key = str(timeframe).strip().lower()
    if key not in LADDER_TIMEFRAMES:
        raise LadderError(f"{timeframe!r} is not one of {', '.join(LADDER_TIMEFRAMES)}")
    if depth < 1:
        raise LadderError("depth must be at least 1")
    start = LADDER_TIMEFRAMES.index(key)
    return LADDER_TIMEFRAMES[start : start + depth]


@dataclass(frozen=True)
class LadderCandle:
    """One closed bar, and the chart it was read on."""

    timeframe: str
    timestamp: datetime  # the bar's OPEN, as Dhan reports it
    open: float
    high: float
    low: float
    close: float

    @property
    def is_red(self) -> bool:
        return self.close < self.open


def order_events(candles: list[LadderCandle]) -> list[LadderCandle]:
    """Chronological by the moment each bar closed, finest chart first.

    A 1m and a 1h bar can close on the same minute.  The finer one is the more
    recent piece of information, so it is seen first; without a fixed order the
    result would depend on how the caller happened to concatenate its fetches.
    """

    def key(row: LadderCandle) -> tuple[datetime, int]:
        minutes = TIMEFRAME_MINUTES.get(row.timeframe, 1)
        return (row.timestamp + timedelta(minutes=minutes), minutes)

    return sorted(candles, key=key)


@dataclass(frozen=True)
class LadderFill:
    """One rung, filled."""

    rung: int
    timeframe: str
    timestamp: datetime
    index_price: float  # the stop level, which is where a stop order fills
    option_premium: Optional[float]
    lots: int
    quantity: int
    strike: int
    option_type: str
    # The lowest the index had been when this rung filled -- the "ultimate low"
    # the next rung is measured against.
    marked_low: float


@dataclass
class _Stage:
    """One rung's own two-red watch."""

    rung: int
    timeframe: str
    lots: int
    reds: list[LadderCandle] = field(default_factory=list)
    stop: Optional[float] = None
    armed_at: Optional[datetime] = None


PremiumLookup = Callable[[datetime, int, str], Optional[float]]


class TwoRedLadder:
    """Two reds, buy the recovery, climb a timeframe, repeat."""

    def __init__(
        self,
        mother: LadderCandle,
        *,
        stages: tuple[str, ...],
        strike_for: Callable[[datetime, float], tuple[int, str]],
        premium_lookup: PremiumLookup,
        lot_size: int,
        lots: tuple[int, ...] = DEFAULT_LOTS,
        target_fraction: float = 0.25,
        require_new_low: bool = True,
    ) -> None:
        if not stages:
            raise LadderError("a ladder needs at least one timeframe")
        if int(lot_size) <= 0:
            raise LadderError("lot size must be positive")
        self.lot_size = int(lot_size)
        self.mother = mother
        self.strike_for = strike_for
        self.premium_lookup = premium_lookup
        self.target_fraction = float(target_fraction)
        self.require_new_low = bool(require_new_low)
        self.stages = [
            _Stage(rung=index + 1, timeframe=timeframe, lots=int(lots[min(index, len(lots) - 1)]))
            for index, timeframe in enumerate(stages)
        ]
        self.active = 0
        self.fills: list[LadderFill] = []
        self.lowest: float = mother.low
        # The low as at the last fill; the next rung may only arm below it.
        self.gate_low: Optional[float] = None
        self.exit_timestamp: Optional[datetime] = None
        self.exit_index_price: Optional[float] = None
        self.exit_premium: Optional[float] = None
        self.exit_reason: Optional[str] = None
        self.gross_pnl: Optional[float] = None
        self.costs: Any = None
        self.net_pnl: Optional[float] = None
        self.events: list[dict[str, Any]] = []
        self.status = "WAITING_TWO_RED"
        self._last_close: dict[str, float] = {}

    # ── reporting ────────────────────────────────────────────
    @property
    def average_entry(self) -> Optional[float]:
        quantity = sum(fill.quantity for fill in self.fills)
        if not quantity:
            return None
        return round(sum(fill.index_price * fill.quantity for fill in self.fills) / quantity, 2)

    @property
    def target_index(self) -> Optional[float]:
        entry = self.average_entry
        if entry is None:
            return None
        # A quarter of the way back to the mother's high, measured from where
        # the basket actually sits -- so a deeper rung pulls the target down.
        return round(entry + self.target_fraction * (self.mother.high - entry), 2)

    def _log(self, candle: LadderCandle, event: str, **payload: Any) -> None:
        self.events.append(
            {
                "timestamp": candle.timestamp.isoformat(),
                "timeframe": candle.timeframe,
                "event": event,
                **payload,
            }
        )

    # ── the state machine ────────────────────────────────────
    def on_candle(self, candle: LadderCandle) -> None:
        if self.status in {"CLOSED", "EXPIRED"} or candle.timestamp <= self.mother.timestamp:
            return
        self.lowest = min(self.lowest, candle.low)

        # An open basket is watching for its target on every chart -- the first
        # bar to reach it closes everything, whatever timeframe spotted it.
        if self.fills and self._target_reached(candle):
            return

        stage = self.stages[self.active] if self.active < len(self.stages) else None
        if stage is None or candle.timeframe != stage.timeframe:
            self._remember_close(candle)
            return

        if stage.stop is not None and stage.armed_at is not None and candle.timestamp > stage.armed_at:
            if candle.high >= stage.stop:
                self._fill(stage, candle)
                self._remember_close(candle)
                return

        self._watch_for_reds(stage, candle)
        self._remember_close(candle)

    def _remember_close(self, candle: LadderCandle) -> None:
        self._last_close[candle.timeframe] = candle.close

    def _watch_for_reds(self, stage: _Stage, candle: LadderCandle) -> None:
        prior_close = self._last_close.get(candle.timeframe)
        if prior_close is None or not candle.is_red or candle.close >= prior_close:
            return
        # The gate: a rung above the first only starts counting once the market
        # has actually gone lower than it was when the last rung filled.
        if self.require_new_low and self.gate_low is not None and candle.low >= self.gate_low:
            return
        stage.reds.append(candle)
        if len(stage.reds) < 2:
            return
        # One red back from the newest -- Phil's "1st/previous red candle".
        stage.stop = stage.reds[-2].close
        stage.armed_at = candle.timestamp
        self._log(
            candle,
            "entry_stop_armed",
            rung=stage.rung,
            stop=stage.stop,
            reds=len(stage.reds),
        )
        self.status = "ARMED"

    def _fill(self, stage: _Stage, candle: LadderCandle) -> None:
        strike, option_type = self.strike_for(candle.timestamp, stage.stop)
        premium = self.premium_lookup(candle.timestamp, strike, option_type)
        quantity = stage.lots * self.lot_size
        self.fills.append(
            LadderFill(
                rung=stage.rung,
                timeframe=stage.timeframe,
                timestamp=candle.timestamp,
                index_price=float(stage.stop),
                option_premium=premium,
                lots=stage.lots,
                quantity=quantity,
                strike=strike,
                option_type=option_type,
                marked_low=self.lowest,
            )
        )
        self._log(
            candle,
            "rung_filled",
            rung=stage.rung,
            index_price=stage.stop,
            premium=premium,
            lots=stage.lots,
            marked_low=self.lowest,
        )
        # Mark the ultimate low and climb.
        self.gate_low = self.lowest
        stage.stop = None
        stage.armed_at = None
        self.active += 1
        self.status = "OPEN" if self.active >= len(self.stages) else "OPEN_CLIMBING"

    def _target_reached(self, candle: LadderCandle) -> bool:
        target = self.target_index
        if target is None or candle.high < target:
            return False
        self._close(candle, target, "target")
        return True

    def close_at_expiry(self, candle: LadderCandle, index_price: float) -> None:
        """End an unfinished campaign on its option's last day."""
        if self.status in {"CLOSED", "EXPIRED"}:
            return
        if not self.fills:
            # Nothing was ever bought: the mother simply never set up.
            self.status = "EXPIRED"
            return
        self._close(candle, index_price, "expiry")
        self.status = "EXPIRED"

    def _close(self, candle: LadderCandle, index_price: float, reason: str) -> None:
        self.exit_timestamp = candle.timestamp
        self.exit_index_price = float(index_price)
        self.exit_reason = reason
        priced = [fill for fill in self.fills if fill.option_premium is not None]
        first = self.fills[0]
        sell = self.premium_lookup(candle.timestamp, first.strike, first.option_type)
        self.exit_premium = sell
        if sell is None or len(priced) != len(self.fills):
            # An unpriced leg makes the whole basket's P&L a guess. Report the
            # exit and leave the money blank rather than quietly costing the
            # trade as though the missing leg were free.
            self.status = "CLOSED"
            self._log(candle, "closed_unpriced", reason=reason)
            return
        quantity = sum(fill.quantity for fill in self.fills)
        lots = sum(fill.lots for fill in self.fills)
        self.costs = calculate_nifty_option_basket_round_costs(
            buys=[OptionCostFill(fill.option_premium, fill.quantity, fill.lots) for fill in self.fills],
            sell_price=float(sell),
            sell_quantity=quantity,
            sell_lots=lots,
        )
        self.gross_pnl = round(
            sum((float(sell) - float(fill.option_premium)) * fill.quantity for fill in self.fills), 2
        )
        self.net_pnl = round(self.gross_pnl - self.costs.total, 2)
        self.status = "CLOSED"
        self._log(candle, "closed", reason=reason, net_pnl=self.net_pnl)

    def run(self, candles: list[LadderCandle]) -> "TwoRedLadder":
        for candle in order_events(candles):
            self.on_candle(candle)
        return self
