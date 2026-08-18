"""Sweep many mothers through the real engine offline; one line each. Crash-hunt + sanity."""

import sys
import traceback
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, "/Users/philipkumar/Documents/PhilForge")
sys.path.insert(0, "/Users/philipkumar/Documents/PhilForge/tools/fib_offline")
import os

from fib_replay import load  # noqa: E402

from data.cascade_upstox import UpstoxPremiumSource  # noqa: E402
from engine.backtest import get_lot_size  # noqa: E402
from engine.fib_touch_ladder import HALVING_LEVELS, FibTouchConfig  # noqa: E402
from engine.fib_touch_ladder import FibTouchLadder as _Base

LEVELS = tuple(int(x) for x in os.environ["LEVELS"].split(",")) if os.environ.get("LEVELS") else HALVING_LEVELS

RULE = os.environ.get("RULE", "")  # "buys" | "rungs" | "fibs" | ""
EXTRA = os.environ.get("EXTRA", "1")  # "1" = one more day, "carry" = until target/mother/expiry


class FibTouchLadder(_Base):
    """Phil's 2026-08-18 experiment: defer the 15:15 close under a condition."""

    def _session_index(self, bar):
        days = {b.timestamp.date() for b in self.history}
        days.add(bar.timestamp.date())
        return len(days)  # 1 on the mother's own day

    def _defer(self, bar):
        if not self.fills:
            return False
        if EXTRA != "carry" and self._session_index(bar) > int(EXTRA):
            return False
        if RULE == "hold":
            return True  # any position still held carries
        if RULE == "buys":
            return len(self.fills) > 4
        if RULE == "rungs":
            return sum(1 for r in self.rungs if r.status == "FILLED") > 4
        if RULE == "fibs":
            return len(self.geometry.fibs) >= 2
        return False

    def _try_intraday_close(self, bar):
        if not self.config.intraday_close or bar.timestamp.time() < self.config.intraday_close_at:
            return False
        if self._defer(bar):
            return False
        return super()._try_intraday_close(bar)


tf = sys.argv[1]
side = sys.argv[2]
buy_mode = sys.argv[3]
intraday = sys.argv[4] == "intraday"
start = date.fromisoformat(sys.argv[5])
end = date.fromisoformat(sys.argv[6])
times = sys.argv[7].split(",") if len(sys.argv) > 7 else ["09:15"]
out_csv = sys.argv[8] if len(sys.argv) > 8 else None
rows_out = []

src = UpstoxPremiumSource(cache_only=False, backfill_missing=True)
expiries = sorted(src.available_expiries())


def premium(when, strike, expiry, opt):
    c = SimpleNamespace(symbol="NIFTY", underlying="NIFTY", strike=float(strike), expiry=expiry, option_type=opt)
    bar = src.lookup(when, c)
    return float(bar.open) if bar is not None and bar.open > 0 else None


def expiry_source(on):
    return [e for e in expiries if e >= on]


geo_all = load(tf)
ent_all = load("1m") if tf != "1m" else geo_all
geo_stamps = {c.timestamp for c in geo_all}

d = start
tot = 0.0
n = 0
while d <= end:
    for t in times:
        hh, mm = map(int, t.split(":"))
        mother = datetime(d.year, d.month, d.day, hh, mm)
        if mother not in geo_stamps:
            continue
        lot = int(get_lot_size("NIFTY", d))
        cfg = FibTouchConfig(
            symbol="NIFTY",
            side=side,
            mother_timestamp=mother,
            lot_size=lot,
            strike_step=50.0,
            timeframe=tf,
            entry_timeframe="1m",
            capital_cap_inr=75000.0,
            itm_steps=2,
            min_dte=4,
            buy_mode=buy_mode,
            intraday_close=intraday,
            levels=LEVELS,
            trailing_stop=bool(os.environ.get("TRAIL")),
            trail_span_multiple=float(os.environ.get("TRAIL") or 1.0),
            target_fraction=float(os.environ.get("TARGET") or 0.25),
            deep_target=os.environ.get("DEEP_TARGET", "1") == "1",
            deep_carry=os.environ.get("DEEP_CARRY", "0") == "1",
        )
        eng = FibTouchLadder(cfg, premium_lookup=premium, expiry_source=expiry_source)
        hz = mother.date() + timedelta(days=10)
        try:
            eng.replay(
                [c for c in geo_all if mother.date() <= c.timestamp.date() <= hz],
                [c for c in ent_all if mother.date() <= c.timestamp.date() <= hz],
            )
        except Exception:
            print(mother, "CRASH")
            traceback.print_exc()
            continue
        st = eng.get_status()
        rounds = st.get("rounds") or []
        net = sum(float(r.get("net_pnl") or 0) for r in rounds)
        fills = sum(len(r.get("fills") or []) for r in rounds) + len(st.get("fills") or [])
        gaps = st.get("data_gaps") or []
        print(
            f"{mother:%Y-%m-%d %H:%M} {st['status']:<14} {str(st.get('exit_reason')):<22} "
            f"fibs={len(st.get('fibs') or [])} rounds={len(rounds)} buys={fills} net={net:>10.2f} "
            f"exit={str(st.get('exit_timestamp'))[:16]} gaps={len(gaps)}"
        )
        if rounds:
            n += 1
            tot += net
        rows_out.append(
            {
                "mother": mother.isoformat(),
                "tf": tf,
                "side": side,
                "mode": buy_mode,
                "session": "intraday" if intraday else "carry",
                "status": st["status"],
                "exit_reason": st.get("exit_reason"),
                "exit_timestamp": st.get("exit_timestamp"),
                "fibs": len(st.get("fibs") or []),
                "rounds": len(rounds),
                "buys": fills,
                "gross": round(sum(float(r.get("gross_pnl") or 0) for r in rounds), 2),
                "costs": round(sum(float(r.get("costs_total") or 0) for r in rounds), 2),
                "net": round(net, 2),
                "gaps": len(gaps),
                "deployed": round(sum(float(r.get("deployed_inr") or 0) for r in rounds), 2),
                "still_open": bool(st.get("fills"))
                and st["status"] not in {"CLOSED", "EXPIRED", "MOTHER_BROKEN", "KILLED"},
                "days": len(
                    {
                        str(e.get("timestamp", ""))[:10]
                        for e in eng.events
                        if e.get("event") in {"fill", "target", "intraday_close", "expiry_exit", "mother_broken"}
                    }
                ),
            }
        )
    d += timedelta(days=1)
print(f"campaigns with a round: {n}, total net {tot:.2f}")
if out_csv:
    import csv

    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()) if rows_out else ["mother"])
        w.writeheader()
        w.writerows(rows_out)
    print("wrote", out_csv, len(rows_out))
