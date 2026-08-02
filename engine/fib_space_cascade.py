"""
engine/fib_space_cascade.py -- trade the spaces where two fibs converge.

The strategy Phil specified on 2026-08-01, on top of
:mod:`engine.fib_space_geometry` (his adjudicated trendline+fib rule) and
:mod:`engine.fib_spaces` (the converging-level boundaries):

  * geometry draws itself as the fall develops -- no typed mother levels;
  * money may only sit in a SPACE, and only in the deepest two;
  * inside a wide space the buy zone is its top down to about the middle;
    a space too small to have a middle is bought on touch;
  * the fill itself is the cascade mechanic everywhere else uses -- two red
    closes arm a buy-stop at the second close, and it fills when price trades
    back up through it;
  * size is a LOT LADDER, not a rupee budget: 1 lot on the first buy, 2 on the
    second, 3 on the third;
  * one 0.25 pullback target closes the whole basket and ends the ROUND --
    not the campaign.

ROUNDS.  A mother's fall is traded in rounds, exactly like the crypto cascade:
the 0.25 target banks the basket, and the SAME mother's geometry keeps running.
The next round may only arm on a close BELOW the previous round's low -- the
fixed cascade invariant -- so a re-entry is always deeper, never a re-buy of
ground already banked.  Phil's 22-Apr-2026 BankNifty chart is the adjudicating
fixture: two arrows, "2 trades taken and profit target hit twice on that
trade" -- entry near the fib on 24 Apr banked on the 27 Apr rally, then a fresh
entry at the L8 touch on 5 May banked on 6 May.  The single-round engine sat
through both rallies holding its first basket and delivered the loss his chart
does not have.

"The already-allocated lots has to move down in the boundaries" falls out of
this for free rather than needing machinery: nothing is pre-placed, so when a
new fib draws deeper spaces the next lot simply goes there, while lots already
bought keep the price they paid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from engine.fib_space_geometry import Bar, SpaceGeometry
from engine.fib_spaces import Space, find_spaces, tradable_zones

__all__ = [
    "SpaceCascadeConfig",
    "SpaceFill",
    "SpaceRound",
    "SpaceCampaignResult",
    "run_space_campaign",
]


@dataclass(frozen=True)
class SpaceCascadeConfig:
    lot_size: int = 65
    target_fraction: float = 0.25
    # Per ROUND: the ladder is 1/2/3 lots and restarts when a round banks.
    max_fills: int = 3
    # "avg_entry": 0.25 from the average entry up toward the mother high (what
    # the shipped engines do).  "structure": 0.25 from the basket's lowest fill
    # up toward the mother high, which is how the 0.25 line was drawn on Phil's
    # own 3 Jul screenshot.  Measured both ways because they disagree.
    # "retrace" is Phil's own chart rule, adjudicated 2026-08-02 against his
    # 26-Feb-2026 11:15 mother: he draws the 0.25 from the LOWEST POINT OF THE
    # FALL back up toward the mother high, not from the average entry.  On that
    # campaign his line sits at 24,649.85 (24,342.85 + 0.25 x (25,570.85 -
    # 24,342.85)) while an avg-entry target sits 406 points higher at 25,056 --
    # the market cleared his and never came near mine, so the avg-entry rule
    # manufactured an expiry loss out of a winning trade.
    target_mode: str = "retrace"
    # Bars a round's basket may be held before it is closed at the market,
    # whatever the index has done.  0 = no time stop (ride to target or
    # expiry).  With no stop loss and a monthly option, expiry is otherwise the
    # only thing that can end a losing round -- and it ends it at a total loss.
    max_bars_held: int = 0
    # WHEN THE FALL BLOWS THROUGH EVERY LEVEL.  A gap or a crash can leave the
    # market hundreds of points below the deepest line the geometry has drawn,
    # and then no zone contains the close, so no money goes in.  Phil's charts
    # show him buying anyway: on 09-Mar-2026 BankNifty gapped 1,661 points and
    # his entry sits ~1,600 points under the deepest level of the fibs that
    # existed.  With this on, the DEEPEST live zone claims everything beneath
    # it, so the two-candle rule alone governs the entry down there.  Measured
    # rather than assumed -- it widens the strategy considerably.
    deepest_zone_open_ended: bool = False

    def lots_for(self, fill_index: int) -> int:
        return int(fill_index) + 1


@dataclass(frozen=True)
class SpaceFill:
    timestamp: datetime
    index_price: float
    lots: int
    quantity: int
    space_label: str
    space_top: float
    space_bottom: float
    space_width: float
    on_touch: bool  # a tiny space, bought on touch rather than into a half


@dataclass
class SpaceRound:
    """One basket: its fills, its 0.25 target, and how it ended."""

    fills: list[SpaceFill] = field(default_factory=list)
    status: str = "open"  # open | closed
    exit_timestamp: Optional[datetime] = None
    exit_index: Optional[float] = None
    exit_reason: Optional[str] = None
    target_index: Optional[float] = None
    average_entry: Optional[float] = None
    index_points: Optional[float] = None  # per-unit index move, entry -> exit
    weighted_points: Optional[float] = None  # index points x quantity
    bars_held: int = 0
    # The fall's lowest low when this round ended -- the floor under which,
    # and only under which, the next round may arm.
    low_at_close: Optional[float] = None

    @property
    def quantity(self) -> int:
        return sum(f.quantity for f in self.fills)


@dataclass
class SpaceCampaignResult:
    mother_timestamp: datetime
    mother_high: float
    mother_low: float
    status: str = "no_entry"
    rounds: list[SpaceRound] = field(default_factory=list)
    fib_count: int = 0
    space_count: int = 0

    @property
    def fills(self) -> list[SpaceFill]:
        """Every fill of every round, in time order."""
        return [f for r in self.rounds for f in r.fills]

    @property
    def quantity(self) -> int:
        return sum(r.quantity for r in self.rounds)

    @property
    def closed_rounds(self) -> list[SpaceRound]:
        return [r for r in self.rounds if r.status == "closed"]

    @property
    def open_round(self) -> Optional[SpaceRound]:
        last = self.rounds[-1] if self.rounds else None
        return last if last is not None and last.status != "closed" else None

    # -- aggregates the layer-1 tools read ---------------------------------
    @property
    def index_points(self) -> Optional[float]:
        pts = [r.index_points for r in self.closed_rounds if r.index_points is not None]
        return sum(pts) if pts else None

    @property
    def weighted_points(self) -> Optional[float]:
        pts = [r.weighted_points for r in self.closed_rounds if r.weighted_points is not None]
        return sum(pts) if pts else None

    @property
    def bars_held(self) -> int:
        return sum(r.bars_held for r in self.rounds)

    # -- last-round conveniences (chart/debug scripts) ---------------------
    @property
    def exit_timestamp(self) -> Optional[datetime]:
        return self.rounds[-1].exit_timestamp if self.rounds else None

    @property
    def exit_reason(self) -> Optional[str]:
        return self.rounds[-1].exit_reason if self.rounds else None

    @property
    def target_index(self) -> Optional[float]:
        return self.rounds[-1].target_index if self.rounds else None

    @property
    def average_entry(self) -> Optional[float]:
        return self.rounds[-1].average_entry if self.rounds else None


def _average_entry(fills: Sequence[SpaceFill]) -> Optional[float]:
    quantity = sum(f.quantity for f in fills)
    if quantity <= 0:
        return None
    return sum(f.index_price * f.quantity for f in fills) / quantity


def run_space_campaign(
    mother: Bar,
    bars: Sequence[Bar],
    config: SpaceCascadeConfig = SpaceCascadeConfig(),
    *,
    arm_from_index: Optional[int] = None,
) -> SpaceCampaignResult:
    """Replay one mother's window. Geometry, spaces and fills all step together.

    Every decision is made from bars already closed: the geometry is advanced
    with the current bar first, and only then may that same bar arm or fill.
    A space cannot exist before the fib that makes it is drawn, so there is no
    way for a level to be known early.

    ``arm_from_index`` is the first bar at which a live system could have known
    the mother was a mother -- a swing high is only a swing high once its right
    shoulder has printed.  Geometry still runs from the mother itself (it must,
    the anchor is the mother high), but no money may be committed before that
    bar.  Without this the sweep quietly credits a pivot to the bars that
    confirmed it.
    """
    geometry = SpaceGeometry(mother=mother)
    result = SpaceCampaignResult(mother_timestamp=mother.timestamp, mother_high=mother.high, mother_low=mother.low)

    streak = 0
    pending_trigger: Optional[float] = None
    pending_space: Optional[Space] = None
    pending_index: Optional[int] = None
    round_fills: list[SpaceFill] = []
    first_fill_index: Optional[int] = None
    lowest: Optional[float] = None
    # The retrace anchor is the low as it stood when the LAST lot went on, and
    # it does not move between fills.  Letting it trail every new low walked the
    # 11-Feb-2026 target down to 23,139 -- unreachable -- so a campaign that
    # should have closed in profit ran to expiry and lost.  Phil's 22-Apr
    # BankNifty zeros agree: 55,917.45 and 54,317.45 are each the low AT THE
    # FILL, not the deeper lows the fall went on to make.
    anchor_low: Optional[float] = None
    # The previous round's low: the next round arms only below it.
    round_floor: Optional[float] = None

    def _close_round(bar: Bar, reason: str, exit_level: float, average: float) -> None:
        nonlocal round_fills, first_fill_index, anchor_low, round_floor
        nonlocal streak, pending_trigger, pending_space, pending_index
        rnd = SpaceRound(
            fills=round_fills,
            status="closed",
            exit_timestamp=bar.timestamp,
            exit_index=exit_level,
            exit_reason=reason,
            target_index=result_target,
            average_entry=average,
            index_points=exit_level - average,
            weighted_points=sum((exit_level - f.index_price) * f.quantity for f in round_fills),
            bars_held=bar.index - first_fill_index if first_fill_index is not None else 0,
            low_at_close=lowest,
        )
        result.rounds.append(rnd)
        round_floor = lowest
        round_fills = []
        first_fill_index = None
        anchor_low = None
        streak = 0
        pending_trigger = pending_space = pending_index = None

    result_target: Optional[float] = None

    for bar in sorted(bars, key=lambda b: b.index):
        if bar.index <= mother.index:
            continue
        geometry.on_bar(bar)

        # --- fill an armed buy-stop: price recovers back through the trigger
        if pending_trigger is not None and pending_index is not None and bar.index > pending_index:
            if bar.high >= pending_trigger:
                lots = config.lots_for(len(round_fills))
                assert pending_space is not None
                round_fills.append(
                    SpaceFill(
                        timestamp=bar.timestamp,
                        index_price=pending_trigger,
                        lots=lots,
                        quantity=lots * config.lot_size,
                        space_label=pending_space.label,
                        space_top=pending_space.top_price,
                        space_bottom=pending_space.bottom_price,
                        space_width=pending_space.width,
                        on_touch=pending_space.is_tiny,
                    )
                )
                if first_fill_index is None:
                    first_fill_index = bar.index
                # The retrace is re-drawn from the low each time a lot is added,
                # then held until the next one.  Phil's 26-Feb mother pins this:
                # his zero is 24,342.85, the low at the moment the second lot
                # went on, giving the 24,649.85 target his chart shows.
                anchor_low = lowest if lowest is not None else bar.low
                pending_trigger = pending_space = pending_index = None
                streak = 0

        # The fall's lowest point, tracked from the mother -- the anchor Phil
        # draws his retracement from.
        if lowest is None or bar.low < lowest:
            lowest = bar.low

        # --- the 0.25 target closes the round's basket and banks it
        if round_fills:
            average = _average_entry(round_fills)
            if config.target_mode == "retrace":
                anchor = anchor_low if anchor_low is not None else lowest
            elif config.target_mode == "structure":
                anchor = min(f.index_price for f in round_fills)
            else:
                anchor = average
            target = anchor + config.target_fraction * (mother.high - anchor)
            result_target = target
            # The basket leaves at the 0.25 retrace, but never below what it
            # paid: the exit is whichever of the two is higher.  Requiring the
            # TARGET itself to clear the average instead (the first attempt at
            # this) silently disabled the exit whenever the retrace landed a
            # few points under the average -- on BankNifty's 05-Mar-2026 mother
            # the target sat Rs 40 under it, the index then rallied 914 points
            # straight through, and a winning round was carried into a total
            # loss at expiry.
            exit_level = target if average is None else max(target, average)
            if bar.high >= exit_level and first_fill_index is not None and bar.index > first_fill_index:
                _close_round(bar, "target", exit_level, average)
                continue
            # The time stop sells at this bar's close, wherever the index is.
            if (
                config.max_bars_held
                and first_fill_index is not None
                and bar.index - first_fill_index >= config.max_bars_held
            ):
                _close_round(bar, "time_stop", bar.close, average)
                continue

        # --- arm the next buy: two red closes, inside a live space's buy zone
        if len(round_fills) >= config.max_fills:
            continue
        if arm_from_index is not None and bar.index < arm_from_index:
            continue
        # A high back above the mother ended this structure: open lots still
        # ride to their target, but no NEW money joins a finished fall.
        if geometry.finished:
            continue
        if not bar.is_red:
            streak = 0
            continue
        streak += 1
        if streak < 2 or pending_trigger is not None:
            continue
        zones = tradable_zones(geometry.fibs, reached=lowest)
        if not zones:
            continue
        for position, space in enumerate(zones):
            in_zone = space.contains_buy(bar.close)
            # See SpaceCascadeConfig.deepest_zone_open_ended: below the last
            # line the geometry drew there are no more zones, and Phil's charts
            # keep buying the two-red recoveries down there anyway.
            open_country = (
                not in_zone
                and config.deepest_zone_open_ended
                and position == len(zones) - 1
                and bar.close < space.buy_floor
            )
            if not (in_zone or open_country):
                continue
            # Never re-buy a space this round already used -- but open country
            # is not a level, it is everything under the last one, so the
            # ladder must be free to step down through it more than once.  With
            # this guard applied there too, the 23-Feb-2026 round bought once
            # and then sat, where Phil's chart adds a second lot on 09-Mar.
            if in_zone and any(abs(f.space_top - space.top_price) < 1e-9 for f in round_fills):
                continue
            # Money moves DOWN the boundaries, never back up: a second lot
            # priced above the first is not a cascade, it is averaging up.
            if round_fills and bar.close >= min(f.index_price for f in round_fills):
                continue
            # Between rounds, the cascade invariant: the next round only arms
            # BELOW the previous round's low.  Ground already banked is never
            # bought again.
            if not round_fills and round_floor is not None and bar.close >= round_floor:
                continue
            pending_trigger = bar.close
            pending_space = space
            pending_index = bar.index
            break

    result.fib_count = len(geometry.fibs)
    result.space_count = len(find_spaces(geometry.fibs))
    if round_fills:
        # The last basket never met its target inside the data.
        result.rounds.append(
            SpaceRound(
                fills=round_fills,
                status="open",
                target_index=result_target,
                average_entry=_average_entry(round_fills),
                low_at_close=lowest,
            )
        )
    if result.rounds:
        result.status = "closed" if all(r.status == "closed" for r in result.rounds) else "open_at_end"
    return result
