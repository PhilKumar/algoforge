"""tools/fib_space_matrix.py -- the converging-fib space design across the grid.

NIFTY and BANKNIFTY x 5m, 15m and 1H, every leg priced on real Upstox
expired-option bars, MONTHLY contracts in Phil's locked 15-45 DTE window.

Because "50 bars" means two days on 15m and seven weeks on 1H, both the horizon
and the time stop are given in SESSIONS and converted per timeframe -- the six
cells are otherwise not comparable.

    python3 tools/fib_space_matrix.py
    python3 tools/fib_space_matrix.py --time-stops 0 2 4 8
    FIB_SPACE_CACHE_ONLY=0 python3 tools/fib_space_matrix.py   # backfill premiums

Writes a JSON summary next to the tool so the numbers can be re-read without
re-running the sweep.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.cascade_upstox import UpstoxAccessError, UpstoxPremiumSource  # noqa: E402
from engine.cascade_mothers import find_mother_candles  # noqa: E402
from engine.cascade_options import IndexCandle, NiftyContractResolver  # noqa: E402
from engine.fib_space_cascade import SpaceCascadeConfig, run_space_campaign  # noqa: E402
from tools.fib_space_premium import CACHE_ONLY, _ResolverView, lot_size_on, price_campaign  # noqa: E402
from tools.fib_space_sweep import BARS_PER_SESSION, SYMBOLS, horizon_bars, load_bars  # noqa: E402

TIMEFRAMES = ("5m", "15m", "1h")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fib_space_matrix_results.json")


def run_cell(symbol: str, timeframe: str, *, horizon_sessions: int, stop_sessions: int, limit: int = 0) -> dict:
    """One symbol/timeframe/time-stop combination, index and rupees."""
    cfg = SYMBOLS[symbol]
    bars = load_bars(timeframe, symbol)
    span = horizon_bars(timeframe, horizon_sessions)
    stop_bars = BARS_PER_SESSION[timeframe] * stop_sessions if stop_sessions else 0

    index_candles = [IndexCandle(b.timestamp, b.open, b.high, b.low, b.close) for b in bars]
    mothers = find_mother_candles(index_candles)
    if limit:
        mothers = mothers[:limit]

    source = UpstoxPremiumSource(
        cache_only=CACHE_ONLY,
        underlying_key=cfg["upstox_key"],
        # Stale 20-day cache files must be re-fetched under the wider window,
        # otherwise the lookback fix changes nothing on an already-warm cache.
        backfill_missing=not CACHE_ONLY,
    )
    expiries = sorted(source.available_expiries())
    view = _ResolverView(strike_step=cfg["strike_step"])

    traded, priced, gaps = 0, [], 0
    index_closed, index_points = 0, []
    for mother in mothers:
        start = mother.index
        if start + 10 >= len(bars):
            continue
        window = bars[start : start + span + 1]
        lot = lot_size_on(mother.timestamp.date(), symbol)
        result = run_space_campaign(
            bars[start],
            window,
            SpaceCascadeConfig(lot_size=lot, max_bars_held=stop_bars),
            arm_from_index=mother.confirmed_index,
        )
        if not result.fills:
            continue
        traded += 1
        if result.status == "closed":
            index_closed += 1
            if result.index_points is not None:
                index_points.append(result.index_points)
        resolver = NiftyContractResolver(
            expiries=expiries, strike_step=cfg["strike_step"], lot_size=lot, symbol=cfg["cache"]
        )
        row = price_campaign(result, window, source, resolver, view, settle_bars=bars, timeframe=timeframe)
        if row.status == "priced":
            priced.append(row)
        else:
            gaps += 1

    nets = [r.net for r in priced]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    expiries_hit = [r.net for r in priced if r.exit_reason == "expiry_square_off"]
    stops_hit = [r.net for r in priced if r.exit_reason == "time_stop"]
    targets_hit = [r.net for r in priced if r.exit_reason == "target"]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "stop_sessions": stop_sessions,
        "mothers": len(mothers),
        "traded": traded,
        "index_hit_rate": round(100.0 * index_closed / traded, 1) if traded else None,
        "index_mean_points": round(mean(index_points), 1) if index_points else None,
        "priced": len(priced),
        "gaps": gaps,
        "net": round(sum(nets), 2) if nets else None,
        "win_rate": round(100.0 * len(wins) / len(priced), 1) if priced else None,
        "mean_win": round(mean(wins), 2) if wins else None,
        "mean_loss": round(mean(losses), 2) if losses else None,
        "n_target": len(targets_hit),
        "net_target": round(sum(targets_hit), 2) if targets_hit else 0.0,
        "n_expiry": len(expiries_hit),
        "net_expiry": round(sum(expiries_hit), 2) if expiries_hit else 0.0,
        "n_stop": len(stops_hit),
        "net_stop": round(sum(stops_hit), 2) if stops_hit else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=sorted(SYMBOLS))
    parser.add_argument("--tfs", nargs="*", default=list(TIMEFRAMES))
    parser.add_argument("--horizon-sessions", type=int, default=45)
    parser.add_argument(
        "--time-stops", nargs="*", type=int, default=[0, 2, 5], help="time stops in SESSIONS (0 = none)"
    )
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    print(f"[matrix] cache_only={CACHE_ONLY} horizon={args.horizon_sessions} sessions")
    rows = []
    for symbol in args.symbols:
        for timeframe in args.tfs:
            for stop in args.time_stops:
                started = datetime.now()
                try:
                    row = run_cell(
                        symbol,
                        timeframe,
                        horizon_sessions=args.horizon_sessions,
                        stop_sessions=stop,
                        limit=args.limit,
                    )
                except UpstoxAccessError as exc:
                    print(f"  {symbol} {timeframe} stop={stop}: upstox unavailable ({exc})")
                    continue
                row["seconds"] = round((datetime.now() - started).total_seconds(), 1)
                rows.append(row)
                print(
                    f"  {symbol:<9} {timeframe:<4} stop={stop or '-':<2} "
                    f"traded={row['traded']:<4} priced={row['priced']:<4} "
                    f"net={row['net'] if row['net'] is not None else 'n/a'}"
                )
                with open(OUT_PATH, "w", encoding="utf-8") as handle:
                    json.dump(rows, handle, indent=2)

    print("\n" + "=" * 96)
    print("CONVERGING-FIB SPACE -- REAL PREMIUMS, MONTHLY 15-45 DTE, ATM-2")
    print("=" * 96)
    header = (
        f"{'symbol':<10}{'tf':<5}{'stop':<6}{'traded':>7}{'priced':>7}{'win%':>7}"
        f"{'NET Rs':>14}{'targets':>16}{'expiry/stop':>18}"
    )
    print(header)
    print("-" * 96)
    for row in rows:
        stop = f"{row['stop_sessions']}s" if row["stop_sessions"] else "none"
        net = f"{row['net']:,.0f}" if row["net"] is not None else "n/a"
        tgt = f"{row['n_target']}: {row['net_target']:,.0f}"
        bad = f"{row['n_expiry'] + row['n_stop']}: {row['net_expiry'] + row['net_stop']:,.0f}"
        win = f"{row['win_rate']}" if row["win_rate"] is not None else "-"
        print(
            f"{row['symbol']:<10}{row['timeframe']:<5}{stop:<6}{row['traded']:>7}"
            f"{row['priced']:>7}{win:>7}{net:>14}{tgt:>16}{bad:>18}"
        )
    print(f"\nwritten to {OUT_PATH}")


if __name__ == "__main__":
    main()
