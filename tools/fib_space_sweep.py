"""tools/fib_space_sweep.py -- backtest the CONVERGING-FIB SPACE design.

Phil's 2026-08-01 redesign: auto trendline+fib geometry (his adjudicated rule),
money only in the SPACES where two different fibs' levels converge, deepest two,
1/2/3 lot ladder, two-red recovery entry, one 0.25 target.

Layer 1 (this tool) measures the design in INDEX SPACE -- points captured per
campaign, hit rate, how often the geometry even produces a boundary.  No rupee
claim is made here: option premium is a separate, slower question and a
strategy that cannot win in index points will not be rescued by it.

    python3 tools/fib_space_sweep.py --tf 15m
    python3 tools/fib_space_sweep.py --tf 15m --target-mode structure
    python3 tools/fib_space_sweep.py --tf 15m --limit 50 --verbose

Candles come from tools/.nifty_cache, so a run is offline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from statistics import mean, median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_mothers import find_mother_candles  # noqa: E402
from engine.cascade_options import IndexCandle  # noqa: E402
from engine.fib_space_cascade import SpaceCascadeConfig, run_space_campaign  # noqa: E402
from engine.fib_space_geometry import Bar  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nifty_cache")

# Per-underlying contract facts, matching tools/fib_cascade_sweep.py.  The DTE
# window here is Phil's locked one -- MONTHLY contracts at 15-45 days -- for
# both underlyings, so the two are compared on the same footing.
SYMBOLS = {
    "nifty": dict(cache="NIFTY", upstox_key="NSE_INDEX|Nifty 50", strike_step=50.0),
    "banknifty": dict(cache="BANKNIFTY", upstox_key="NSE_INDEX|Nifty Bank", strike_step=100.0),
}

# NSE trades 09:15-15:30, so a session is 6.25 hours.  Horizons and time stops
# are expressed in SESSIONS and converted here, otherwise "50 bars" means two
# days on 15m and seven weeks on 1H and the timeframes cannot be compared.
#
# The horizon is counted from the MOTHER, but the option's life runs from the
# ENTRY, which can be many sessions later.  A window sized to the DTE window
# therefore stopped some campaigns watching before their own contract expired
# and manufactured "expiry" losses on trades whose target actually arrived --
# 7 of 13 NIFTY 15m expiry losses were this, not the market.  The horizon is
# now deliberately far longer than any contract's life; expiry does the real
# capping, in price_campaign.
BARS_PER_SESSION = {"5m": 75, "15m": 25, "1h": 7}
DEFAULT_HORIZON_SESSIONS = 120

# How many bars either side a swing high must beat to be a mother HERE.  The
# detector's own default is 3, which in a downtrend promotes every minor bounce:
# Phil rejected 2024-12-17 11:00 on sight, and he was right -- its high sits 398
# points UNDER the real high of the surrounding 40 bars and its range is 0.92
# ATR.  Widening to 5 drops 2024-12-17 11:00 and 261 other junk pivots (770 ->
# 509) while keeping all four mothers Phil has confirmed by chart (05-Mar-2026
# 14:45, 23-Feb-2026 09:30, 25-Mar-2026 14:00, 22-Apr-2026 14:00).  Raising the
# ATR floor instead loses his mothers, so the pivot width is the right knob.
MOTHER_PIVOT_BARS = 5


def find_space_mothers(index_candles):
    """The mother scan this design uses -- see MOTHER_PIVOT_BARS."""
    return find_mother_candles(index_candles, left_bars=MOTHER_PIVOT_BARS, right_bars=MOTHER_PIVOT_BARS)


# Kept for callers that still think in bars (the original 15m default).
DEFAULT_HORIZON_BARS = BARS_PER_SESSION["15m"] * DEFAULT_HORIZON_SESSIONS


def horizon_bars(timeframe: str, sessions: int = DEFAULT_HORIZON_SESSIONS) -> int:
    return BARS_PER_SESSION[timeframe] * sessions


def load_bars(timeframe: str, symbol: str = "nifty") -> list[Bar]:
    """Every cached bar for one underlying and timeframe, as geometry bars."""
    prefix = f"{SYMBOLS[symbol]['cache']}_{timeframe}_"
    names = [n for n in os.listdir(CACHE_DIR) if n.startswith(prefix) and n.endswith(".json")]
    if not names:
        raise SystemExit(f"No cached {symbol} {timeframe} candles under {CACHE_DIR}")
    # The widest cache file wins.
    best, rows = None, []
    for name in names:
        with open(os.path.join(CACHE_DIR, name), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if len(data) > len(rows):
            best, rows = name, data
    print(f"[data] {best}: {len(rows)} bars")
    bars: list[Bar] = []
    prev = None
    for i, r in enumerate(rows):
        stamp = datetime.fromisoformat(r[0])
        # THE GAP RULE: the first bar of a session carries the previous
        # session's close, so red/green and the trendline anchor are measured
        # from where the move began rather than from where the gap ended.
        prev_close = prev.close if prev is not None and prev.timestamp.date() != stamp.date() else None
        bar = Bar(
            index=i,
            timestamp=stamp,
            open=r[1],
            high=r[2],
            low=r[3],
            close=r[4],
            session_prev_close=prev_close,
        )
        bars.append(bar)
        prev = bar
    return bars


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", default="15m", choices=("5m", "15m", "1h"))
    parser.add_argument("--symbol", default="nifty", choices=sorted(SYMBOLS))
    parser.add_argument("--target-mode", default="avg_entry", choices=("avg_entry", "structure"))
    parser.add_argument("--horizon-sessions", type=int, default=DEFAULT_HORIZON_SESSIONS)
    parser.add_argument("--limit", type=int, default=0, help="only the first N mothers")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    bars = load_bars(args.tf, args.symbol)
    span = horizon_bars(args.tf, args.horizon_sessions)
    index_candles = [IndexCandle(b.timestamp, b.open, b.high, b.low, b.close) for b in bars]
    mothers = find_space_mothers(index_candles)
    if args.limit:
        mothers = mothers[: args.limit]
    print(f"[mothers] {len(mothers)} swing-high mothers on {args.symbol} {args.tf}")
    print(f"[config] target-mode={args.target_mode} horizon={args.horizon_sessions} sessions ({span} bars)\n")

    config = SpaceCascadeConfig(target_mode=args.target_mode)
    results = []
    for mother in mothers:
        start = mother.index
        if start + 10 >= len(bars):
            continue
        window = bars[start : start + span + 1]
        result = run_space_campaign(bars[start], window, config, arm_from_index=mother.confirmed_index)
        results.append(result)
        if args.verbose and result.fills:
            print(
                f"  {result.mother_timestamp:%Y-%m-%d %H:%M}  {result.status:<11} "
                f"fibs={result.fib_count} spaces={result.space_count} fills={len(result.fills)} "
                f"pts={result.index_points if result.index_points is None else round(result.index_points, 2)}"
            )

    traded = [r for r in results if r.fills]
    closed = [r for r in traded if r.status == "closed"]
    stranded = [r for r in traded if r.status != "closed"]
    with_geometry = [r for r in results if r.fib_count > 0]
    with_spaces = [r for r in results if r.space_count > 0]

    print("\n" + "=" * 64)
    print(f"CONVERGING-FIB SPACE BACKTEST -- {args.symbol.upper()} {args.tf}, index space")
    print("=" * 64)
    print(f"mothers scanned         {len(results)}")
    print(f"  drew >=1 fib          {len(with_geometry)}")
    print(f"  produced >=1 space    {len(with_spaces)}   <- needs TWO fibs to converge")
    print(f"  actually traded       {len(traded)}")
    print(f"    reached target      {len(closed)}")
    print(f"    still open at end   {len(stranded)}")
    if traded:
        rate = 100.0 * len(closed) / len(traded)
        print(f"    hit rate            {rate:.1f}%")
    if closed:
        pts = [r.index_points for r in closed if r.index_points is not None]
        held = [r.bars_held for r in closed]
        print("\n  points per closed campaign (per unit, entry -> target)")
        print(f"    mean   {mean(pts):8.2f}")
        print(f"    median {median(pts):8.2f}")
        print(f"    min    {min(pts):8.2f}   max {max(pts):8.2f}")
        print(f"    total  {sum(pts):8.2f}")
        print(f"  bars held: median {median(held):.0f}, max {max(held)}")
    if stranded:
        print(f"\n  UNCLOSED campaigns hold {sum(r.quantity for r in stranded)} units of open risk")
        print("  (index space cannot price these -- an option leg would decay or expire)")
    if traded:
        rounds = Counter(len(r.rounds) for r in traded)
        print(f"\n  rounds per campaign: {dict(sorted(rounds.items()))}")
        fills = Counter(len(r.fills) for r in traded)
        print(f"  fills per campaign: {dict(sorted(fills.items()))}")
        labels = Counter(f.space_label for r in traded for f in r.fills)
        print(f"  space labels bought: {dict(labels.most_common())}")
        touch = sum(1 for r in traded for f in r.fills if f.on_touch)
        total = sum(len(r.fills) for r in traded)
        print(f"  bought on touch (tiny space): {touch}/{total}")
        widths = [f.space_width for r in traded for f in r.fills]
        print(f"  space width: median {median(widths):.2f} pts, min {min(widths):.2f}, max {max(widths):.2f}")
    print()


if __name__ == "__main__":
    main()
