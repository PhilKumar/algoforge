"""tools/two_red_sweep.py -- replay the TWO-RED CLIMBING LADDER over months of
real index data, the same way tools/fib_cascade_sweep.py replays the fib
cascade.

The strategy is `TwoRedLadder` (engine/candle_ladder.py), which is what the Test
Bench and the Cascade "Candle entry" tab run:

    two reds on the base chart -> buy the recovery above the second red's high
    -> climb one timeframe -> two reds there -> buy again, bigger
    -> 1 / 2 / 3 / 4 lots across 1m -> 5m -> 15m -> 1H
    -> exit a quarter of the way back to the mother's high, or at expiry

Two things differ from the fib sweep and both matter when reading the numbers:

  * SIZE IS LOTS, NOT RUPEES. The fib cascade sizes each level to a rupee
    budget; this ladder buys a fixed 1/2/3/4 lots whatever the premium costs.
    So there is no --rung-inr here, and deployed capital is reported instead.
  * THE LADDER NEEDS EVERY CHART AT ONCE. Rung 1 watches the base timeframe
    while rung 2 watches the next one up, so all four series are loaded and
    merged into one stream ordered by when each bar CLOSED -- never derived
    from the fast series, so the replay sees the same bars Dhan would serve.

Mothers come from the same scanners as the fib sweep, so campaign starts are the
rules', not the eye's:

    --mother-rule wick    a bullish run, then a candle whose upper wick is over
                          half its range (Phil's rule; confirmed at its own close)
    --mother-rule swing   swing-high pivot, confirmed `right_bars` later

    python3 tools/two_red_sweep.py --from 2026-01-01 --to 2026-07-30 --tf 5m --mother-rule wick --premium
    python3 tools/two_red_sweep.py --from 2026-01-01 --to 2026-07-30 --tf 1m --mother-rule wick --premium

Candles share tools/.nifty_cache with the fib sweep, so repeat runs are offline.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.candle_ladder import LadderCandle, TwoRedLadder, ladder_from  # noqa: E402
from engine.cascade_mothers import MotherCandidate, find_mother_candles, find_wick_mothers  # noqa: E402
from engine.cascade_options import (  # noqa: E402
    CascadeConfig,
    FixedCampaignOption,
    NiftyContractResolver,
)
from tools.fib_cascade_sweep import SYMBOLS, load_index_candles  # noqa: E402


@dataclass
class LadderOutcome:
    label: str
    mother_timestamp: datetime
    status: str
    rungs: int
    lots: int
    deepest_timeframe: Optional[str]
    deployed: Optional[float]  # rupees actually spent on premium
    net_pnl: Optional[float]
    gross_pnl: Optional[float]
    costs: float
    exit_reason: Optional[str]
    fully_priced: bool

    @property
    def traded(self) -> bool:
        return self.rungs > 0


def build_stream(
    series_by_tf: dict[str, list],
    stages: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> list[LadderCandle]:
    """Every stage's bars between two moments, ordered by when each CLOSED."""
    from engine.candle_ladder import order_events

    rows: list[LadderCandle] = []
    for timeframe in stages:
        for candle in series_by_tf[timeframe]:
            if start < candle.timestamp <= end:
                rows.append(
                    LadderCandle(timeframe, candle.timestamp, candle.open, candle.high, candle.low, candle.close)
                )
    return order_events(rows)


