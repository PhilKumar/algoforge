"""tools/fib_cascade_sweep.py -- replay the FIB-BOUNDARY option cascade (the
CryptoForge geometry: auto trendline -> touch -> fib deep levels 2/4/8, 1/2/3
lot ladder, ATM-2 per entry) over months of real index data, for NIFTY or
BANKNIFTY.

This is the sweep for the SAME engine the fib-boundary tab and the historical
backtest run -- `NiftyOptionsPaperCascade` over `NiftyIndexCascadeGeometry`.
It is NOT the candle-entry cascade that tools/cascade_backtest_nifty.py drives
(that one is `OneHourCascade`, a different strategy). Every mother is found by
the same swing-high scanner, so the campaign starts are the rules', not the eye's.

  --symbol nifty       weekly expiry, 65-unit lot, 50-point strikes
  --symbol banknifty   MONTHLY expiry, 30-unit lot, 100-point strikes

  Layer 1 (default)    A flat premium drives the state machine, so every
                       fill/round is the REAL index-space geometry with no rupee
                       claim.  Expiry dates still come from Upstox so the
                       target-vs-expiry split is honest.
  Layer 2 (--premium)  Real Upstox expired-option premiums price every leg.
                       Rupee P&L only for campaigns Upstox priced in full; a
                       missing strike/expiry is a recorded gap, never faked.

  --single-shot        One trade per mother: the first boundary fills, rides to
                       its target or expiry, and the mother is done -- no deeper
                       level buys and no re-armed round.

    python3 tools/fib_cascade_sweep.py --symbol nifty     --from 2025-09-01 --to 2026-07-30 --tf 15m --premium
    python3 tools/fib_cascade_sweep.py --symbol banknifty --from 2025-09-01 --to 2026-07-30 --tf 15m --premium
    python3 tools/fib_cascade_sweep.py --symbol nifty     --from 2025-09-01 --to 2026-07-30 --tf 15m --premium --single-shot

Candles cache under tools/.nifty_cache, so repeat runs are offline for candles.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_mothers import MotherCandidate, find_mother_candles  # noqa: E402
from engine.cascade_options import (  # noqa: E402
    CascadeConfig,
    CascadeOptionsAdapter,
    FixedCampaignOption,
    IndexCandle,
    NiftyContractResolver,
    NiftyOptionsPaperCascade,
    PaperCascadeConfig,
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nifty_cache")

# Per-underlying contract facts. NIFTY is weekly; BANKNIFTY moved to MONTHLY-only
# expiry, so its DTE window spans a whole month and a campaign can run ~30 days.
SYMBOLS = {
    "nifty": dict(
        cache="NIFTY",
        security_id="13",
        upstox_key="NSE_INDEX|Nifty 50",
        lot_size=65,
        strike_step=50.0,
        min_dte=7,
        max_dte=13,
        max_days=20,
    ),
    "banknifty": dict(
        cache="BANKNIFTY",
        security_id="25",
        upstox_key="NSE_INDEX|Nifty Bank",
        lot_size=30,
        strike_step=100.0,
        min_dte=5,
        max_dte=45,
        max_days=35,
    ),
}


@dataclass
class FibOutcome:
    label: str
    mother_timestamp: datetime
    status: str
    rounds: int
    entries: int
    lots: int
    deepest_level: Optional[int]
    net_pnl: Optional[float]
    gross_pnl: Optional[float]
    costs: float
    target_rounds: int
    expiry_rounds: int
    fully_priced: bool
    gaps: int

    @property
    def traded(self) -> bool:
        return self.entries > 0


def load_index_candles(cfg: dict, timeframe: str, from_date: str, to_date: str, *, refetch: bool) -> list[IndexCandle]:
    """Cached index candles for one underlying, fetched from Dhan on a miss."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{cfg['cache']}_{timeframe}_{from_date}_{to_date}.json")
    if os.path.exists(path) and not refetch:
        with open(path, "r", encoding="utf-8") as handle:
            rows = json.load(handle)
        return [IndexCandle(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4]) for r in rows]
    from broker.dhan import DhanClient
    from data.cascade_dhan import DhanOneHourSource

    print(f"[fetch] {cfg['cache']} {timeframe} {from_date} -> {to_date} (secId={cfg['security_id']})")
    source = DhanOneHourSource(DhanClient(), nifty_security_id=cfg["security_id"])
    fetched = source.fetch_index_cascade(from_date, to_date, [timeframe])
    candles = [IndexCandle(c.timestamp, c.open, c.high, c.low, c.close) for c in fetched[timeframe]]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([[c.timestamp.isoformat(), c.open, c.high, c.low, c.close] for c in candles], handle)
    return candles


