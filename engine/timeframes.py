"""
engine/timeframes.py — shared timeframe parsing and resampling helpers.

Backtest, paper, and live should all agree on:
  - which strategy timeframe is being requested
  - which Dhan interval should be fetched
  - how derived candles (3m, 30m, etc.) are aligned to the NSE session
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

import pandas as pd

DEFAULT_TIMEFRAME_MINUTES = 5
DhanInterval = int
NATIVE_DHAN_INTERVALS: tuple[DhanInterval, ...] = (1, 5, 15, 25, 60)
MAX_INTRADAY_HISTORY_DAYS = 365 * 5
INTRADAY_CHUNK_DAYS = 90
SESSION_OFFSET_MINUTES = 15  # NSE session anchors at 09:15 IST


@dataclass(frozen=True)
class TimeframeSpec:
    requested: int
    fetch: int
    derived: bool
    source: str = "default"


def _extract_indicator_minutes(indicator_id: str) -> int | None:
    if not isinstance(indicator_id, str) or "_" not in indicator_id:
        return None
    for part in reversed(indicator_id.split("_")):
        if part.endswith("m") and part[:-1].isdigit():
            return int(part[:-1])
    return None


def collect_strategy_timeframes(indicators: Sequence[str] | None) -> list[int]:
    timeframes = {_extract_indicator_minutes(ind) for ind in (indicators or [])}
    timeframes.discard(None)
    return sorted(int(tf) for tf in timeframes)


def get_fetch_timeframe(requested_minutes: int) -> int:
    if requested_minutes <= 0:
        raise ValueError(f"Timeframe must be positive, got {requested_minutes}")
    if requested_minutes in NATIVE_DHAN_INTERVALS:
        return requested_minutes
    exact_divisors = [tf for tf in NATIVE_DHAN_INTERVALS if requested_minutes % tf == 0]
    if exact_divisors:
        return max(exact_divisors)
    return 1


def resolve_strategy_timeframe(
    indicators: Sequence[str] | None, default: int = DEFAULT_TIMEFRAME_MINUTES
) -> TimeframeSpec:
    frames = collect_strategy_timeframes(indicators)
    if not frames:
        return TimeframeSpec(
            requested=default, fetch=get_fetch_timeframe(default), derived=default not in NATIVE_DHAN_INTERVALS
        )
    if len(frames) > 1:
        joined = ", ".join(f"{tf}m" for tf in frames)
        raise ValueError(
            "Mixed indicator timeframes are not supported in one strategy yet. "
            f"Found: {joined}. Use a single execution timeframe."
        )
    requested = frames[0]
    fetch = get_fetch_timeframe(requested)
    return TimeframeSpec(requested=requested, fetch=fetch, derived=fetch != requested, source="indicators")


def describe_timeframe(spec: TimeframeSpec) -> str:
    if spec.derived:
        return f"{spec.requested}m (derived from {spec.fetch}m)"
    return f"{spec.requested}m"


def derived_timeframe_warning(spec: TimeframeSpec) -> str | None:
    if not spec.derived:
        return None
    return (
        f"Backtest timeframe {spec.requested}m is derived from {spec.fetch}m Dhan candles "
        "and resampled locally for indicator and signal evaluation."
    )


def aligned_candle_start(
    ts: datetime, timeframe_minutes: int, session_offset_minutes: int = SESSION_OFFSET_MINUTES
) -> datetime:
    anchor = ts.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=session_offset_minutes)
    elapsed_seconds = (ts - anchor).total_seconds()
    slot_index = int(elapsed_seconds // (timeframe_minutes * 60))
    return anchor + timedelta(minutes=slot_index * timeframe_minutes)


def resample_ohlcv(
    df: pd.DataFrame,
    timeframe_minutes: int,
    *,
    session_offset_minutes: int = SESSION_OFFSET_MINUTES,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if timeframe_minutes <= 0:
        raise ValueError(f"Timeframe must be positive, got {timeframe_minutes}")

    agg_map: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in df.columns:
        agg_map["volume"] = "sum"
    if "oi" in df.columns:
        agg_map["oi"] = "last"

    rule = f"{timeframe_minutes}min"
    resampled = (
        df.sort_index()
        .resample(
            rule,
            label="left",
            closed="left",
            origin="start_day",
            offset=f"{session_offset_minutes}min",
        )
        .agg(agg_map)
        .dropna(subset=["open"])
    )
    return resampled


def is_supported_timeframe(timeframe_minutes: int, allowed: Iterable[int] = NATIVE_DHAN_INTERVALS) -> bool:
    return timeframe_minutes in set(int(v) for v in allowed)
