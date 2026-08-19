"""tools/fib_offline/fib_chain_sweep.py -- the AUTO-MOTHER rule, backtested.

Phil, 2026-08-19: "every day the 09:15 candle as the mother; if it breaks,
move to the next mother candle the same day; next day the engine selects the
candle of the same time and starts." Numbers first, then he decides.

Per session day in the cache: the 09:15 5m bar is the mother. The campaign is
the exact ladder the sheet published (5m geometry, 1m entries, CE, Lone,
intraday out by 15:15, trailing 1 span, target 0.25, max_buys default, Rs 75k
cap, ATM-2, expiry >= 4 days, lot by date, real recorded premiums). When it
ends MOTHER_BROKEN at minute t, the next mother is

    NEXT_MOTHER=following   the first 5m bar that OPENS after the 5m slot
                            holding t              (Phil: "the next 5m candle")
    NEXT_MOTHER=breaking    the 5m bar that HOLDS t (the breaking candle)

and a fresh ladder starts there, fed the same day's streams (the engine refuses
bars before its mother, and replay() keeps geometry and entries interleaved by
close time, so nothing leaks). The chain stops at the first non-broken end, at
a candidate that opens at/after 15:10, or after BARREN_CAP no-buy breaks in a
row. NEXT_MOTHER=none gives the 09:15-only reference (no chain).

    SYMBOL=NIFTY NEXT_MOTHER=following python3 tools/fib_offline/fib_chain_sweep.py \
        2024-10-03 2026-08-17 /tmp/fib_offline/chain/NIFTY_following.csv

Env: TRAIL (span multiple, default 1; unset = fixed target), TARGET (0.25),
MAX_BUYS (engine default), CAP (75000), LEVELS, BARREN_CAP (10), VERIFY=1 runs
the chain checks and a tick-simulated re-walk on a sample of days.
"""

from __future__ import annotations

import csv
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fib_replay import STRIKE_STEP, SYMBOL, _listed_source, load  # noqa: E402

from engine.backtest import get_lot_size  # noqa: E402
from engine.fib_touch_ladder import (  # noqa: E402
    HALVING_LEVELS,
    TERMINAL_STATUSES,
    TIMEFRAME_MINUTES,
    FibTouchConfig,
    FibTouchLadder,
)

TF = "5m"
TF_MIN = TIMEFRAME_MINUTES[TF]
NEXT_MOTHER = os.environ.get("NEXT_MOTHER", "following").lower()
assert NEXT_MOTHER in {"following", "breaking", "none"}, NEXT_MOTHER
BARREN_CAP = int(os.environ.get("BARREN_CAP", "10") or 10)
LAST_MOTHER_OPEN = dt_time(15, 10)  # a mother that opens here closes at 15:15: it could not buy
FIRST_MOTHER = dt_time(9, 15)
LEVELS = tuple(int(x) for x in os.environ["LEVELS"].split(",")) if os.environ.get("LEVELS") else HALVING_LEVELS
# TRAIL unset = fixed target. (TRAIL=0 would NOT turn trailing off -- bool("0") is True -- so unset it.)
TRAIL = os.environ.get("TRAIL")

start = date.fromisoformat(sys.argv[1])
end = date.fromisoformat(sys.argv[2])
out_csv = sys.argv[3] if len(sys.argv) > 3 else None

src = _listed_source()
EXPIRIES = src.expiries()


def premium(when, strike, expiry, opt):
    c = SimpleNamespace(symbol=SYMBOL, underlying=SYMBOL, strike=float(strike), expiry=expiry, option_type=opt)
    return src.lookup(when, c)


def expiry_source(on):
    return [e for e in EXPIRIES if e >= on]


GEO = load(TF)
ENT = load("1m")
geo_by_day: dict[date, list] = defaultdict(list)
ent_by_day: dict[date, list] = defaultdict(list)
for c in GEO:
    geo_by_day[c.timestamp.date()].append(c)
for c in ENT:
    ent_by_day[c.timestamp.date()].append(c)


def make(mother: datetime) -> FibTouchLadder:
    cfg = FibTouchConfig(
        symbol=SYMBOL,
        side="CE",
        mother_timestamp=mother,
        lot_size=int(get_lot_size(SYMBOL, mother.date())),
        strike_step=STRIKE_STEP,
        timeframe=TF,
        entry_timeframe="1m",
        capital_cap_inr=float(os.environ.get("CAP", "75000") or 75000),
        itm_steps=2,
        min_dte=4,
        buy_mode="levels",
        intraday_close=True,
        levels=LEVELS,
        trailing_stop=bool(TRAIL),
        trail_span_multiple=float(TRAIL or 1.0),
        target_fraction=float(os.environ.get("TARGET") or 0.25),
        **({"max_buys": int(os.environ["MAX_BUYS"])} if os.environ.get("MAX_BUYS") else {}),
    )
    return FibTouchLadder(cfg, premium_lookup=premium, expiry_source=expiry_source)


