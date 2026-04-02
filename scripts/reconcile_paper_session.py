#!/usr/bin/env python3
"""Reconcile one paper-trading session against replayed entry conditions.

Typical use on the deployed host:
  PHILFORGE_TOKEN=... ./venv/bin/python scripts/reconcile_paper_session.py \
    --date 2026-03-30 \
    --run-id Strategy_PE \
    --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.replay import infer_signal_candle_times_from_trades, replay_condition_debug


def _request_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {detail}") from exc


def _load_status(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load_ohlcv_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    lower = {column.lower(): column for column in df.columns}
    timestamp_col = lower.get("timestamp") or lower.get("datetime") or lower.get("date")
    if not timestamp_col:
        raise ValueError("CSV must include one of: timestamp, datetime, date")
    rename_map = {lower["open"]: "open", lower["high"]: "high", lower["low"]: "low", lower["close"]: "close"}
    if "volume" in lower:
        rename_map[lower["volume"]] = "volume"
    df[timestamp_col] = pd.to_datetime(df[timestamp_col])
    df = df.set_index(timestamp_col).sort_index().rename(columns=rename_map)
    if "volume" not in df.columns:
        df["volume"] = 0
    return df[["open", "high", "low", "close", "volume"]].copy()


def _fetch_status(base_url: str, token: str, run_id: str) -> dict[str, Any]:
    query = ""
    if run_id:
        query = "?" + urllib.parse.urlencode({"run_id": run_id})
    return _request_json(f"{base_url.rstrip('/')}/api/paper/status{query}", token=token)


def _export_ohlcv(
    base_url: str, token: str, strategy: dict[str, Any], date_str: str, export_name: str
) -> dict[str, Any]:
    payload = {
        "instrument": str(strategy.get("instrument") or ""),
        "segment": str(strategy.get("segment") or "indices"),
        "from_date": date_str,
        "to_date": date_str,
        "candle_interval": "1",
        "split_by_day": False,
        "export_name": export_name,
    }
    return _request_json(
        f"{base_url.rstrip('/')}/api/replay/export-ohlcv",
        token=token,
        method="POST",
        payload=payload,
    )


def _filter_trades_for_date(trades: list[dict[str, Any]], date_str: str) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for trade in trades or []:
        stamp = str(trade.get("entry_time") or "")
        if stamp.startswith(date_str):
            filtered.append(trade)
    return filtered


def _normalize_signal_time(stamp: pd.Timestamp, timeframe_minutes: int) -> pd.Timestamp:
    return pd.Timestamp(stamp).floor(f"{max(int(timeframe_minutes or 1), 1)}min")


def _build_signal_windows(decisions: list[dict[str, Any]], timeframe_minutes: int) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    spacing = pd.Timedelta(minutes=max(int(timeframe_minutes or 1), 1))
    current: dict[str, Any] | None = None
    for item in decisions or []:
        if not item.get("overall"):
            if current:
                windows.append(current)
                current = None
            continue
        stamp = pd.Timestamp(item["time"])
        if current and stamp - current["end"] == spacing:
            current["end"] = stamp
            current["decision_count"] += 1
            continue
        if current:
            windows.append(current)
        current = {"start": stamp, "end": stamp, "decision_count": 1}
    if current:
        windows.append(current)
    return windows


def _find_window_index(stamp: pd.Timestamp, windows: list[dict[str, Any]]) -> int | None:
    for idx, window in enumerate(windows):
        if window["start"] <= stamp <= window["end"]:
            return idx
    return None


def _build_reconciliation(
    *,
    date_str: str,
    status: dict[str, Any],
    ohlcv_path: str,
) -> dict[str, Any]:
    strategy = status.get("strategy") or {}
    if not strategy:
        raise ValueError("Paper status does not include strategy payload; keep the engine running while reconciling")

    timeframe_minutes = int(strategy.get("timeframe_minutes") or strategy.get("fetch_timeframe_minutes") or 5)
    entry_delay_candles = int(strategy.get("entry_delay_candles") or 0)
    raw = _load_ohlcv_csv(ohlcv_path)
    replay = replay_condition_debug(
        raw,
        strategy.get("entry_conditions") or [],
        strategy.get("indicators") or [],
        default_timeframe_minutes=timeframe_minutes,
        source_timeframe_minutes=1,
        market_open=str(strategy.get("market_open") or "09:15"),
        market_close=str(strategy.get("market_close") or "15:25"),
    )
    decisions = [item for item in replay["decisions"] if str(item["time"]).startswith(date_str)]
    signal_windows = _build_signal_windows(decisions, timeframe_minutes)
    trades = _filter_trades_for_date(status.get("closed_trades") or [], date_str)
    inferred_signal_times = [
        _normalize_signal_time(stamp, timeframe_minutes)
        for stamp in infer_signal_candle_times_from_trades(
            trades,
            timeframe_minutes,
            entry_delay_candles=entry_delay_candles,
        )
    ]

    matched_actual_signals = []
    unmatched_actual_signals = []
    for stamp in inferred_signal_times:
        match_idx = _find_window_index(stamp, signal_windows)
        target = {"signal_time": stamp.isoformat(), "window_index": match_idx}
        if match_idx is None:
            unmatched_actual_signals.append(target)
        else:
            matched_actual_signals.append(target)

    replay_missing = sum(1 for item in decisions if str(item.get("gate", "")).startswith("missing_condition_data"))
    first_window = signal_windows[0] if signal_windows else None
    warnings = [event for event in status.get("event_log") or [] if str(event.get("type", "")).lower() == "warning"]
    errors = [event for event in status.get("event_log") or [] if str(event.get("type", "")).lower() == "error"]

    findings: list[str] = []
    overall = "PASS"
    if replay_missing:
        overall = "FAIL"
        findings.append(f"Replay found missing condition data on {replay_missing} candle(s)")
    if unmatched_actual_signals:
        overall = "FAIL"
        findings.append(
            f"{len(unmatched_actual_signals)} actual paper entry signal(s) fell outside replay pass windows"
        )
    if signal_windows and not inferred_signal_times:
        overall = "FAIL"
        findings.append("Replay found at least one valid entry window but paper took no trade")
    if not signal_windows and inferred_signal_times:
        overall = "FAIL"
        findings.append("Paper took trade(s) even though replay found no valid entry window")
    if (
        overall != "FAIL"
        and int(strategy.get("max_trades_per_day") or 0) <= 1
        and inferred_signal_times
        and first_window
    ):
        if _find_window_index(inferred_signal_times[0], [first_window]) is None:
            overall = "FAIL"
            findings.append("First actual paper entry did not occur in the first replay entry window")
    if overall == "PASS" and errors:
        overall = "WARN"
        findings.append(f"Paper event log contains {len(errors)} error event(s)")
    if overall == "PASS" and warnings:
        overall = "WARN"
        findings.append(f"Paper event log contains {len(warnings)} warning event(s)")
    if not findings:
        findings.append("No replay-vs-paper entry mismatch detected for the session")

    exit_audit = []
    for trade in trades:
        exit_audit.append(
            {
                "entry_time": trade.get("entry_time"),
                "exit_time": trade.get("exit_time"),
                "exit_reason": trade.get("exit_reason"),
                "pnl": trade.get("pnl"),
                "complete": bool(trade.get("exit_time") and trade.get("exit_reason")),
            }
        )

    return {
        "date": date_str,
        "run_id": status.get("run_id") or status.get("strategy_name") or strategy.get("run_name"),
        "strategy_name": status.get("strategy_name") or strategy.get("run_name") or strategy.get("name"),
        "timeframe_minutes": timeframe_minutes,
        "strategy": {
            "instrument": strategy.get("instrument"),
            "indicators": strategy.get("indicators"),
            "entry_conditions": strategy.get("entry_conditions"),
            "exit_conditions": strategy.get("exit_conditions"),
            "max_trades_per_day": strategy.get("max_trades_per_day"),
        },
        "replay": {
            "ohlcv_path": ohlcv_path,
            "decision_count": len(decisions),
            "pass_window_count": len(signal_windows),
            "signal_windows": [
                {
                    "start": window["start"].isoformat(),
                    "end": window["end"].isoformat(),
                    "decision_count": window["decision_count"],
                }
                for window in signal_windows
            ],
            "missing_data_candles": replay_missing,
        },
        "paper": {
            "trade_count": len(trades),
            "trades": trades,
            "inferred_signal_times": [stamp.isoformat() for stamp in inferred_signal_times],
            "matched_actual_signals": matched_actual_signals,
            "unmatched_actual_signals": unmatched_actual_signals,
            "exit_audit": exit_audit,
            "warnings": warnings,
            "errors": errors,
        },
        "summary": {
            "overall": overall,
            "findings": findings,
        },
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Paper Session Reconciliation - {report['date']}",
        "",
        f"- Overall: {report['summary']['overall']}",
        f"- Strategy: {report.get('strategy_name')}",
        f"- Run ID: {report.get('run_id')}",
        f"- Timeframe: {report.get('timeframe_minutes')}m",
        f"- Replay pass windows: {report['replay']['pass_window_count']}",
        f"- Replay missing-data candles: {report['replay']['missing_data_candles']}",
        f"- Paper trades: {report['paper']['trade_count']}",
        f"- Event warnings: {len(report['paper']['warnings'])}",
        f"- Event errors: {len(report['paper']['errors'])}",
        "",
        "## Findings",
    ]
    for finding in report["summary"]["findings"]:
        lines.append(f"- {finding}")

    if report["replay"]["signal_windows"]:
        lines.extend(["", "## Replay Windows"])
        for window in report["replay"]["signal_windows"]:
            lines.append(f"- {window['start']} -> {window['end']} ({window['decision_count']} candle(s))")

    if report["paper"]["trades"]:
        lines.extend(["", "## Paper Trades"])
        for trade in report["paper"]["trades"]:
            lines.append(
                f"- {trade.get('entry_time')} -> {trade.get('exit_time')} | "
                f"{trade.get('symbol', '')} | reason={trade.get('exit_reason')} | pnl={trade.get('pnl')}"
            )

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile one deployed paper session against replayed entry logic.")
    parser.add_argument("--date", required=True, help="Trading date in YYYY-MM-DD format")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("PHILFORGE_TOKEN", ""))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--status-json", default="", help="Use a saved /api/paper/status snapshot instead of API")
    parser.add_argument("--ohlcv-path", default="", help="Use an existing OHLCV CSV instead of API export")
    parser.add_argument("--output-dir", default="reconciliation_reports")
    parser.add_argument("--export-name", default="")
    args = parser.parse_args()

    if args.status_json:
        status = _load_status(args.status_json)
    else:
        if not args.token:
            raise SystemExit("PHILFORGE_TOKEN or --token is required when using the API")
        status = _fetch_status(args.base_url, args.token, args.run_id)

    strategy = status.get("strategy") or {}
    if not strategy:
        raise SystemExit("Paper status does not include strategy payload; keep the paper engine running")

    run_name = str(status.get("run_id") or status.get("strategy_name") or strategy.get("run_name") or "paper_run")
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) / f"{args.date}_{run_name}_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    _save_json(output_dir / "paper_status.json", status)

    if args.ohlcv_path:
        ohlcv_path = args.ohlcv_path
        export_meta = {"status": "skipped", "ohlcv_path": ohlcv_path}
    else:
        if not args.token:
            raise SystemExit("PHILFORGE_TOKEN or --token is required when exporting OHLCV via the API")
        export_name = args.export_name or f"{run_name}_{args.date}_reconcile"
        export_meta = _export_ohlcv(args.base_url, args.token, strategy, args.date, export_name)
        files = export_meta.get("files") or []
        if not files:
            raise SystemExit("OHLCV export returned no files")
        ohlcv_path = files[0]["path"]
    _save_json(output_dir / "ohlcv_export.json", export_meta)

    report = _build_reconciliation(date_str=args.date, status=status, ohlcv_path=ohlcv_path)
    _save_json(output_dir / "report.json", report)
    (output_dir / "summary.md").write_text(_render_markdown(report), encoding="utf-8")

    print(f"Report dir: {output_dir}")
    print(f"Overall: {report['summary']['overall']}")
    for finding in report["summary"]["findings"]:
        print(f"- {finding}")

    return 1 if report["summary"]["overall"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
