#!/usr/bin/env python3
"""Offline session replay for strategy condition debugging.

Example:
  python scripts/replay_session.py candles.csv \
    --indicators-file indicators.json \
    --entry-file entry_conditions.json \
    --default-timeframe 5 \
    --signals-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.replay import decision_summary, replay_condition_debug


def _load_json(path: str | None, fallback: list | None = None) -> list:
    if not path:
        return list(fallback or [])
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return data


def _load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    lower = {column.lower(): column for column in df.columns}
    timestamp_col = lower.get("timestamp") or lower.get("datetime") or lower.get("date")
    if not timestamp_col:
        raise ValueError("CSV must include one of: timestamp, datetime, date")
    required = ["open", "high", "low", "close"]
    for column in required:
        if column not in lower:
            raise ValueError(f"CSV missing required OHLC column '{column}'")
    df[timestamp_col] = pd.to_datetime(df[timestamp_col])
    df = df.set_index(timestamp_col).sort_index()
    rename_map = {lower["open"]: "open", lower["high"]: "high", lower["low"]: "low", lower["close"]: "close"}
    if "volume" in lower:
        rename_map[lower["volume"]] = "volume"
    df = df.rename(columns=rename_map)
    if "volume" not in df.columns:
        df["volume"] = 0
    return df[["open", "high", "low", "close", "volume"]].copy()


def _resolve_csv_paths(path: str, pattern: str, recursive: bool) -> list[Path]:
    root = Path(path)
    if root.is_file():
        return [root]
    if root.is_dir():
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        return sorted(item for item in iterator if item.is_file())
    raise FileNotFoundError(f"No such file or directory: {path}")


def _run_one_file(
    csv_path: Path,
    *,
    indicators: list,
    entry_conditions: list,
    default_timeframe: int,
    source_timeframe: int | None,
    market_open: str,
    market_close: str,
) -> tuple[dict, dict]:
    raw = _load_csv(str(csv_path))
    replay = replay_condition_debug(
        raw,
        entry_conditions,
        indicators,
        default_timeframe_minutes=default_timeframe,
        source_timeframe_minutes=source_timeframe,
        market_open=market_open,
        market_close=market_close,
    )
    return replay, {
        "path": str(csv_path),
        "rows_loaded": len(raw),
        "summary": decision_summary(replay["decisions"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a CSV session offline and print strategy signal decisions.")
    parser.add_argument("csv_path", help="Path to OHLCV CSV, or a directory of OHLCV CSVs")
    parser.add_argument("--indicators-file", help="Path to JSON list of indicators", default=None)
    parser.add_argument("--entry-file", help="Path to JSON list of entry conditions", default=None)
    parser.add_argument("--default-timeframe", type=int, default=5, help="Default strategy timeframe in minutes")
    parser.add_argument("--source-timeframe", type=int, default=0, help="Raw candle timeframe in minutes")
    parser.add_argument("--market-open", default="09:15")
    parser.add_argument("--market-close", default="15:25")
    parser.add_argument("--glob", default="*.csv", help="Glob to use when csv_path is a directory")
    parser.add_argument("--recursive", action="store_true", help="Recurse when csv_path is a directory")
    parser.add_argument("--signals-only", action="store_true", help="Print only candles where the strategy passed")
    parser.add_argument("--show-details", action="store_true", help="Include per-condition values for each replay row")
    parser.add_argument(
        "--fail-on-missing-data",
        action="store_true",
        help="Exit non-zero if any candle evaluation is blocked by missing condition data",
    )
    args = parser.parse_args()

    indicators = _load_json(args.indicators_file)
    entry_conditions = _load_json(args.entry_file)
    csv_paths = _resolve_csv_paths(args.csv_path, args.glob, args.recursive)
    if not csv_paths:
        raise FileNotFoundError(f"No CSV files found for {args.csv_path} ({args.glob})")

    aggregate = {"files": 0, "rows_loaded": 0, "candles": 0, "signals": 0, "missing_data": 0}

    for idx, csv_path in enumerate(csv_paths):
        replay, report = _run_one_file(
            csv_path,
            indicators=indicators,
            entry_conditions=entry_conditions,
            default_timeframe=args.default_timeframe,
            source_timeframe=args.source_timeframe or None,
            market_open=args.market_open,
            market_close=args.market_close,
        )

        summary = report["summary"]
        aggregate["files"] += 1
        aggregate["rows_loaded"] += report["rows_loaded"]
        aggregate["candles"] += summary["total"]
        aggregate["signals"] += summary["passed"]
        aggregate["missing_data"] += summary["missing_data"]

        if len(csv_paths) > 1:
            if idx:
                print()
            print(f"File: {report['path']}")
        print(f"Rows loaded: {report['rows_loaded']}")
        print(f"Replay candles: {summary['total']}")
        print(f"Signals: {summary['passed']}")
        print(f"Missing-data candles: {summary['missing_data']}")
        print(f"Indicators used: {replay['indicators']}")
        print(f"Timeframe info: {replay['timeframe_info']}")

        decisions = replay["decisions"]
        if args.signals_only:
            decisions = [item for item in decisions if item.get("overall")]

        for item in decisions:
            stamp = pd.Timestamp(item["time"]).strftime("%Y-%m-%d %H:%M")
            status = "PASS" if item.get("overall") else "FAIL"
            gate = item.get("gate", "evaluating")
            suffix = "" if gate == "evaluating" else f"  [{gate}]"
            print(f"{stamp}  {status}{suffix}")
            if args.show_details:
                for detail in item.get("conditions", []):
                    marker = "PASS" if detail.get("result") else "FAIL"
                    missing = detail.get("missing_fields") or []
                    missing_suffix = f" missing={','.join(map(str, missing))}" if missing else ""
                    print(
                        f"  - {detail.get('condition')}: {marker} "
                        f"(L={detail.get('left_value')} R={detail.get('right_value')}){missing_suffix}"
                    )

    if len(csv_paths) > 1:
        print()
        print(f"Files processed: {aggregate['files']}")
        print(f"Total rows loaded: {aggregate['rows_loaded']}")
        print(f"Total replay candles: {aggregate['candles']}")
        print(f"Total signals: {aggregate['signals']}")
        print(f"Total missing-data candles: {aggregate['missing_data']}")

    if args.fail_on_missing_data and aggregate["missing_data"] > 0:
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
