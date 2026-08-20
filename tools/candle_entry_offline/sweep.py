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
from engine.candle_ladder import LADDER_DEPTH, LadderCandle, ladder_from  # noqa: E402
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
    kept = [rows[stamp] for stamp in sorted(rows) if "09:15:00" <= rows[stamp][0][11:19] < "15:30:00"]
    # NO MUHURAT. Diwali's session is an hour of ceremonial trade that opens in
    # the afternoon -- 2025-10-21 ran 13:45 to 14:45, twelve 5m bars. Its
    # candles are as real as any other and that is the problem: they shift the
    # 278-bar box by half a day, and two reds inside a token session are not
    # the pattern this rule is about. A session is a session when it starts at
    # 09:15, which is true of every regular day and of no muhurat.
    opens: dict[str, str] = {}
    for r in kept:
        day = r[0][:10]
        opens[day] = min(opens.get(day, "99:99:99"), r[0][11:19])
    out = []
    for r in kept:
        if opens[r[0][:10]] != "09:15:00":
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
    ap.add_argument("--trail", default="0", help="comma list: 0 = the target is the sale; above 0 it arms a trail")
    ap.add_argument("--target", default="0.25", help="comma list: how far back toward the mother the target sits")
    ap.add_argument("--expiry", default="weekly4")
    ap.add_argument("--start", default="2024-10-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--stop", default="0", help="comma list: sell when the option is this pct below what it paid")
    ap.add_argument("--fall", default="0", help="comma list: do not arm until price is this pct below the mother high")
    ap.add_argument(
        "--min-buys", dest="min_buys", type=int, default=1, help="ignore the target until this many rungs are bought"
    )
    ap.add_argument("--bars", default="0", help="comma list: rolling window of bars whose high/low makes the box")
    ap.add_argument("--pos", default="0.5", help="comma list: only arm below this position in the box (0.5 = midpoint)")
    ap.add_argument("--depth", default=str(LADDER_DEPTH), help="how many charts the ladder climbs")
    ap.add_argument(
        "--hold", default="", help="comma list: sell at 15:15 this many days after the first buy (0 = same day)"
    )
    ap.add_argument(
        "--strike-offset",
        dest="strike_offset",
        type=int,
        default=-100,
        help="strike relative to ATM: -100 is two in the money for a CE",
    )
    ap.add_argument(
        "--slip", type=float, default=0.0, help="rupees per unit of slippage, charged AGAINST the campaign on both legs"
    )
    ap.add_argument(
        "--shift", type=int, default=0, help="placebo: start the campaign this many bars AFTER the qualifying mother"
    )
    ap.add_argument(
        "--strike-at",
        dest="strike_at",
        default="each_buy",
        help="each_buy = every rung ATM of its own fill (the page's rule); first_buy = one contract, ATM of the first fill; mother = ATM of the mother's close",
    )
    ap.add_argument(
        "--rechain",
        default="until_profit",
        help=(
            "what happens when a campaign ends: none = wait for the next NEW box high (the original rule); "
            "always = start again at once on the current box's high bar, watching from the exit; "
            "until_profit = the same, but only while the last campaign on it did not end in profit; "
            "always_same = always, but the mother's high RATCHETS -- it is replaced only by a higher bar, "
            "never by the lower high the sliding 278-bar box leaves behind"
        ),
    )
    ap.add_argument(
        "--below-mother",
        dest="below_mother",
        action="store_true",
        help="refuse a rung whose buy-stop is at or above the mother's high -- its target would sit BEHIND the buy",
    )
    ap.add_argument(
        "--atm-fallback",
        dest="atm_fallback",
        action="store_true",
        help="a rung whose own strike has no recorded price is re-struck AT THE MONEY and priced there",
    )
    ap.add_argument(
        "--atm-step",
        dest="atm_step",
        type=int,
        default=0,
        help="round every strike choice to this step (100 = round numbers only); 0 = the exchange's own 50",
    )
    ap.add_argument(
        "--fallback-step",
        dest="fallback_step",
        type=int,
        default=0,
        help="the same, for the unpriced-rung search only; 0 = follow --atm-step",
    )
    ap.add_argument("--side", default="ce", help="ce = the rule as written; pe = its mirror (mother's LOW, two greens)")
    ap.add_argument(
        "--mother",
        default="clock",
        help="clock = the 7 fixed times a day; box = the bar that MAKES the --bars high (Phil's rule)",
    )
    ap.add_argument("--csv", default="")
    args = ap.parse_args()
    tf = args.tf.lower()
    side = "PE" if str(args.side).upper() == "PE" else "CE"
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

    def premium(when: datetime, contract) -> float | None:  # noqa: C901
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

    def box_mothers(window: int) -> list:
        """Phil's rule: the mother is the bar that MAKES the box high (CE) or
        low (PE) -- its high tops the last `window` bars, itself included,
        which is knowable the moment it closes. Picked PER WINDOW, because a
        grid over several --bars values must not borrow the first one's
        mothers (it did, on 2026-08-19, and two rows of a nine-row grid were
        measured on the wrong mothers)."""
        if window <= 0:
            raise SystemExit("--mother box needs --bars (e.g. --bars 278)")
        rows = series[tf]
        found = []
        for i in range(window - 1, len(rows)):
            bar = rows[i]
            if not (start <= bar.timestamp.date() <= end):
                continue
            span = rows[i - window + 1 : i + 1]
            made_it = bar.high >= max(r.high for r in span) if side == "CE" else bar.low <= min(r.low for r in span)
            if made_it:
                # A placebo run moves the mother off the bar that earned it. If
                # the result survives that, the rule is not what is making it.
                j = i + args.shift
                if 0 <= j < len(rows):
                    found.append(rows[j])
        return found

    clock_mothers = [c for c in series[tf] if start <= c.timestamp.date() <= end and c.timestamp.time() in CLOCK_TIMES]
    by_tf_index = {k: rows for k, rows in series.items()}

    # ONE PROCESS FOR THE WHOLE GRID. Loading these candle caches costs about a
    # gigabyte, so running a dozen configs as a dozen parallel processes buries
    # the machine in swap (it hung Phil's Mac on 2026-08-19). The data is loaded
    # once here and every configuration walks it in turn.
    def run_config(
        stop_pct: float,
        fall_pct: float,
        bars: int = 0,
        pos: float = 0.5,
        hold: int | None = None,
        target: float = 0.25,
        trail: float = 0.0,
    ) -> dict:
        rows_out: list[dict] = []
        free_from: datetime | None = None
        stale_fills[0] = 0
        candidates = box_mothers(bars) if args.mother == "box" else clock_mothers

        def current_box_high(at: datetime):
            """The bar holding the high of the `bars` bars ending at `at` -- the
            box's own mother as the page would find it at that moment."""
            rows = [c for c in series[tf] if c.timestamp <= at][-bars:]
            if len(rows) < bars:
                return None
            top = max(rows, key=lambda c: c.high)
            # the LATEST bar at that high, as the finder returns it
            for c in reversed(rows):
                if c.high >= top.high:
                    return c
            return top

        def run_campaign(mother, watch_from: datetime | None):
            """One campaign: the mother's high is the reference, the box is the
            window ending where the watch starts, and the ladder reads bars
            from there. `watch_from` None = from the mother itself (the
            original rule); set = a historical mother, watched from that
            moment (Phil, 2026-08-20: 'a history mother ... the second trade
            direct with 1 buy start')."""
            starts = watch_from or mother.timestamp
            expiry = pick_expiry(args.expiry, expiries, starts.date())
            if expiry is None:
                return None
            atm = int(mother.close / 50.0 + 0.5) * 50
            strike = atm + args.strike_offset if side == "CE" else atm - args.strike_offset
            contract = FixedCampaignOption("NIFTY", strike, expiry, side, int(get_lot_size("NIFTY", starts.date())), "")
            slip = float(args.slip)
            holder: list = [None]

            def slipped(when, contract_, _holder=holder):
                raw = premium(when, contract_)
                if raw is None or not slip:
                    return raw
                box = _holder[0]
                selling = box is not None and box.ladder.fills and box.ladder.exit_timestamp is not None
                return max(0.05, raw - slip) if selling else raw + slip

            window_end = min(expiry, data_end)
            batches = {
                k: [c for c in rows if starts.date() <= c.timestamp.date() <= window_end and c.timestamp >= starts]
                for k, rows in by_tf_index.items()
            }
            offset_points = args.strike_offset if side == "CE" else -args.strike_offset
            engine = LadderCandleEntryPaper(
                mother,
                tf,
                contract,
                _Sink(),
                slipped,
                target_fraction=target,
                trail_fraction=trail,
                hold_days=hold,
                min_buys_before_exit=args.min_buys,
                stop_loss_pct=stop_pct,
                min_fall_pct=fall_pct,
                range_bars=bars,
                range_position=pos,
                require_below_mother=bool(args.below_mother),
                atm_fallback=bool(args.atm_fallback),
                strike_step=int(args.atm_step or 0),
                fallback_step=int(args.fallback_step or 0),
                strike_at=args.strike_at,
                strike_offset_points=offset_points,
            )
            holder[0] = engine
            if bars:
                before = [c for c in series[tf] if c.timestamp <= starts][-bars:]
                engine.ladder.prime_range(
                    [LadderCandle(tf, c.timestamp, c.open, c.high, c.low, c.close) for c in before]
                )
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
                freed = ended_at or datetime.fromisoformat(last)
            else:
                freed = datetime.combine(data_end + timedelta(days=1), dt_time(0, 0), tzinfo=IST)
            rows_out.append(
                {
                    "mother": mother.timestamp.isoformat(),
                    "watch_from": starts.isoformat(),
                    "tf": tf,
                    "expiry": expiry.isoformat(),
                    "strike": engine.contract.strike,
                    "lot": engine.contract.lot_size,
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
                    "legs": json.dumps(
                        [
                            {
                                "t": f["timestamp"],
                                "tf": f["timeframe"],
                                "priced_at": f["priced_at"],
                                "index": f["index_price"],
                                "premium": f["option_premium"],
                                "qty": f["quantity"],
                                "rung": f["rung"],
                                "strike": f["strike"],
                            }
                            for f in st["fills"]
                        ]
                    ),
                    "exit_detail": json.dumps(st["exit"] or {}),
                    "target_index": st.get("target_index"),
                }
            )
            return freed, st

        index = 0
        while index < len(candidates):
            mother = candidates[index]
            index += 1
            if free_from is not None and mother.timestamp < free_from:
                continue
            result = run_campaign(mother, None)
            if result is None:
                continue
            free_from, st = result
            held = mother
            # RE-CHAIN: while the rule says so, the next campaign starts at once
            # on the current box's high bar, watching from where the last ended.
            while args.rechain != "none" and args.mother == "box" and free_from <= candidates[-1].timestamp:
                if args.rechain == "until_profit" and st["net_pnl"] is not None and st["net_pnl"] > 0:
                    break
                if st["status"] not in {"CLOSED", "EXPIRED"}:
                    break
                again = current_box_high(free_from)
                if again is None:
                    break
                if args.rechain == "always_same" and again.high <= held.high:
                    # RATCHET: the mother's high never steps DOWN. The box high
                    # slides lower as the old mother scrolls out of the 278-bar
                    # window; this variant holds the old high until a bar beats
                    # it (Phil, 2026-08-20).
                    again = held
                result = run_campaign(again, free_from)
                if result is None:
                    break
                held = again
                free_from, st = result
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
            "side": side,
            "mother": args.mother,
            "strike_at": args.strike_at,
            "rechain": args.rechain,
            "below_mother": bool(args.below_mother),
            "atm_fallback": bool(args.atm_fallback),
            "atm_step": int(args.atm_step or 0),
            "fallback_step": int(args.fallback_step or 0),
            "slip": float(args.slip),
            "depth": int(args.depth),
            "expiry": args.expiry,
            "target": target,
            "trail": trail,
            "hold": hold,
            "min_buys": args.min_buys,
            "stop": stop_pct,
            "fall": fall_pct,
            "bars": bars,
            "pos": pos,
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
    windows = [int(x) for x in str(args.bars).split(",") if x != ""]
    positions = [float(x) for x in str(args.pos).split(",") if x != ""]
    holds = [None if x in ("", "none") else int(x) for x in str(args.hold).split(",")] if args.hold != "" else [None]
    targets = [float(x) for x in str(args.target).split(",") if x != ""]
    trails = [float(x) for x in str(args.trail).split(",") if x != ""]
    for bars in windows:
        for pos in positions:
            for hold in holds:
                for target in targets:
                    for trail in trails:
                        for fall_pct in falls:
                            for stop_pct in stops:
                                print(
                                    json.dumps(run_config(stop_pct, fall_pct, bars, pos, hold, target, trail)),
                                    flush=True,
                                )


if __name__ == "__main__":
    main()
