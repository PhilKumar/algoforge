"""tools/candle_entry_offline/sweep.py -- the two-red ladder over the whole history.

Blind mothers, one campaign at a time (a new mother is taken only once the
previous campaign has ended, the way the page runs), NIFTY CE, real recorded
premiums from the local Upstox archive, zero broker calls. Same engine the
page trades: LadderCandleEntryPaper on TwoRedLadder.

    python3 tools/candle_entry_offline/sweep.py [--tf 5m] [--trail 0.3] [--expiry weekly4|monthly]
                                                [--target 0.25] [--start 2024-10-01] [--end 2026-08-17]
                                                [--csv out.csv]

Mothers: the bar opening at 09:15, 09:30, 10:15, 11:15, 12:15, 13:15, 14:15
on the starting chart (the fib sweep's clock times), taken only when no
campaign is running. Contract: weekly4 = the first weekly expiry at least 4
calendar days after the mother; monthly = the page's rule (15-45 DTE, else
closest). Strike ATM-2 of the mother close, lot size by the mother's date.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "fib_offline"))

from fib_replay import _listed_source  # noqa: E402

from engine.backtest import get_lot_size  # noqa: E402
from engine.candle_ladder import LADDER_TIMEFRAMES  # noqa: E402
from engine.cascade_options import (  # noqa: E402
    CascadeOptionsAdapter,
    FixedCampaignOption,
    IndexCandle,
    LadderCandleEntryPaper,  # noqa: E402
)

IST = ZoneInfo("Asia/Kolkata")
CACHE = os.path.join(ROOT, "tools", ".nifty_cache")
CLOCK_TIMES = [
    dt_time(9, 15),
    dt_time(9, 30),
    dt_time(10, 15),
    dt_time(11, 15),
    dt_time(12, 15),
    dt_time(13, 15),
    dt_time(14, 15),
]


class _Sink:
    paper_only = True

    def place_order(self, *_a, **_k):
        return None


def load(tf: str) -> list[IndexCandle]:
    rows: dict[str, list] = {}
    for path in glob.glob(os.path.join(CACHE, f"NIFTY_{tf}_*.json")):
        data = json.load(open(path))
        for row in data if isinstance(data, list) else data.get("candles") or data.get("data") or []:
            rows[row[0]] = row
    out = []
    for stamp in sorted(rows):
        r = rows[stamp]
        if not ("09:15:00" <= r[0][11:19] < "15:30:00"):
            continue
        out.append(
            IndexCandle(
                datetime.fromisoformat(r[0]).replace(tzinfo=IST), float(r[1]), float(r[2]), float(r[3]), float(r[4])
            )
        )
    return out


def pick_expiry(rule: str, expiries: list[date], mother_day: date) -> date | None:
    if rule == "monthly":
        try:
            return CascadeOptionsAdapter._next_expiry(expiries, mother_day, "NIFTY", monthly_only=True)
        except Exception:
            return None
    days = int(rule.replace("weekly", "") or 4)
    later = [e for e in expiries if (e - mother_day).days >= days]
    return min(later) if later else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--trail", type=float, default=0.0)
    ap.add_argument("--target", type=float, default=0.25)
    ap.add_argument("--expiry", default="weekly4")
    ap.add_argument("--start", default="2024-10-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--csv", default="")
    args = ap.parse_args()
    tf = args.tf.lower()
    stages = LADDER_TIMEFRAMES[LADDER_TIMEFRAMES.index(tf) :]
    series = {k: load(k) for k in stages}
    data_end = min(rows[-1].timestamp.date() for rows in series.values())
    end = date.fromisoformat(args.end) if args.end else data_end
    start = date.fromisoformat(args.start)
    src = _listed_source()
    expiries = sorted(src.expiries())

    def premium(when: datetime, contract) -> float | None:
        stamp = when.replace(tzinfo=None) if when.tzinfo is not None else when
        return src.lookup(stamp, contract)

    candidates = [c for c in series[tf] if start <= c.timestamp.date() <= end and c.timestamp.time() in CLOCK_TIMES]
    by_tf_index = {k: rows for k, rows in series.items()}
    rows_out: list[dict] = []
    free_from: datetime | None = None
    for mother in candidates:
        if free_from is not None and mother.timestamp < free_from:
            continue
        expiry = pick_expiry(args.expiry, expiries, mother.timestamp.date())
        if expiry is None:
            continue
        atm = int(mother.close / 50.0 + 0.5) * 50
        contract = FixedCampaignOption(
            "NIFTY", atm - 100, expiry, "CE", int(get_lot_size("NIFTY", mother.timestamp.date())), ""
        )
        engine = LadderCandleEntryPaper(
            mother, tf, contract, _Sink(), premium, target_fraction=args.target, trail_fraction=args.trail
        )
        window_end = min(expiry, data_end)
        batches = {
            k: [c for c in rows if mother.timestamp.date() <= c.timestamp.date() <= window_end]
            for k, rows in by_tf_index.items()
        }
        engine.ingest(batches)
        engine.settle_past_expiry(
            datetime.combine(window_end, dt_time(15, 31), tzinfo=IST)
            if window_end >= expiry
            else datetime.combine(window_end, dt_time(0, 0), tzinfo=IST)
        )
        st = engine.get_status()
        ended_at = datetime.fromisoformat(st["exit"]["timestamp"]) if st["exit"] else None
        if st["status"] in {"CLOSED", "EXPIRED"}:
            last = st["latest_closed_candle"]["timestamp"]
            free_from = ended_at or datetime.fromisoformat(last)
        else:
            # still open when the data ends: nothing after it can be taken
            free_from = datetime.combine(data_end + timedelta(days=1), dt_time(0, 0), tzinfo=IST)
        rows_out.append(
            {
                "mother": mother.timestamp.isoformat(),
                "tf": tf,
                "expiry": expiry.isoformat(),
                "strike": contract.strike,
                "lot": contract.lot_size,
                "buys": len(st["fills"]),
                "lots": sum(f["lots"] for f in st["fills"]),
                "first_fill": st["fills"][0]["timestamp"] if st["fills"] else "",
                "exit": st["exit"]["timestamp"] if st["exit"] else "",
                "reason": st["exit"]["reason"] if st["exit"] else ("open" if st["fills"] else st["status"].lower()),
                "deployed": st["deployed_inr"],
                "gross": st["gross_pnl"],
                "costs": st["costs_total"],
                "net": st["net_pnl"],
                "unpriced": st["unpriced_fills"],
            }
        )
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
            w.writeheader()
            w.writerows(rows_out)
    bought = [r for r in rows_out if r["buys"]]
    priced = [r for r in bought if r["net"] is not None]
    nets = [r["net"] for r in priced]
    wins = [n for n in nets if n > 0]
    equity, peak, dd = 0.0, 0.0, 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    reasons = {}
    for r in bought:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    print(
        json.dumps(
            {
                "tf": tf,
                "trail": args.trail,
                "expiry": args.expiry,
                "target": args.target,
                "window": f"{start}..{end}",
                "mothers": len(rows_out),
                "bought": len(bought),
                "priced": len(priced),
                "unpriced_campaigns": len(bought) - len(priced),
                "wins": len(wins),
                "win_rate": round(100 * len(wins) / len(nets), 1) if nets else None,
                "net": round(sum(nets), 2),
                "avg_net": round(sum(nets) / len(nets), 2) if nets else None,
                "max_dd": round(dd, 2),
                "avg_buys": round(sum(r["buys"] for r in bought) / len(bought), 2) if bought else None,
                "reasons": reasons,
            }
        )
    )


if __name__ == "__main__":
    main()
