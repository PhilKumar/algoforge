"""
tools/cascade_backtest_nifty.py -- replay the NIFTY option cascade over months
of real index data, so a rule change is a number instead of an argument.

The strategy's every decision (arm, fill, escalate, target, exit) is computed
from NIFTY index candles alone. Option premiums move the P&L, never a choice.
That splits the work cleanly in two, and the split is the whole reason a
one-year backtest is possible at all:

  Layer 1 (this tool, by default)
      Signal replay on index candles. Exact, free, and complete. It answers
      how often the pattern fires, how deep the cascade goes, how far price
      runs against it, and how often the target arrives before expiry does.

  Layer 2 (--premium)
      A fixed-strike premium source layered on Layer 1's exact entry and exit
      levels. Rupee P&L only appears when a real premium priced every leg.

Layer 1 reports no rupee P&L at all: with no premium history there is no entry
price, and inventing one is how a backtest starts lying. What it does give is
every entry and exit level exactly, which is what Layer 2 then prices.

Expiry exits need only half of Layer 2. An option at its own expiry is worth
intrinsic value, which the index settles exactly, so those rows need an entry
premium and nothing else. Since expiry is where this strategy takes its losses,
the downside becomes measurable as soon as entry premiums exist -- before any
exit-side premium history is available.

    python3 tools/cascade_backtest_nifty.py --from 2025-09-01 --to 2026-07-01 --tf 15m
    python3 tools/cascade_backtest_nifty.py --tf 1h --sweep arm_compare
    python3 tools/cascade_backtest_nifty.py --tf 15m --legacy-fill   # measure the fill bug

Candles are cached under tools/.nifty_cache, so repeat runs are offline and the
numbers only move when the rules do.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Callable, Iterable, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_calendar import ContractCalendar, optional_calendar  # noqa: E402
from engine.cascade_mothers import MotherCandidate, find_mother_candles  # noqa: E402
from engine.cascade_options import (  # noqa: E402
    Candle,
    CascadeConfig,
    CascadeResult,
    Contract,
    NiftyContractResolver,
    OneHourCascade,
    OptionCandle,
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nifty_cache")
LADDERS = {"5m": ("5m", "15m", "1h"), "15m": ("15m", "1h", "1h"), "1h": ("1h", "1h", "1h")}


# ── candle loading ────────────────────────────────────────────────


def _cache_path(timeframe: str, from_date: str, to_date: str) -> str:
    return os.path.join(CACHE_DIR, f"NIFTY_{timeframe}_{from_date}_{to_date}.json")


def _rows_to_candles(rows: Iterable[Sequence]) -> list[Candle]:
    return [
        Candle(datetime.fromisoformat(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        for row in rows
    ]


def load_candles(
    timeframes: Sequence[str],
    from_date: str,
    to_date: str,
    *,
    refetch: bool = False,
) -> dict[str, list[Candle]]:
    """Cached NIFTY index candles, fetched from Dhan only when missing.

    A cache miss needs live Dhan credentials. The failure message says so
    explicitly rather than letting an empty series look like an empty result.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    wanted = sorted(set(timeframes))
    result: dict[str, list[Candle]] = {}
    missing: list[str] = []

    for timeframe in wanted:
        path = _cache_path(timeframe, from_date, to_date)
        if os.path.exists(path) and not refetch:
            with open(path, "r", encoding="utf-8") as handle:
                result[timeframe] = _rows_to_candles(json.load(handle))
        else:
            missing.append(timeframe)

    if missing:
        fetched = _fetch_from_dhan(missing, from_date, to_date)
        for timeframe, rows in fetched.items():
            with open(_cache_path(timeframe, from_date, to_date), "w", encoding="utf-8") as handle:
                json.dump(
                    [[c.timestamp.isoformat(), c.open, c.high, c.low, c.close] for c in rows],
                    handle,
                )
            result[timeframe] = rows
    return result


