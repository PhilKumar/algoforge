"""Automatic mother-candle detection for a multi-month cascade backtest.

Hand-marking mother candles works for twenty screenshots.  It does not work for
a year of NIFTY, and a backtest whose campaign starts were chosen by eye is not
a backtest of the rules -- it is a backtest of the eye.

Two detectors live here, and they answer different questions.

`find_mother_candles` -- the SWING-HIGH pivot: a candle whose high stands above
its neighbours on both sides, whose own range is meaningful against recent ATR,
and which is far enough from the previous mother not to spawn a duplicate
campaign on the same swing.  The rule that matters more than the pivot
definition is `confirmed_at`: a pivot cannot be recognised until `right_bars`
candles have closed after it, so a campaign may only start there -- never at the
pivot's own timestamp.  Starting at the pivot would let the replay trade a high
that nothing yet knew was a high, which is the classic way a backtest invents
its own edge.

`find_wick_mothers` -- Phil's own rule, written down: a strong bullish run, then
one candle that pokes to a new high of that run and gives most of it back,
leaving an upper wick over half its range.  Red or green both count; the shape
covers the inverted hammer and the red evening-star opener.  This detector needs
NO right bars: everything it tests is known the moment the candle closes, so
`confirmed_at` is simply the next bar.  That makes it strictly free of lookahead,
unlike the pivot scanner which must wait to be sure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence


class MotherScanError(ValueError):
    """Invalid scanner configuration."""


@dataclass(frozen=True)
class MotherCandidate:
    """One detected mother candle and the bar that confirmed it."""

    timestamp: datetime
    high: float
    low: float
    index: int  # position of the pivot in the source series
    confirmed_at: datetime  # first timestamp a live system could have known
    confirmed_index: int
    atr: float
    range_atr: float  # candle range as a multiple of ATR
    # Only the wick detector fills these; the swing scanner leaves them at 0.
    upper_wick_fraction: float = 0.0  # upper wick as a share of the candle range
    run_atr: float = 0.0  # height of the bullish run into the candle, in ATRs
    run_green: int = 0  # green candles in the run window

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


def find_wick_mothers(
    candles: Sequence,
    *,
    run_bars: int = 4,
    min_run_green: int = 3,
    min_run_atr: float = 1.5,
    min_wick_fraction: float = 0.5,
    atr_period: int = 14,
    min_range_atr: float = 0.8,
    min_separation_bars: int = 0,
    same_session_only: bool = True,
) -> list[MotherCandidate]:
    """Rejection candles that end a bullish run -- Phil's mother rule.

    A candle qualifies when all of these hold at its own close:

    run_bars / min_run_green
        The `run_bars` candles immediately before it are a bullish run: at least
        `min_run_green` of them closed green.  Not all of them, because a real
        rally breathes -- one red pause inside four bars is still a rally.
    min_run_atr
        The run has to be worth rejecting.  Height is measured from the lowest
        low of the run window up to the candle's own high, as a multiple of ATR,
        so "huge" scales with the instrument instead of being a point count that
        rots as NIFTY drifts.
    min_wick_fraction
        The upper wick -- high minus the top of the body -- must be at least this
        share of the candle's whole range.  At the default 0.5 the candle gave
        back more than half of what it reached for.  Red and green both pass:
        the shape is the signal, not the colour.
    min_range_atr
        Floor on the candle's own range, same purpose as in the pivot scanner --
        a doji-sized "wick" is noise, and its 0.25 target cannot clear costs.
    same_session_only
        Keep the whole run inside one trading day.  Across an overnight gap the
        "run" is really a gap, and the wick is measuring a different market.

    The candle's high must also be the highest of the run window: a rejection
    that never made a new high is not rejecting anything.

    `confirmed_at` is the NEXT bar, always.  Every test above reads bars at or
    before the candle, so its close is the first moment a live system could know
    -- and the first bar it could act on is the one after.
    """
    if run_bars < 1:
        raise MotherScanError("run_bars must be at least 1")
    if min_run_green < 0 or min_run_green > run_bars:
        raise MotherScanError("min_run_green must be between 0 and run_bars")
    if not 0.0 < min_wick_fraction <= 1.0:
        raise MotherScanError("min_wick_fraction must be between 0 and 1")
    if min_range_atr < 0 or min_run_atr < 0:
        raise MotherScanError("ATR multiples cannot be negative")

    atrs = atr_series(candles, atr_period)
    found: list[MotherCandidate] = []
    last_index: Optional[int] = None

    for position in range(run_bars, len(candles) - 1):
        candle = candles[position]
        atr = atrs[position]
        if atr is None or atr <= 0:
            continue

        span = candle.high - candle.low
        if span <= 0 or span < min_range_atr * atr:
            continue

        body_top = max(candle.open, candle.close)
        wick_fraction = (candle.high - body_top) / span
        if wick_fraction < min_wick_fraction:
            continue

        run = list(candles[position - run_bars : position])
        if same_session_only and any(bar.timestamp.date() != candle.timestamp.date() for bar in run):
            continue
        greens = sum(1 for bar in run if bar.close > bar.open)
        if greens < min_run_green:
            continue
        if any(bar.high > candle.high for bar in run):
            continue

        run_height = candle.high - min(bar.low for bar in run)
        if run_height < min_run_atr * atr:
            continue
        if last_index is not None and position - last_index < min_separation_bars:
            continue

        confirm = candles[position + 1]
        found.append(
            MotherCandidate(
                timestamp=candle.timestamp,
                high=candle.high,
                low=candle.low,
                index=position,
                confirmed_at=confirm.timestamp,
                confirmed_index=position + 1,
                atr=atr,
                range_atr=span / atr,
                upper_wick_fraction=wick_fraction,
                run_atr=run_height / atr,
                run_green=greens,
            )
        )
        last_index = position

    return found
