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
from engine.candle_ladder import LADDER_DEPTH, ladder_from  # noqa: E402
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


def regroup(bars: list[IndexCandle], period: str) -> list[IndexCandle]:
    """Daily and weekly bars, built from the finest intraday series there is.

    A day is that session's own OHLC stamped at its 09:15 open (375 session
    minutes wide, which is how TIMEFRAME_MINUTES measures it); a week is the
    ISO week's days stamped at the MONDAY 09:15 -- grouped by ISO week so a
    holiday cannot slide the boundary, exactly as the equity ladder does it.
    """
    buckets: dict = {}
    for bar in bars:
        day = bar.timestamp.date()
        key = day if period == "1d" else day.isocalendar()[:2]
        buckets.setdefault(key, []).append(bar)
    out: list[IndexCandle] = []
    for key in sorted(buckets):
        rows = buckets[key]
        first = rows[0]
        stamp = first.timestamp
        if period == "1w":
            # Monday 09:15 of that ISO week, whether or not Monday traded.
            stamp = (first.timestamp - timedelta(days=first.timestamp.isoweekday() - 1)).replace(hour=9, minute=15)
        out.append(
            IndexCandle(
                stamp,
                rows[0].open,
                max(r.high for r in rows),
                min(r.low for r in rows),
                rows[-1].close,
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
    ap.add_argument("--stop", default="0", help="comma list: sell when the option is this pct below what it paid")
    ap.add_argument("--fall", default="0", help="comma list: do not arm until price is this pct below the mother high")
    ap.add_argument(
        "--min-buys", dest="min_buys", type=int, default=1, help="ignore the target until this many rungs are bought"
    )
    ap.add_argument("--depth", default=str(LADDER_DEPTH), help="how many charts the ladder climbs")
    ap.add_argument("--hold", default="", help="sell at 15:15 this many days after the first buy (0 = same day)")
    ap.add_argument("--csv", default="")
    args = ap.parse_args()
    tf = args.tf.lower()
    # The ladder's own charts only -- the engine climbs one step and stops.
    stages = ladder_from(tf, int(args.depth))
    intraday = [k for k in stages if k in ("1m", "5m", "15m", "1h")]
    series = {k: load(k) for k in intraday}
    # The slow charts are folded from the finest series loaded, so they agree
    # with the bars the ladder is already reading.
    base = series[intraday[0]]
    for slow in [k for k in stages if k in ("1d", "1w")]:
        series[slow] = regroup(base if slow == "1d" else regroup(base, "1d"), slow)
    data_end = min(rows[-1].timestamp.date() for rows in series.values())
    end = date.fromisoformat(args.end) if args.end else data_end
    start = date.fromisoformat(args.start)
    src = _listed_source()
    expiries = sorted(src.expiries())

    stale_fills = [0]

    def premium(when: datetime, contract) -> float | None:
        """The contract's price at that minute, or the nearest REAL trade to it.

        The archive holds a bar only for a minute the contract actually
        traded, and a deep ITM monthly can go minutes without a print -- so an
        exact-minute-only lookup silently threw away 11% of 15m campaigns and
        45% of 1H ones, which is a result built on dropping half the trades.
        This is the app's own rule (`_hybrid_premium_lookup`): the minute
        itself, then FORWARD through the rest of the bar (an order resting at
        the level fills at the option's next trade), then BACK up to 30
        minutes the same day. Never across a day, and never invented.
        """
        stamp = when.replace(tzinfo=None) if when.tzinfo is not None else when
        price = src.lookup(stamp, contract)
        if price is not None:
            return price
        for ahead in range(1, 16):
            later = stamp + timedelta(minutes=ahead)
            if later.date() != stamp.date() or later.time() > dt_time(15, 30):
                break
            price = src.lookup(later, contract)
            if price is not None:
                stale_fills[0] += 1
                return price
        for back in range(1, 31):
            earlier = stamp - timedelta(minutes=back)
            if earlier.date() != stamp.date() or earlier.time() < dt_time(9, 15):
                break
            price = src.lookup(earlier, contract)
            if price is not None:
                stale_fills[0] += 1
                return price
        return None

    candidates = [c for c in series[tf] if start <= c.timestamp.date() <= end and c.timestamp.time() in CLOCK_TIMES]
    by_tf_index = {k: rows for k, rows in series.items()}

    # ONE PROCESS FOR THE WHOLE GRID. Loading these candle caches costs about a
    # gigabyte, so running a dozen configs as a dozen parallel processes buries
    # the machine in swap (it hung Phil's Mac on 2026-08-19). The data is loaded
    # once here and every configuration walks it in turn.
    def run_config(stop_pct: float, fall_pct: float) -> dict:
        rows_out: list[dict] = []
        free_from: datetime | None = None
        stale_fills[0] = 0
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
                mother,
                tf,
                contract,
                _Sink(),
                premium,
                target_fraction=args.target,
                trail_fraction=args.trail,
                hold_days=int(args.hold) if args.hold != "" else None,
                min_buys_before_exit=args.min_buys,
                stop_loss_pct=stop_pct,
                min_fall_pct=fall_pct,
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
            name = (
                args.csv.replace(".csv", f"_s{stop_pct}_f{fall_pct}.csv") if len(stops) * len(falls) > 1 else args.csv
            )
            with open(name, "w", newline="") as fh:
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
        reasons: dict = {}
        for r in bought:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        ranked = sorted(nets)
        return {
            "tf": tf,
            "depth": int(args.depth),
            "expiry": args.expiry,
            "target": args.target,
            "trail": args.trail,
            "hold": args.hold if args.hold != "" else None,
            "min_buys": args.min_buys,
            "stop": stop_pct,
            "fall": fall_pct,
            "window": f"{start}..{end}",
            "mothers": len(rows_out),
            "bought": len(bought),
            "priced": len(priced),
            "unpriced_campaigns": len(bought) - len(priced),
            "win_rate": round(100 * len(wins) / len(nets), 1) if nets else None,
            "net": round(sum(nets), 2),
            # Phil's standard: a book that dies without its best five was five trades.
            "minus_best5": round(sum(ranked[:-5]), 2) if len(ranked) > 5 else None,
            "max_dd": round(dd, 2),
            "avg_buys": round(sum(r["buys"] for r in bought) / len(bought), 2) if bought else None,
            "reasons": reasons,
        }

    stops = [float(x) for x in str(args.stop).split(",") if x != ""]
    falls = [float(x) for x in str(args.fall).split(",") if x != ""]
    for fall_pct in falls:
        for stop_pct in stops:
            print(json.dumps(run_config(stop_pct, fall_pct)), flush=True)


if __name__ == "__main__":
    main()
