"""Automatic mother-candle detection for a multi-month cascade backtest.

Hand-marking mother candles works for twenty screenshots.  It does not work for
a year of NIFTY, and a backtest whose campaign starts were chosen by eye is not
a backtest of the rules -- it is a backtest of the eye.

A mother here is a swing-high pivot: a candle whose high stands above its
neighbours on both sides, whose own range is meaningful against recent ATR, and
which is far enough from the previous mother not to spawn a duplicate campaign
on the same swing.

The one rule that matters more than the pivot definition is `confirmed_at`.
A pivot cannot be recognised until `right_bars` candles have closed after it,
so a campaign may only start there -- never at the pivot's own timestamp.
Starting at the pivot would let the replay trade a high that nothing yet knew
was a high, which is the classic way a backtest invents its own edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence


class MotherScanError(ValueError):
    """Invalid scanner configuration."""


@dataclass(frozen=True)
class MotherCandidate:
    """One detected swing-high mother and the bar that confirmed it."""

    timestamp: datetime
    high: float
    low: float
    index: int  # position of the pivot in the source series
    confirmed_at: datetime  # first timestamp a live system could have known
    confirmed_index: int
    atr: float
    range_atr: float  # candle range as a multiple of ATR

    @property
    def range_points(self) -> float:
        return self.high - self.low


def true_range(current, previous) -> float:
    if previous is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def atr_series(candles: Sequence, period: int) -> list[Optional[float]]:
    """Wilder's ATR, `None` until `period` bars exist.

    `None` rather than a partial average on purpose: a half-formed ATR at the
    start of the series would wave through mothers that the same filter rejects
    everywhere else, biasing the first weeks of every run.
    """
    if period <= 0:
        raise MotherScanError("atr period must be positive")
    out: list[Optional[float]] = []
    running: Optional[float] = None
    ranges: list[float] = []
    for position, candle in enumerate(candles):
        ranges.append(true_range(candle, candles[position - 1] if position else None))
        if position + 1 < period:
            out.append(None)
        elif position + 1 == period:
            running = sum(ranges) / period
            out.append(running)
        else:
            running = (running * (period - 1) + ranges[-1]) / period
            out.append(running)
    return out


def find_mother_candles(
    candles: Sequence,
    *,
    left_bars: int = 3,
    right_bars: int = 3,
    atr_period: int = 14,
    min_range_atr: float = 0.8,
    min_separation_bars: int = 0,
    same_session_only: bool = False,
) -> list[MotherCandidate]:
    """Swing-high pivots usable as cascade mothers.

    left_bars/right_bars
        How many candles either side the pivot high must beat.  The left side
        is strict (`>`) and the right side permissive (`>=`) so a flat double
        top resolves to its first candle instead of producing two campaigns.
    min_range_atr
        Floor on candle range as a multiple of ATR.  Without it a quiet
        sideways session produces dozens of technically-valid pivots whose
        mother range is too small for the 0.25 target to clear costs.
    min_separation_bars
        Minimum gap between accepted mothers, measured on pivot index.
    same_session_only
        Require the confirming bars to fall on the pivot's own trading day, so
        an overnight gap cannot confirm a pivot.
    """
    if left_bars < 1 or right_bars < 1:
        raise MotherScanError("left_bars and right_bars must be at least 1")
    if min_range_atr < 0:
        raise MotherScanError("min_range_atr cannot be negative")

    atrs = atr_series(candles, atr_period)
    found: list[MotherCandidate] = []
    last_index: Optional[int] = None

    for position in range(left_bars, len(candles) - right_bars):
        pivot = candles[position]
        atr = atrs[position]
        if atr is None or atr <= 0:
            continue

        if any(candles[other].high >= pivot.high for other in range(position - left_bars, position)):
            continue
        if any(candles[other].high > pivot.high for other in range(position + 1, position + right_bars + 1)):
            continue

        confirm_index = position + right_bars
        confirm = candles[confirm_index]
        if same_session_only and confirm.timestamp.date() != pivot.timestamp.date():
            continue

        span = pivot.high - pivot.low
        if span < min_range_atr * atr:
            continue
        if last_index is not None and position - last_index < min_separation_bars:
            continue

        found.append(
            MotherCandidate(
                timestamp=pivot.timestamp,
                high=pivot.high,
                low=pivot.low,
                index=position,
                confirmed_at=confirm.timestamp,
                confirmed_index=confirm_index,
                atr=atr,
                range_atr=span / atr,
            )
        )
        last_index = position

    return found