def _fetch_from_dhan(timeframes: Sequence[str], from_date: str, to_date: str) -> dict[str, list[Candle]]:
    from broker.dhan import DhanClient
    from data.cascade_dhan import DhanOneHourSource

    print(f"[fetch] NIFTY {','.join(timeframes)} {from_date} -> {to_date} (cache miss)")
    try:
        source = DhanOneHourSource(DhanClient())
        return source.fetch_index_cascade(from_date, to_date, timeframes)
    except Exception as exc:
        raise SystemExit(
            f"Could not fetch NIFTY candles from Dhan: {exc}\n"
            "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (Data APIs must be enabled on the account), "
            "or place a cached file at "
            f"{_cache_path(timeframes[0], from_date, to_date)}"
        ) from exc


# ── one campaign's outcome ────────────────────────────────────────


@dataclass
class Outcome:
    label: str
    mother_timestamp: datetime
    mother_high: float
    mother_low: float
    started_at: datetime
    status: str
    stages: int
    lots: int
    entries: list = field(default_factory=list)
    target_index: Optional[float] = None
    average_spot: Optional[float] = None
    exit_timestamp: Optional[datetime] = None
    exit_reason: Optional[str] = None
    gross_pnl: Optional[float] = None
    costs: float = 0.0
    net_pnl: Optional[float] = None
    mae_points: Optional[float] = None  # deepest index excursion below avg entry
    hours_to_exit: Optional[float] = None
    data_gaps: int = 0

    @property
    def traded(self) -> bool:
        return self.stages > 0


def _max_adverse_excursion(candles: Sequence[Candle], outcome_entries, exit_timestamp) -> Optional[float]:
    """Deepest the index went below the running average entry while open.

    This is the number that decides whether the cascade is survivable. The
    target is reachable on paper for almost any drawdown; the account is not.
    """
    if not outcome_entries:
        return None
    first = outcome_entries[0].timestamp
    last = exit_timestamp or candles[-1].timestamp
    total_qty = sum(entry.quantity for entry in outcome_entries)
    average = sum(entry.spot * entry.quantity for entry in outcome_entries) / total_qty
    lows = [candle.low for candle in candles if first <= candle.timestamp <= last]
    return round(average - min(lows), 2) if lows else None


def replay_one(
    mother: MotherCandidate,
    series: dict[str, list[Candle]],
    calendar: ContractCalendar,
    *,
    base_timeframe: str,
    max_days: int,
    option_lookup: Callable[[datetime, Contract], Optional[OptionCandle]],
    label: str,
    start_at_pivot: bool = False,
    **config_overrides,
) -> Outcome:
    rule = calendar.rule_for(mother.timestamp.date())
    base = series[base_timeframe]
    sessions = {candle.timestamp.date() for candle in base}

    start = mother.timestamp if start_at_pivot else mother.confirmed_at
    horizon = start + timedelta(days=max_days)
    window = {
        timeframe: [candle for candle in rows if start <= candle.timestamp <= horizon]
        for timeframe, rows in series.items()
    }

    config = CascadeConfig(
        # The engine ignores everything at or before the mother timestamp. Using
        # the confirmation bar keeps the replay from acting on a swing high that
        # nothing yet knew was a swing high.
        mother_timestamp=start - timedelta(seconds=1),
        mother_high=mother.high,
        mother_low=mother.low,
        timeframe=base_timeframe,
        stage_timeframes=LADDERS[base_timeframe],
        lot_size=rule.lot_size,
        strike_step=rule.strike_step,
        strict_option_data=False,
        **config_overrides,
    )
    resolver = NiftyContractResolver(
        calendar.weekly_expiries(mother.timestamp.date(), horizon.date(), sessions),
        lot_size=rule.lot_size,
        strike_step=rule.strike_step,
    )
    result: CascadeResult = OneHourCascade(config, resolver, option_lookup).run(window)

    entries = result.entries
    hours = None
    if result.exit_timestamp and entries:
        hours = round((result.exit_timestamp - entries[0].timestamp).total_seconds() / 3600, 1)

    return Outcome(
        label=label,
        mother_timestamp=mother.timestamp,
        mother_high=mother.high,
        mother_low=mother.low,
        started_at=start,
        status=result.status,
        stages=len(entries),
        lots=sum(entry.lots for entry in entries),
        entries=list(entries),
        target_index=result.target_index,
        average_spot=result.average_spot,
        exit_timestamp=result.exit_timestamp,
        exit_reason=result.exit_reason,
        gross_pnl=result.realized_pnl,
        costs=result.costs_total,
        net_pnl=result.net_pnl,
        mae_points=_max_adverse_excursion(window[base_timeframe], entries, result.exit_timestamp),
        hours_to_exit=hours,
        data_gaps=len(result.data_gaps),
    )


