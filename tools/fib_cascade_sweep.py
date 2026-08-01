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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_fib_geometry import boundaries_for_timeframe  # noqa: E402
from engine.cascade_mothers import MotherCandidate, find_mother_candles, find_wick_mothers  # noqa: E402
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
    # Index-target-hit vs. premium-book outcome. The engine exits on the INDEX
    # target, but P&L is realised in premium space, so a target hit does not
    # guarantee the option book was green. These count the leak, over priced
    # rounds only (net_pnl is None on a gapped round).
    target_priced_rounds: int = 0
    target_net_neg_rounds: int = 0  # target hit, book still net-negative after costs
    target_gross_neg_rounds: int = 0  # target hit, book negative even before costs
    target_net_pnl: float = 0.0  # summed net over target-exit rounds
    expiry_net_pnl: float = 0.0  # summed net over expiry-square-off rounds
    # One row per round, for --detail: (round number, exit reason, entries,
    # lots, deepest level, net P&L). The aggregate counters above answer "how
    # often"; this answers "which trade, and when".
    round_rows: list[tuple] = field(default_factory=list)

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
    max_rounds: Optional[int],
    max_round_premium_inr: Optional[float],
    exit_days_before_expiry: int,
    fib_levels: Optional[tuple[int, ...]],
    fill_at_boundary: bool,
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
            max_rounds=max_rounds,
            max_round_premium_inr=max_round_premium_inr,
            exit_days_before_expiry=exit_days_before_expiry,
            fill_at_boundary=fill_at_boundary,
            **({"fib_levels": fib_levels} if fib_levels else {}),
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

    # Split the priced rounds by exit reason to expose the index-target-vs-premium
    # leak: rounds where the INDEX target was hit but the premium book still lost.
    target_priced = [r for r in engine.rounds if r.exit_reason == "target" and r.net_pnl is not None]
    expiry_priced = [r for r in engine.rounds if r.exit_reason == "expiry_square_off" and r.net_pnl is not None]

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
        target_priced_rounds=len(target_priced),
        target_net_neg_rounds=sum(1 for r in target_priced if r.net_pnl < 0),
        target_gross_neg_rounds=sum(1 for r in target_priced if r.gross_pnl < 0),
        target_net_pnl=round(sum(r.net_pnl for r in target_priced), 2),
        expiry_net_pnl=round(sum(r.net_pnl for r in expiry_priced), 2),
        round_rows=[
            (
                number,
                r.exit_reason,
                getattr(r, "exit_timestamp", None),
                len(r.fills),
                sum(f.lots for f in r.fills),
                max((lvl for lvl in (_level_of(f) for f in r.fills) if lvl is not None), default=None),
                r.net_pnl,
            )
            for number, r in enumerate(engine.rounds, start=1)
        ],
    )


def run_sweep(
    index_series: list[IndexCandle],
    cfg: dict,
    *,
    timeframe: str,
    max_concurrent: int,
    scanner: Callable[..., list[MotherCandidate]],
    scanner_kwargs: dict,
    premium_lookup: Callable[[datetime, FixedCampaignOption], Optional[float]],
    expiries: list[date],
    rung_inr: float,
    itm_steps: int,
    single_shot: bool,
    max_rounds: Optional[int],
    max_round_premium_inr: Optional[float],
    exit_days_before_expiry: int,
    fib_levels: Optional[tuple[int, ...]],
    fill_at_boundary: bool,
) -> tuple[list[FibOutcome], list[MotherCandidate]]:
    mothers = scanner(index_series, **scanner_kwargs)
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
            max_rounds=max_rounds,
            max_round_premium_inr=max_round_premium_inr,
            exit_days_before_expiry=exit_days_before_expiry,
            fib_levels=fib_levels,
            fill_at_boundary=fill_at_boundary,
            label=f"#{number} {mother.timestamp:%Y-%m-%d %H:%M}",
        )
        outcomes.append(outcome)
        if outcome.traded:
            open_until.append(start + timedelta(days=cfg["max_days"]))
    if skipped:
        print(f"  ({skipped} mothers skipped: max-concurrent {max_concurrent} already deployed)")
    return outcomes, mothers


