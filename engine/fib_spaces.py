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
    """A place money may sit.

    Normally a converging pair -- a level from fib A meeting a level from fib B.
    When only ONE fib ever gets drawn there is nothing to converge with, and
    Phil's fallback applies: "that time we can follow the single fib levels
    rule".  Such a zone carries ``kind="level"``, has no width, and is bought
    when price trades beyond the line itself.
    """

    top_price: float
    bottom_price: float
    top_fib_id: int
    bottom_fib_id: int
    top_level: int
    bottom_level: int
    kind: str = "space"
    # How far below a LONE LEVEL a close may sit and still claim the level.
    # 0.0 means only the line itself.  single_fib_levels sets half the fib's
    # span -- the same top-to-middle working a wide space gets.
    depth: float = 0.0

    @property
    def is_level(self) -> bool:
        return self.kind == "level"

    @property
    def width(self) -> float:
        return self.top_price - self.bottom_price

    @property
    def is_tiny(self) -> bool:
        return not self.is_level and self.width < TINY_SPACE_POINTS

    @property
    def midpoint(self) -> float:
        return (self.top_price + self.bottom_price) / 2.0

    @property
    def label(self) -> str:
        """How Phil names them: the two level numbers, shallow-first."""
        if self.is_level:
            return f"L{self.top_level}"
        return f"{self.top_level}-{self.bottom_level}"

    @property
    def buy_floor(self) -> float:
        """The deepest price this space may still be bought at.

        Wide space: its middle.  Tiny space: its own bottom level, because the
        whole thing is a touch.  A lone level is bought on the TOUCH of the
        line, so its zone runs half a fib-span under it and no further.  The
        first cut of this returned -inf ("price beyond the line is the
        trigger, however far it runs"), and on BankNifty's 22-Apr-2026 mother
        that let a close 494 points under L4 claim the level -- the campaign
        bought the first small dip's lines from half a kilometre below and
        then owned entries Phil's own chart never took.
        """
        if self.is_level:
            return self.top_price - self.depth
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


def tradable_spaces(
    spaces: Sequence[Space], *, below: Optional[float] = None, reached: Optional[float] = None
) -> list[Space]:
    """The spaces money may go into: the DEEPEST TWO.

    "let the last 2 spaces come into effect for the buy" -- so with three
    boundaries the 2nd and 3rd trade, which is the same sentence as his
    "2nd - 3rd boundary 50% or before works better".

    Taking the last two rather than literally skipping index 0 is what his own
    chart forces: the 26 May window converges exactly ONCE, and he said of that
    single space "The first one is above 50% so that can be taken".  A rule
    that skipped the topmost space would have traded nothing there.

    ``below`` restricts to spaces the market has not already passed.

    ``reached`` is the fall's lowest low so far: only spaces the fall has
    actually ENTERED are boundaries yet.  Without it, every new fib deepens
    the stack and "the deepest two" chases ground thousands of points under
    the market -- on Phil's 22-Apr-2026 BankNifty mother the 5-May entry his
    chart takes at ~54,360 was refused because two untouched spaces sat near
    48-51k, and the campaign funded nothing for the rest of the fall.
    """
    live = [s for s in spaces if below is None or s.top_price <= below]
    if reached is not None:
        live = [s for s in live if s.top_price >= reached]
    return live[-2:]


def single_fib_levels(fib, levels: Iterable[int] = (4, 8)) -> list[Space]:
    """Phil's fallback when the market never gave a second structure.

    "That will happen if the market comes without any move upward.. that time
    we can follow the single fib levels rule."  One fib cannot converge with
    anything, so its own deep lines are the buy zones -- the same L4/L8 pair
    the locked config trades on every chart above 1m.
    """
    return [
        Space(
            top_price=fib.level_price(level),
            bottom_price=fib.level_price(level),
            top_fib_id=fib.fib_id,
            bottom_fib_id=fib.fib_id,
            top_level=int(level),
            bottom_level=int(level),
            kind="level",
            depth=0.5 * fib.span,
        )
        for level in levels
    ]


def tradable_zones(fibs: Sequence, *, below: Optional[float] = None, reached: Optional[float] = None) -> list[Space]:
    """Where money may go, whichever geometry the market actually produced.

    Two or more fibs that converge -> the deepest two spaces the fall has
    reached.  Otherwise the lone (most recent) structure's own L4 and L8 --
    a level needs no ``reached`` gate because its buy zone is only half a
    span deep, so a close cannot be inside it before price has come there.
    """
    spaces = tradable_spaces(find_spaces(fibs), below=below, reached=reached)
    if spaces:
        return spaces
    if not fibs:
        return []
    return single_fib_levels(fibs[-1])