# ── the portfolio run ─────────────────────────────────────────────


def run_backtest(
    series: dict[str, list[Candle]],
    calendar: ContractCalendar,
    *,
    base_timeframe: str,
    max_concurrent: int,
    max_days: int,
    scanner_kwargs: dict,
    option_lookup: Callable[[datetime, Contract], Optional[OptionCandle]],
    start_at_pivot: bool = False,
    **config_overrides,
) -> tuple[list[Outcome], list[MotherCandidate]]:
    base = series[base_timeframe]
    mothers = find_mother_candles(base, **scanner_kwargs)

    outcomes: list[Outcome] = []
    open_until: list[datetime] = []  # exit time of each campaign counted as open
    skipped = 0

    for number, mother in enumerate(mothers, start=1):
        start = mother.timestamp if start_at_pivot else mother.confirmed_at
        open_until = [end for end in open_until if end > start]
        if len(open_until) >= max_concurrent:
            skipped += 1
            continue
        outcome = replay_one(
            mother,
            series,
            calendar,
            base_timeframe=base_timeframe,
            max_days=max_days,
            option_lookup=option_lookup,
            label=f"#{number} {mother.timestamp:%Y-%m-%d %H:%M}",
            start_at_pivot=start_at_pivot,
            **config_overrides,
        )
        outcomes.append(outcome)
        if outcome.traded:
            open_until.append(outcome.exit_timestamp or (start + timedelta(days=max_days)))

    if skipped:
        print(f"  ({skipped} mothers skipped: max-concurrent {max_concurrent} already deployed)")
    return outcomes, mothers


# ── report ────────────────────────────────────────────────────────


