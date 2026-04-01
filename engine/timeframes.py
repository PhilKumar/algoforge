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
ENTRY_BUFFER_SECONDS = 1


@dataclass(frozen=True)
class TimeframeSpec:
    requested: int
    fetch: int
    derived: bool
    source: str = "default"
    all_frames: tuple[int, ...] = ()
    derived_frames: tuple[int, ...] = ()

    @property
    def mixed(self) -> bool:
        return len(self.all_frames) > 1


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


def get_common_fetch_timeframe(requested_frames: Sequence[int]) -> int:
    frames = [int(tf) for tf in requested_frames if int(tf) > 0]
    if not frames:
        return get_fetch_timeframe(DEFAULT_TIMEFRAME_MINUTES)

    divisors = [tf for tf in NATIVE_DHAN_INTERVALS if all(frame % tf == 0 for frame in frames)]
    if divisors:
        return max(divisors)
    return 1


def resolve_strategy_timeframe(
    indicators: Sequence[str] | None,
    default: int = DEFAULT_TIMEFRAME_MINUTES,
    *,
    execution_hint: int | None = None,
) -> TimeframeSpec:
    frames = collect_strategy_timeframes(indicators)
    requested_hint = int(execution_hint or 0) or None
    if not frames:
        requested = requested_hint or default
        fetch = get_fetch_timeframe(requested)
        return TimeframeSpec(
            requested=requested,
            fetch=fetch,
            derived=requested not in NATIVE_DHAN_INTERVALS,
            all_frames=(requested,),
            derived_frames=((requested,) if fetch != requested else ()),
        )
    requested = requested_hint or min(frames)
    fetch = get_common_fetch_timeframe(frames)
    derived_frames = tuple(tf for tf in frames if tf != requested)
    return TimeframeSpec(
        requested=requested,
        fetch=fetch,
        derived=fetch != requested,
        source="indicators",
        all_frames=tuple(frames),
        derived_frames=derived_frames,
    )


def describe_timeframe(spec: TimeframeSpec) -> str:
    if spec.mixed:
        other_frames = [tf for tf in spec.all_frames if tf != spec.requested]
        other_label = ", ".join(f"{tf}m" for tf in other_frames)
        if spec.fetch != spec.requested:
            return f"{spec.requested}m execution + {other_label} context (from {spec.fetch}m raw candles)"
        return f"{spec.requested}m execution + {other_label} context"
    if spec.derived:
        return f"{spec.requested}m (derived from {spec.fetch}m)"
    return f"{spec.requested}m"


def derived_timeframe_warning(spec: TimeframeSpec) -> str | None:
    if spec.mixed:
        other_frames = [tf for tf in spec.all_frames if tf != spec.requested]
        other_label = ", ".join(f"{tf}m" for tf in other_frames)
        return (
            f"Strategy uses mixed timeframes. Execution runs on {spec.requested}m candles; "
            f"other indicator frames ({other_label}) are aligned using the last closed candle "
            f"and built from {spec.fetch}m Dhan data."
        )
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


def candle_close_time(candle_start: datetime, timeframe_minutes: int) -> datetime:
    return candle_start + timedelta(minutes=timeframe_minutes)


def is_candle_closed(candle_start: datetime, timeframe_minutes: int, now: datetime) -> bool:
    return now >= candle_close_time(candle_start, timeframe_minutes)


def next_entry_ready_at(
    signal_candle_start: datetime,
    timeframe_minutes: int,
    *,
    buffer_seconds: int = ENTRY_BUFFER_SECONDS,
) -> datetime:
    return candle_close_time(signal_candle_start, timeframe_minutes) + timedelta(seconds=buffer_seconds)


def drop_incomplete_candle(df: pd.DataFrame, timeframe_minutes: int, now: datetime) -> pd.DataFrame:
    if df is None or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    last_start = df.index[-1]
    if not is_candle_closed(last_start, timeframe_minutes, now):
        return df.iloc[:-1].copy()
    return df.copy()


def resample_ohlcv(
    df: pd.DataFrame,
    timeframe_minutes: int,
    *,
    session_offset_minutes: int = SESSION_OFFSET_MINUTES,
    source_timeframe_minutes: int | None = None,
    drop_incomplete: bool = False,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if timeframe_minutes <= 0:
        raise ValueError(f"Timeframe must be positive, got {timeframe_minutes}")
    if source_timeframe_minutes and timeframe_minutes == source_timeframe_minutes:
        return df.sort_index().copy()

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
    if drop_incomplete and source_timeframe_minutes and timeframe_minutes > source_timeframe_minutes:
        expected_rows = timeframe_minutes // source_timeframe_minutes
        counts = (
            df.sort_index()
            .resample(
                rule,
                label="left",
                closed="left",
                origin="start_day",
                offset=f"{session_offset_minutes}min",
            )
            .size()
        )
        resampled = resampled[counts.reindex(resampled.index, fill_value=0) >= expected_rows]
    return resampled


def is_supported_timeframe(timeframe_minutes: int, allowed: Iterable[int] = NATIVE_DHAN_INTERVALS) -> bool:
    return timeframe_minutes in set(int(v) for v in allowed)
