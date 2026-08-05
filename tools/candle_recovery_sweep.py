"""tools/candle_recovery_sweep.py -- replay the STOP-LOSS RECOVERY rules
(engine/candle_recovery.py) over real NIFTY index data with real Upstox
expired-option premiums.

Phil's rules, 2026-08-05: two reds where the second CLOSES BELOW THE FIRST'S
LOW, buy the recovery at the second red's HIGH, stop on a CLOSE below the
ultimate low since the mother, repeat the pattern after each stop, and the
open trade's target is every booked loss paid back plus a margin.

Each trade picks its own contract at its own fill: ATM minus --itm-steps
strikes, nearest expiry at least --min-dte days out (the resolver rolls to the
next week when the current one is too close).

    python3 tools/candle_recovery_sweep.py --from 2026-01-01 --to 2026-08-05 --tf 15m --premium

Candles share tools/.nifty_cache with the other sweeps, so repeat runs are
offline; premiums share tools/.upstox_cache.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from statistics import median
from types import SimpleNamespace
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.candle_recovery import FibZoneEntry, RecoveryBar, RecoveryConfig, TwoRedRecovery  # noqa: E402
from engine.cascade_mothers import MotherCandidate, find_mother_candles  # noqa: E402
from engine.cascade_options import CascadeConfig, NiftyContractResolver  # noqa: E402
from tools.fib_cascade_sweep import SYMBOLS, load_index_candles  # noqa: E402


@dataclass
class CampaignOutcome:
    mother_timestamp: datetime
    status: str
    end_reason: Optional[str]
    trades: int
    stops: int
    booked_net: Optional[float]
    fully_priced: bool


def replay_one(
    mother: MotherCandidate,
    series: list,
    cfg: dict,
    *,
    timeframe: str,
    config: RecoveryConfig,
    premium_lookup: Callable[[datetime, SimpleNamespace], Optional[float]],
    expiries: list[date],
    mode: str = "ladder",
) -> tuple[CampaignOutcome, TwoRedRecovery]:
    resolver = NiftyContractResolver(
        expiries=expiries, strike_step=cfg["strike_step"], lot_size=cfg["lot_size"], symbol=cfg["cache"]
    )
    resolver_config = CascadeConfig(
        mother_timestamp=mother.timestamp,
        mother_high=mother.high,
        mother_low=mother.low,
        option_type="CE",
        timeframe=timeframe,
        itm_steps=config.itm_steps,
        strike_step=cfg["strike_step"],
        lot_size=cfg["lot_size"],
        min_dte=config.min_dte,
        max_dte=config.max_dte,
    )

    def contract_for(when: datetime, index_price: float) -> Optional[tuple[int, date]]:
        try:
            picked = resolver.select(when, index_price, "CE", resolver_config)
        except Exception:
            return None
        return int(picked.strike), picked.expiry

    def lookup(when: datetime, strike: int, expiry: date) -> Optional[float]:
        # The archive keys on .symbol/.strike/.expiry/.option_type -- the same
        # shape the resolver's Contract carries, so a namespace is enough.
        contract = SimpleNamespace(symbol=cfg["cache"], strike=int(strike), expiry=expiry, option_type="CE")
        return premium_lookup(when, contract)

    mother_row = next(row for row in series if row.timestamp == mother.timestamp)
    mother_bar = RecoveryBar(mother_row.timestamp, mother_row.open, mother_row.high, mother_row.low, mother_row.close)
    engine_cls = FibZoneEntry if mode == "fib-zone" else TwoRedRecovery
    engine = engine_cls(
        mother_bar, config, contract_for=contract_for, premium_lookup=lookup, lot_size=int(cfg["lot_size"])
    )
    bars = [
        RecoveryBar(row.timestamp, row.open, row.high, row.low, row.close)
        for row in series
        if row.timestamp > mother.timestamp
    ]
    engine.run(bars)
    stops = sum(1 for t in engine.trades if t.exit_reason == "stop")
    outcome = CampaignOutcome(
        mother_timestamp=mother.timestamp,
        status=engine.status,
        end_reason=engine.end_reason,
        trades=len(engine.trades),
        stops=stops,
        booked_net=engine.booked_net if engine.fully_priced else None,
        fully_priced=engine.fully_priced,
    )
    return outcome, engine


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", choices=sorted(SYMBOLS), default="nifty")
    ap.add_argument("--from", dest="from_date", required=True)
    ap.add_argument("--to", dest="to_date", required=True)
    ap.add_argument("--tf", dest="timeframe", default="15m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--premium", action="store_true", help="price every trade with real Upstox premiums")
    ap.add_argument("--lots", default="1,2", help="lots by trade number, e.g. 1,2 = 1 lot first, 2 after")
    ap.add_argument("--min-profit", type=float, default=500.0)
    ap.add_argument(
        "--sl",
        dest="sl_source",
        choices=["entry", "previous", "ultimate"],
        default="entry",
        help="where the stop sits: the entry candle low, the previous candle low, or the ultimate low",
    )
    ap.add_argument("--itm-steps", type=int, default=2)
    ap.add_argument("--min-dte", type=int, default=4)
    ap.add_argument("--max-dte", type=int, default=45)
    ap.add_argument("--max-trades", type=int, default=12)
    ap.add_argument("--horizon-sessions", type=int, default=15)
    ap.add_argument("--left-bars", type=int, default=3)
    ap.add_argument("--right-bars", type=int, default=3)
    ap.add_argument("--min-range-atr", type=float, default=0.8)
    ap.add_argument("--min-separation-bars", type=int, default=0)
    ap.add_argument(
        "--mode",
        choices=["ladder", "fib-zone"],
        default="ladder",
        help="ladder = repeat-till-recovered; fib-zone = entries only at the 2-2/4-4 fib zones",
    )
    ap.add_argument("--dump", default=None, help="write per-campaign trade detail as JSON here")
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    cfg = dict(SYMBOLS[args.symbol])
    series = load_index_candles(cfg, args.timeframe, args.from_date, args.to_date, refetch=args.refetch)
    if not series:
        print("No candles in range.")
        return 1
    print(
        f"[data] {args.symbol} {args.timeframe}: {len(series)} candles "
        f"{series[0].timestamp:%Y-%m-%d} -> {series[-1].timestamp:%Y-%m-%d}"
    )

    from pathlib import Path

    from data.cascade_upstox import UpstoxPremiumSource

    try:
        from upstox_token_manager import ensure_fresh_token

        ensure_fresh_token()
    except Exception as exc:
        print(f"[upstox] token pre-check skipped: {exc}")
    source = UpstoxPremiumSource(
        underlying_key=cfg["upstox_key"],
        cache_dir=Path(os.path.dirname(os.path.abspath(__file__))) / ".upstox_cache",
    )
    expiries = sorted(source.available_expiries())
    if not expiries:
        print(f"[upstox] no expiry coverage for {cfg['upstox_key']}.")
        return 1
    print(f"[upstox] {len(expiries)} expiries: {expiries[0]} -> {expiries[-1]}")

    if args.premium:

        def premium_lookup(timestamp, contract):
            bar = source.lookup(timestamp, contract)
            return float(bar.open) if bar is not None else None
    else:

        def premium_lookup(_timestamp, _contract):
            return 100.0

    config = RecoveryConfig(
        timeframe=args.timeframe,
        lots_schedule=tuple(int(x) for x in str(args.lots).split(",") if x),
        min_profit_inr=args.min_profit,
        sl_source=args.sl_source,
        itm_steps=args.itm_steps,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        max_trades=args.max_trades,
        horizon_sessions=args.horizon_sessions,
    )
    mothers = find_mother_candles(
        series,
        left_bars=args.left_bars,
        right_bars=args.right_bars,
        min_range_atr=args.min_range_atr,
        min_separation_bars=args.min_separation_bars,
    )
    print(f"[mothers] {len(mothers)} swing mothers, applied mechanically\n")

    outcomes: list[CampaignOutcome] = []
    engines: list[TwoRedRecovery] = []
    for mother in mothers:
        outcome, engine = replay_one(
            mother,
            series,
            cfg,
            timeframe=args.timeframe,
            config=config,
            premium_lookup=premium_lookup,
            expiries=expiries,
            mode=args.mode,
        )
        outcomes.append(outcome)
        engines.append(engine)

    traded = [o for o in outcomes if o.trades > 0]
    priced = [o for o in traded if o.fully_priced and o.booked_net is not None]
    recovered = [o for o in priced if o.status == "RECOVERED"]
    abandoned = [o for o in priced if o.status == "ABANDONED"]
    other = [o for o in priced if o.status not in {"RECOVERED", "ABANDONED"}]

    print(f"  campaigns with at least one fill   {len(traded)} of {len(outcomes)}")
    print(f"  fully priced                       {len(priced)}   (unpriced fills are excluded from every rupee)")
    total = round(sum(o.booked_net for o in priced), 2)
    for label, rows in (
        ("RECOVERED (target hit)", recovered),
        ("ABANDONED (ledger red)", abandoned),
        ("other end", other),
    ):
        if not rows:
            print(f"  {label:<26} 0")
            continue
        net = round(sum(o.booked_net for o in rows), 2)
        med_trades = median(o.trades for o in rows)
        print(f"  {label:<26} {len(rows):>4}   net Rs {net:>12,.2f}   median trades/campaign {med_trades:.0f}")
    stops = sum(o.stops for o in priced)
    print(f"  stops taken across the book        {stops}")
    print("  " + "-" * 74)
    print(f"  NET (all priced campaigns)         Rs {total:>12,.2f}")

    if args.dump:
        payload = []
        for outcome, engine in zip(outcomes, engines):
            payload.append(
                {
                    "mother": outcome.mother_timestamp.isoformat(),
                    "status": outcome.status,
                    "end_reason": outcome.end_reason,
                    "booked_net": outcome.booked_net,
                    "fully_priced": outcome.fully_priced,
                    "trades": [
                        {**{k: (v.isoformat() if isinstance(v, (datetime, date)) else v) for k, v in asdict(t).items()}}
                        for t in engine.trades
                    ],
                }
            )
        with open(args.dump, "w") as fh:
            json.dump(payload, fh, indent=1, default=str)
        print(f"\n[dump] per-campaign trades -> {args.dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