def report(outcomes: list[Outcome], mothers: list[MotherCandidate], label: str) -> None:
    traded = [row for row in outcomes if row.traded]
    print(f"\n=== {label} ===")
    print(f"  mothers detected      {len(mothers)}")
    print(f"  campaigns replayed    {len(outcomes)}")
    print(f"  took at least 1 entry {len(traded)}")
    if not traded:
        print("  no entries -- nothing further to measure")
        return

    stage_counts = {stage: sum(1 for row in traded if row.stages >= stage) for stage in (1, 2, 3)}
    print(
        f"  reached stage         1: {stage_counts[1]}   2: {stage_counts[2]}   3: {stage_counts[3]}"
        "        <- if stage 3 is rare, the 3rd lot is theoretical"
    )

    reasons: dict[str, int] = {}
    for row in traded:
        reasons[row.exit_reason or row.status] = reasons.get(row.exit_reason or row.status, 0) + 1
    print("  exits                 " + "   ".join(f"{name}: {count}" for name, count in sorted(reasons.items())))

    hit = sum(1 for row in traded if row.exit_reason == "target")
    print(f"  target hit rate       {hit}/{len(traded)} = {100 * hit / len(traded):.1f}%")

    maes = [row.mae_points for row in traded if row.mae_points is not None]
    if maes:
        print(
            f"  index drawdown below avg entry (points):  median {median(maes):,.0f}   "
            f"mean {mean(maes):,.0f}   worst {max(maes):,.0f}"
        )
    hours = [row.hours_to_exit for row in traded if row.hours_to_exit is not None]
    if hours:
        # Wall-clock hours between first entry and exit, NOT trading hours: 266
        # here is ~11 calendar days, a normal 7-13 DTE hold, not 40 trading days.
        print(f"  calendar hours to exit  median {median(hours):,.1f}   worst {max(hours):,.1f}  (wall-clock)")

    lots = [row.lots for row in traded]
    print(f"  lots committed        median {median(lots):.0f}   max {max(lots)}")

    # A hit rate on its own says nothing about size. The index move captured on
    # a win, and given up on a loss, are both exact here -- they are the numbers
    # the premium will be applied to, so they are worth stating before Layer 2
    # exists rather than after.
    won = [
        row.target_index - row.average_spot
        for row in traded
        if row.exit_reason == "target" and row.target_index is not None and row.average_spot is not None
    ]
    if won:
        print(
            f"  index points won      median {median(won):,.0f}   mean {mean(won):,.0f}   "
            f"best {max(won):,.0f}   (target is 0.25 of the way back to the mother high)"
        )
    lost = [row.mae_points for row in traded if row.exit_reason != "target" and row.mae_points is not None]
    if lost:
        print(
            f"  index points against  median {median(lost):,.0f}   worst {max(lost):,.0f}   "
            f"on the {len(lost)} that never reached target"
        )
    if won and lost:
        print(
            f"  >> {len(won)} wins of ~{median(won):,.0f} pts against {len(lost)} losses "
            f"of ~{median(lost):,.0f} pts. Whether that pays depends entirely on premium; "
            "see the priced P&L below when --premium is on."
        )

    priced = [row for row in traded if row.net_pnl is not None]
    unpriced = len(traded) - len(priced)
    print(
        f"\n  priced outcomes       {len(priced)} of {len(traded)}" + (f"  ({unpriced} unpriced)" if unpriced else "")
    )
    if priced:
        nets = [row.net_pnl for row in priced]
        wins = [value for value in nets if value > 0]
        losses = [value for value in nets if value <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        print(f"  net P&L               Rs {sum(nets):,.0f}   costs Rs {sum(row.costs for row in priced):,.0f}")
        print(f"  win rate              {len(wins)}/{len(priced)} = {100 * len(wins) / len(priced):.1f}%")
        print(f"  expectancy per trade  Rs {mean(nets):,.0f}")
        if gross_loss:
            print(f"  profit factor         {gross_win / gross_loss:.2f}")
        running = 0.0
        peak = 0.0
        drawdown = 0.0
        for value in nets:
            running += value
            peak = max(peak, running)
            drawdown = max(drawdown, peak - running)
        print(f"  max drawdown          Rs {drawdown:,.0f}")
    if unpriced:
        print(
            "  Unpriced rows have no entry premium, so no rupee figure is claimed for them.\n"
            "  Their index entry, target and exit levels above are exact and are what Layer 2 prices."
        )


# ── CLI ───────────────────────────────────────────────────────────


def _default_range() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=365)).isoformat(), today.isoformat()


SWEEPS: dict[str, list[dict]] = {
    "arm_compare": [{"arm_compare": "last_qualifying"}, {"arm_compare": "previous_candle"}],
    "mark_low_mode": [{"mark_low_mode": "lowest"}, {"mark_low_mode": "latest"}],
    "first_leg": [{"first_leg_beyond_reference": True}, {"first_leg_beyond_reference": False}],
    "restrike": [{"restrike_on_stop_walk": False}, {"restrike_on_stop_walk": True}],
    "itm_steps": [{"itm_steps": steps} for steps in (1, 2, 3)],
    "target_fraction": [{"target_fraction": fraction} for fraction in (0.15, 0.25, 0.4)],
    "fill_rule": [{"fill_before_stop_walk": True}, {"fill_before_stop_walk": False}],
}