def replay_one_ladder(
    mother: MotherCandidate,
    mother_row,
    series_by_tf: dict[str, list],
    cfg: dict,
    *,
    timeframe: str,
    stages: tuple[str, ...],
    premium_lookup: Callable[[datetime, FixedCampaignOption], Optional[float]],
    expiries: list[date],
    itm_steps: int,
    label: str,
) -> LadderOutcome:
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
    try:
        anchor = resolver.select(mother.timestamp, mother_row.close, "CE", resolver_config)
    except Exception:
        return LadderOutcome(label, mother.timestamp, "no_strike", 0, 0, None, None, None, None, 0.0, None, False)
    expiry = anchor.expiry

    def strike_for(_timestamp, index_price) -> tuple[int, str]:
        contract = resolver.select(mother.timestamp, index_price, "CE", resolver_config)
        return int(contract.strike), contract.option_type

    def lookup(timestamp, strike, option_type) -> Optional[float]:
        return premium_lookup(
            timestamp,
            FixedCampaignOption(cfg["cache"], int(strike), expiry, option_type, int(cfg["lot_size"]), ""),
        )

    horizon = min(
        mother.timestamp + timedelta(days=cfg["max_days"]),
        datetime.combine(expiry, datetime.min.time()) + timedelta(hours=15, minutes=30),
    )
    replay = build_stream(series_by_tf, stages, mother.timestamp, horizon)
    if not replay:
        return LadderOutcome(label, mother.timestamp, "no_bars", 0, 0, None, None, None, None, 0.0, None, False)

    mother_bar = LadderCandle(
        timeframe, mother_row.timestamp, mother_row.open, mother_row.high, mother_row.low, mother_row.close
    )
    ladder = TwoRedLadder(
        mother_bar,
        stages=stages,
        strike_for=strike_for,
        premium_lookup=lookup,
        lot_size=int(cfg["lot_size"]),
    ).run(replay)
    # Only a window that actually reached the expiry may be squared off there.
    if ladder.fills and ladder.status not in {"CLOSED", "EXPIRED", "KILLED"}:
        last = replay[-1]
        if last.timestamp.date() >= expiry:
            ladder.close_at_expiry(last, last.close)

    priced = [fill for fill in ladder.fills if fill.option_premium is not None]
    deployed = round(sum(fill.option_premium * fill.quantity for fill in priced), 2) if priced else None
    fully_priced = bool(ladder.fills) and len(priced) == len(ladder.fills) and ladder.net_pnl is not None
    return LadderOutcome(
        label=label,
        mother_timestamp=mother.timestamp,
        status=str(ladder.status).lower(),
        rungs=len(ladder.fills),
        lots=sum(fill.lots for fill in ladder.fills),
        deepest_timeframe=ladder.fills[-1].timeframe if ladder.fills else None,
        deployed=deployed,
        net_pnl=ladder.net_pnl,
        gross_pnl=ladder.gross_pnl,
        costs=round(ladder.costs.total, 2) if ladder.costs else 0.0,
        exit_reason=ladder.exit_reason,
        fully_priced=fully_priced,
    )