def _level_of(fill) -> Optional[int]:
    for key in fill.rung_keys:
        try:
            return int(str(key).split(":")[1])
        except (IndexError, ValueError):
            continue
    return None


def replay_one_fib(
    mother: MotherCandidate,
    index_series: list[IndexCandle],
    cfg: dict,
    *,
    timeframe: str,
    premium_lookup: Callable[[datetime, FixedCampaignOption], Optional[float]],
    expiries: list[date],
    rung_inr: float,
    itm_steps: int,
    single_shot: bool,
    label: str,
) -> FibOutcome:
    # Feed from the mother pivot itself: the fib geometry needs the bars right
    # after it (first dip -> trendline anchor -> touch). This is NOT lookahead --
    # the earliest a leg can FILL is many bars later, after the structure forms
    # and price re-crosses the line, well past the pivot's own confirmation.
    mother_row = next((row for row in index_series if row.timestamp == mother.timestamp), None)
    if mother_row is None:
        mother_row = IndexCandle(mother.timestamp, mother.high, mother.high, mother.low, mother.low)
    horizon = mother.timestamp + timedelta(days=cfg["max_days"])
    forward = [row for row in index_series if mother.timestamp < row.timestamp <= horizon]

    resolver_config = CascadeConfig(
        mother_timestamp=mother.timestamp,
        mother_high=mother.high,
        mother_low=mother.low,
        option_type="CE",
        timeframe=timeframe,
        itm_steps=itm_steps,
        strike_step=cfg["strike_step"],
        lot_size=cfg["lot_size"],
        min_dte=cfg["min_dte"],
        max_dte=cfg["max_dte"],
    )
    resolver = NiftyContractResolver(
        expiries=expiries, strike_step=cfg["strike_step"], lot_size=cfg["lot_size"], symbol=cfg["cache"]
    )

    def to_fixed(contract) -> FixedCampaignOption:
        return FixedCampaignOption(
            cfg["cache"], int(contract.strike), contract.expiry, contract.option_type, int(contract.lot_size), ""
        )

    def select(_timestamp, index_price) -> FixedCampaignOption:
        # Strike is ATM-N at THIS fill's index; expiry stays the mother's next
        # expiry (weekly for NIFTY, monthly for BANKNIFTY) for the whole campaign.
        return to_fixed(resolver.select(mother.timestamp, index_price, "CE", resolver_config))

    try:
        initial = select(mother.timestamp, mother_row.close)
    except Exception:
        return FibOutcome(label, mother.timestamp, "no_strike", 0, 0, 0, None, None, None, 0.0, 0, 0, False, 0)

    adapter = CascadeOptionsAdapter(None, paper_only=True)
    engine = NiftyOptionsPaperCascade(
        mother_row,
        initial,
        adapter,
        premium_lookup,
        PaperCascadeConfig(
            rung_inr=rung_inr,
            ce_offset_steps=-itm_steps,
            target_fraction=0.25,
            lot_ladder=True,
            per_entry_strike=True,
            single_shot=single_shot,
        ),
        contract_selector=select,
    ).run(forward)

    all_fills = [fill for row in engine.rounds for fill in row.fills] + list(engine.open_fills)
    levels = [lvl for lvl in (_level_of(f) for f in all_fills) if lvl is not None]
    gaps = sum(1 for ev in engine.events if ev.get("event") == "option_quote_missing")
    net = round(sum(r.net_pnl for r in engine.rounds), 2) if engine.rounds else None
    gross = round(sum(r.gross_pnl for r in engine.rounds), 2) if engine.rounds else None
    costs = round(sum(r.costs.total for r in engine.rounds), 2) if engine.rounds else 0.0
    fully_priced = bool(engine.rounds) and gaps == 0 and all(f.option_premium is not None for f in all_fills)

    return FibOutcome(
        label=label,
        mother_timestamp=mother.timestamp,
        status=str(engine.status).lower(),
        rounds=len(engine.rounds),
        entries=len(all_fills),
        lots=sum(f.lots for f in all_fills),
        deepest_level=max(levels) if levels else None,
        net_pnl=net,
        gross_pnl=gross,
        costs=costs,
        target_rounds=sum(1 for r in engine.rounds if r.exit_reason == "target"),
        expiry_rounds=sum(1 for r in engine.rounds if r.exit_reason == "expiry_square_off"),
        fully_priced=fully_priced,
        gaps=gaps,
    )


