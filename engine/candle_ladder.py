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

from cascade_costs import OptionCostFill, OptionRoundCosts, calculate_nifty_option_basket_round_costs
from engine.options_live_executor import ExecutionRefused

# The last minute a position is sold on a day it must be flat: the 15:15 IST
# bar closes at 15:30 on every chart, and the closing auction starts at 15:40,
# so any bar that CLOSES at or after 15:15 is the day's last chance to act.
SESSION_CLOSE_TIME = dt_time(15, 15)
SESSION_END_TIME = dt_time(15, 30)

# Slowest-last, because that is the direction a campaign climbs. These are the
# charts a campaign may START on -- the ones the app can fetch and poll.
LADDER_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h")

# THE WHOLE CHAIN, including the two slow charts above 1H. Phil, 2026-08-19:
# "taking the entry on 1m, escalating to 5m and then to 15m -- 3 layers for
# all. If 1H entry then it goes till 1D and 1W." So a campaign climbs three
# charts from wherever it starts, and a 1H mother ends on the weekly.
LADDER_CHAIN: tuple[str, ...] = ("1m", "5m", "15m", "1h", "1d", "1w")

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
#
# TWO, not three (Phil, 2026-08-20: "2 rungs is fine"). The third rung fired
# four times in 22 months and cost Rs 2.1L of standing capital to catch them:
# peak deployment falls from Rs 3,79,948 to Rs 1,69,991 while the book keeps
# Rs 2,87,019 of Rs 3,99,958 -- 169% of the capital at risk against 105%, the
# same -Rs 7,453 drawdown, and a minus-best-five that is slightly BETTER
# (Rs 1,36,997). Every campaign still reached its target on the trail; the
# third rung was upside on the biggest winners, not the thing that got them
# out. Only this strategy reads this constant -- the equity two-red ladder
# keeps its own LADDERS table.
LADDER_DEPTH = 2


def _sum_costs(parts: list[OptionRoundCosts]) -> OptionRoundCosts:
    """The charges of several contracts' round trips, as one schedule.

    Brokerage is per order so it is simply added; every other line is a rate
    on turnover and adds too. One part returns itself unchanged.
    """
    if len(parts) == 1:
        return parts[0]
    return OptionRoundCosts(
        buy_turnover=round(sum(p.buy_turnover for p in parts), 2),
        sell_turnover=round(sum(p.sell_turnover for p in parts), 2),
        brokerage=round(sum(p.brokerage for p in parts), 2),
        stt=round(sum(p.stt for p in parts), 2),
        exchange_transaction=round(sum(p.exchange_transaction for p in parts), 2),
        sebi=round(sum(p.sebi for p in parts), 2),
        stamp=round(sum(p.stamp for p in parts), 2),
        gst=round(sum(p.gst for p in parts), 2),
    )


class LadderError(ValueError):
    """The ladder cannot be built from the timeframes given."""


