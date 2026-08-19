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
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Callable, Optional

from cascade_costs import OptionCostFill, calculate_nifty_option_basket_round_costs

# The last minute a position is sold on a day it must be flat: the 15:15 IST
# bar closes at 15:30 on every chart, and the closing auction starts at 15:40,
# so any bar that CLOSES at or after 15:15 is the day's last chance to act.
SESSION_CLOSE_TIME = dt_time(15, 15)
SESSION_END_TIME = dt_time(15, 30)

# Slowest-last, because that is the direction a campaign climbs.
LADDER_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h")

# Minutes per bar, used only to order events by the moment each candle CLOSED.
#
# 1d and 1w are here but deliberately NOT in LADDER_TIMEFRAMES: nothing in the
# app offers them, and the live poll has no daily/weekly feed. A backtest can
# still climb onto them by passing `stages` itself. Their spans are measured to
# the NSE session, not the clock -- a day closes 375 minutes after its 09:15
# open, and a Monday-open week closes at Friday 15:30 -- so a slow bar is never
# acted on before it has really closed. A short week (a holiday) only makes the
# reckoning LATER, which is the safe direction.
TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 375, "1w": 6135}

# The lot on each successive rung, deepest last.
DEFAULT_LOTS: tuple[int, ...] = (1, 2, 3, 4)

# HOW FAR A CAMPAIGN CLIMBS. Phil, 2026-08-19: "Don't climb to the next slower
# chart after 5m for 1m first buy and after 15m for 5m 1st buy and so on ...
# till 1H." One step up and no further: 1m -> 5m, 5m -> 15m, 15m -> 1H; a 1H
# start has 1H alone (the chart runs out). Two rungs, 1 lot then 2.
LADDER_DEPTH = 2


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
        return (closed_at(row), TIMEFRAME_MINUTES.get(row.timeframe, 1))

    return sorted(candles, key=key)


