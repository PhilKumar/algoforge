"""tools/fib_space_premium.py -- put rupees on the converging-fib space design.

Layer 1 (tools/fib_space_sweep.py) measures the design in index points.  That
number has misled this project before: the auto-geometry fib cascade showed a
70% win rate on 15m and still lost Rs 10 lakh, because the winners were small,
the losers expired worthless, and theta ate the long holds.  So every campaign
here is priced on real Upstox expired-option bars.

    python3 tools/fib_space_premium.py --tf 15m
    python3 tools/fib_space_premium.py --tf 15m --target-mode structure

Runs OFFLINE against tools/.upstox_cache (cache_only), so it never mints an
Upstox token and never touches Dhan.  A leg the cache cannot price is reported
as a gap, never guessed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from statistics import mean, median
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cascade_costs import OptionCostFill, calculate_nifty_option_basket_round_costs  # noqa: E402
from data.cascade_upstox import UpstoxAccessError, UpstoxPremiumSource  # noqa: E402
from engine.cascade_mothers import find_mother_candles  # noqa: E402
from engine.cascade_options import IndexCandle, NiftyContractResolver  # noqa: E402
from engine.fib_space_cascade import SpaceCascadeConfig, run_space_campaign  # noqa: E402
from tools.fib_space_sweep import DEFAULT_HORIZON_BARS, load_bars  # noqa: E402

CACHE_ONLY = os.environ.get("FIB_SPACE_CACHE_ONLY", "1") != "0"

# NIFTY's lot size as it actually stood -- 50 -> 75 on 2024-11-20, 75 -> 65 on
# 2026-01-01.  A single number is wrong for any sweep crossing those dates.
_LOT_HISTORY = ((date(2026, 1, 1), 65), (date(2024, 11, 20), 75), (date(1900, 1, 1), 50))


def lot_size_on(day: date) -> int:
    for start, size in _LOT_HISTORY:
        if day >= start:
            return size
    return 50


@dataclass
class _ResolverView:
    """The handful of fields NiftyContractResolver.select reads."""

    itm_steps: int = 2
    strike_step: float = 50.0
    min_dte: int = 15
    max_dte: int = 45
    monthly_only: bool = True


@dataclass
class PricedCampaign:
    mother: datetime
    status: str
    gross: Optional[float] = None
    costs: Optional[float] = None
    net: Optional[float] = None
    exit_reason: Optional[str] = None
    gap: Optional[str] = None
    quantity: int = 0
    bars_held: int = 0


def price_campaign(result, bars_by_index, source, resolver, view) -> PricedCampaign:
    """Price one campaign's legs on real premium bars, settling at expiry."""
    priced = PricedCampaign(mother=result.mother_timestamp, status=result.status, bars_held=result.bars_held)
    if not result.fills:
        priced.status = "no_trade"
        return priced

    legs = []
    for fill in result.fills:
        try:
            contract = resolver.select(fill.timestamp, fill.index_price, "CE", view)
        except Exception as exc:  # no monthly in the DTE window
            priced.gap = f"no contract at {fill.timestamp:%Y-%m-%d %H:%M}: {exc}"
            priced.status = "gap"
            return priced
        bar = source.lookup(fill.timestamp, contract)
        if bar is None:
            priced.gap = f"no entry bar {int(contract.strike)}CE {contract.expiry} @ {fill.timestamp:%Y-%m-%d %H:%M}"
            priced.status = "gap"
            return priced
        legs.append((fill, contract, float(bar.open)))

    expiry = min(c.expiry for _f, c, _p in legs)
    # The trade cannot outlive its contract.  A campaign still open when the
    # monthly expires is settled at intrinsic against the index -- which for a
    # CE bought above the settlement price is a total loss of premium, and that
    # is exactly the tail the index-space layer cannot see.
    exit_at = result.exit_timestamp
    exit_reason = result.exit_reason or "open_at_end"
    expiry_close = datetime.combine(expiry, dt_time(15, 15))
    if exit_at is None or exit_at > expiry_close:
        exit_at = expiry_close
        exit_reason = "expiry_square_off"

    settle_index = None
    if exit_reason == "expiry_square_off":
        candidates = [b for b in bars_by_index if b.timestamp <= expiry_close]
        settle_index = candidates[-1].close if candidates else None
        if settle_index is None:
            priced.gap = "no index bar at expiry"
            priced.status = "gap"
            return priced

    buys, sell_value, quantity, lots_total = [], 0.0, 0, 0
    for fill, contract, entry_premium in legs:
        if exit_reason == "expiry_square_off":
            exit_premium = max(float(settle_index) - float(contract.strike), 0.0)
        else:
            bar = source.lookup(exit_at, contract)
            if bar is None:
                priced.gap = f"no exit bar {int(contract.strike)}CE {contract.expiry} @ {exit_at:%Y-%m-%d %H:%M}"
                priced.status = "gap"
                return priced
            exit_premium = float(bar.open)
        buys.append(OptionCostFill(price=entry_premium, quantity=fill.quantity, lots=fill.lots))
        sell_value += exit_premium * fill.quantity
        quantity += fill.quantity
        lots_total += fill.lots

    gross = sell_value - sum(b.price * b.quantity for b in buys)
    costs = calculate_nifty_option_basket_round_costs(
        buys=buys,
        sell_price=sell_value / quantity,
        sell_quantity=quantity,
        sell_lots=lots_total,
    )
    priced.gross = round(gross, 2)
    priced.costs = round(costs.total, 2)
    priced.net = round(gross - costs.total, 2)
    priced.exit_reason = exit_reason
    priced.quantity = quantity
    priced.status = "priced"
    return priced


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", default="15m")
    parser.add_argument("--target-mode", default="avg_entry", choices=("avg_entry", "structure"))
    parser.add_argument("--horizon-bars", type=int, default=DEFAULT_HORIZON_BARS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-bars-held", type=int, default=0, help="time stop, in bars (0=none)")
    args = parser.parse_args()

    bars = load_bars(args.tf)
    index_candles = [IndexCandle(b.timestamp, b.open, b.high, b.low, b.close) for b in bars]
    mothers = find_mother_candles(index_candles)
    if args.limit:
        mothers = mothers[: args.limit]

    try:
        source = UpstoxPremiumSource(cache_only=CACHE_ONLY)
        expiries = sorted(source.available_expiries())
    except UpstoxAccessError as exc:
        raise SystemExit(f"Upstox cache unusable: {exc}")
    print(f"[premiums] cache-only, {len(expiries)} expiries on disk")

    view = _ResolverView()
    config = SpaceCascadeConfig(target_mode=args.target_mode)
    rows = []
    for mother in mothers:
        start = mother.index
        if start + 10 >= len(bars):
            continue
        window = bars[start : start + args.horizon_bars + 1]
        lot = lot_size_on(mother.timestamp.date())
        result = run_space_campaign(
            bars[start],
            window,
            SpaceCascadeConfig(lot_size=lot, target_mode=config.target_mode, max_bars_held=args.max_bars_held),
            arm_from_index=mother.confirmed_index,
        )
        if not result.fills:
            continue
        resolver = NiftyContractResolver(expiries=expiries, strike_step=50.0, lot_size=lot, symbol="NIFTY")
        rows.append(price_campaign(result, window, source, resolver, view))

    priced = [r for r in rows if r.status == "priced"]
    gaps = [r for r in rows if r.status == "gap"]

    print("\n" + "=" * 64)
    stop = f", time-stop {args.max_bars_held} bars" if args.max_bars_held else ", no time stop"
    print(f"CONVERGING-FIB SPACE -- REAL PREMIUMS, NIFTY {args.tf} ({args.target_mode}{stop})")
    print("=" * 64)
    print(f"campaigns that traded   {len(rows)}")
    print(f"  priced in full        {len(priced)}")
    print(f"  unpriceable (gaps)    {len(gaps)}")
    if not priced:
        for row in gaps[:5]:
            print(f"    e.g. {row.gap}")
        return

    nets = [r.net for r in priced]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    print(f"\n  NET P&L (after costs)  Rs {sum(nets):,.2f}")
    print(f"  gross                  Rs {sum(r.gross for r in priced):,.2f}")
    print(f"  costs                  Rs {sum(r.costs for r in priced):,.2f}")
    print(f"\n  wins {len(wins)} / losses {len(losses)}   win rate {100 * len(wins) / len(priced):.1f}%")
    if wins:
        print(f"  mean win   Rs {mean(wins):>12,.2f}   biggest Rs {max(wins):,.2f}")
    if losses:
        print(f"  mean loss  Rs {mean(losses):>12,.2f}   worst   Rs {min(losses):,.2f}")
    print(f"  median trade Rs {median(nets):,.2f}")
    reasons = Counter(r.exit_reason for r in priced)
    print(f"\n  exit reasons: {dict(reasons)}")
    for reason in reasons:
        subset = [r.net for r in priced if r.exit_reason == reason]
        print(f"    {reason:<20} n={len(subset):<4} net Rs {sum(subset):>14,.2f}")
    if gaps:
        print("\n  first gaps:")
        for row in gaps[:3]:
            print(f"    {row.gap}")
    print()


if __name__ == "__main__":
    main()