def detail(outcomes: list[FibOutcome]) -> None:
    """Every round of every campaign that traded, newest rule first: WHICH ones.

    The summary counts expiry exits; this names them, because "six trades ate
    thirty-eight winners" is only actionable once you can go look at the six.
    """
    traded = [o for o in outcomes if o.traded]
    if not traded:
        return
    print("\n--- every round, campaign by campaign ---")
    print(
        f"  {'mother':>16}  {'rnd':>3}  {'exit':>18}  {'exited':>16}  {'buys':>4} {'lots':>4} {'lvl':>3}  {'net Rs':>13}"
    )
    for outcome in traded:
        for number, reason, exit_at, entries, lots, level, net in outcome.round_rows:
            when = f"{exit_at:%Y-%m-%d %H:%M}" if exit_at else "-"
            money = f"{net:,.0f}" if net is not None else "no price"
            print(
                f"  {outcome.mother_timestamp:%Y-%m-%d %H:%M}  {number:>3}  {str(reason):>18}  "
                f"{when:>16}  {entries:>4} {lots:>4} {str(level):>3}  {money:>13}"
            )
    print("\n--- expiry square-offs only (the tail that pays for the winners) ---")
    rows = [
        (o.mother_timestamp, exit_at, entries, lots, level, net)
        for o in traded
        for _n, reason, exit_at, entries, lots, level, net in o.round_rows
        if reason == "expiry_square_off"
    ]
    for mother, exit_at, entries, lots, level, net in sorted(rows, key=lambda row: (row[5] is None, row[5])):
        when = f"{exit_at:%Y-%m-%d %H:%M}" if exit_at else "-"
        money = f"{net:,.0f}" if net is not None else "no price"
        print(
            f"  mother {mother:%d %b %Y %H:%M}   squared off {when}   "
            f"{entries} buys / {lots} lots / deepest L{level}   Rs {money}"
        )


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

        # Index-target-vs-premium leak: how often hitting the INDEX target still
        # left the option book underwater, and where the money actually came from.
        tgt_priced = sum(o.target_priced_rounds for o in fp)
        if tgt_priced:
            net_neg = sum(o.target_net_neg_rounds for o in fp)
            gross_neg = sum(o.target_gross_neg_rounds for o in fp)
            tgt_net = round(sum(o.target_net_pnl for o in fp), 2)
            exp_net = round(sum(o.expiry_net_pnl for o in fp), 2)
            print(
                f"  target-exit leak        {net_neg}/{tgt_priced} target hits lost money "
                f"({round(100 * net_neg / tgt_priced, 1)}%); {gross_neg} lost before costs"
            )
            print(f"  P&L by exit reason      target-exit Rs {tgt_net:,}   expiry-exit Rs {exp_net:,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", choices=sorted(SYMBOLS), default="nifty")
    ap.add_argument("--from", dest="from_date", required=True)
    ap.add_argument("--to", dest="to_date", required=True)
    ap.add_argument("--tf", dest="timeframe", default="15m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--premium", action="store_true", help="Layer 2: price legs with real Upstox premiums")
    ap.add_argument("--single-shot", action="store_true", help="one trade per mother, no deeper buys / re-arm")
    ap.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="cap rounds per mother (keeps deeper-level averaging within a round; "
        "1 = one-and-done, a fresh campaign needs a fresh mother). Default: unlimited re-arms.",
    )
    ap.add_argument(
        "--max-round-premium",
        type=float,
        default=None,
        help="per-round premium ceiling in Rs: stop taking deeper legs once deployed "
        "option premium would breach it (the first entry always fills). Default: none.",
    )
    ap.add_argument("--max-concurrent", type=int, default=999)
    ap.add_argument("--rung-inr", type=float, default=15000.0)
    ap.add_argument("--itm-steps", type=int, default=2)
    ap.add_argument(
        "--mother-rule",
        choices=["swing", "wick"],
        default="swing",
        help="swing = swing-high pivot (needs right-bars to confirm). "
        "wick = Phil's rule: a bullish run, then a candle whose upper wick is over "
        "half its range (red or green), confirmed at its own close.",
    )
    ap.add_argument("--left-bars", type=int, default=3)
    ap.add_argument("--right-bars", type=int, default=3)
    ap.add_argument("--min-range-atr", type=float, default=0.8)
    # --mother-rule wick only
    ap.add_argument("--run-bars", type=int, default=4, help="wick rule: bars of bullish run before the mother")
    ap.add_argument("--min-run-green", type=int, default=3, help="wick rule: how many of those must close green")
    ap.add_argument("--min-run-atr", type=float, default=1.5, help="wick rule: run height in ATRs -- the 'huge' test")
    ap.add_argument("--min-wick", type=float, default=0.5, help="wick rule: upper wick as a share of candle range")
    ap.add_argument(
        "--allow-overnight-run",
        action="store_true",
        help="wick rule: let the bullish run cross a session boundary (default: one day)",
    )
    ap.add_argument(
        "--min-separation-bars",
        type=int,
        default=0,
        help="minimum bars between accepted mothers, so one swing spawns one campaign "
        "instead of a cluster of adjacent local highs. Default: 0 (no separation).",
    )
    ap.add_argument(
        "--exit-days-before-expiry",
        type=int,
        default=0,
        help="time-stop: square off this many calendar days before expiry to cut late "
        "theta on legs that never hit target. Default: 0 (square off at expiry).",
    )
    ap.add_argument(
        "--timeframe-levels",
        action="store_true",
        help="apply Phil's timeframe rule to which fib lines may trade: (4, 8) on "
        "1m/5m, (2, 4, 8) on 15m/1h. Default: the engine's own (2, 4, 8) everywhere.",
    )
    ap.add_argument("--detail", action="store_true", help="print every round of every campaign that traded")
    ap.add_argument(
        "--min-dte",
        type=int,
        default=None,
        help="days to expiry the campaign's contract must have AT THE MOTHER. The engine's "
        "own CascadeConfig default is 10; this tool's per-symbol table says 7. Overrides both.",
    )
    ap.add_argument("--max-dte", type=int, default=None, help="upper end of the same window")
    ap.add_argument(
        "--monthly-only",
        action="store_true",
        help="keep only the LAST expiry of each calendar month, so a weekly "
        "underlying is replayed on a monthly cadence. This is the control for "
        "'is it the monthly expiry helping, or is it the other index?'",
    )
    ap.add_argument(
        "--at-boundary",
        action="store_true",
        help="buy the fib line itself the moment price trades there, instead of "
        "waiting for two reds below it and buying the recovery.",
    )
    ap.add_argument(
        "--levels",
        default=None,
        help="which fib lines may trade, e.g. '8' or '4,8'. Overrides both the engine default and --timeframe-levels.",
    )
    ap.add_argument(
        "--upstox-cache",
        default=None,
        help="override the premium cache directory. Point two concurrent sweeps at "
        "separate copies: the cache writes whole files, so a reader can catch a half-written one.",
    )
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    cfg = dict(SYMBOLS[args.symbol])
    if args.min_dte is not None:
        cfg["min_dte"] = args.min_dte
    if args.max_dte is not None:
        cfg["max_dte"] = args.max_dte
    print(f"[dte] contract picked {cfg['min_dte']}-{cfg['max_dte']} days from expiry, measured at the mother")
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
    # UpstoxPremiumSource namespaces its per-underlying caches (expiry list,
    # option chains) internally: NIFTY keeps the root, every other underlying
    # gets its own subdir. So all symbols can safely share this one root dir --
    # no per-symbol cache_dir juggling needed here.
    upstox_cache = Path(args.upstox_cache or (Path(os.path.dirname(os.path.abspath(__file__))) / ".upstox_cache"))
    source = UpstoxPremiumSource(underlying_key=cfg["upstox_key"], cache_dir=upstox_cache)
    expiries = sorted(source.available_expiries())
    if not expiries:
        print(f"[upstox] no expiry coverage for {cfg['upstox_key']}.")
        return 1
    if args.monthly_only:
        # Last expiry of each calendar month -- that IS the monthly contract on
        # both NSE weekly chains, so no separate calendar is needed.
        by_month: dict[tuple[int, int], date] = {}
        for expiry in expiries:
            key = (expiry.year, expiry.month)
            if key not in by_month or expiry > by_month[key]:
                by_month[key] = expiry
        expiries = sorted(by_month.values())
        print(f"[expiry] monthly cadence only: {len(expiries)} contracts kept")
    print(f"[upstox] {len(expiries)} {args.symbol} expiries: {expiries[0]} -> {expiries[-1]}")

    if args.premium:

        def premium_lookup(timestamp, contract):
            bar = source.lookup(timestamp, contract)
            return float(bar.open) if bar is not None else None
    else:

        def premium_lookup(_timestamp, _contract):
            return 100.0  # flat: drives the geometry, prices nothing

    # Phil's timeframe rule: a 1m/5m mother buys only the two DEEPEST lines (4, 8);
    # 15m/1h start one step earlier (2, 4, 8). The engine's own default is (2,4,8)
    # for every timeframe, so without --timeframe-levels a 5m sweep arms L2 -- a
    # level Phil's rule says that mother should never take.
    if args.levels:
        fib_levels = tuple(int(part) for part in args.levels.replace(" ", "").split(",") if part)
    elif args.timeframe_levels:
        fib_levels = tuple(boundaries_for_timeframe(args.timeframe))
    else:
        fib_levels = None
    if fib_levels:
        print(f"[levels] {args.timeframe} trades fib levels {fib_levels}")
    print(f"[entry] {'AT the fib line' if args.at_boundary else 'two reds below the line, buy the recovery'}")

    if args.mother_rule == "wick":
        scanner = find_wick_mothers
        scanner_kwargs = dict(
            run_bars=args.run_bars,
            min_run_green=args.min_run_green,
            min_run_atr=args.min_run_atr,
            min_wick_fraction=args.min_wick,
            min_range_atr=args.min_range_atr,
            min_separation_bars=args.min_separation_bars,
            same_session_only=not args.allow_overnight_run,
        )
    else:
        scanner = find_mother_candles
        scanner_kwargs = dict(
            left_bars=args.left_bars,
            right_bars=args.right_bars,
            min_range_atr=args.min_range_atr,
            min_separation_bars=args.min_separation_bars,
        )
    outcomes, mothers = run_sweep(
        index_series,
        cfg,
        timeframe=args.timeframe,
        max_concurrent=args.max_concurrent,
        scanner=scanner,
        scanner_kwargs=scanner_kwargs,
        premium_lookup=premium_lookup,
        expiries=expiries,
        rung_inr=args.rung_inr,
        itm_steps=args.itm_steps,
        single_shot=args.single_shot,
        max_rounds=args.max_rounds,
        max_round_premium_inr=args.max_round_premium,
        exit_days_before_expiry=args.exit_days_before_expiry,
        fib_levels=fib_levels,
        fill_at_boundary=args.at_boundary,
    )
    layer = "Layer 2 (real Upstox premiums)" if args.premium else "Layer 1 (signal geometry, no P&L)"
    if args.single_shot:
        mode = "SINGLE-SHOT (1 trade/mother)"
    elif args.max_rounds is not None:
        mode = f"max {args.max_rounds} round(s)/mother"
    else:
        mode = "full cascade"
    if args.max_round_premium is not None:
        mode += f" · premium cap Rs {int(args.max_round_premium):,}"
    rule = "wick rejection" if args.mother_rule == "wick" else "swing high"
    if fib_levels:
        mode += f" · levels {'/'.join(str(level) for level in fib_levels)}"
    mode += " · AT the line" if args.at_boundary else " · two-red entry"
    report(
        outcomes,
        mothers,
        priced=args.premium,
        label=f"{args.symbol.upper()} {args.timeframe} · mothers: {rule} · {mode} · {layer}",
    )
    if args.detail:
        detail(outcomes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