def main() -> int:
    start, end = _default_range()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_date", default=start)
    parser.add_argument("--to", dest="to_date", default=end)
    parser.add_argument("--tf", default="15m", choices=sorted(LADDERS), help="mother candle timeframe")
    parser.add_argument("--calendar", help="JSON file of dated NSE contract rules")
    parser.add_argument("--refetch", action="store_true", help="ignore the candle cache")
    parser.add_argument("--max-days", type=int, default=30, help="campaign horizon in calendar days")
    parser.add_argument("--max-concurrent", type=int, default=3)
    parser.add_argument("--start-at-pivot", action="store_true", help="start at the mother, not its confirmation")
    parser.add_argument("--legacy-fill", action="store_true", help="restore the stop-walk fill suppression")
    parser.add_argument("--slippage-points", type=float, default=1.0)
    parser.add_argument("--option-slippage-pct", type=float, default=0.01)
    parser.add_argument("--left-bars", type=int, default=3)
    parser.add_argument("--right-bars", type=int, default=3)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--min-range-atr", type=float, default=0.8)
    parser.add_argument("--min-separation-bars", type=int, default=5)
    parser.add_argument("--sweep", choices=sorted(SWEEPS), help="rerun the range across one rule interpretation")
    parser.add_argument(
        "--premium",
        action="store_true",
        help="Layer 2: price every leg with real fixed-strike Upstox premiums (needs UPSTOX_ACCESS_TOKEN)",
    )
    args = parser.parse_args()

    calendar = optional_calendar(args.calendar)
    print(calendar.describe())

    timeframes = sorted(set(LADDERS[args.tf]))
    series = load_candles(timeframes, args.from_date, args.to_date, refetch=args.refetch)
    base = series[args.tf]
    if not base:
        raise SystemExit(f"no NIFTY {args.tf} candles for {args.from_date} -> {args.to_date}")
    print(f"  {args.tf} candles: {len(base)}  {base[0].timestamp} -> {base[-1].timestamp}")

    scanner_kwargs = dict(
        left_bars=args.left_bars,
        right_bars=args.right_bars,
        atr_period=args.atr_period,
        min_range_atr=args.min_range_atr,
        min_separation_bars=args.min_separation_bars,
    )

    # Layer 1 default: no premium history, so only expiry exits price (from
    # intrinsic). Layer 2 (--premium) swaps in real fixed-strike Upstox bars.
    premium_source = None
    option_lookup = lambda _timestamp, _contract: None  # noqa: E731
    if args.premium:
        from data.cascade_upstox import UpstoxPremiumSource

        premium_source = UpstoxPremiumSource()
        covered = premium_source.available_expiries()
        oldest = min(covered)
        if date.fromisoformat(args.from_date) < oldest:
            print(
                f"  [premium] Upstox history starts {oldest}; legs before it stay unpriced gaps "
                f"(requested from {args.from_date})."
            )
        option_lookup = premium_source.lookup

    shared = dict(
        base_timeframe=args.tf,
        max_concurrent=args.max_concurrent,
        max_days=args.max_days,
        scanner_kwargs=scanner_kwargs,
        option_lookup=option_lookup,
        start_at_pivot=args.start_at_pivot,
        slippage_points=args.slippage_points,
        option_slippage_pct=args.option_slippage_pct,
    )

    variants = SWEEPS[args.sweep] if args.sweep else [{}]
    if not args.sweep and args.legacy_fill:
        variants = [{"fill_before_stop_walk": False}]

    for overrides in variants:
        name = ", ".join(f"{key}={value}" for key, value in overrides.items()) or "defaults"
        outcomes, mothers = run_backtest(series, calendar, **shared, **overrides)
        report(outcomes, mothers, f"{args.tf}  {args.from_date}..{args.to_date}  [{name}]")

    if premium_source is not None:
        print(
            f"\n  [premium] Upstox calls {premium_source.requests_made}   "
            f"unlisted-strike gaps {premium_source.missing_contracts}   "
            f"missing-minute gaps {premium_source.missing_minutes}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
