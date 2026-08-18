"""Offline replay of ONE fib-boundary mother through the real FibTouchLadder.

Zero broker calls: cached Dhan NIFTY candles + the local Upstox option archive.
Usage: python fib_replay.py 2026-07-22T09:15 15m CE levels [intraday]
"""

import glob
import json
import sys
from datetime import date, datetime
from types import SimpleNamespace

sys.path.insert(0, "/Users/philipkumar/Documents/PhilForge")

from data.cascade_upstox import UpstoxPremiumSource  # noqa: E402
from engine.cascade_options import Candle  # noqa: E402
from engine.fib_touch_ladder import FibTouchConfig, FibTouchLadder  # noqa: E402

CACHE = "/Users/philipkumar/Documents/PhilForge/tools/.nifty_cache"


def load(tf: str) -> list[Candle]:
    rows: dict[str, list] = {}
    for f in glob.glob(f"{CACHE}/NIFTY_{tf}_*.json"):
        d = json.load(open(f))
        lst = d if isinstance(d, list) else d.get("candles") or d.get("data") or []
        for r in lst:
            rows[r[0]] = r
    out = []
    for k in sorted(rows):
        r = rows[k]
        ts = datetime.fromisoformat(r[0])
        # session only
        if (
            not (ts.hour > 9 or (ts.hour == 9 and ts.minute >= 15))
            or ts.hour > 15
            or (ts.hour == 15 and ts.minute >= 30)
        ):
            continue
        out.append(Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    return out


def _listed_source():
    """Archive (expired) + Upstox listed contracts, one lookup returning the OPEN or None."""
    import importlib.util as _ilu
    import os as _os

    _spec = _ilu.spec_from_file_location(
        "fib_offline_listed", _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "upstox_listed.py")
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod.ListedPremiumSource(UpstoxPremiumSource(cache_only=False, backfill_missing=True))


def main() -> None:
    mother = datetime.fromisoformat(sys.argv[1])
    tf = sys.argv[2]
    side = sys.argv[3]
    buy_mode = sys.argv[4] if len(sys.argv) > 4 else "levels"
    intraday = (sys.argv[5] if len(sys.argv) > 5 else "intraday") == "intraday"
    horizon_days = 10

    src = _listed_source()
    expiries = src.expiries()

    def premium(when: datetime, strike: float, expiry: date, opt: str):
        c = SimpleNamespace(symbol="NIFTY", underlying="NIFTY", strike=float(strike), expiry=expiry, option_type=opt)
        return src.lookup(when, c)

    def expiry_source(on: date):
        return [e for e in expiries if e >= on]

    lot = 65 if mother.date() >= date(2026, 1, 1) else 75
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
    )
    eng = FibTouchLadder(cfg, premium_lookup=premium, expiry_source=expiry_source)
    geo = [
        c
        for c in load(tf)
        if c.timestamp.date() >= mother.date() and (c.timestamp.date() - mother.date()).days <= horizon_days
    ]
    ent = (
        geo
        if tf == "1m"
        else [
            c
            for c in load("1m")
            if c.timestamp.date() >= mother.date() and (c.timestamp.date() - mother.date()).days <= horizon_days
        ]
    )
    assert any(c.timestamp == mother for c in geo), "mother not in cache"
    eng.replay(geo, ent)
    st = eng.get_status()
    print("STATUS", st["status"], "exit", st.get("exit_reason"), st.get("exit_timestamp"))
    print("mother", st.get("mother_high"), st.get("mother_low"))
    print("fibs", len(st.get("fibs") or []), "trendlines", len(st.get("trendlines") or []))
    for f in st.get("fibs") or []:
        print("  fib", f)
    print("levels:")
    for r in st.get("levels") or []:
        print("  ", r["key"], r["index_price"], r["status"], r.get("drawn_at"))
    print("fills:")
    for f in st.get("fills") or []:
        print(
            "  ",
            f["buy_number"],
            f["rung_key"],
            f["timestamp"],
            "idx",
            f["index_price"],
            "prem",
            f["premium"],
            f["strike"],
            f["expiry"],
            "covered",
            f["covered"],
        )
    print("rounds", st.get("rounds"))
    print("target_index", st.get("target_index"), "avg", st.get("average_index_entry"))
    print("gross", st.get("gross_pnl"), "costs", st.get("costs_total"), "net", st.get("net_pnl"))
    print("gaps", st.get("data_gaps"))
    print("events:")
    for e in eng.events:
        print("  ", e)


if __name__ == "__main__":
    main()