def run_sweep(
    index_series: list[IndexCandle],
    cfg: dict,
    *,
    timeframe: str,
    max_concurrent: int,
    scanner_kwargs: dict,
    premium_lookup: Callable[[datetime, FixedCampaignOption], Optional[float]],
    expiries: list[date],
    rung_inr: float,
    itm_steps: int,
    single_shot: bool,
) -> tuple[list[FibOutcome], list[MotherCandidate]]:
    mothers = find_mother_candles(index_series, **scanner_kwargs)
    outcomes: list[FibOutcome] = []
    open_until: list[datetime] = []
    skipped = 0
    for number, mother in enumerate(mothers, start=1):
        start = mother.timestamp
        open_until = [end for end in open_until if end > start]
        if len(open_until) >= max_concurrent:
            skipped += 1
            continue
        outcome = replay_one_fib(
            mother,
            index_series,
            cfg,
            timeframe=timeframe,
            premium_lookup=premium_lookup,
            expiries=expiries,
            rung_inr=rung_inr,
            itm_steps=itm_steps,
            single_shot=single_shot,
            label=f"#{number} {mother.timestamp:%Y-%m-%d %H:%M}",
        )
        outcomes.append(outcome)
        if outcome.traded:
            open_until.append(start + timedelta(days=cfg["max_days"]))
    if skipped:
        print(f"  ({skipped} mothers skipped: max-concurrent {max_concurrent} already deployed)")
    return outcomes, mothers


