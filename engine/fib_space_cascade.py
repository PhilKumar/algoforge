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
  * one 0.25 pullback target closes the whole basket and ENDS the campaign.

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
from engine.fib_spaces import Space, find_spaces, tradable_spaces

__all__ = ["SpaceCascadeConfig", "SpaceFill", "SpaceCampaignResult", "run_space_campaign"]


@dataclass(frozen=True)
class SpaceCascadeConfig:
    lot_size: int = 65
    target_fraction: float = 0.25
    max_fills: int = 3
    # "avg_entry": 0.25 from the average entry up toward the mother high (what
    # the shipped engines do).  "structure": 0.25 from the basket's lowest fill
    # up toward the mother high, which is how the 0.25 line was drawn on Phil's
    # own 3 Jul screenshot.  Measured both ways because they disagree.
    target_mode: str = "avg_entry"
    # Bars the basket may be held before it is closed at the market, whatever
    # the index has done.  0 = no time stop (ride to target or expiry).  With
    # no stop loss and a monthly option, expiry is otherwise the only thing
    # that can end a losing campaign -- and it ends it at a total loss.
    max_bars_held: int = 0

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
class SpaceCampaignResult:
    mother_timestamp: datetime
    mother_high: float
    mother_low: float
    status: str = "no_entry"
    fills: list[SpaceFill] = field(default_factory=list)
    exit_timestamp: Optional[datetime] = None
    exit_index: Optional[float] = None
    exit_reason: Optional[str] = None
    target_index: Optional[float] = None
    average_entry: Optional[float] = None
    index_points: Optional[float] = None  # per-unit index move, entry -> exit
    weighted_points: Optional[float] = None  # index points x quantity
    fib_count: int = 0
    space_count: int = 0
    bars_held: int = 0

    @property
    def quantity(self) -> int:
        return sum(f.quantity for f in self.fills)


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
    first_fill_index: Optional[int] = None

    for bar in sorted(bars, key=lambda b: b.index):
        if bar.index <= mother.index:
            continue
        geometry.on_bar(bar)

        # --- fill an armed buy-stop: price recovers back through the trigger
        if pending_trigger is not None and pending_index is not None and bar.index > pending_index:
            if bar.high >= pending_trigger:
                lots = config.lots_for(len(result.fills))
                assert pending_space is not None
                result.fills.append(
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
                pending_trigger = pending_space = pending_index = None
                streak = 0
                result.status = "open"

        # --- the 0.25 target closes the whole basket and ends the campaign
        if result.fills:
            average = _average_entry(result.fills)
            if config.target_mode == "structure":
                anchor = min(f.index_price for f in result.fills)
            else:
                anchor = average
            target = anchor + config.target_fraction * (mother.high - anchor)
            result.target_index = target
            if bar.high >= target and first_fill_index is not None and bar.index > first_fill_index:
                result.status = "closed"
                result.exit_reason = "target"
                result.exit_timestamp = bar.timestamp
                result.exit_index = target
                result.average_entry = average
                result.index_points = target - average
                result.weighted_points = sum((target - f.index_price) * f.quantity for f in result.fills)
                result.bars_held = bar.index - first_fill_index
                break
            # The time stop sells at this bar's close, wherever the index is.
            if (
                config.max_bars_held
                and first_fill_index is not None
                and bar.index - first_fill_index >= config.max_bars_held
            ):
                result.status = "closed"
                result.exit_reason = "time_stop"
                result.exit_timestamp = bar.timestamp
                result.exit_index = bar.close
                result.average_entry = average
                result.index_points = bar.close - average
                result.weighted_points = sum((bar.close - f.index_price) * f.quantity for f in result.fills)
                result.bars_held = bar.index - first_fill_index
                break

        # --- arm the next buy: two red closes, inside a live space's buy zone
        if len(result.fills) >= config.max_fills:
            continue
        if arm_from_index is not None and bar.index < arm_from_index:
            continue
        if not bar.is_red:
            streak = 0
            continue
        streak += 1
        if streak < 2 or pending_trigger is not None:
            continue
        spaces = find_spaces(geometry.fibs)
        if not spaces:
            continue
        for space in tradable_spaces(spaces):
            # Never re-buy a space this campaign already used.
            if any(abs(f.space_top - space.top_price) < 1e-9 for f in result.fills):
                continue
            if space.contains_buy(bar.close):
                pending_trigger = bar.close
                pending_space = space
                pending_index = bar.index
                break

    result.fib_count = len(geometry.fibs)
    result.space_count = len(find_spaces(geometry.fibs))
    if result.fills and result.status != "closed":
        result.status = "open_at_end"
        result.average_entry = _average_entry(result.fills)
    return result
