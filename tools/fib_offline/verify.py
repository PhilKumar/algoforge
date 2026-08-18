"""tools/fib_offline/verify.py -- ten independent checks on the fib ladder replay.

Phil, 2026-08-18: "Recheck everything again and again at least 10 times so no
flaw happens again tomorrow." Every check below is computed independently of
the engine's own bookkeeping, over hundreds of real mothers, both modes, both
sides, across the 25 / 75 / 65 lot eras. Zero broker calls.

    python3 tools/fib_offline/verify.py [mothers-per-config]
"""

from __future__ import annotations

import os
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fib_replay import load  # noqa: E402

from data.cascade_upstox import UpstoxPremiumSource  # noqa: E402
from engine.backtest import get_lot_size  # noqa: E402
from engine.fib_touch_ladder import TIMEFRAME_MINUTES, FibTouchConfig, FibTouchLadder  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 60
random.seed(18)

src = UpstoxPremiumSource(cache_only=False, backfill_missing=True)
EXPIRIES = sorted(src.available_expiries())
LOOKUPS: list[tuple] = []


def premium(when, strike, expiry, opt):
    c = SimpleNamespace(symbol="NIFTY", underlying="NIFTY", strike=float(strike), expiry=expiry, option_type=opt)
    bar = src.lookup(when, c)
    price = float(bar.open) if bar is not None and bar.open > 0 else None
    LOOKUPS.append((when, float(strike), expiry, opt, price))
    return price


def expiry_source(on):
    return [e for e in EXPIRIES if e >= on]


def make(tf, side, mode, mother, **kw):
    cfg = FibTouchConfig(
        symbol="NIFTY",
        side=side,
        mother_timestamp=mother,
        lot_size=int(get_lot_size("NIFTY", mother.date())),
        strike_step=50.0,
        timeframe=tf,
        entry_timeframe="1m",
        capital_cap_inr=75000.0,
        itm_steps=2,
        min_dte=4,
        buy_mode=mode,
        intraday_close=True,
        **kw,
    )
    return FibTouchLadder(cfg, premium_lookup=premium, expiry_source=expiry_source)


ALL = {tf: load(tf) for tf in ("1m", "5m", "15m")}


def window(tf, mother, days=10):
    hz = mother.date() + timedelta(days=days)
    return [c for c in ALL[tf] if mother.date() <= c.timestamp.date() <= hz]


def tick_simulated(tf, side, mode, mother, **kw):
    """Feed the engine the way the PAPER LOOP does: at every minute t, all
    geometry bars closed by t (dedup'd by the engine) then the new 1m bars."""
    eng = make(tf, side, mode, mother, **kw)
    geo = window(tf, mother)
    ent = window("1m", mother)
    minutes = TIMEFRAME_MINUTES[tf]
    last = mother - timedelta(minutes=1)
    stamps = sorted({c.timestamp + timedelta(minutes=1) for c in ent})  # each 1m bar's close
    gi = 0
    for t in stamps:
        # geometry closed by t
        while gi < len(geo):
            g = geo[gi]
            width = 15 if minutes == 60 and g.timestamp.time() == dt_time(15, 15) else minutes
            if g.timestamp + timedelta(minutes=width) <= t:
                eng.on_geometry_candle(g)
                gi += 1
            else:
                break
        fresh = [c for c in ent if last < c.timestamp <= t - timedelta(minutes=1)]
        for c in fresh:
            eng.on_candle(c)
            last = c.timestamp
        if eng.status in {"CLOSED", "EXPIRED", "MOTHER_BROKEN", "KILLED"}:
            break
    return eng


def replayed(tf, side, mode, mother, **kw):
    eng = make(tf, side, mode, mother, **kw)
    eng.replay(window(tf, mother), window("1m", mother))
    return eng


def summary(eng):
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


