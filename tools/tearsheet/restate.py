"""Restate Phil's 5-year external PE/CE exports onto real lot sizes and real fees.

The export stamps qty 260 (4 lots at TODAY's NIFTY lot) on every trade in all
five years, and its Profit column is GROSS. Both have to be corrected before
any figure is quoted.
"""

import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime

sys.path.insert(0, "/Users/philipkumar/Documents/PhilForge/.worktrees/skip-days")
from engine.backtest import _calc_fees, get_option_contract_lot_size  # noqa: E402

DL = "/Users/philipkumar/Downloads/"
MON = {
    m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])
}
SYM = re.compile(r"^NIFTY(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$")
LOTS = 4  # Phil trades 4 lots


def parse_symbol(s):
    m = SYM.match(s.strip())
    if not m:
        raise ValueError(f"unparsed symbol: {s}")
    dd, mmm, yy, strike, side = m.groups()
    return date(2000 + int(yy), MON[mmm], int(dd)), int(strike), side


def load_external(path, side_label):
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            expiry, strike, side = parse_symbol(r["Instrument"])
            entry = float(r["Entry Price"])
            exit_ = float(r["Exit Price"])
            lot = get_option_contract_lot_size("26000", expiry)
            qty = LOTS * lot
            gross = (exit_ - entry) * qty
            fee = _calc_fees((entry + exit_) * qty, gross)
            et = datetime.strptime(r["Entry Time"], "%d %b %Y %H:%M:%S")
            xt = datetime.strptime(r["Exit Time"], "%d %b %Y %H:%M:%S")
            out.append(
                {
                    "side": side_label,
                    "symbol": r["Instrument"],
                    "expiry": expiry.isoformat(),
                    "strike": strike,
                    "entry": entry,
                    "exit": exit_,
                    "lot": lot,
                    "qty": qty,
                    "entry_time": et.isoformat(sep=" "),
                    "exit_time": xt.isoformat(sep=" "),
                    "date": et.date().isoformat(),
                    "gross": round(gross, 2),
                    "fee": round(fee, 2),
                    "net": round(gross - fee, 2),
                    "as_exported": float(r["Profit"]),
                    "hold_min": int((xt - et).total_seconds() // 60),
                }
            )
    return out


def load_upstox(path, side_label):
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            et = datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M")
            xt = datetime.strptime(r["exit_time"], "%Y-%m-%d %H:%M")
            out.append(
                {
                    "side": side_label,
                    "symbol": r["strike"],
                    "entry": float(r["entry_price"]),
                    "exit": float(r["exit_price"]),
                    "qty": int(r["qty"]),
                    "entry_time": et.isoformat(sep=" "),
                    "exit_time": xt.isoformat(sep=" "),
                    "date": et.date().isoformat(),
                    "net": float(r["pnl"]),
                    "exit_reason": r["exit_reason"],
                    "hold_min": int((xt - et).total_seconds() // 60),
                }
            )
    return out


def streaks(seq):
    """Longest run of wins and of losses, in trade order."""
    best_w = best_l = cur_w = cur_l = 0
    for p in seq:
        if p > 0:
            cur_w += 1
            cur_l = 0
        elif p < 0:
            cur_l += 1
            cur_w = 0
        else:
            cur_w = cur_l = 0
        best_w = max(best_w, cur_w)
        best_l = max(best_l, cur_l)
    return best_w, best_l


def drawdown(daily_sorted):
    """Peak-to-trough on the cumulative curve; returns depth and its window."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    peak_at = None
    trough_at = None
    pa = None
    for d, p in daily_sorted:
        cum += p
        if cum > peak:
            peak = cum
            pa = d
        dd = cum - peak
        if dd < worst:
            worst = dd
            peak_at = pa
            trough_at = d
    return round(worst, 2), peak_at, trough_at


def summarise(trades, label):
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["date"]] += t["net"]
    daily = sorted(by_day.items())
    dd, dd_from, dd_to = drawdown(daily)
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    bw, bl = streaks([t["net"] for t in ordered])
    total = sum(nets)
    by_year = defaultdict(lambda: {"net": 0.0, "n": 0, "w": 0})
    by_month = defaultdict(lambda: {"net": 0.0, "n": 0, "w": 0})
    by_dow = defaultdict(lambda: {"net": 0.0, "n": 0, "w": 0})
    for t in ordered:
        y = t["date"][:4]
        m = t["date"][:7]
        dow = datetime.fromisoformat(t["date"]).strftime("%A")
        for bucket, key in ((by_year, y), (by_month, m), (by_dow, dow)):
            bucket[key]["net"] += t["net"]
            bucket[key]["n"] += 1
            bucket[key]["w"] += 1 if t["net"] > 0 else 0

    def fin(d):
        return {k: {"net": round(v["net"], 2), "n": v["n"], "w": v["w"]} for k, v in sorted(d.items())}

    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "label": label,
        "trades": len(trades),
        "first": ordered[0]["date"],
        "last": ordered[-1]["date"],
        "net": round(total, 2),
        "fees": round(sum(t.get("fee", 0) for t in trades), 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(nets), 1),
        "avg_trade": round(total / len(nets), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "max_gain": round(max(nets), 2),
        "max_loss": round(min(nets), 2),
        "max_gain_trade": max(trades, key=lambda t: t["net"]),
        "max_loss_trade": min(trades, key=lambda t: t["net"]),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy": round(total / len(nets), 2),
        "max_dd": dd,
        "dd_from": dd_from,
        "dd_to": dd_to,
        "return_over_dd": round(total / abs(dd), 2) if dd else None,
        "streak_win": bw,
        "streak_loss": bl,
        "median_hold_min": sorted(t["hold_min"] for t in trades)[len(trades) // 2],
        "trading_days": len(daily),
        "by_year": fin(by_year),
        "by_month": fin(by_month),
        "by_dow": fin(by_dow),
        "daily": [[d, round(p, 2)] for d, p in daily],
    }


pe = load_external(DL + "12170898-PE_BUY_LIVE_BEST_5yrs Mod.csv", "PE")
ce = load_external(DL + "12170969-CE_BUY_LIVE Mod.csv", "CE")
upe = load_upstox(DL + "PE_CPR_4Lot_Upstox_Real_trades.csv", "PE")
uce = load_upstox(DL + "CE_BUY_LIVE_copy-1_Upstox_Real_trades.csv", "CE")

out = {
    "pe": summarise(pe, "PE (5-year, restated)"),
    "ce": summarise(ce, "CE (5-year, restated)"),
    "combined": summarise(pe + ce, "PE + CE combined"),
    "upstox_pe": summarise(upe, "PE (Upstox real prices)"),
    "upstox_ce": summarise(uce, "CE (Upstox real prices)"),
    "upstox_combined": summarise(upe + uce, "Upstox combined"),
    "as_exported": {
        "pe": round(sum(t["as_exported"] for t in pe), 2),
        "ce": round(sum(t["as_exported"] for t in ce), 2),
    },
}
json.dump(
    out,
    open(
        "/private/tmp/claude-501/-Users-philipkumar-Documents-CryptoForge/0e050fdd-b00e-48ad-a141-3e8cf88189b3/scratchpad/metrics.json",
        "w",
    ),
    indent=1,
)

for k in ("pe", "ce", "combined", "upstox_pe", "upstox_ce", "upstox_combined"):
    s = out[k]
    print(
        f"{s['label']:32s} n={s['trades']:4d} {s['first']}..{s['last']} "
        f"net={s['net']:>12,.0f} win%={s['win_rate']:5.1f} maxDD={s['max_dd']:>11,.0f} "
        f"PF={s['profit_factor']} streak W{s['streak_win']}/L{s['streak_loss']}"
    )
print("\nas exported (gross, flat qty 260):", out["as_exported"])
print("\nPE by year:", {y: v["net"] for y, v in out["pe"]["by_year"].items()})
print("CE by year:", {y: v["net"] for y, v in out["ce"]["by_year"].items()})
