"""tools/portfolio_combined.py -- what running Gap Carry, PE and CE together looks like.

Each of the three is published on its own, which answers "does this rule work"
and answers nothing at all about "what happens if I run them at the same time".
Those are different questions. Three books that each drew down Rs 1 lakh in the
same week need more than one lakh; three that drew down in different years need
much less than the sum. Only a combined day-by-day walk can say which.

    python3 tools/portfolio_combined.py                       # as published
    python3 tools/portfolio_combined.py --pe-lots 2 --ce-lots 2 --gc-lots 1
    python3 tools/portfolio_combined.py --lots 1              # all three at one lot
    python3 tools/portfolio_combined.py --priced-only         # drop estimated exits
    python3 tools/portfolio_combined.py --csv /tmp/combined.csv

THE THREE ARE NOT PUBLISHED AT THE SAME SIZE, which is the first thing to get
wrong here. PE and CE are 4 lots (rebuild_data.LOTS); Gap Carry is 1. Summed as
published, the total describes a portfolio nobody would trade, so the size is
printed in the header of every run and can be set per book. Re-sizing
recomputes charges rather than scaling them -- brokerage is flat per order, so
a book scaled by multiplying its net flatters every size above one lot.

WHAT IS BEING COMBINED
    PE and CE come through rebuild_data.py's own loaders and splice, so they are
    the same trades the five-year tearsheet publishes -- effective-dated lots,
    real statutory charges, engine runs joined to the export at the measured
    seam. NOT the raw CSVs, which carry a flat lot and no charges at all.

    Gap Carry comes from tools/gapcarry_offline/runs/, the book its own
    tearsheet is built from.

CAPITAL IS MEASURED AT THE MINUTE, NOT THE DAY. PE and CE are intraday and
mostly SHORT: 95% of PE trades and 84% of CE trades are closed before 15:10,
which is the minute Gap Carry enters. Summing a day's premium therefore bills
the same rupees twice for positions that never coexisted -- it overstated the
live-size peak by Rs 34,194, 35%. The sweep here opens and closes each position
at its real timestamp, so the only overlap it counts is the real one: Gap
Carry's 15:10 entry against whatever intraday leg is still running, and its
09:20 exit against the next morning's entries.

WHAT IT WILL NOT DO, AND MUST NOT BE MADE TO DO. This is Phil's own planning
tool. It publishes nothing. It does NOT feed the tearsheets, and the capital
and ROI figures on those sheets are deliberately computed a different way --
per-day commitments rather than the minute-level sweep here -- because each
strategy is funded on its own terms and a broker does not release margin the
instant a leg closes (Phil, 2026-09-02: "Tearsheet is not going to change...
Every strategy is different and will have their own capital and calculations..
Don't mess with that").

If a later session is tempted to "fix" rebuild_data.py's peak_day_premium to
match this file: don't. The difference is 59% on the published sheet and it
runs in the strategy's favour, which is exactly the direction that needs a
decision rather than a refactor. It was put to Phil and he declined.

It will also not tell you what to trade or how much to risk. It reports what
these three books did on the days they ran.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "tearsheet"))

import rebuild_data as rb  # noqa: E402

from cascade_costs import calculate_nifty_option_round_costs  # noqa: E402

GAP_CARRY = os.path.join(ROOT, "tools", "gapcarry_offline", "runs", "NIFTY_5m_rsi70_atm4.csv")
# Gap Carry's clock, from engine/gap_carry.py: read the chart at 15:10, sell
# into the next session's 09:20. The capital is committed between the two.
ENTRY_TIME = dtime(15, 10)
EXIT_TIME = dtime(9, 20)


def load_gap_carry(priced_only: bool, lots: int) -> list:
    """The overnight book, with the dates its capital is actually tied up.

    `held_from`/`held_to` are the entry session and the exit session, because
    the position exists across that night. An intraday book has them equal.

    Re-sized from the published ONE lot, recomputing charges rather than
    multiplying them: brokerage is flat per order, so it does not scale, and a
    book scaled by multiplying its net would flatter every size above one lot.
    """
    out = []
    if not os.path.exists(GAP_CARRY):
        return out
    for row in csv.DictReader(open(GAP_CARRY, newline="")):
        estimated = str(row.get("priced", "True")).strip().lower() != "true"
        if priced_only and estimated:
            continue
        entry = date.fromisoformat(row["session"])
        exit_ = date.fromisoformat(row["exit_session"]) if row.get("exit_session") else entry
        buy, sell = float(row["entry_premium"]), float(row["exit_premium"])
        qty = int(row["lot"]) * int(lots)
        gross = (sell - buy) * qty
        charges = float(calculate_nifty_option_round_costs(buy_price=buy, sell_price=sell, quantity=qty).total)
        out.append(
            {
                "book": "Gap Carry",
                "date": exit_,  # P&L lands when the position is sold
                "open_at": datetime.combine(entry, ENTRY_TIME),
                "close_at": datetime.combine(exit_, EXIT_TIME),
                "net": round(gross - charges, 2),
                "capital": round(buy * qty, 2),
                "estimated": estimated,
            }
        )
    return out


def load_pe_ce(pe_lots: int, ce_lots: int) -> list:
    """PE and CE exactly as the five-year sheet publishes them, at any size.

    `rb.resize` recomputes fees for the new quantity for the same reason.
    """
    pe = rb.splice(
        rb.load_external(rb.src(rb.PE_FILE), "PE", False), rb.load_engine_run(rb.src(rb.PE_ENGINE_FILE), "PE")
    )
    ce = rb.splice(
        rb.load_external(rb.src(rb.CE_FILE), "CE", False), rb.load_engine_run(rb.src(rb.CE_ENGINE_FILE), "CE")
    )
    out = []
    for trades, label, lots in ((pe, "PE", pe_lots), (ce, "CE", ce_lots)):
        sized = trades if int(lots) == rb.LOTS else rb.resize(trades, int(lots))
        for t in sized:
            # The REAL window the premium is committed for, not the whole day.
            # It matters: 95% of PE trades and 84% of CE trades are already
            # closed by 15:10, so treating them as day-long double-counts them
            # against a Gap Carry position that only opens once they are gone.
            opened = datetime.fromisoformat(t["entry_time"])
            out.append(
                {
                    "book": label,
                    "date": opened.date(),
                    "open_at": opened,
                    "close_at": opened + timedelta(minutes=int(t["hold_min"])),
                    "net": float(t["net"]),
                    "capital": float(t["premium"]),
                    "estimated": False,
                }
            )
    return out


def peak_capital(trades: list) -> tuple:
    """The most that was ever committed AT ONE MOMENT, and when.

    A sweep of open/close events, not a per-day sum. Bucketing by day charged
    the account for positions that never coexisted: 95% of PE trades and 84% of
    CE trades close before 15:10, and Gap Carry does not enter until 15:10, so
    a day-sum billed the same rupees twice. At the live size it overstated the
    peak by Rs 34,194 -- 35% (Phil, 2026-09-02: "if we close the CE PE trade at
    15:10 and starting GC, the capital used for these will be less correct?").

    The real overlap is the one it now finds: Gap Carry's 15:10 entry against
    whichever intraday leg is still open at that minute, and its 09:20 exit
    against the next morning's entries.
    """
    events = []
    for t in trades:
        events.append((t["open_at"], t["capital"]))
        events.append((t["close_at"], -t["capital"]))
    events.sort(key=lambda e: e[0])
    run = peak = 0.0
    at = None
    for when, delta in events:
        run += delta
        if run > peak:
            peak, at = run, when
    return peak, at


def capital_by_day(trades: list) -> dict:
    """Peak committed within each date -- the same sweep, per session."""
    per_day = defaultdict(list)
    for t in trades:
        per_day[t["open_at"].date()].append((t["open_at"], t["capital"]))
        per_day[t["close_at"].date()].append((t["close_at"], -t["capital"]))
        # A carry spans the night: it is held all through any day between.
        day = t["open_at"].date() + timedelta(days=1)
        while day < t["close_at"].date():
            per_day[day].append((datetime.combine(day, ENTRY_TIME), 0.0))
            day += timedelta(days=1)
    out = {}
    for day, evs in per_day.items():
        run = hi = 0.0
        # Carries already open at the start of this day.
        for t in trades:
            if t["open_at"].date() < day <= t["close_at"].date():
                run += t["capital"]
        hi = run
        for _when, delta in sorted(evs, key=lambda e: e[0]):
            run += delta
            hi = max(hi, run)
        out[day] = hi
    return out


def drawdown(daily: list) -> tuple:
    """Deepest peak-to-trough fall of the running total, and when."""
    peak, worst, run = 0.0, 0.0, 0.0
    at_from = at_to = None
    peak_day = None
    for day, pnl in daily:
        run += pnl
        if run > peak:
            peak, peak_day = run, day
        fall = run - peak
        if fall < worst:
            worst, at_from, at_to = fall, peak_day, day
    return worst, at_from, at_to


def streaks(months: list) -> tuple:
    """Longest run of losing months, and of winning months."""
    best_loss = best_win = 0
    loss_at = win_at = None
    cur_l = cur_w = 0
    for key, net in months:
        cur_l = cur_l + 1 if net < 0 else 0
        cur_w = cur_w + 1 if net > 0 else 0
        if cur_l > best_loss:
            best_loss, loss_at = cur_l, key
        if cur_w > best_win:
            best_win, win_at = cur_w, key
    return (best_loss, loss_at), (best_win, win_at)


def money(x: float) -> str:
    """Indian grouping, because every other figure in this repo uses it."""
    neg = x < 0
    s = f"{abs(x):,.0f}"
    parts = s.split(",")
    if len(parts) > 1:  # regroup 1,234,567 -> 12,34,567
        digits = s.replace(",", "")
        last3, rest = digits[-3:], digits[:-3]
        out = ""
        while len(rest) > 2:
            out = "," + rest[-2:] + out
            rest = rest[:-2]
        s = (rest + out).lstrip(",") + "," + last3
    return ("-Rs " if neg else "Rs ") + s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--priced-only", action="store_true", help="drop Gap Carry exits priced from the index")
    # THE THREE BOOKS ARE NOT PUBLISHED AT THE SAME SIZE. PE and CE are 4 lots
    # (rebuild_data.LOTS); Gap Carry is 1. Summing them as published answers a
    # question nobody asked, so the size is named here and printed in the
    # header -- and `--lots N` puts all three on the same footing.
    ap.add_argument("--lots", type=int, default=0, help="one size for all three (overrides the three below)")
    ap.add_argument("--pe-lots", type=int, default=rb.LOTS, help="default 4, the published size")
    ap.add_argument("--ce-lots", type=int, default=rb.LOTS, help="default 4, the published size")
    ap.add_argument("--gc-lots", type=int, default=1, help="default 1, the published size")
    ap.add_argument("--csv", default="", help="write the day-by-day series here")
    args = ap.parse_args(argv)
    if args.lots:
        args.pe_lots = args.ce_lots = args.gc_lots = int(args.lots)
    for name, n in (("--pe-lots", args.pe_lots), ("--ce-lots", args.ce_lots), ("--gc-lots", args.gc_lots)):
        if n < 1:
            raise SystemExit(f"{name} must be at least 1")

    trades = load_pe_ce(args.pe_lots, args.ce_lots) + load_gap_carry(args.priced_only, args.gc_lots)
    if not trades:
        raise SystemExit("no trades loaded")
    trades.sort(key=lambda t: t["date"])

    by_day = defaultdict(float)
    by_book = defaultdict(float)
    counts = defaultdict(int)
    for t in trades:
        by_day[t["date"]] += t["net"]
        by_book[t["book"]] += t["net"]
        counts[t["book"]] += 1
    daily = sorted(by_day.items())
    held = capital_by_day(trades)
    peak_capital_value, peak_moment = peak_capital(trades)

    total = sum(t["net"] for t in trades)
    first, last = daily[0][0], daily[-1][0]
    years = max((last - first).days / 365.25, 1e-9)

    by_month_map = defaultdict(float)
    for day, pnl in daily:
        by_month_map[f"{day:%Y-%m}"] += pnl
    months = sorted(by_month_map.items())
    green = [m for m in months if m[1] > 0]
    red = [m for m in months if m[1] < 0]

    dd, dd_from, dd_to = drawdown(daily)
    (loss_run, loss_end), (win_run, win_end) = streaks(months)

    # What the account must carry: the most it ever had at work, plus the
    # deepest hole it ever dug. Both can happen at once, and a book that funds
    # only the first is margin-called by the second.
    floor = peak_capital_value + abs(dd)

    print("=" * 74)
    print("  GAP CARRY + PE + CE, RUN TOGETHER")
    print(f"  {first} -> {last}   ({years:.1f} years, {len(daily)} trading days)")
    print(f"  Size: PE {args.pe_lots} lot(s), CE {args.ce_lots} lot(s), Gap Carry {args.gc_lots} lot(s)")
    if args.priced_only:
        print("  Gap Carry exits priced from the index are EXCLUDED")
    print("=" * 74)

    print("\nWHAT EACH BOOK CONTRIBUTED")
    for book in ("PE", "CE", "Gap Carry"):
        if counts[book]:
            print(
                f"  {book:<10} {counts[book]:>4} trades   {money(by_book[book]):>16}   {100 * by_book[book] / total:5.1f}%"
            )
    print(f"  {'TOTAL':<10} {len(trades):>4} trades   {money(total):>16}")

    print("\nCAPITAL")
    print(f"  Most ever at work at one moment {money(peak_capital_value)}   ({peak_moment})")
    print(f"  Deepest drawdown               {money(dd)}   ({dd_from} -> {dd_to})")
    print(f"  So the account must carry      {money(floor)}   (peak at work + deepest hole)")
    print(f"  With a 30% cushion             {money(floor * 1.3)}")

    print("\nA SINGLE DAY")
    best_day = max(daily, key=lambda kv: kv[1])
    worst_day = min(daily, key=lambda kv: kv[1])
    print(f"  Best day                       {money(best_day[1])}   ({best_day[0]})")
    print(f"  Worst day                      {money(worst_day[1])}   ({worst_day[0]})")
    print(f"  Average day                    {money(total / len(daily))}")
    print(f"  Days green / red               {sum(1 for _, v in daily if v > 0)} / {sum(1 for _, v in daily if v < 0)}")

    print("\nBY MONTH")
    print(f"  Months traded                  {len(months)}   ({len(green)} green, {len(red)} red)")
    print(
        f"  Best month                     {money(max(months, key=lambda kv: kv[1])[1])}   ({max(months, key=lambda kv: kv[1])[0]})"
    )
    print(
        f"  Worst month                    {money(min(months, key=lambda kv: kv[1])[1])}   ({min(months, key=lambda kv: kv[1])[0]})"
    )
    print(f"  Average month                  {money(total / len(months))}")
    print(f"  Longest losing run             {loss_run} months   (ending {loss_end})")
    print(f"  Longest winning run            {win_run} months   (ending {win_end})")

    print("\nPER YEAR")
    by_year = defaultdict(float)
    for day, pnl in daily:
        by_year[day.year] += pnl
    for y in sorted(by_year):
        red_m = sum(1 for k, v in months if k.startswith(str(y)) and v < 0)
        print(f"  {y}   {money(by_year[y]):>16}   {red_m} losing months")
    print(f"  {'average':<6} {money(total / years):>16}   per year")

    print("\nEVERY MONTH")
    for i in range(0, len(months), 3):
        row = "  "
        for key, net in months[i : i + 3]:
            row += f"{key}  {money(net):>14}    "
        print(row.rstrip())

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "net", "running_net", "capital_at_work"])
            run = 0.0
            for day, pnl in daily:
                run += pnl
                w.writerow([day.isoformat(), round(pnl, 2), round(run, 2), round(held.get(day, 0.0), 2)])
        print(f"\nwrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
