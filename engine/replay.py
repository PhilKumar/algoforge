from __future__ import annotations

from datetime import datetime, time
from typing import Any

import pandas as pd

from engine.backtest import inspect_condition_group
from engine.indicators import compute_dynamic_indicators, normalize_strategy_indicators
from engine.timeframes import resolve_strategy_timeframe


def _parse_time(value: str | time | None, fallback: str) -> time:
    if isinstance(value, time):
        return value
    raw = str(value or fallback).strip()
    parts = raw.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    second = int(parts[2]) if len(parts) > 2 else 0
    return time(hour, minute, second)


def _format_missing_condition_gate(missing_fields: list[str]) -> str:
    preview = ", ".join(missing_fields[:3])
    extra = len(missing_fields) - 3
    if extra > 0:
        preview = f"{preview} +{extra} more"
    return f"missing_condition_data ({preview})"


def build_replay_frame(
    raw_df: pd.DataFrame,
    indicators: list[str] | None,
    *,
    entry_conditions: list[dict] | None = None,
    exit_conditions: list[dict] | None = None,
    default_timeframe_minutes: int = 5,
    source_timeframe_minutes: int | None = None,
) -> tuple[pd.DataFrame, list[str], Any]:
    normalized_indicators = normalize_strategy_indicators(
        indicators or [],
        entry_conditions=entry_conditions or [],
        exit_conditions=exit_conditions or [],
    )
    tf_spec = resolve_strategy_timeframe(normalized_indicators, default=default_timeframe_minutes)
    frame = compute_dynamic_indicators(
        raw_df.copy().sort_index(),
        normalized_indicators,
        default_timeframe_minutes=tf_spec.requested,
        source_timeframe_minutes=source_timeframe_minutes or tf_spec.fetch,
    )
    return frame, normalized_indicators, tf_spec


def replay_condition_debug(
    raw_df: pd.DataFrame,
    conditions: list[dict] | None,
    indicators: list[str] | None,
    *,
    default_timeframe_minutes: int = 5,
    source_timeframe_minutes: int | None = None,
    market_open: str | time = "09:15",
    market_close: str | time = "15:25",
) -> dict[str, Any]:
    frame, normalized_indicators, tf_spec = build_replay_frame(
        raw_df,
        indicators,
        entry_conditions=conditions,
        default_timeframe_minutes=default_timeframe_minutes,
        source_timeframe_minutes=source_timeframe_minutes,
    )
    open_time = _parse_time(market_open, "09:15")
    close_time = _parse_time(market_close, "15:25")

    decisions: list[dict[str, Any]] = []
    previous_row = None
    for timestamp, row in frame.iterrows():
        candle_time = timestamp.time()
        if candle_time < open_time or candle_time > close_time:
            previous_row = row
            continue
        raw_overall, details, missing_fields = inspect_condition_group(row, conditions or [], previous_row)
        overall = raw_overall and not missing_fields
        decisions.append(
            {
                "time": timestamp,
                "overall": overall,
                "raw_overall": raw_overall,
                "gate": "evaluating" if not missing_fields else _format_missing_condition_gate(missing_fields),
                "conditions": details,
                "missing_fields": missing_fields,
            }
        )
        previous_row = row

    return {
        "frame": frame,
        "indicators": normalized_indicators,
        "timeframe_info": {
            "requested_minutes": tf_spec.requested,
            "fetch_minutes": tf_spec.fetch,
            "all_frames": list(tf_spec.all_frames),
        },
        "decisions": decisions,
    }


def decision_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(decisions or [])
    passed = sum(1 for item in decisions or [] if bool(item.get("overall")))
    missing_data = sum(1 for item in decisions or [] if str(item.get("gate", "")).startswith("missing_condition_data"))
    first_time = pd.Timestamp(decisions[0]["time"]) if total else None
    last_time = pd.Timestamp(decisions[-1]["time"]) if total else None
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "missing_data": missing_data,
        "first_time": first_time,
        "last_time": last_time,
    }


def decision_times(decisions: list[dict[str, Any]], *, overall: bool | None = None) -> list[pd.Timestamp]:
    times: list[pd.Timestamp] = []
    for item in decisions:
        if overall is not None and bool(item.get("overall")) is not overall:
            continue
        times.append(pd.Timestamp(item["time"]))
    return times


def infer_signal_candle_time_from_entry(
    entry_time: str | datetime | pd.Timestamp,
    timeframe_minutes: int,
    *,
    entry_delay_candles: int = 0,
) -> pd.Timestamp:
    return pd.Timestamp(entry_time) - pd.Timedelta(minutes=timeframe_minutes * (entry_delay_candles + 1))


def infer_signal_candle_times_from_trades(
    trades: list[dict[str, Any]],
    timeframe_minutes: int,
    *,
    entry_delay_candles: int = 0,
) -> list[pd.Timestamp]:
    signal_times: list[pd.Timestamp] = []
    for trade in trades or []:
        entry_time = trade.get("entry_time")
        if not entry_time:
            continue
        signal_times.append(
            infer_signal_candle_time_from_entry(
                entry_time,
                timeframe_minutes,
                entry_delay_candles=entry_delay_candles,
            )
        )
    return signal_times