def ladder_from(timeframe: str, depth: int = 4) -> tuple[str, ...]:
    """The timeframes a campaign starting here will climb through.

    A 15m start has only 15m, 1h above it, so it ladders two deep however many
    lots were asked for -- the chart runs out before the schedule does.
    """
    key = str(timeframe).strip().lower()
    if key not in LADDER_CHAIN:
        raise LadderError(f"{timeframe!r} is not one of {', '.join(LADDER_CHAIN)}")
    if depth < 1:
        raise LadderError("depth must be at least 1")
    start = LADDER_CHAIN.index(key)
    return LADDER_CHAIN[start : start + depth]


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

    @property
    def is_green(self) -> bool:
        return self.close > self.open


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
    # REAL ORDERS, when this campaign is a live one. Both stay None on paper,
    # which is every run until the shared executor is proven against Dhan.
    order_id: Optional[str] = None
    bracket_order_id: Optional[str] = None


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
        hold_days: Optional[int] = None,
        min_buys_before_exit: int = 1,
        stop_loss_pct: float = 0.0,
        min_fall_pct: float = 0.0,
        range_bars: int = 0,
        range_position: float = 0.5,
        require_below_mother: bool = False,
        fallback_strike_for: Optional[Callable[[datetime, float], list[tuple[int, str]]]] = None,
        direction: str = "CE",
        executor: Optional[object] = None,
    ) -> None:
        if not stages:
            raise LadderError("a ladder needs at least one timeframe")
        if int(lot_size) <= 0:
            raise LadderError("lot size must be positive")
        self.lot_size = int(lot_size)
        # SENDS THE REAL ORDERS, when this campaign is live. None is paper,
        # and the RULES never consult it: the same two reds buy the same rung
        # either way, and only the last inch to the exchange differs.
        self.executor = executor
        # Set when an order's fate is unknown. Nothing automatic follows.
        self.frozen_reason: Optional[str] = None
        # WHAT IS STILL WORKING AT THE BROKER. A LadderFill is frozen -- it is
        # the record of what happened and must never be rewritten -- so the
        # live state of each leg is kept here: bracket ids still on the book,
        # and entry ids not yet sold.
        self._brackets_open: set[str] = set()
        self._legs_open: set[str] = set()
        self.mother = mother
        self.strike_for = strike_for
        # WHEN THE CHOSEN STRIKE HAS NO PRICE. Phil, 2026-08-20: "As we are
        # using monthly strikes.. better try ATM if price is not available or
        # liquidity is not available". A monthly ATM-2 can go unquoted for a
        # minute; rather than record an unpriced leg (which withholds the
        # whole basket's P&L) the rung is re-struck at the money and priced
        # there. Only used when the first ask comes back empty.
        self.fallback_strike_for = fallback_strike_for
        self.premium_lookup = premium_lookup
        self.target_fraction = float(target_fraction)
        # WHICH WAY THE CAMPAIGN LEANS. "CE" is the rule as Phil wrote it: the
        # mother's HIGH, two RED closes stepping DOWN, a buy-stop on the first
        # red's close, and a target a quarter of the way back UP to that high.
        # "PE" is the same rule in a mirror -- the mother's LOW, two GREEN
        # closes stepping UP, a sell-stop on the first green's close, and a
        # target a quarter of the way back DOWN. Every comparison below reads
        # this, so there is one geometry, not two engines.
        self.direction = "PE" if str(direction).upper() == "PE" else "CE"
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
        # HOW MANY DAYS IT MAY HOLD. Phil asked on 2026-08-19: "how about
        # holding 1 day?" -- a time stop, counted from the FIRST BUY (from the
        # mother while nothing is bought). 0 is the intraday rule above: out on
        # the buy's own day. 1 carries it one night and sells at 15:15 the next
        # session; a market holiday only pushes that to the next session there
        # is, which is the safe direction. None holds to the target or expiry.
        self.hold_days = 0 if (intraday_close and hold_days is None) else hold_days
        # LET IT CLIMB FIRST. Phil, 2026-08-19: the target is reached so fast
        # that 93% of campaigns buy once and the ladder never happens. Above 1,
        # the target is ignored until this many rungs have been bought -- the
        # basket must be built before it is allowed to take profit. Expiry (and
        # a time stop) still end it whatever it holds, so this is a delay on
        # the profit-taking, never a promise to buy more.
        self.min_buys_before_exit = max(1, int(min_buys_before_exit))
        # A STOP ON THE PREMIUM. The strategy has none by design, and the whole
        # measured loss is the handful of baskets that never recover and give
        # back the entire premium at expiry. Above zero, the basket is sold
        # when the option is this far below what the basket paid for it,
        # checked on the close of every bar the ladder reads.
        self.stop_loss_pct = float(stop_loss_pct)
        # WAIT FOR A REAL FALL. Phil's cash rule (the 8% gate) is what turned
        # the equity version: nothing may arm until price is this far below the
        # mother's high, so a shallow dip is not traded at all.
        self.min_fall_pct = float(min_fall_pct)
        # THE RANGE GATE. Phil, 2026-08-19: "count some 278 candle bars ... get
        # the high and the low and mark 50% and then use this strategy below
        # 50%." A rolling window of this many bars on the mother's own chart:
        # its high and low make a box, and nothing may arm while price sits in
        # the top half of it. Zero switches the gate off. `range_position` is
        # the line inside the box (0.5 = the midpoint).
        self.range_bars = int(range_bars)
        self.range_position = float(range_position)
        # THE TARGET MUST BE AHEAD OF THE BUY. The target is measured from the
        # average entry back toward the MOTHER's high, so a rung filling above
        # that high has a target BEHIND it: it is satisfied the instant it
        # fills, arms the trail and sells on the next bar. Phil found one on
        # the tearsheet (02-Jul-2026: mother high 24,176, bought 24,227, target
        # 24,214, out 15 minutes later). It happens because nothing cancels a
        # broken mother while the rolling box climbs away from it. With this
        # on, such a rung is simply not bought and the ladder keeps waiting.
        self.require_below_mother = bool(require_below_mother)
        self._range: list[tuple[float, float]] = []
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
        # The furthest price has gone the campaign's way: the running LOW on a
        # CE, the running HIGH on a PE.
        self.lowest: float = mother.low if self.direction == "CE" else mother.high
        # That extreme as at the last fill; the next rung may only arm beyond it.
        self.gate_low: Optional[float] = None
        self.exit_timestamp: Optional[datetime] = None
        self.exit_timeframe: Optional[str] = None
        self.exit_priced_at: Optional[datetime] = None
        self.exit_index_price: Optional[float] = None
        self.exit_premium: Optional[float] = None
        # Per contract, when the basket holds more than one strike.
        self.exit_premiums: dict[str, Optional[float]] = {}
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
        # A PE reads the mother's LOW and the target sits below the basket.
        if self.direction == "CE":
            return round(entry + self.target_fraction * (self.mother.high - entry), 2)
        return round(entry - self.target_fraction * (entry - self.mother.low), 2)

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
        if self.direction == "CE":
            self.lowest = min(self.lowest, candle.low)
        else:
            self.lowest = max(self.lowest, candle.high)

        # THE EXIT IS READ ON THE CHART THE CAMPAIGN WAS STARTED ON, and only
        # on a bar that began after the last buy. Two ways a slower chart lied:
        #
        #   * its bar SPANS the buy -- the 1d bar of the day you bought carries
        #     that morning's high, hours before the rung filled. On 2025-02-11
        #     NIFTY made 23,390 at 09:15, the rung filled at 23,277 at 10:45,
        #     and the daily bar sold the basket at the day's close of 23,070:
        #     -Rs 6,598, booked as a target hit.
        #   * even a clean one cannot say WHEN inside itself the target was
        #     touched, and the basket is priced at the bar's close. A daily bar
        #     that trades through the target at 10:00 and gives it all back
        #     books the exit at 15:30, at a premium the target never saw.
        #
        # The mother's own chart has neither problem: its bars are 1 to 60
        # minutes, so the close is close to the touch. The slower charts stay
        # what they are for -- watching for the two reds that buy the next rung.
        watching = self._exit_chart(candle)
        if self.fills and watching and self._target_reached(candle):
            return
        # ... and for its stop, if it has been given one.
        if self.fills and watching and self.exit_timestamp is None and self._stop_loss_hit(candle):
            self._close(candle, float(candle.close), "stop_loss")
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
            triggered = candle.high >= stage.stop if self.direction == "CE" else candle.low <= stage.stop
            if triggered:
                self._fill(stage, candle)
                self._remember_close(candle)
                return

        self._watch_for_reds(stage, candle)
        self._remember_close(candle)

    def _remember_close(self, candle: LadderCandle) -> None:
        self._last_close[candle.timeframe] = candle.close
        if self.range_bars and candle.timeframe == self.mother.timeframe:
            self._range.append((candle.high, candle.low))
            if len(self._range) > self.range_bars:
                del self._range[0]

    def prime_range(self, bars: list[LadderCandle]) -> None:
        """Seed the rolling window with the bars BEFORE the mother.

        The engine is only ever fed bars after its mother, so without this the
        window would need 278 fresh bars before it could say anything -- by
        which time the campaign is long over on a 15m chart.
        """
        self._range = [(row.high, row.low) for row in bars][-self.range_bars :] if self.range_bars else []

    def _range_gate_open(self, candle: LadderCandle) -> bool:
        """Is price in the LOWER half of the last N bars' range?"""
        if not self.range_bars:
            return True
        if len(self._range) < self.range_bars:
            # Not enough history to know the box: say nothing rather than
            # guess, and let the campaign wait.
            return False
        high = max(row[0] for row in self._range)
        low = min(row[1] for row in self._range)
        if high <= low:
            return False
        if self.direction == "CE":
            return candle.close <= low + self.range_position * (high - low)
        # The mirror: a PE campaign wants the TOP of the box, measured the same
        # distance down from the high as a CE measures up from the low.
        return candle.close >= high - self.range_position * (high - low)

    def _fall_gate_open(self, candle: LadderCandle) -> bool:
        """Has price fallen far enough below the mother's high to trade at all?"""
        if not self.min_fall_pct:
            return True
        if self.direction == "CE":
            return candle.close <= self.mother.high * (1.0 - self.min_fall_pct / 100.0)
        return candle.close >= self.mother.low * (1.0 + self.min_fall_pct / 100.0)

    def _stop_loss_hit(self, candle: LadderCandle) -> bool:
        """Is the basket this far under water at this bar's close?"""
        if not self.stop_loss_pct or not self.fills:
            return False
        priced = [fill for fill in self.fills if fill.option_premium is not None]
        if len(priced) != len(self.fills):
            return False
        quantity = sum(fill.quantity for fill in self.fills)
        paid = sum(float(fill.option_premium) * fill.quantity for fill in self.fills) / quantity
        first = self.fills[0]
        now = self.premium_lookup(closed_at(candle), first.strike, first.option_type)
        if now is None:
            return False
        return float(now) <= paid * (1.0 - self.stop_loss_pct / 100.0)

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
        if not (candle.is_red if self.direction == "CE" else candle.is_green):
            return
        if not self._fall_gate_open(candle) or not self._range_gate_open(candle):
            return
        # The gate: a rung above the first only starts counting once the market
        # has actually gone further its way than it was when the last rung
        # filled -- lower on a CE, higher on a PE.
        if self.require_new_low and self.gate_low is not None:
            beyond = candle.low < self.gate_low if self.direction == "CE" else candle.high > self.gate_low
            if not beyond:
                return
        if stage.reds:
            # A bar of the right colour, but not a further one: the move has
            # not continued, so this is not the next step. The sequence waits.
            last = stage.reds[-1].close
            further = candle.close < last if self.direction == "CE" else candle.close > last
            if not further:
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
        if self.expiry is not None and candle.timestamp.date() >= self.expiry:
            return True
        if self.hold_days is None:
            return False
        if self.intraday_close and not self.hold_days:
            # "Close at 3:15" is a rule about the DAY, so it ends the campaign
            # whether or not anything was bought -- nothing may be carried
            # overnight, and a setup still waiting at 15:15 is over too.
            return True
        if not self.fills:
            # A time stop is a rule about a POSITION, so it cannot fire before
            # there is one. It used to run from the mother's own day when
            # nothing had been bought, which read as a time stop but behaved as
            # a deadline on the setup: on the box-mother runs the two reds
            # typically arrive a week after the mother, so "hold 1 day" threw
            # away 38 of 44 campaigns before they ever traded and the result
            # measured the wrong rule. The mother's expiry still ends a setup
            # that never sets up.
            return False
        started = self.fills[0].timestamp.date()
        return candle.timestamp.date() >= started + timedelta(days=int(self.hold_days))

    def _end_session(self, candle: LadderCandle) -> None:
        """Sell whatever is open at this bar's close and end the campaign."""
        on_expiry = self.expiry is not None and candle.timestamp.date() >= self.expiry
        reason = "expiry" if on_expiry else ("intraday_close" if not self.hold_days else "time_stop")
        if not self.fills:
            self._log(candle, "session_ended_without_a_trade", reason=reason)
            self.status = "EXPIRED" if on_expiry else "CLOSED"
            return
        self._close(candle, float(candle.close), reason)
        if on_expiry:
            self.status = "EXPIRED"

    def _fill(self, stage: _Stage, candle: LadderCandle) -> None:
        if self.require_below_mother:
            past = (
                float(stage.stop) >= self.mother.high
                if self.direction == "CE"
                else float(stage.stop) <= self.mother.low
            )
            if past:
                # A buy here would have its target behind it. The arm is torn
                # up and the rung waits for the next two reds, exactly as it
                # does for a fall too shallow to be worth a share.
                self._log(candle, "above_mother", rung=stage.rung, index_price=stage.stop)
                stage.stop = None
                stage.armed_at = None
                if not self.fills:
                    self.status = "WAITING_TWO_RED"
                return
        strike, option_type = self.strike_for(candle.timestamp, stage.stop)
        # PRICED AT THE BAR'S CLOSE. That is the moment the engine sees the
        # bar and buys, in a backtest and in the paper loop alike -- so the two
        # agree, and a 15m or 1H rung has a live quote to price against (the
        # bar's OPEN minute is long gone by the time the bar closes). It is
        # also the conservative side for a bar that rose through the stop.
        priced_at = closed_at(candle)
        premium = self.premium_lookup(priced_at, strike, option_type)
        if premium is None and self.fallback_strike_for is not None:
            # The candidates come back in preference order -- at the money
            # first, then the lines either side of it -- and the first one
            # that answers is bought. A strike nobody quotes is not a strike
            # this campaign can hold.
            for alt_strike, alt_type in self.fallback_strike_for(candle.timestamp, stage.stop):
                if (alt_strike, alt_type) == (strike, option_type):
                    continue
                alt = self.premium_lookup(priced_at, alt_strike, alt_type)
                if alt is None:
                    continue
                self._log(candle, "strike_fallback", rung=stage.rung, strike=alt_strike, premium=alt)
                strike, option_type, premium = alt_strike, alt_type, alt
                break
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
        order_id = bracket_id = None
        if self.executor is not None and premium is not None and float(premium) > 0:
            # A net, not a rule: this ladder's own stop is an INDEX level
            # (`stop_loss_pct` off the mother) and the engine still owns it.
            # What rests at Dhan is a premium a long option only reaches in a
            # collapse -- there for the minutes nothing here can act.
            stop = max(0.05, round(float(premium) * 0.30, 2))
            try:
                receipt = self.executor.buy(
                    when=priced_at,
                    strike=int(strike),
                    expiry=self.expiry,
                    option_type=option_type,
                    quantity=int(quantity),
                    premium=float(premium),
                    stop_price=stop,
                )
            except ExecutionRefused as exc:
                # Nothing is working at the broker. The rung keeps waiting.
                self._log(candle, "rung_not_sent", rung=stage.rung, detail=str(exc))
                stage.stop = None
                stage.armed_at = None
                if not self.fills:
                    self.status = "WAITING_TWO_RED"
                return
            except Exception as exc:
                # UNKNOWN. Record no leg and stop deciding: a phantom rung and
                # a missing real one are both wrong, and only a human looking
                # at the Dhan book can tell which happened.
                self.frozen_reason = f"rung {stage.rung} entry outcome unknown -- {exc}"
                self._log(candle, "rung_send_unknown", rung=stage.rung, detail=str(exc))
                stage.stop = None
                stage.armed_at = None
                return
            order_id = str(receipt.get("order_id") or "") or None
            bracket_id = str(receipt.get("bracket_order_id") or "") or None
            if order_id:
                self._legs_open.add(order_id)
            if bracket_id:
                self._brackets_open.add(bracket_id)
            if receipt.get("traded_premium"):
                premium = float(receipt["traded_premium"])
            if receipt.get("traded_quantity"):
                quantity = int(receipt["traded_quantity"])
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
                order_id=order_id,
                bracket_order_id=bracket_id,
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

    def _exit_chart(self, candle: LadderCandle) -> bool:
        """May this bar close the basket?

        Only the chart the campaign was started on, and only after the last
        buy -- see the note in `on_candle` for the two trades that proved it.

        THE FIRST RUNG'S CHART, not the mother's own. On the options page they
        are the same bar, but the equity page reads a DAILY mother and trades
        the ladder on 1h, so anchoring to the mother left that strategy with no
        chart allowed to sell at all (its own tests caught it).
        """
        return candle.timeframe == self.stages[0].timeframe and self._after_last_fill(candle)

    def _after_last_fill(self, candle: LadderCandle) -> bool:
        """Did this bar begin after the last rung filled?

        The fill happens at its own bar's close, so that bar's own high is
        already spent, and any slower bar containing it is part history.
        """
        if not self.fills:
            return True
        return candle.timestamp > self.fills[-1].timestamp

    def _target_reached(self, candle: LadderCandle) -> bool:
        target = self.target_index
        if target is None:
            return False
        # Not allowed to leave yet -- unless the ladder has run out of charts
        # to climb, in which case there is nothing left to wait for.
        if len(self.fills) < self.min_buys_before_exit and self.active < len(self.stages):
            return False
        # The bar's best price IN THE CAMPAIGN'S FAVOUR: its high on a CE,
        # its low on a PE.
        best_of_bar = float(candle.high) if self.direction == "CE" else float(candle.low)
        reached = best_of_bar >= target if self.direction == "CE" else best_of_bar <= target

        if not self.trail_fraction:
            if not reached:
                return False
            self._close(candle, target, "target")
            return True

        if not self._trail_armed:
            if not reached:
                return False
            self._trail_armed = True
            self._trail_best = best_of_bar
            self._log(candle, "trail_armed", target=target, best=self._trail_best)
            return False
        entry = self.average_entry or 0.0
        if self.direction == "CE":
            self._trail_best = max(float(self._trail_best or 0.0), best_of_bar)
            stop = self._trail_best - (self._trail_best - entry) * self.trail_fraction
            if float(candle.close) > stop:
                return False
        else:
            self._trail_best = min(float(self._trail_best or best_of_bar), best_of_bar)
            stop = self._trail_best + (entry - self._trail_best) * self.trail_fraction
            if float(candle.close) < stop:
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
        if self.executor is not None and self._legs_open:
            if not self._sell_basket_for_real(closed_at(candle)):
                # NOT closed. The basket stays held and the campaign stays
                # open: writing an exit that did not happen is how a ledger
                # stops describing the account.
                return
        self.exit_timestamp = closed_at(candle)
        # WHICH CHART CLOSED IT: still worth recording, since a hold time only
        # makes sense against the chart the exit was read on.
        self.exit_timeframe = candle.timeframe
        self.exit_index_price = float(index_price)
        self.exit_reason = reason
        priced = [fill for fill in self.fills if fill.option_premium is not None]
        # AND PRICED THERE TOO -- the sale is read at the same instant it is
        # stamped, which is when a paper campaign actually sells.
        self.exit_priced_at = priced_at or closed_at(candle)
        # EACH CONTRACT IS SOLD AT ITS OWN PRICE. One strike for the whole
        # ladder is the common case and this reduces to it exactly; with the
        # strike chosen at every buy (Phil, 2026-08-19) the basket holds up to
        # three contracts, each priced and charged on its own and summed.
        groups: dict[tuple[int, str], list[LadderFill]] = {}
        for fill in self.fills:
            groups.setdefault((fill.strike, fill.option_type), []).append(fill)
        sells = {key: self.premium_lookup(self.exit_priced_at, key[0], key[1]) for key in groups}
        first_key = (self.fills[0].strike, self.fills[0].option_type)
        self.exit_premium = sells[first_key]
        self.exit_premiums = {f"{key[0]}{key[1]}": value for key, value in sells.items()}
        if any(value is None for value in sells.values()) or len(priced) != len(self.fills):
            # An unpriced leg makes the whole basket's P&L a guess. Report the
            # exit and leave the money blank rather than quietly costing the
            # trade as though the missing leg were free.
            self.status = "CLOSED"
            self._log(candle, "closed_unpriced", reason=reason)
            return
        gross = 0.0
        parts = []
        for key, fills in groups.items():
            sell = float(sells[key])
            parts.append(
                calculate_nifty_option_basket_round_costs(
                    buys=[OptionCostFill(fill.option_premium, fill.quantity, fill.lots) for fill in fills],
                    sell_price=sell,
                    sell_quantity=sum(fill.quantity for fill in fills),
                    sell_lots=sum(fill.lots for fill in fills),
                )
            )
            gross += sum((sell - float(fill.option_premium)) * fill.quantity for fill in fills)
        self.costs = _sum_costs(parts)
        self.gross_pnl = round(gross, 2)
        self.net_pnl = round(self.gross_pnl - self.costs.total, 2)
        self.status = "CLOSED"
        self._log(candle, "closed", reason=reason, net_pnl=self.net_pnl)

    def _sell_basket_for_real(self, when: datetime) -> bool:
        """Sell every held leg at the broker. False means the basket is NOT flat.

        The brackets come off FIRST and all of them, before a single leg is
        sold: a stop still working at Dhan against a position that is gone is
        a short nobody asked for. A leg one of those brackets already sold is
        recorded at its price rather than sold again.
        """
        release = getattr(self.executor, "cancel_bracket", None)
        for fill in self.fills:
            if not fill.bracket_order_id or fill.bracket_order_id not in self._brackets_open:
                continue
            if release is None:
                continue
            try:
                outcome = release(order_id=fill.bracket_order_id)
            except Exception as exc:
                self.frozen_reason = f"bracket release failed -- {exc}"
                return False
            self._brackets_open.discard(fill.bracket_order_id)
            if isinstance(outcome, dict) and outcome.get("traded"):
                # One of its legs already sold this rung. Nothing left to sell.
                self._legs_open.discard(str(fill.order_id or ""))
        for fill in self.fills:
            if not fill.order_id or fill.order_id not in self._legs_open:
                continue
            try:
                receipt = self.executor.sell(
                    when=when,
                    strike=int(fill.strike),
                    expiry=self.expiry,
                    option_type=fill.option_type,
                    quantity=int(fill.quantity),
                )
            except Exception as exc:
                self.frozen_reason = f"exit outcome unknown -- {exc}"
                return False
            status = str((receipt or {}).get("status") or "UNKNOWN").upper()
            if status == "FILLED":
                self._legs_open.discard(fill.order_id)
                continue
            if status == "REJECTED":
                # Nothing is working; the next bar takes the exit again.
                return False
            self.frozen_reason = f"exit outcome unknown at Dhan (order {receipt.get('order_id')})"
            return False
        return True

    def mark_open(self, at: datetime) -> Optional[dict]:
        """The open basket, priced as if it were sold at `at`.

        The monitor had deployed capital and nothing else: a basket could be
        thousands up or down and the page said the same either way (Phil,
        2026-08-20: "Now I don't know whether I started paper or not"). This
        is the CLOSE's own arithmetic -- each contract read on its own quote,
        the whole NSE charge schedule on the round trip -- so what the tile
        shows is what the campaign would book, not a gross figure that
        flatters it. `net_pnl` is None when any leg has no quote, because one
        unpriced leg makes the basket a guess; the legs are still returned so
        the ladder table can show what did price.
        """
        if not self.fills or self.exit_timestamp is not None:
            return None
        groups: dict[tuple[int, str], list[LadderFill]] = {}
        for fill in self.fills:
            groups.setdefault((fill.strike, fill.option_type), []).append(fill)
        quotes = {key: self.premium_lookup(at, key[0], key[1]) for key in groups}
        priced = all(value is not None for value in quotes.values()) and all(
            fill.option_premium is not None for fill in self.fills
        )
        legs = []
        parts: list[OptionRoundCosts] = []
        gross = 0.0
        deployed = 0.0
        for key, fills in groups.items():
            quote = quotes[key]
            quantity = sum(fill.quantity for fill in fills)
            paid = sum(float(fill.option_premium) * fill.quantity for fill in fills if fill.option_premium is not None)
            deployed += paid
            leg_gross = (
                sum((float(quote) - float(fill.option_premium)) * fill.quantity for fill in fills) if priced else None
            )
            legs.append(
                {
                    "strike": key[0],
                    "option_type": key[1],
                    "quantity": quantity,
                    "paid": round(paid / quantity, 2) if quantity else None,
                    "mark": round(float(quote), 2) if quote is not None else None,
                    "gross_pnl": round(leg_gross, 2) if leg_gross is not None else None,
                }
            )
            if priced:
                gross += float(leg_gross)
                parts.append(
                    calculate_nifty_option_basket_round_costs(
                        buys=[OptionCostFill(fill.option_premium, fill.quantity, fill.lots) for fill in fills],
                        sell_price=float(quote),
                        sell_quantity=quantity,
                        sell_lots=sum(fill.lots for fill in fills),
                    )
                )
        costs = _sum_costs(parts) if parts else None
        net = round(gross - costs.total, 2) if costs is not None else None
        return {
            "at": at.isoformat(),
            "legs": legs,
            "deployed_inr": round(deployed, 2),
            "gross_pnl": round(gross, 2) if priced else None,
            "costs_total": round(costs.total, 2) if costs is not None else None,
            "net_pnl": net,
            "return_pct": round(100 * net / deployed, 2) if net is not None and deployed else None,
            "unpriced": not priced,
        }

    def run(self, candles: list[LadderCandle]) -> "TwoRedLadder":
        for candle in order_events(candles):
            self.on_candle(candle)
        return self