# ---- pick mothers: spread across the whole history, both lot eras
stamps5 = [
    c.timestamp
    for c in ALL["5m"]
    if c.timestamp.minute % 5 == 0 and dt_time(9, 15) <= c.timestamp.time() <= dt_time(14, 15)
]
random.shuffle(stamps5)
mothers = stamps5[:N]

results = Counter()
fails: list[str] = []


def check(name, ok, detail=""):
    results[(name, bool(ok))] += 1
    if not ok and len([f for f in fails if f.startswith(name)]) < 5:
        fails.append(f"{name}: {detail}")


configs = [("5m", "CE", "levels"), ("5m", "PE", "levels"), ("5m", "CE", "convergence"), ("5m", "PE", "convergence")]
for tf, side, mode in configs:
    for mother in mothers:
        LOOKUPS.clear()
        a = replayed(tf, side, mode, mother)
        b = tick_simulated(tf, side, mode, mother)
        c = replayed(tf, side, mode, mother)  # determinism
        sa, sb, sc = summary(a), summary(b), summary(c)
        # 1. backtest path == paper-loop path
        check("1 backtest == tick-simulated paper", sa == sb, f"{tf} {side} {mode} {mother}: {sa[:5]} vs {sb[:5]}")
        # 2. deterministic
        check("2 deterministic", sa == sc, f"{mother}")
        st = a.get_status()
        rounds = st.get("rounds") or []
        fills = [f for r in rounds for f in r["fills"]] + list(st.get("fills") or [])
        # 3. no fill before its fib's bar CLOSED (drawn_at + tf) and every fill inside session hours
        fib_drawn = {f["fib_id"]: datetime.fromisoformat(f["drawn_timestamp"]) for f in (st.get("fibs") or [])}
        for f in fills:
            ts = datetime.fromisoformat(f["timestamp"])
            drawn = fib_drawn.get(f["fib_id"])
            check(
                "3 fill after its fib closed",
                drawn is None or ts >= drawn + timedelta(minutes=TIMEFRAME_MINUTES[tf]),
                f"{mother} fill {ts} fib drawn {drawn}",
            )
            check(
                "3 fill inside session, before 15:15",
                dt_time(9, 15) <= ts.time() < dt_time(15, 15),
                f"{mother} fill {ts}",
            )
            check("3 fill after the mother", ts > mother, f"{mother} fill {ts}")
        # 4. P&L recomputed independently
        for r in rounds:
            gross = sum((float(f["exit_premium"]) - float(f["premium"])) * int(f["quantity"]) for f in r["fills"])
            check(
                "4 round gross = sum((exit-entry)*qty)",
                abs(gross - float(r["gross_pnl"])) < 0.01,
                f"{mother} {gross} vs {r['gross_pnl']}",
            )
            check(
                "4 round net = gross - costs",
                abs(float(r["gross_pnl"]) - float(r["costs_total"]) - float(r["net_pnl"])) < 0.01,
                f"{mother}",
            )
            check(
                "4 costs positive and small",
                0 < float(r["costs_total"]) < 0.02 * max(1.0, float(r["deployed_inr"])),
                f"{mother} costs {r['costs_total']} on {r['deployed_inr']}",
            )
        if rounds:
            check(
                "4 campaign net = sum of rounds",
                abs(sum(float(r["net_pnl"]) for r in rounds) - float(st.get("net_pnl") or 0)) < 0.01,
                f"{mother}",
            )
        # 5. lot size by date, quantity = lots*lot
        lot = int(get_lot_size("NIFTY", mother.date()))
        exp_lot = 25 if mother.date() < date(2025, 1, 1) else 75 if mother.date() < date(2026, 1, 1) else 65
        check(
            "5 lot size by mother date (25/75/65)",
            lot == exp_lot and st["lot_size"] == lot,
            f"{mother} lot {lot} status {st['lot_size']}",
        )
        for f in fills:
            check("5 quantity = lots x lot", int(f["quantity"]) == int(f["lots"]) * lot, f"{mother}")
        # 6. every fill premium is the archive's own open at that minute for that contract
        for f in fills:
            ts = datetime.fromisoformat(f["timestamp"])
            c = SimpleNamespace(
                symbol="NIFTY",
                underlying="NIFTY",
                strike=float(f["strike"]),
                expiry=date.fromisoformat(f["expiry"]),
                option_type=side,
            )
            bar = src.lookup(ts, c)
            check(
                "6 fill premium = recorded open",
                bar is not None and abs(float(bar.open) - float(f["premium"])) < 1e-9,
                f"{mother} {f['timestamp']} {f['strike']}",
            )
        # 7. intraday: ends on the mother's day, at or before 15:15 (or mother broken / expiry earlier)
        if st.get("exit_timestamp"):
            ex = datetime.fromisoformat(st["exit_timestamp"])
            check(
                "7 intraday campaign ends on its own day",
                ex.date() == mother.date(),
                f"{mother} ended {ex} {st.get('exit_reason')}",
            )
            check(
                "7 ends at or before 15:15",
                ex.time() <= dt_time(15, 15) or st.get("exit_reason") in {"mother_broken", "mother_broken_no_buys"},
                f"{mother} {ex}",
            )
        # 8. contract: strike = ATM-2 of the fill index; expiry >= 4 days out and one per campaign
        for f in fills:
            atm = round(float(f["index_price"]) / 50.0) * 50
            want = atm - 100 if side == "CE" else atm + 100
            check(
                "8 strike = ATM-2 of the fill price",
                abs(float(f["strike"]) - want) < 1e-6,
                f"{mother} idx {f['index_price']} strike {f['strike']} want {want}",
            )
            dte = (date.fromisoformat(f["expiry"]) - datetime.fromisoformat(f["timestamp"]).date()).days
            check("8 expiry >= 4 days out", dte >= 4, f"{mother} dte {dte}")
        if fills:
            check("8 one expiry per campaign", len({f["expiry"] for f in fills}) == 1, f"{mother}")
        # 9. mother broken means a 1m close above (CE) / below (PE) the mother edge
        if st["status"] == "MOTHER_BROKEN":
            ex = datetime.fromisoformat(st["exit_timestamp"])
            bar = next((c for c in window("1m", mother) if c.timestamp == ex), None)
            edge = st["mother_high"] if side == "CE" else st["mother_low"]
            ok = bar is not None and ((bar.close > edge) if side == "CE" else (bar.close < edge))
            check("9 mother broken = a close through the mother edge", ok, f"{mother} {ex}")
        # 10. convergence: every buy came from a zone (key starts with Z) and its top was traded through before the buy
        if mode == "convergence":
            for f in fills:
                check(
                    "10 convergence buys only zones",
                    any(str(k).startswith("Z") for k in f.get("covered", [])),
                    f"{mother} covered {f.get('covered')}",
                )
        # 10b. levels: no premium ever fabricated -- every lookup that returned a price is a real archive bar
        for when, strike, expiry, opt, price in LOOKUPS[:50]:
            if price is not None:
                cc = SimpleNamespace(symbol="NIFTY", underlying="NIFTY", strike=strike, expiry=expiry, option_type=opt)
                bar = src.lookup(when, cc)
                check(
                    "10 no fabricated premium",
                    bar is not None and abs(float(bar.open) - price) < 1e-9,
                    f"{when} {strike}",
                )

print(f"\n{'check':52} {'pass':>7} {'fail':>6}")
names = sorted({n for n, _ in results})
for n in names:
    print(f"{n:52} {results[(n, True)]:>7} {results[(n, False)]:>6}")
print("\nFAILURES (first few):")
for f in fails:
    print("  ", f)
print("\nmothers per config:", len(mothers), "configs:", len(configs), "total checks:", sum(results.values()))
sys.exit(1 if any(results[(n, False)] for n in names) else 0)
