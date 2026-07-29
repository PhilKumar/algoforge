"""
engine/cascade_fib_geometry.py -- the manual-mother fib-boundary geometry.

One tiny, dependency-free module so the batch backtester
(:mod:`engine.cascade_fib_boundary`) and the incremental paper engine
(:class:`engine.cascade_options.FibBoundaryPaper`) price the SAME lines from the
SAME rules.  Keeping it here, below both callers, avoids an import cycle and a
second copy of the level maths that could silently drift.

A buy may happen only on deep fib lines measured straight off the typed mother
candle:

    CE:  price = mother_high - level * (mother_high - mother_low)
    PE:  price = mother_low  + level * (mother_high - mother_low)

Which levels trade depends on the timeframe the mother was read on:

    1m / 5m   -> (4, 8)      only the two deepest lines; shallow bounces are noise
    15m / 1h  -> (2, 4, 8)   the move is structural enough to start one step earlier
"""

from __future__ import annotations

VALID_TIMEFRAMES: frozenset[str] = frozenset({"1m", "5m", "15m", "1h"})

DEEP_FIB_BOUNDARIES: tuple[int, ...] = (4, 8)
STRUCTURAL_FIB_BOUNDARIES: tuple[int, ...] = (2, 4, 8)


class FibGeometryError(ValueError):
    """Raised for an invalid timeframe or mother range."""


def normalise_timeframe(timeframe: str) -> str:
    tf = str(timeframe).strip().lower()
    if tf not in VALID_TIMEFRAMES:
        raise FibGeometryError("timeframe must be 1m, 5m, 15m or 1h")
    return tf


def boundaries_for_timeframe(timeframe: str) -> tuple[int, ...]:
    """The fib levels a buy may happen on, given the timeframe it was read on."""
    tf = normalise_timeframe(timeframe)
    if tf in {"1m", "5m"}:
        return DEEP_FIB_BOUNDARIES
    return STRUCTURAL_FIB_BOUNDARIES


def boundary_price(option_type: str, mother_high: float, mother_low: float, level: int) -> float:
    """Index price of a fib boundary, on the side the option trades."""
    if mother_high <= mother_low:
        raise FibGeometryError("mother_high must be greater than mother_low")
    span = mother_high - mother_low
    if str(option_type).upper() == "CE":
        return mother_high - level * span
    return mother_low + level * span
