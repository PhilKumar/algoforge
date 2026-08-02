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
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from statistics import mean, median
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cascade_costs import OptionCostFill, calculate_nifty_option_basket_round_costs  # noqa: E402
from data.cascade_upstox import UpstoxAccessError, UpstoxPremiumSource  # noqa: E402
from engine.cascade_options import IndexCandle, NiftyContractResolver  # noqa: E402
from engine.fib_space_cascade import SpaceCascadeConfig, run_space_campaign  # noqa: E402
from tools.fib_space_sweep import (  # noqa: E402
    DEFAULT_HORIZON_SESSIONS,
    SYMBOLS,
    find_space_mothers,
    horizon_bars,
    load_bars,
)

CACHE_ONLY = os.environ.get("FIB_SPACE_CACHE_ONLY", "1") != "0"

# NIFTY's lot size as it actually stood -- 50 -> 75 on 2024-11-20, 75 -> 65 on
# 2026-01-01.  A single number is wrong for any sweep crossing those dates.
_LOT_HISTORY = ((date(2026, 1, 1), 65), (date(2024, 11, 20), 75), (date(1900, 1, 1), 50))


def lot_size_on(day: date, symbol: str = "nifty") -> int:
    """Contract lot as it stood on a trade date.

    NIFTY's steps are documented and dated.  BANKNIFTY is carried at the repo's
    flat 30 (tools/fib_cascade_sweep.py) because no dated table for it exists
    here -- a constant scales that symbol's rupees uniformly and so cannot
    change which timeframe wins, but it is not a precise cash figure.
    """
    if symbol != "nifty":
        return 30
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
    # How the campaign ENDED: the final round's reason.  Earlier rounds that
    # banked at target are inside the same net figure.
    exit_reason: Optional[str] = None
    gap: Optional[str] = None
    quantity: int = 0
    bars_held: int = 0
    rounds: int = 0
    round_reasons: tuple = ()


# A deep-ITM strike does not print every minute -- 83 of 114 NIFTY 15m campaigns
# were unpriceable purely because the exact minute had no bar, while the strike
# itself was listed in the chain.  So the same rule the live backtest route
# ships is used here: take the option's next real trade inside the fill's own
# candle, else its last real trade up to 10 minutes back.  Every price is a
# genuine print; nothing is interpolated.
_FORWARD_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
_STALE_LIMIT_MINUTES = 10


def _premium_at(source, contract, when: datetime, forward_minutes: int) -> Optional[float]:
    bar = source.lookup(when, contract)
    if bar is not None:
        return float(bar.open)
    for ahead in range(1, max(int(forward_minutes), 1)):
        later = when + timedelta(minutes=ahead)
        if later.date() != when.date():
            break
        bar = source.lookup(later, contract)
        if bar is not None:
            return float(bar.open)
    for back in range(1, _STALE_LIMIT_MINUTES + 1):
        earlier = when - timedelta(minutes=back)
        if earlier.date() != when.date():
            break
        bar = source.lookup(earlier, contract)
        if bar is not None:
            return float(bar.open)
    return None


