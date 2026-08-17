"""Sweep the PE book's earliest entry minute against the real Upstox premiums.

The live PE book signals on a 5-minute candle and fills at the next minute's
open, so the earliest entry it can ever take is 09:20. This asks what happens
when that floor moves — 09:16 being the first minute the session can offer.

Everything runs offline:
  * NIFTY 1-minute index candles from tools/.nifty_cache
  * real expired-option minute premiums from tools/.upstox_cache (cache_only)

Run the baseline first. If `--entry 09:20` does not reproduce the saved run,
nothing below it is worth reading.
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.backtest_upstox import UpstoxHistoricalPremiumSelector  # noqa: E402
from engine.backtest import run_backtest  # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nifty_cache")

# The live PE book, as documented from the deployed state.
INDICATORS = ["Current_Candle_5m", "EMA_20_5m", "CPR_0.2_0.5", "Previous_Day"]
ENTRY = [
    {"logic": "IF", "left": "current_close", "operator": "is_below", "right": "EMA_20_5m"},
    {"logic": "AND", "left": "CPR_is_wide", "operator": "==", "right": "false"},
    {"logic": "AND", "left": "current_close", "operator": "is_below", "right": "CPR_BC"},
    {
        "logic": "AND",
        "left": "Day_Of_Week",
        "operator": "contains",
        "right": "days",
        "right_days": ["Monday", "Tuesday", "Thursday", "Friday"],
    },
]
LEVELS = ["CPR_S1", "CPR_S2", "CPR_S3", "CPR_TC"]


def build_exit(mode: str) -> list:
    """Exit variants.

    A PE buy is a bearish bet, so a close crossing DOWN through a support level
    is the trade working. Crossing UP through one is the reversal. The live book
    exits on both, which is what these variants exist to question.
    """
    kind, _, arg = mode.partition(":")
    if kind == "none":
        return [
            {
                "logic": "IF",
                "left": "current_close",
                "operator": "is_below",
                "right": "number",
                "right_number_value": -1,
            }
        ]
    if kind == "level":
        levels, ops = [arg], ["crosses_below", "crosses_above"]
    elif kind == "up":
        levels, ops = LEVELS, ["crosses_above"]
    elif kind == "down":
        levels, ops = LEVELS, ["crosses_below"]
    elif kind == "levelup":
        levels, ops = [arg], ["crosses_above"]
    else:  # "both" — the live rule
        levels, ops = LEVELS, ["crosses_below", "crosses_above"]
    out = []
    for level in levels:
        for op in ops:
            out.append({"logic": "IF" if not out else "OR", "left": "current_close", "operator": op, "right": level})
    return out


def load_index_1m(from_date: str, to_date: str) -> pd.DataFrame:
    """Every cached NIFTY 1-minute bar, de-duplicated — the files overlap."""
    rows: dict[str, list] = {}
    for path in sorted(glob.glob(os.path.join(CACHE, "NIFTY_1m_*.json"))):
        for ts, o, h, low, c, *rest in json.load(open(path)):
            rows[str(ts)[:19]] = [o, h, low, c, (rest[0] if rest else 0)]
    frame = pd.DataFrame.from_dict(rows, orient="index", columns=["open", "high", "low", "close", "volume"])
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    return frame.loc[from_date : to_date + " 23:59"]


def build_config(
    entry_earliest: str,
    execution_minutes: int,
    target: str = "rupees:10000",
    lot_size: int = 0,
    trail_pct: float = 0.0,
    sl_pct: float = 20.0,
) -> dict:
    """Target modes.

    rupees:<amt>     the live flat target — strategy-level, and the engine fills
                     it at the candle's BEST price, not at the target itself
    legrupees:<amt>  the same rupee target expressed on the leg, which fills at
                     the threshold; the honest version of `rupees`
    pct:<percent>    percent of entry premium, also filled at the threshold
    """
    kind, _, value = target.partition(":")
    target_rupees = float(value) if kind == "rupees" else 0.0
    leg_target_rupees = float(value) if kind == "legrupees" else 0.0
    target_pct = float(value) if kind == "pct" else 0.0
    return {
        "mode": "backtest",
        "segment": "indices",
        "instrument": "26000",
        "lots": 4,
        "lot_size": lot_size,
        "market_open": "09:15",
        "market_close": "15:25",
        "combined_sqoff_time": "15:20",
        "max_trades_per_day": 1,
        "combined_target_rupees": target_rupees,
        "indicators": INDICATORS,
        "timeframe_minutes": 5,
        "fetch_timeframe_minutes": 1,
        "execution_timeframe_minutes": execution_minutes,
        "entry_evaluation_timeframe_minutes": execution_minutes,
        "signal_exit_next_open": True,
        "entry_earliest_time": entry_earliest,
        # The deployed engine's execution costs.
        "spread_bps": 12,
        "entry_slippage_bps": 6,
        "exit_slippage_bps": 8,
        "legs": [
            {
                "transaction_type": "BUY",
                "option_type": "PE",
                "expiry": "current_week",
                "strike_type": "premium_near",
                "strike_value": 250,
                "lots": 4,
                "sl_pct": sl_pct,
                "target_pct": target_pct,
                "target_rupees": leg_target_rupees,
                "trail_pct": trail_pct,
                "sqoff_time": "15:20",
            }
        ],
    }


def _max_dd(trades) -> float:
    peak = cum = dd = 0.0
    for t in sorted(trades, key=lambda r: str(r.get("entry_time"))):
        cum += float(t.get("pnl", 0) or 0)
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2024-10-01")
    ap.add_argument("--to-date", default="2026-07-30")
    ap.add_argument("--entry", nargs="+", default=["09:20"], help="earliest entry times to sweep")
    ap.add_argument("--execution", type=int, default=1, help="execution timeframe in minutes")
    ap.add_argument(
        "--target",
        nargs="+",
        default=["rupees:10000"],
        help="targets to sweep: rupees:<amt> (the live flat target) or pct:<percent>",
    )
    ap.add_argument(
        "--lot-size",
        type=int,
        default=0,
        help="pin the lot size so a flat rupee target is not distorted by lot history",
    )
    ap.add_argument("--sl", nargs="+", type=float, default=[20.0], help="leg stop loss as %% of entry premium")
    ap.add_argument(
        "--trail",
        nargs="+",
        type=float,
        default=[0.0],
        help="trailing stop as %% off the option's peak premium; 0 = off",
    )
    ap.add_argument(
        "--exit",
        nargs="+",
        default=["both"],
        help="exit variants: both | up | down | none | level:CPR_S1 | levelup:CPR_S1",
    )
    ap.add_argument("--out", default="", help="write per-trade CSV for the first entry time")
    args = ap.parse_args()

    df = load_index_1m(args.from_date, args.to_date)
    print(f"[data] {len(df):,} 1-minute index bars, {df.index[0]} -> {df.index[-1]}")

    print(f"\n{'exit':<10}{'target':<9}{'SL':>6}{'trades':>8}{'win%':>7}{'net':>13}{'avg':>9}{'maxDD':>11}")
    for entry_time, target, exit_mode, trail, sl in itertools.product(
        args.entry, args.target, args.exit, args.trail, args.sl
    ):
        config = build_config(entry_time, args.execution, target, args.lot_size, trail, sl)
        config["_upstox_premium_selector"] = UpstoxHistoricalPremiumSelector("26000", cache_only=True)
        result = run_backtest(
            df_raw=df.copy(),
            entry_conditions=ENTRY,
            exit_conditions=build_exit(exit_mode),
            strategy_config=config,
        )
        if result.get("status") == "error":
            print(f"{exit_mode:<16}{target:<12}  ERROR: {result.get('message')}")
            continue
        trades = result.get("trades", []) or []
        net = sum(float(t.get("pnl", 0) or 0) for t in trades)
        wins = sum(1 for t in trades if float(t.get("pnl", 0) or 0) > 0)
        hits = sum(1 for t in trades if str(t.get("exit_reason", "")).startswith(("StrategyTP", "Target", "TARGET")))
        n = len(trades) or 1
        print(
            f"{exit_mode:<10}{target:<9}{sl:>5.0f}%{len(trades):>8}{wins / n * 100:>6.0f}%{net:>13,.0f}"
            f"{net / n:>9,.0f}{_max_dd(trades):>11,.0f}"
        )
        if (
            args.out
            and entry_time == args.entry[0]
            and target == args.target[0]
            and exit_mode == args.exit[0]
            and trail == args.trail[0]
            and sl == args.sl[0]
        ):
            pd.DataFrame(trades).to_csv(args.out, index=False)
            print(f"       wrote {args.out}")
        gaps = config.get("_option_data_gaps") or []
        if gaps:
            print(f"       {len(gaps)} signal(s) skipped for missing premium data")


if __name__ == "__main__":
    main()