def closed_at(candle: LadderCandle) -> datetime:
    """The moment this bar CLOSED, which is when it could first be acted on.

    An intraday bar never closes past the session: the 15:15 hourly stub is a
    15-minute bar and closes at 15:30 like the 15:29 minute bar. Without the
    clamp that stub "closed" at 16:15, after the market, and any price asked
    for that minute could only be a stale one. Daily and weekly spans are
    measured in session minutes already and are left alone.
    """
    minutes = TIMEFRAME_MINUTES.get(candle.timeframe, 1)
    closed = candle.timestamp + timedelta(minutes=minutes)
    if minutes < TIMEFRAME_MINUTES["1d"]:
        session_end = candle.timestamp.replace(
            hour=SESSION_END_TIME.hour, minute=SESSION_END_TIME.minute, second=0, microsecond=0
        )
        if closed > session_end >= candle.timestamp:
            closed = session_end
    return closed


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
    # The minute the premium was read at: the bar's CLOSE, which is the moment
    # a paper campaign sees the bar and buys. `timestamp` stays the bar's open,
    # the way every table on the site names a candle.
    priced_at: Optional[datetime] = None


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
        quantity_for: Optional[Callable[[float, int], int]] = None,
        trail_fraction: float = 0.0,
        expiry: Optional[date] = None,
        intraday_close: bool = False,
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
        # WHEN THE OPTION RUNS OUT. On the expiry day the first bar to close at
        # or after 15:15 sells whatever is open and ends the campaign, bought
        # or not -- past its expiry the contract does not exist, so neither
        # can the ladder. None keeps the older behaviour, where only the
        # caller's `close_at_expiry` can end it.
        self.expiry = expiry
        # FLAT BY 3:15. Phil's fib rule, offered here too: with it on, the
        # first bar to close at or after 15:15 on ANY day sells the basket at
        # its close and ENDS the campaign -- a setup that never bought by then
        # ends as well; it is not carried into tomorrow. Off (the default) the
        # ladder holds to its target or the option's expiry.
        self.intraday_close = bool(intraday_close)
        # HOW BIG A RUNG IS. Left alone, it is the option law: lots x lot size,
        # so rung 3 is three times rung 1 whatever the price did. Phil's cash
        # rule sizes off the FALL instead -- "calculate the percent the market
        # is down from the mother high, and invest that percent of capital", so
        # a 1% fall commits 1% of the purse and a 6% fall commits 6%. Injected
        # rather than coded here because it is the caller who knows the purse.
        # It is given the price the rung fills at and the lots that rung would
        # have taken, and returns a quantity.
        self.quantity_for = quantity_for
        # A TRAIL INSTEAD OF A SALE. Zero keeps the shipped rule: the target is
        # a resting sell and the campaign ends there. Above zero the target only
        # ARMS a trail -- the basket then rides the move and leaves when price
        # CLOSES back this fraction of the run below the best it has seen. A
        # close, not a wick, so one poke does not end a move still going.
        self.trail_fraction = float(trail_fraction)
        self._trail_armed = False
        self._trail_best: Optional[float] = None
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
        self.exit_timeframe: Optional[str] = None
        self.exit_priced_at: Optional[datetime] = None
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
        if self.status in {"CLOSED", "EXPIRED", "KILLED"} or candle.timestamp <= self.mother.timestamp:
            return
        self.lowest = min(self.lowest, candle.low)

        # An open basket is watching for its target on every chart -- the first
        # bar to reach it closes everything, whatever timeframe spotted it.
        if self.fills and self._target_reached(candle):
            return

        # The day's last chance to act, checked BEFORE a rung may arm or fill
        # on this bar: a stop reached at 15:14 must not open a position the
        # very next bar has to shut.
        if self._session_close_due(candle):
            self._end_session(candle)
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
        """Phil's rule, stated by him on 2026-08-19:

            "Green is not the matter here. Any number of green candles can be
            between the 2 red candles. The thing is the price of the current
            candle has to be below the previous red candle close."

        So the watch is a sequence of RED closes, each below the one before it,
        however many greens sit in between. A red that closes ABOVE the last
        red in the sequence is not a step down and is simply ignored -- it
        neither joins nor resets. Greens are ignored entirely.

        Two earlier readings were both wrong and are worth naming, because each
        one produced trades:
        - comparing against the PREVIOUS CANDLE's close (any colour) let a red
          that closed above the previous red qualify. On his 10-Aug-2026 12:30
          mother that armed a buy-stop at 24,573.55 while NIFTY was at 24,591
          -- a "recovery" buy below the market, at a price it never traded;
        - requiring the two reds to be BACK-TO-BACK threw away every setup
          with a green in the middle, which is most of them.

        The sequence gives the stop for free: it is always ``reds[-2].close``,
        one red back from the newest, and by construction that is ABOVE the
        close of the red that armed it. A stop can never again sit below the
        market at the moment it is placed.
        """
        if not candle.is_red:
            return
        # The gate: a rung above the first only starts counting once the market
        # has actually gone lower than it was when the last rung filled.
        if self.require_new_low and self.gate_low is not None and candle.low >= self.gate_low:
            return
        if stage.reds and candle.close >= stage.reds[-1].close:
            # A red, but not a lower one: the fall has not continued, so this
            # is not the next step down. The sequence waits where it is.
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

    def _session_close_due(self, candle: LadderCandle) -> bool:
        """Is this bar the day's last chance to act, on a day the ladder must end?"""
        if closed_at(candle).time() < SESSION_CLOSE_TIME:
            return False
        if self.intraday_close:
            return True
        return self.expiry is not None and candle.timestamp.date() >= self.expiry

    def _end_session(self, candle: LadderCandle) -> None:
        """Sell whatever is open at this bar's close and end the campaign."""
        on_expiry = self.expiry is not None and candle.timestamp.date() >= self.expiry
        if not self.fills:
            self._log(candle, "session_ended_without_a_trade", reason="expiry" if on_expiry else "intraday_close")
            self.status = "EXPIRED" if on_expiry else "CLOSED"
            return
        self._close(candle, float(candle.close), "expiry" if on_expiry else "intraday_close")
        if on_expiry:
            self.status = "EXPIRED"

    def _fill(self, stage: _Stage, candle: LadderCandle) -> None:
        strike, option_type = self.strike_for(candle.timestamp, stage.stop)
        # PRICED AT THE BAR'S CLOSE. That is the moment the engine sees the
        # bar and buys, in a backtest and in the paper loop alike -- so the two
        # agree, and a 15m or 1H rung has a live quote to price against (the
        # bar's OPEN minute is long gone by the time the bar closes). It is
        # also the conservative side for a bar that rose through the stop.
        priced_at = closed_at(candle)
        premium = self.premium_lookup(priced_at, strike, option_type)
        if self.quantity_for is not None:
            quantity = int(self.quantity_for(float(stage.stop), stage.lots))
            if quantity <= 0:
                # The fall is too shallow to be worth a share. The rung stays
                # unfilled and the ladder keeps waiting, which is the rule
                # working, not an error.
                self._log(candle, "rung_too_small", rung=stage.rung, index_price=stage.stop)
                stage.stop = None
                stage.armed_at = None
                # Nothing is resting any more, so the campaign is back to
                # watching -- reading ARMED with no order on the book is a
                # state no screen can explain.
                if not self.fills:
                    self.status = "WAITING_TWO_RED"
                return
        else:
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
                priced_at=priced_at,
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
            priced_at=priced_at.isoformat(),
        )
        # Mark the ultimate low and climb.
        self.gate_low = self.lowest
        stage.stop = None
        stage.armed_at = None
        self.active += 1
        self.status = "OPEN" if self.active >= len(self.stages) else "OPEN_CLIMBING"

    def _target_reached(self, candle: LadderCandle) -> bool:
        target = self.target_index
        if target is None:
            return False
        if not self.trail_fraction:
            if candle.high < target:
                return False
            self._close(candle, target, "target")
            return True

        if not self._trail_armed:
            if candle.high < target:
                return False
            self._trail_armed = True
            self._trail_best = float(candle.high)
            self._log(candle, "trail_armed", target=target, best=self._trail_best)
            return False
        self._trail_best = max(float(self._trail_best or 0.0), float(candle.high))
        entry = self.average_entry or 0.0
        give_back = (self._trail_best - entry) * self.trail_fraction
        stop = self._trail_best - give_back
        if float(candle.close) > stop:
            return False
        self._close(candle, stop, "trail")
        return True

    def close_at_expiry(self, candle: LadderCandle, index_price: float) -> None:
        """End an unfinished campaign on its option's last day."""
        if self.status in {"CLOSED", "EXPIRED", "KILLED"}:
            return
        if not self.fills:
            # Nothing was ever bought: the mother simply never set up.
            self.status = "EXPIRED"
            return
        self._close(candle, index_price, "expiry")
        self.status = "EXPIRED"

    def kill(self, candle: LadderCandle, index_price: float) -> None:
        """Stop the campaign by hand, selling any open basket at this price.

        A kill always succeeds: if no sell premium can be found the exit is
        still recorded and the money is left blank, exactly as `_close` does
        for any other unpriced leg.  Refusing the kill would leave a basket
        Phil asked to stop still watching the market.
        """
        if self.status in {"CLOSED", "EXPIRED", "KILLED"}:
            return
        if self.fills:
            # A kill happens NOW, not when some bar closes: the caller hands in
            # a synthetic bar stamped at the moment of the kill, and the sale
            # is priced at that moment.
            self._close(candle, index_price, "manual_kill", priced_at=candle.timestamp)
        else:
            self._log(candle, "campaign_killed")
        self.status = "KILLED"

    def _close(
        self, candle: LadderCandle, index_price: float, reason: str, *, priced_at: Optional[datetime] = None
    ) -> None:
        # STAMPED AT THE BAR'S CLOSE, which is the moment the exit could first
        # be acted on -- the same instant `order_events` sequences by. A bar's
        # `timestamp` is its OPEN, so stamping that put an exit taken on a slow
        # bar BEFORE the buy a faster bar recorded: a 12:15 hourly bar closing
        # at 13:15 read as "bought 12:45, closed 12:15" (Phil, 2026-08-14).
        # Fills keep the open, as every table on the site reads them, and the
        # ordering guarantees a close is never earlier than the fill's own bar.
        self.exit_timestamp = closed_at(candle)
        # WHICH CHART CLOSED IT: still worth recording, since a hold time only
        # makes sense against the chart the exit was read on.
        self.exit_timeframe = candle.timeframe
        self.exit_index_price = float(index_price)
        self.exit_reason = reason
        priced = [fill for fill in self.fills if fill.option_premium is not None]
        first = self.fills[0]
        # AND PRICED THERE TOO -- the sale is read at the same instant it is
        # stamped, which is when a paper campaign actually sells.
        self.exit_priced_at = priced_at or closed_at(candle)
        sell = self.premium_lookup(self.exit_priced_at, first.strike, first.option_type)
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