def price_campaign(
    result, bars_by_index, source, resolver, view, settle_bars=None, timeframe: str = "15m"
) -> PricedCampaign:
    """Price one campaign's ROUNDS on real premium bars, settling at expiry.

    Each round is a complete basket: its own legs, its own exit, its own cost
    round.  The campaign's net is the sum of its rounds -- Phil's 22-Apr-2026
    BankNifty mother banks the 27-Apr rally AND the 6-May rally as two banked
    rounds, where the single-basket pricing saw only one doomed hold.

    ``settle_bars`` is the FULL index series; expiry settlement is read from it
    rather than from the campaign's replay window (see below).
    """
    forward = _FORWARD_MINUTES.get(timeframe, 15)
    priced = PricedCampaign(mother=result.mother_timestamp, status=result.status, bars_held=result.bars_held)
    rounds = getattr(result, "rounds", None)
    if not rounds:
        priced.status = "no_trade"
        return priced

    total_gross, total_costs, quantity = 0.0, 0.0, 0
    reasons = []
    for rnd in rounds:
        legs = []
        for fill in rnd.fills:
            try:
                contract = resolver.select(fill.timestamp, fill.index_price, "CE", view)
            except Exception as exc:  # no monthly in the DTE window
                priced.gap = f"no contract at {fill.timestamp:%Y-%m-%d %H:%M}: {exc}"
                priced.status = "gap"
                return priced
            entry_premium = _premium_at(source, contract, fill.timestamp, forward)
            if entry_premium is None:
                priced.gap = (
                    f"no entry bar {int(contract.strike)}CE {contract.expiry} @ {fill.timestamp:%Y-%m-%d %H:%M}"
                )
                priced.status = "gap"
                return priced
            legs.append((fill, contract, entry_premium))

        expiry = min(c.expiry for _f, c, _p in legs)
        # The trade cannot outlive its contract.  A round still open when the
        # monthly expires is settled at intrinsic against the index -- which
        # for a CE bought above the settlement price is a total loss of
        # premium, and that is exactly the tail the index layer cannot see.
        exit_at = rnd.exit_timestamp
        exit_reason = rnd.exit_reason or "open_at_end"
        expiry_close = datetime.combine(expiry, dt_time(15, 15))
        if exit_at is None or exit_at > expiry_close:
            exit_at = expiry_close
            exit_reason = "expiry_square_off"

        settle_index = None
        if exit_reason == "expiry_square_off":
            # Settlement must be read from the FULL series, never the replay
            # window: a monthly bought at 45 DTE expires after a 30-session
            # window ends, and taking that window's last bar prices the
            # settlement on the wrong day at the wrong price -- silently, on
            # the one term that dominates this strategy's P&L.
            series = settle_bars if settle_bars is not None else bars_by_index
            candidates = [b for b in series if b.timestamp <= expiry_close]
            settle_index = candidates[-1].close if candidates else None
            if settle_index is None or candidates[-1].timestamp.date() < expiry:
                # The data ends before this contract expires: its outcome is
                # genuinely unknown, so it is a gap, not a loss we invented.
                priced.gap = f"index data ends before expiry {expiry}"
                priced.status = "gap"
                return priced

        buys, sell_value, round_quantity, lots_total = [], 0.0, 0, 0
        for fill, contract, entry_premium in legs:
            if exit_reason == "expiry_square_off":
                exit_premium = max(float(settle_index) - float(contract.strike), 0.0)
            else:
                exit_premium = _premium_at(source, contract, exit_at, forward)
                if exit_premium is None:
                    priced.gap = f"no exit bar {int(contract.strike)}CE {contract.expiry} @ {exit_at:%Y-%m-%d %H:%M}"
                    priced.status = "gap"
                    return priced
            buys.append(OptionCostFill(price=entry_premium, quantity=fill.quantity, lots=fill.lots))
            sell_value += exit_premium * fill.quantity
            round_quantity += fill.quantity
            lots_total += fill.lots

        total_gross += sell_value - sum(b.price * b.quantity for b in buys)
        total_costs += calculate_nifty_option_basket_round_costs(
            buys=buys,
            sell_price=sell_value / round_quantity,
            sell_quantity=round_quantity,
            sell_lots=lots_total,
        ).total
        quantity += round_quantity
        reasons.append(exit_reason)

    priced.gross = round(total_gross, 2)
    priced.costs = round(total_costs, 2)
    priced.net = round(total_gross - total_costs, 2)
    priced.exit_reason = reasons[-1]
    priced.round_reasons = tuple(reasons)
    priced.rounds = len(reasons)
    priced.quantity = quantity
    priced.status = "priced"
    return priced


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", default="15m", choices=("5m", "15m", "1h"))
    parser.add_argument("--symbol", default="nifty", choices=sorted(SYMBOLS))
    parser.add_argument("--target-mode", default="avg_entry", choices=("avg_entry", "structure"))
    parser.add_argument("--horizon-sessions", type=int, default=DEFAULT_HORIZON_SESSIONS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-bars-held", type=int, default=0, help="time stop, in bars (0=none)")
    args = parser.parse_args()

    cfg = SYMBOLS[args.symbol]
    bars = load_bars(args.tf, args.symbol)
    span = horizon_bars(args.tf, args.horizon_sessions)
    index_candles = [IndexCandle(b.timestamp, b.open, b.high, b.low, b.close) for b in bars]
    mothers = find_space_mothers(index_candles)
    if args.limit:
        mothers = mothers[: args.limit]

    try:
        source = UpstoxPremiumSource(
            cache_only=CACHE_ONLY,
            underlying_key=cfg["upstox_key"],
            # Stale 20-day cache files must be re-fetched under the wider window,
            # otherwise the lookback fix changes nothing on an already-warm cache.
            backfill_missing=not CACHE_ONLY,
        )
        expiries = sorted(source.available_expiries())
    except UpstoxAccessError as exc:
        raise SystemExit(f"Upstox cache unusable: {exc}")
    print(f"[premiums] cache-only, {len(expiries)} expiries on disk")

    view = _ResolverView(strike_step=cfg["strike_step"])
    config = SpaceCascadeConfig(target_mode=args.target_mode)
    rows = []
    for mother in mothers:
        start = mother.index
        if start + 10 >= len(bars):
            continue
        window = bars[start : start + span + 1]
        lot = lot_size_on(mother.timestamp.date(), args.symbol)
        result = run_space_campaign(
            bars[start],
            window,
            SpaceCascadeConfig(lot_size=lot, target_mode=config.target_mode, max_bars_held=args.max_bars_held),
            arm_from_index=mother.confirmed_index,
        )
        if not result.fills:
            continue
        resolver = NiftyContractResolver(
            expiries=expiries, strike_step=cfg["strike_step"], lot_size=lot, symbol=cfg["cache"]
        )
        rows.append(price_campaign(result, window, source, resolver, view, settle_bars=bars, timeframe=args.tf))

    priced = [r for r in rows if r.status == "priced"]
    gaps = [r for r in rows if r.status == "gap"]

    print("\n" + "=" * 64)
    stop = f", time-stop {args.max_bars_held} bars" if args.max_bars_held else ", no time stop"
    print(f"CONVERGING-FIB SPACE -- REAL PREMIUMS, {args.symbol.upper()} {args.tf} ({args.target_mode}{stop})")
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
    round_counts = Counter(r.rounds for r in priced)
    banked = Counter(reason for r in priced for reason in r.round_reasons)
    print(f"\n  rounds per campaign: {dict(sorted(round_counts.items()))}")
    print(f"  round outcomes: {dict(banked.most_common())}")
    reasons = Counter(r.exit_reason for r in priced)
    print(f"\n  final-round exit reasons: {dict(reasons)}")
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
