"""tools/portfolio_combined.py -- what running Gap Carry, PE and CE together looks like.

Each of the three is published on its own, which answers "does this rule work"
and answers nothing at all about "what happens if I run them at the same time".
Those are different questions. Three books that each drew down Rs 1 lakh in the
same week need more than one lakh; three that drew down in different years need
much less than the sum. Only a combined day-by-day walk can say which.

    python3 tools/portfolio_combined.py
    python3 tools/portfolio_combined.py --priced-only     # drop estimated exits
    python3 tools/portfolio_combined.py --csv /tmp/combined.csv

WHAT IS BEING COMBINED
    PE and CE come through rebuild_data.py's own loaders and splice, so they are
    the same trades the five-year tearsheet publishes -- effective-dated lots,
    real statutory charges, engine runs joined to the export at the measured
    seam. NOT the raw CSVs, which carry a flat lot and no charges at all.

    Gap Carry comes from tools/gapcarry_offline/runs/, the book its own
    tearsheet is built from.

CAPITAL IS HELD, NOT SPENT. PE and CE are intraday: their premium is committed
and released the same session. Gap Carry is an overnight carry -- it buys at
15:10 and sells at 09:20 the NEXT session -- so its capital spans two dates and
overlaps whatever PE and CE do the following morning. This walks that overlap
day by day rather than adding three peaks that never happened together.

WHAT IT WILL NOT DO. It will not tell you what to trade or how much to risk.
It reports what these three books did on the days they ran.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "tearsheet"))

import rebuild_data as rb  # noqa: E402

GAP_CARRY = os.path.join(ROOT, "tools", "gapcarry_offline", "runs", "NIFTY_5m_rsi70_atm4.csv")


def load_gap_carry(priced_only: bool) -> list:
    """The overnight book, with the dates its capital is actually tied up.

    `held_from`/`held_to` are the entry session and the exit session, because
    the position exists across that night. An intraday book has them equal.
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
        out.append(
            {
                "book": "Gap Carry",
                "date": exit_,  # P&L lands when the position is sold
                "held_from": entry,
                "held_to": exit_,
                "net": float(row["net"]),
                "capital": float(row["capital"]),
                "estimated": estimated,
            }
        )
    return out


def load_pe_ce() -> list:
    """PE and CE exactly as the five-year sheet publishes them."""
    pe = rb.splice(
        rb.load_external(rb.src(rb.PE_FILE), "PE", False), rb.load_engine_run(rb.src(rb.PE_ENGINE_FILE), "PE")
    )
    ce = rb.splice(
        rb.load_external(rb.src(rb.CE_FILE), "CE", False), rb.load_engine_run(rb.src(rb.CE_ENGINE_FILE), "CE")
    )
    out = []
    for trades, label in ((pe, "PE"), (ce, "CE")):
        for t in trades:
            day = date.fromisoformat(t["date"])
            out.append(
                {
                    "book": label,
                    "date": day,
                    "held_from": day,  # intraday: committed and released same session
                    "held_to": day,
                    "net": float(t["net"]),
                    "capital": float(t["premium"]),
                    "estimated": False,
                }
            )
    return out


def capital_by_day(trades: list) -> dict:
    """How much was tied up on each date, counting an overnight carry on BOTH."""
    held = defaultdict(float)
    for t in trades:
        day = t["held_from"]
        while day <= t["held_to"]:
            held[day] += t["capital"]
            day += timedelta(days=1)
    return dict(held)


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
    ap.add_argument("--csv", default="", help="write the day-by-day series here")
    args = ap.parse_args(argv)

    trades = load_pe_ce() + load_gap_carry(args.priced_only)
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
    peak_capital = max(held.values())
    peak_day = max(held, key=held.get)
    # What the account must carry: the most it ever had at work, plus the
    # deepest hole it ever dug. Both can happen at once, and a book that funds
    # only the first is margin-called by the second.
    floor = peak_capital + abs(dd)

    print("=" * 74)
    print("  GAP CARRY + PE + CE, RUN TOGETHER")
    print(f"  {first} -> {last}   ({years:.1f} years, {len(daily)} trading days)")
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
    print(f"  Most ever at work on one day   {money(peak_capital)}   (on {peak_day})")
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