def slot_of(t: datetime) -> datetime:
    """The 5m bar that holds minute t (bars open on the 5m grid from 09:15)."""
    base = t.replace(hour=9, minute=15, second=0, microsecond=0)
    k = int((t - base).total_seconds() // (TF_MIN * 60))
    return base + timedelta(minutes=TF_MIN * k)


def next_mother(exit_ts: datetime, day_stamps: set, current: datetime) -> datetime | None:
    if NEXT_MOTHER == "none":
        return None
    holder = slot_of(exit_ts)
    cand = holder if NEXT_MOTHER == "breaking" else holder + timedelta(minutes=TF_MIN)
    # A mother can only move FORWARD. Under "breaking", a 1m close in the last
    # minute of the mother's own 5m bar can read a tick above that bar's high
    # when the two tapes disagree, which would hand the same candle back as
    # the next mother forever (one NIFTY day looped to the 1000 cap).
    if cand <= current:
        cand = current + timedelta(minutes=TF_MIN)
    if cand.time() >= LAST_MOTHER_OPEN:
        return None
    return cand if cand in day_stamps else None


def run_chain(day: date, *, engines: list | None = None) -> list[dict]:
    """Every campaign the auto rule would run on `day`, in order."""
    geo = geo_by_day.get(day) or []
    ent = ent_by_day.get(day) or []
    stamps = {c.timestamp for c in geo}
    mother = datetime.combine(day, FIRST_MOTHER)
    if mother not in stamps or not ent:
        return []  # holiday / half session / no 1m tape: nothing starts
    rows: list[dict] = []
    barren = 0
    seq = 0
    while mother is not None:
        seq += 1
        eng = make(mother)
        eng.replay(geo, ent)  # both streams self-clip before the mother
        if engines is not None:
            engines.append(eng)
        st = eng.get_status()
        rounds = st.get("rounds") or []
        open_still = bool(st.get("fills")) and st["status"] not in TERMINAL_STATUSES
        buys = sum(len(r.get("fills") or []) for r in rounds) + (len(st.get("fills") or []) if open_still else 0)
        net = sum(float(r.get("net_pnl") or 0) for r in rounds)
        exit_ts = st.get("exit_timestamp")
        rows.append(
            {
                "day": day.isoformat(),
                "seq": seq,
                "mother": mother.isoformat(),
                "status": st["status"],
                "exit_reason": st.get("exit_reason") or "",
                "exit_timestamp": exit_ts or "",
                "rounds": len(rounds),
                "buys": buys,
                "gross": round(sum(float(r.get("gross_pnl") or 0) for r in rounds), 2),
                "costs": round(sum(float(r.get("costs_total") or 0) for r in rounds), 2),
                "net": round(net, 2),
                "deployed": round(sum(float(r.get("deployed_inr") or 0) for r in rounds), 2),
                "lot": int(st.get("lot_size") or 0),
                "gaps": len(st.get("data_gaps") or []),
                "mother_high": st.get("mother_high"),
            }
        )
        if st["status"] != "MOTHER_BROKEN" or not exit_ts:
            break
        barren = barren + 1 if buys == 0 else 0
        if barren >= BARREN_CAP:
            rows[-1]["chain_cut"] = "barren_cap"
            break
        mother = next_mother(datetime.fromisoformat(exit_ts), stamps, mother)
    return rows


def book(rows: list[dict]) -> dict:
    traded = [r for r in rows if int(r["rounds"]) > 0]
    nets = [float(r["net"]) for r in traded]
    cum = peak = dd = 0.0
    for n in nets:
        cum += n
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    w = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    by_year: dict[int, float] = defaultdict(float)
    for r in traded:
        by_year[int(r["day"][:4])] += float(r["net"])
    ex: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for r in traded:
        ex[r["exit_reason"]][0] += 1
        ex[r["exit_reason"]][1] += float(r["net"])
    days = len({r["day"] for r in rows})
    per_day: dict[str, int] = defaultdict(int)
    for r in rows:
        per_day[r["day"]] += 1
    return {
        "days": days,
        "campaigns_started": len(rows),
        "campaigns_traded": len(traded),
        "net": round(sum(nets)),
        "max_dd": -round(dd),
        "r_dd": round(sum(nets) / dd, 2) if dd else None,
        "win": round(100 * len(w) / len(nets), 1) if nets else 0,
        "pf": round(sum(w) / -sum(losses), 2) if losses else None,
        "per_campaign": round(sum(nets) / len(nets)) if nets else 0,
        "avg_win": round(sum(w) / len(w)) if w else 0,
        "avg_loss": round(sum(losses) / len(losses)) if losses else 0,
        "by_year": {k: round(v) for k, v in sorted(by_year.items())},
        "exits": {k: (v[0], round(v[1])) for k, v in ex.items()},
        "chain_len_max": max(per_day.values()) if per_day else 0,
        "chain_len_avg": round(sum(per_day.values()) / len(per_day), 2) if per_day else 0,
        "barren_cuts": sum(1 for r in rows if r.get("chain_cut")),
    }


# ---- verification (VERIFY=1): chain invariants + backtest == tick-simulated paper loop
def tick_simulated(eng_cfg_mother: datetime, geo: list, ent: list) -> FibTouchLadder:
    eng = make(eng_cfg_mother)
    stamps = sorted({c.timestamp + timedelta(minutes=1) for c in ent})
    gi = 0
    last = eng_cfg_mother - timedelta(minutes=1)
    for t in stamps:
        while gi < len(geo) and geo[gi].timestamp + timedelta(minutes=TF_MIN) <= t:
            eng.on_geometry_candle(geo[gi])
            gi += 1
        for c in ent:
            if last < c.timestamp <= t - timedelta(minutes=1):
                eng.on_candle(c)
                last = c.timestamp
        if eng.status in TERMINAL_STATUSES:
            break
    return eng


def summary(eng: FibTouchLadder):
    st = eng.get_status()
    return (
        st["status"],
        st.get("exit_reason"),
        str(st.get("exit_timestamp")),
        len(st.get("rounds") or []),
        round(float(st.get("net_pnl") or 0), 2),
        tuple(
            (f["timestamp"], f["index_price"], f["premium"], f["strike"], f["expiry"])
            for r in (st.get("rounds") or [])
            for f in r["fills"]
        ),
    )


def verify(all_rows: list[dict], sample_days: int = 20) -> int:
    fails = 0
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_day[r["day"]].append(r)
    for day, rows in by_day.items():
        rows = sorted(rows, key=lambda r: r["seq"])
        if rows[0]["mother"][11:16] != "09:15":
            print("FAIL first mother not 09:15", day)
            fails += 1
        prev = None
        for r in rows:
            m = datetime.fromisoformat(r["mother"])
            if prev is not None:
                if m <= prev["m"]:
                    print("FAIL mothers not increasing", day, r["seq"])
                    fails += 1
                stamps = {c.timestamp for c in geo_by_day[date.fromisoformat(day)]}
                if next_mother(prev["x"], stamps, prev["m"]) != m:
                    print("FAIL next mother != rule(prev exit)", day, r["seq"], prev["x"], m)
                    fails += 1
                if prev["x"] > m + timedelta(minutes=TF_MIN):
                    print("FAIL overlap: prev exit after next mother closed", day, r["seq"])
                    fails += 1
            prev = {"m": m, "x": datetime.fromisoformat(r["exit_timestamp"]) if r["exit_timestamp"] else m}
    # backtest == paper-loop feed on a sample of days (every campaign of the day)
    random.seed(18)
    days = sorted(by_day)
    for day in random.sample(days, min(sample_days, len(days))):
        d = date.fromisoformat(day)
        engines: list = []
        rows = run_chain(d, engines=engines)
        for r, eng in zip(rows, engines):
            sim = tick_simulated(datetime.fromisoformat(r["mother"]), geo_by_day[d], ent_by_day[d])
            if summary(eng) != summary(sim):
                print("FAIL backtest != tick-simulated", day, r["seq"], summary(eng)[:5], summary(sim)[:5])
                fails += 1
            st = eng.get_status()
            for rr in st.get("rounds") or []:
                for f in rr["fills"]:
                    ts = datetime.fromisoformat(f["timestamp"])
                    if not (datetime.fromisoformat(r["mother"]) < ts < datetime.combine(d, dt_time(15, 15))):
                        print("FAIL fill outside its campaign's window", day, r["seq"], ts)
                        fails += 1
    print(f"verify: {len(by_day)} days, {len(all_rows)} campaigns, {fails} failures")
    return fails


# ---- main
all_rows: list[dict] = []
d = start
while d <= end:
    if d.weekday() < 5:
        all_rows.extend(run_chain(d))
    d += timedelta(days=1)

b = book(all_rows)
print(f"{SYMBOL} NEXT_MOTHER={NEXT_MOTHER} TRAIL={TRAIL} TARGET={os.environ.get('TARGET', '0.25')}")
for k, v in b.items():
    print(f"  {k:18} {v}")
if out_csv:
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    with open(out_csv, "w", newline="") as fh:
        fields = list(all_rows[0].keys()) + (["chain_cut"] if any("chain_cut" in r for r in all_rows) else [])
        fields = list(dict.fromkeys(fields))
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)
    print("wrote", out_csv, len(all_rows))
if os.environ.get("VERIFY") == "1":
    sys.exit(1 if verify(all_rows) else 0)