def report(outcomes: list[LadderOutcome], mothers: list[MotherCandidate], *, priced: bool, label: str) -> None:
    traded = [row for row in outcomes if row.traded]
    print(f"\n=== {label} ===")
    print(f"  mothers detected        {len(mothers)}")
    print(f"  campaigns replayed      {len(outcomes)}")
    print(f"  took at least 1 rung    {len(traded)}")
    if not traded:
        print("  no entries -- no mother ever produced two reds and a recovery")
        return
    depth = {rung: sum(1 for row in traded if row.rungs >= rung) for rung in (1, 2, 3, 4)}
    print(f"  climbed to rung         1: {depth[1]}   2: {depth[2]}   3: {depth[3]}   4: {depth[4]}")
    print(f"  rungs / lots            {sum(row.rungs for row in traded)} / {sum(row.lots for row in traded)}")
    reasons: dict[str, int] = {}
    for row in traded:
        reasons[row.exit_reason or row.status] = reasons.get(row.exit_reason or row.status, 0) + 1
    print(f"  exits                   {reasons}")
    if not priced:
        print("  P&L                     withheld (signal layer -- run with --premium for rupees)")
        return
    fp = [row for row in traded if row.fully_priced]
    print(f"  fully priced campaigns  {len(fp)}   with gaps (P&L withheld): {len(traded) - len(fp)}")
    if not fp:
        return
    nets = [row.net_pnl for row in fp]
    wins = [value for value in nets if value > 0]
    spends = [row.deployed for row in fp if row.deployed is not None]
    print(f"  NET P&L (priced only)   Rs {round(sum(nets), 2):,}")
    print(
        f"  gross / costs           Rs {round(sum(row.gross_pnl for row in fp), 2):,} / "
        f"Rs {round(sum(row.costs for row in fp), 2):,}"
    )
    print(f"  win rate                {len(wins)}/{len(fp)} = {round(100 * len(wins) / len(fp), 1)}%")
    print(f"  avg / median per camp   Rs {round(mean(nets), 2):,} / Rs {round(median(nets), 2):,}")
    print(f"  best / worst            Rs {round(max(nets), 2):,} / Rs {round(min(nets), 2):,}")
    if spends:
        print(f"  capital per campaign    avg Rs {round(mean(spends), 2):,}   max Rs {round(max(spends), 2):,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", choices=sorted(SYMBOLS), default="nifty")
    ap.add_argument("--from", dest="from_date", required=True)
    ap.add_argument("--to", dest="to_date", required=True)
    ap.add_argument("--tf", dest="timeframe", default="5m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--premium", action="store_true", help="price every rung with real Upstox premiums")
    ap.add_argument("--mother-rule", choices=["swing", "wick"], default="wick")
    ap.add_argument("--itm-steps", type=int, default=2)
    ap.add_argument("--max-concurrent", type=int, default=999)
    ap.add_argument("--left-bars", type=int, default=3)
    ap.add_argument("--right-bars", type=int, default=3)
    ap.add_argument("--min-range-atr", type=float, default=0.8)
    ap.add_argument("--min-separation-bars", type=int, default=0)
    ap.add_argument("--run-bars", type=int, default=4)
    ap.add_argument("--min-run-green", type=int, default=3)
    ap.add_argument("--min-run-atr", type=float, default=1.5)
    ap.add_argument("--min-wick", type=float, default=0.5)
    ap.add_argument("--allow-overnight-run", action="store_true")
    ap.add_argument("--min-dte", type=int, default=None)
    ap.add_argument("--max-dte", type=int, default=None)
    ap.add_argument("--upstox-cache", default=None, help="separate cache dir, so two sweeps can run at once")
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    cfg = dict(SYMBOLS[args.symbol])
    if args.min_dte is not None:
        cfg["min_dte"] = args.min_dte
    if args.max_dte is not None:
        cfg["max_dte"] = args.max_dte
    print(f"[dte] contract picked {cfg['min_dte']}-{cfg['max_dte']} days from expiry, measured at the mother")
    stages = ladder_from(args.timeframe, 4)
    series_by_tf = {
        stage: load_index_candles(cfg, stage, args.from_date, args.to_date, refetch=args.refetch) for stage in stages
    }
    base = series_by_tf[args.timeframe]
    if not base:
        print("No candles in range.")
        return 1
    print(
        f"[data] {args.symbol} ladder {' -> '.join(stages)}; base {args.timeframe} "
        f"{len(base)} candles {base[0].timestamp:%Y-%m-%d} -> {base[-1].timestamp:%Y-%m-%d}"
    )

    from pathlib import Path

    from data.cascade_upstox import UpstoxPremiumSource

    try:
        from upstox_token_manager import ensure_fresh_token

        ensure_fresh_token()
    except Exception as exc:
        print(f"[upstox] token pre-check skipped: {exc}")
    upstox_cache = Path(args.upstox_cache or (Path(os.path.dirname(os.path.abspath(__file__))) / ".upstox_cache"))
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
            return 100.0

    if args.mother_rule == "wick":
        mothers = find_wick_mothers(
            base,
            run_bars=args.run_bars,
            min_run_green=args.min_run_green,
            min_run_atr=args.min_run_atr,
            min_wick_fraction=args.min_wick,
            min_range_atr=args.min_range_atr,
            min_separation_bars=args.min_separation_bars,
            same_session_only=not args.allow_overnight_run,
        )
    else:
        mothers = find_mother_candles(
            base,
            left_bars=args.left_bars,
            right_bars=args.right_bars,
            min_range_atr=args.min_range_atr,
            min_separation_bars=args.min_separation_bars,
        )

    by_time = {candle.timestamp: candle for candle in base}
    outcomes: list[LadderOutcome] = []
    open_until: list[datetime] = []
    skipped = 0
    for number, mother in enumerate(mothers, start=1):
        open_until = [end for end in open_until if end > mother.timestamp]
        if len(open_until) >= args.max_concurrent:
            skipped += 1
            continue
        mother_row = by_time.get(mother.timestamp)
        if mother_row is None:
            continue
        outcome = replay_one_ladder(
            mother,
            mother_row,
            series_by_tf,
            cfg,
            timeframe=args.timeframe,
            stages=stages,
            premium_lookup=premium_lookup,
            expiries=expiries,
            itm_steps=args.itm_steps,
            label=f"#{number} {mother.timestamp:%Y-%m-%d %H:%M}",
        )
        outcomes.append(outcome)
        if outcome.traded:
            open_until.append(mother.timestamp + timedelta(days=cfg["max_days"]))
    if skipped:
        print(f"  ({skipped} mothers skipped: max-concurrent {args.max_concurrent} already deployed)")

    rule = "wick rejection" if args.mother_rule == "wick" else "swing high"
    layer = "real Upstox premiums" if args.premium else "signal geometry, no P&L"
    report(
        outcomes,
        mothers,
        priced=args.premium,
        label=f"{args.symbol.upper()} two-red ladder {' -> '.join(stages)} · mothers: {rule} · {layer}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
