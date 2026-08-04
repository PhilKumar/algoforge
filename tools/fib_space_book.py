"""tools/fib_space_book.py -- run the converging-fib space book as CONFIGURED.

Every other tool here explores: fib_space_sweep measures index points,
fib_space_matrix walks a grid of timeframes and time stops.  This one runs each
symbol on exactly the terms it is locked to and nothing else, so the book's
headline numbers are reproducible from git rather than from a scratch file.

    python3 tools/fib_space_book.py
    python3 tools/fib_space_book.py --symbols banknifty
    python3 tools/fib_space_book.py --since 2026-01-01

What "as configured" means, and where each piece is written down:

    geometry            15m for every symbol            (this module)
    entries             SYMBOLS[sym]["entry_timeframe"], default 5m
    mothers             5-bar swing pivots on the 15m   (fib_space_sweep)
    contract terms      SYMBOLS[sym]["contract"]        (fib_space_sweep)
    portfolio throttle  SYMBOLS[sym]["cooldown_days"]   (fib_space_sweep)
    ladder / zones      SpaceCascadeConfig defaults     (fib_space_cascade)
    premium fills       real Upstox prints, cache-only  (fib_space_premium)

Geometry stays on 15m for ALL symbols because moving it to 5m has been measured
twice and lost both times: the standing trendline goes flat, is never spent, the
next line never arms, and campaigns sit on stale targets while a fall runs for
weeks.  5m earns its place as the ENTRY layer only.

Alongside P&L it reports what the book COSTS to run, which is a different
question and the one that decides whether a result is reachable:

    MIN CAPITAL   smallest opening balance that never goes negative, once
                  banked profits are allowed to fund later trades
    PEAK OPEN     the most premium alive at any one instant -- exposure at the
                  worst moment, which is what a margin call actually reads

Both come from PricedCampaign.flows, emitted by the same walk that produces net.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.cascade_upstox import UpstoxAccessError, UpstoxPremiumSource  # noqa: E402
from engine.cascade_options import IndexCandle, NiftyContractResolver  # noqa: E402
from engine.fib_space_cascade import SpaceCascadeConfig, run_space_campaign  # noqa: E402
from tools.fib_space_premium import lot_size_on, price_campaign, resolver_view  # noqa: E402
from tools.fib_space_sweep import (  # noqa: E402
    SYMBOLS,
    find_space_mothers,
    horizon_bars,
    load_bars,
)

GEOMETRY_TIMEFRAME = "15m"
HORIZON_SESSIONS = 120

# A campaign needs enough entry bars for the ladder to have anywhere to walk.
# Below this the window is a stub at the end of the data, not a trade.
MIN_ENTRY_BARS = 30


def apply_cooldown(rows: list, days: int) -> list:
    """Drop campaigns starting within ``days`` of the last ACCEPTED one.

    Accepted, not merely previous: two rejected starts in a row do not shorten
    the wait, or a cluster of near-duplicate mothers would let its own tail
    through -- which is the exact thing the throttle exists to stop.  ``rows``
    must already be in start order.
    """
    if not days:
        return rows
    kept, last = [], None
    for row in rows:
        start = row[0]
        if last is None or (start - last).days >= days:
            kept.append(row)
            last = start
    return kept


def capital_profile(flows: list) -> tuple[float, float, Optional[datetime]]:
    """``(min_capital, peak_open, peak_when)`` for a merged flow timeline.

    ``min_capital`` runs a single account: it is how far the balance dips below
    where it started, so a profit banked in March genuinely pays for April.
    ``peak_open`` ignores that and tracks premium currently bought, which is the
    number a broker sees.  They differ by however much the book has banked by
    the time it is most extended.
    """
    ordered = sorted(flows, key=lambda f: f[0])
    balance, worst = 0.0, 0.0
    for _, amount in ordered:
        balance += amount
        worst = min(worst, balance)

    open_premium, peak, peak_when = 0.0, 0.0, None
    for when, amount in ordered:
        if amount < 0:
            open_premium += -amount
            if open_premium > peak:
                peak, peak_when = open_premium, when
        else:
            open_premium = max(open_premium - amount, 0.0)
    return -worst, peak, peak_when


def run_symbol(symbol: str, *, since: str = "") -> tuple[list, int]:
    """Price every campaign this symbol's configuration produces.

    Returns ``(rows, gaps)`` where a row is ``(first_fill, PricedCampaign)`` in
    start order, already throttled by the symbol's cooldown.
    """
    cfg = SYMBOLS[symbol]
    entry_tf = cfg.get("entry_timeframe", "5m")
    geo_bars = load_bars(GEOMETRY_TIMEFRAME, symbol)
    entry_bars = load_bars(entry_tf, symbol) if entry_tf != GEOMETRY_TIMEFRAME else geo_bars

    mothers = [
        m
        for m in find_space_mothers([IndexCandle(b.timestamp, b.open, b.high, b.low, b.close) for b in geo_bars])
        if not since or m.timestamp.strftime("%Y-%m-%d") >= since
    ]

    source = UpstoxPremiumSource(cache_only=True, underlying_key=cfg["upstox_key"])
    expiries = sorted(source.available_expiries())
    view = resolver_view(symbol)
    span = horizon_bars(GEOMETRY_TIMEFRAME, HORIZON_SESSIONS)

    rows, gaps = [], 0
    for mother in mothers:
        if mother.index + 10 >= len(geo_bars):
            continue
        window = geo_bars[mother.index : mother.index + span + 1]
        if entry_tf == GEOMETRY_TIMEFRAME:
            replay, arm_from, geometry = window, mother.confirmed_index, None
        else:
            replay = [b for b in entry_bars if mother.timestamp <= b.timestamp <= window[-1].timestamp]
            if len(replay) < MIN_ENTRY_BARS:
                continue
            armed = next((i for i, b in enumerate(replay) if b.timestamp >= mother.confirmed_at), None)
            arm_from = replay[armed].index if armed is not None else None
            geometry = window

        lot = lot_size_on(mother.timestamp.date(), symbol)
        result = run_space_campaign(
            geo_bars[mother.index],
            replay,
            SpaceCascadeConfig(lot_size=lot),
            arm_from_index=arm_from,
            geometry_bars=geometry,
        )
        if not result.rounds:
            continue
        resolver = NiftyContractResolver(
            expiries=expiries, strike_step=cfg["strike_step"], lot_size=lot, symbol=cfg["cache"]
        )
        priced = price_campaign(
            result, replay, source, resolver, view, settle_bars=entry_bars, timeframe=entry_tf, symbol=symbol
        )
        if priced.status != "priced":
            gaps += 1
            continue
        rows.append((min(f.timestamp for f in result.fills), priced))

    rows.sort(key=lambda r: r[0])
    return apply_cooldown(rows, int(cfg.get("cooldown_days", 0))), gaps


def summarise(label: str, rows: list, gaps: Optional[int] = None) -> None:
    nets = [p.net for _, p in rows]
    wins = [n for n in nets if n > 0]
    deaths = [p.net for _, p in rows if "expiry_square_off" in p.round_reasons]
    need, peak, peak_when = capital_profile([f for _, p in rows for f in p.flows])
    win_rate = 100.0 * len(wins) / max(len(rows), 1)
    tail = f"  {gaps} gaps" if gaps is not None else ""
    print(f"\n  {label}{tail}")
    print(f"    {len(rows):>4} campaigns   win {win_rate:>5.1f}%   NET Rs {sum(nets):>12,.0f}")
    print(f"    expiry deaths {len(deaths):>3}            Rs {sum(deaths):>12,.0f}")
    print(f"    MIN CAPITAL   Rs {need:>12,.0f}   peak open Rs {peak:>12,.0f}", end="")
    print(f"  ({peak_when:%Y-%m-%d})" if peak_when else "")
    if need:
        print(f"    return on capital {100.0 * sum(nets) / need:>6.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="banknifty,nifty,sensex")
    parser.add_argument("--since", default="", help="only mothers on or after YYYY-MM-DD")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in SYMBOLS]
    if unknown:
        raise SystemExit(f"unknown symbol(s): {', '.join(unknown)}")

    print(f"[book] geometry {GEOMETRY_TIMEFRAME}, entries per-symbol, since={args.since or 'all'}")
    book = []
    for symbol in symbols:
        try:
            rows, gaps = run_symbol(symbol, since=args.since)
        except UpstoxAccessError as exc:
            raise SystemExit(f"Upstox cache unusable for {symbol}: {exc}")
        cfg = SYMBOLS[symbol]
        view = resolver_view(symbol)
        term = "monthly" if view.monthly_only else "weekly"
        cooldown = cfg.get("cooldown_days")
        label = (
            f"{symbol.upper()}  (entries {cfg.get('entry_timeframe', '5m')}, "
            f"{term} {view.min_dte}-{view.max_dte} DTE"
            f"{f', cooldown {cooldown}d' if cooldown else ''})"
        )
        summarise(label, rows, gaps)
        book.extend(rows)

    if len(symbols) > 1:
        summarise("THE BOOK  (all symbols, one account)", book)
    print()


if __name__ == "__main__":
    main()