def report(outcomes: list[FibOutcome], mothers: list[MotherCandidate], *, priced: bool, label: str) -> None:
    traded = [o for o in outcomes if o.traded]
    print(f"\n=== {label} ===")
    print(f"  mothers detected        {len(mothers)}")
    print(f"  campaigns replayed      {len(outcomes)}")
    print(f"  took at least 1 entry   {len(traded)}")
    if not traded:
        print("  no entries -- price never reached a deep boundary before expiry")
        return
    depth = {lvl: sum(1 for o in traded if (o.deepest_level or 0) >= lvl) for lvl in (2, 4, 8)}
    print(f"  reached level           2: {depth[2]}   4: {depth[4]}   8: {depth[8]}")
    total_rounds = sum(o.rounds for o in traded)
    tgt = sum(o.target_rounds for o in traded)
    exp = sum(o.expiry_rounds for o in traded)
    print(f"  rounds (books)          {total_rounds}   target-exit: {tgt}   expiry-exit: {exp}")
    print(f"  entries / lots          {sum(o.entries for o in traded)} / {sum(o.lots for o in traded)}")
    if not priced:
        print("  P&L                     withheld (signal layer -- run with --premium for rupees)")
        return
    fp = [o for o in traded if o.fully_priced and o.net_pnl is not None]
    gapped = [o for o in traded if not o.fully_priced]
    print(f"  fully priced campaigns  {len(fp)}   with gaps (P&L withheld): {len(gapped)}")
    if fp:
        nets = [o.net_pnl for o in fp]
        wins = [n for n in nets if n > 0]
        print(f"  NET P&L (priced only)   Rs {round(sum(nets), 2):,}")
        print(
            f"  gross / costs           Rs {round(sum(o.gross_pnl for o in fp), 2):,} / Rs {round(sum(o.costs for o in fp), 2):,}"
        )
        print(f"  win rate                {len(wins)}/{len(fp)} = {round(100 * len(wins) / len(fp), 1)}%")
        print(f"  avg / median per camp   Rs {round(mean(nets), 2):,} / Rs {round(median(nets), 2):,}")
        print(f"  best / worst            Rs {round(max(nets), 2):,} / Rs {round(min(nets), 2):,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", choices=sorted(SYMBOLS), default="nifty")
    ap.add_argument("--from", dest="from_date", required=True)
    ap.add_argument("--to", dest="to_date", required=True)
    ap.add_argument("--tf", dest="timeframe", default="15m", choices=["5m", "15m", "1h"])
    ap.add_argument("--premium", action="store_true", help="Layer 2: price legs with real Upstox premiums")
    ap.add_argument("--single-shot", action="store_true", help="one trade per mother, no deeper buys / re-arm")
    ap.add_argument("--max-concurrent", type=int, default=999)
    ap.add_argument("--rung-inr", type=float, default=15000.0)
    ap.add_argument("--itm-steps", type=int, default=2)
    ap.add_argument("--left-bars", type=int, default=3)
    ap.add_argument("--right-bars", type=int, default=3)
    ap.add_argument("--min-range-atr", type=float, default=0.8)
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    cfg = SYMBOLS[args.symbol]
    index_series = load_index_candles(cfg, args.timeframe, args.from_date, args.to_date, refetch=args.refetch)
    if not index_series:
        print("No candles in range.")
        return 1
    print(
        f"[data] {args.symbol} {len(index_series)} {args.timeframe} candles "
        f"{index_series[0].timestamp:%Y-%m-%d} -> {index_series[-1].timestamp:%Y-%m-%d}"
    )

    # Expiries come from Upstox for BOTH layers so the weekly/monthly cadence is
    # the real one; Layer 1 still prices nothing.
    from pathlib import Path

    from data.cascade_upstox import UpstoxPremiumSource

    try:
        from upstox_token_manager import ensure_fresh_token

        ensure_fresh_token()
    except Exception as exc:
        print(f"[upstox] token pre-check skipped: {exc}")
    # IMPORTANT: the Upstox source caches expiries.json and contracts_<expiry>.json
    # WITHOUT an underlying prefix, so NIFTY and BANKNIFTY would collide on a
    # shared dir (BANKNIFTY would read NIFTY's chain). NIFTY keeps the root cache
    # (its 244 files are already there); every other underlying gets its own dir.
    upstox_cache = Path(os.path.dirname(os.path.abspath(__file__))) / ".upstox_cache"
    if args.symbol != "nifty":
        upstox_cache = upstox_cache / cfg["cache"]
    source = UpstoxPremiumSource(underlying_key=cfg["upstox_key"], cache_dir=upstox_cache)
    expiries = sorted(source.available_expiries())
    if not expiries:
        print(f"[upstox] no expiry coverage for {cfg['upstox_key']}.")
        return 1
    print(f"[upstox] {len(expiries)} {args.symbol} expiries: {expiries[0]} -> {expiries[-1]}")

    if args.premium:

        def premium_lookup(timestamp, contract):
            bar = source.lookup(timestamp, contract)
            return float(bar.open) if bar is not None else None
    else:

        def premium_lookup(_timestamp, _contract):
            return 100.0  # flat: drives the geometry, prices nothing

    scanner_kwargs = dict(left_bars=args.left_bars, right_bars=args.right_bars, min_range_atr=args.min_range_atr)
    outcomes, mothers = run_sweep(
        index_series,
        cfg,
        timeframe=args.timeframe,
        max_concurrent=args.max_concurrent,
        scanner_kwargs=scanner_kwargs,
        premium_lookup=premium_lookup,
        expiries=expiries,
        rung_inr=args.rung_inr,
        itm_steps=args.itm_steps,
        single_shot=args.single_shot,
    )
    layer = "Layer 2 (real Upstox premiums)" if args.premium else "Layer 1 (signal geometry, no P&L)"
    mode = "SINGLE-SHOT (1 trade/mother)" if args.single_shot else "full cascade"
    report(
        outcomes,
        mothers,
        priced=args.premium,
        label=f"{args.symbol.upper()} {args.timeframe} · {mode} · {layer}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
