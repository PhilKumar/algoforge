"""
engine/fib_spaces.py -- where two fibs' levels converge, money may be placed.

Phil's correction, verbatim (2026-08-01): "I am explicitly telling it about
2 fibs converging levels not 1."  A buy zone is NOT a gap between two levels of
one fib.  It is the SPACE between a level of one fib and a level of ANOTHER --
the "2-4 or 4-8 or 2-8" he names, whichever two levels the drawn fibs happen to
land next to each other.

Both charts he sent are the same shape: a tight fib's deep level meeting a wide
fib's shallow one.

    26 May - 8 Jun 2026   small fib L8 23,740.95  ->  big fib L2 23,713.70
                          27.25 points wide
    25 Jun -  3 Jul 2026  fib B L2   23,890.90  ->  fib A L8  23,888.25
                          2.65 points wide

The width is what decides how the space is traded, and he gave both cases:

  * a WIDE space is worked from its top level down to about its middle --
    "from top near 50%"; below halfway it is too deep and the next space owns
    the money;
  * a TINY space has no meaningful middle -- "One is very small.. so you can
    buy it on the touch of the 2 levels" -- so the whole space is the trigger.

Spaces are numbered from the top down.  The first is skipped; the 2nd and 3rd
are the ones that trade ("2nd - 3rd boundary 50% or before works better").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from engine.fib_space_geometry import FIB_LEVELS, DrawnFib

__all__ = [
    "Space",
    "TINY_SPACE_POINTS",
    "find_spaces",
    "tradable_spaces",
]

# Below this width a space has no usable middle: 50% of two points is inside
# the tick, and Phil's own reading of the 2.65-point space was "buy it on the
# touch of the 2 levels".  NIFTY's tick is 0.05 and a 15m bar runs tens of
# points, so single-digit points is genuinely one line drawn twice.
TINY_SPACE_POINTS = 10.0


@dataclass(frozen=True)
class Space:
    """One converging pair: a level from fib A meeting a level from fib B."""

    top_price: float
    bottom_price: float
    top_fib_id: int
    bottom_fib_id: int
    top_level: int
    bottom_level: int

    @property
    def width(self) -> float:
        return self.top_price - self.bottom_price

    @property
    def is_tiny(self) -> bool:
        return self.width < TINY_SPACE_POINTS

    @property
    def midpoint(self) -> float:
        return (self.top_price + self.bottom_price) / 2.0

    @property
    def label(self) -> str:
        """How Phil names them: the two level numbers, shallow-first."""
        return f"{self.top_level}-{self.bottom_level}"

    @property
    def buy_floor(self) -> float:
        """The deepest price this space may still be bought at.

        Wide space: its middle.  Tiny space: its own bottom level, because the
        whole thing is a touch.
        """
        return self.bottom_price if self.is_tiny else self.midpoint

    def contains_buy(self, price: float) -> bool:
        """Is ``price`` inside this space's buy zone?"""
        return self.buy_floor <= price <= self.top_price


def find_spaces(fibs: Sequence[DrawnFib], levels: Iterable[int] = FIB_LEVELS) -> list[Space]:
    """Every converging pair among the drawn fibs, deepest-last.

    All levels of all fibs are sorted by price; a space is an adjacent pair
    whose two levels come from DIFFERENT fibs.  Two adjacent levels of the same
    fib are not a boundary -- that was the misreading Phil corrected.
    """
    if len(fibs) < 2:
        return []
    marks: list[tuple[float, int, int]] = []
    for fib in fibs:
        for level in levels:
            marks.append((fib.level_price(level), fib.fib_id, int(level)))
    marks.sort(key=lambda row: -row[0])

    spaces: list[Space] = []
    for upper, lower in zip(marks, marks[1:]):
        if upper[1] == lower[1]:
            continue  # same fib: a gap inside one structure is not a boundary
        spaces.append(
            Space(
                top_price=upper[0],
                bottom_price=lower[0],
                top_fib_id=upper[1],
                bottom_fib_id=lower[1],
                top_level=upper[2],
                bottom_level=lower[2],
            )
        )
    return spaces


def tradable_spaces(spaces: Sequence[Space], *, below: Optional[float] = None) -> list[Space]:
    """The spaces money may go into: the DEEPEST TWO.

    "let the last 2 spaces come into effect for the buy" -- so with three
    boundaries the 2nd and 3rd trade, which is the same sentence as his
    "2nd - 3rd boundary 50% or before works better".

    Taking the last two rather than literally skipping index 0 is what his own
    chart forces: the 26 May window converges exactly ONCE, and he said of that
    single space "The first one is above 50% so that can be taken".  A rule
    that skipped the topmost space would have traded nothing there.

    ``below`` restricts to spaces the market has not already passed.
    """
    live = [s for s in spaces if below is None or s.top_price <= below]
    return live[-2:]
