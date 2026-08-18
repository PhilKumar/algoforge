"""Sweep many mothers through the real engine offline; one line each. Crash-hunt + sanity."""

import sys
import traceback
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, "/Users/philipkumar/Documents/PhilForge")
sys.path.insert(0, "/Users/philipkumar/Documents/PhilForge/tools/fib_offline")
import os

from fib_replay import load  # noqa: E402

from engine.backtest import get_lot_size  # noqa: E402
from engine.fib_touch_ladder import HALVING_LEVELS, FibTouchConfig, FibTouchLadder  # noqa: E402

LEVELS = tuple(int(x) for x in os.environ["LEVELS"].split(",")) if os.environ.get("LEVELS") else HALVING_LEVELS

tf = sys.argv[1]
side = sys.argv[2]
buy_mode = sys.argv[3]
intraday = sys.argv[4] == "intraday"
start = date.fromisoformat(sys.argv[5])
end = date.fromisoformat(sys.argv[6])
times = sys.argv[7].split(",") if len(sys.argv) > 7 else ["09:15"]
out_csv = sys.argv[8] if len(sys.argv) > 8 else None
rows_out = []

from fib_replay import _listed_source  # noqa: E402

src = _listed_source()
expiries = src.expiries()


def premium(when, strike, expiry, opt):
    c = SimpleNamespace(symbol="NIFTY", underlying="NIFTY", strike=float(strike), expiry=expiry, option_type=opt)
    return src.lookup(when, c)


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
                "lot": lot,
                "levels": ",".join(map(str, LEVELS)),
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
