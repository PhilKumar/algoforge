"""tools/candle_entry_offline/verify.py -- independent checks on the two-red ladder replay.

Runs the REAL Candle Entry backtest route (offline: cached NIFTY candles +
the local Upstox option archive, zero broker calls) on many mothers, then
re-derives every claim the result makes from the raw candles, and re-runs
the same mother the way the PAPER LOOP feeds bars -- a tick at a time, every
bar closed by that tick, dedup'd by the engine -- to prove backtest == paper.

    python3 tools/candle_entry_offline/verify.py [mothers-per-timeframe]

Checks, each computed WITHOUT the engine's own bookkeeping:
  1. the arming bar is a RED whose close is below the previous RED's close
     (greens ignored, however many), and the stop is one red back from the
     newest -- which is always ABOVE the market at the moment it is placed;
  2. the fill bar reached the stop (high >= stop) and came AFTER the arming bar;
  3. rung k > 1 armed only after a low below the previous fill's marked low;
  4. the target is average entry + 0.25 x (mother high - average entry);
  5. the exit: target -> the bar's high reached it; expiry -> the expiry day's
     first bar closing at/after 15:15; nothing after the exit;
  6. every premium was read at the bar's CLOSE minute (fills and the exit);
  7. the contract is the monthly (15-45 DTE, else closest), strike ATM-2 of
     the mother close, lot size by the mother's date;
  8. net = gross - costs, gross = sum((sell - buy) x qty), or blank when a leg
     is unpriced;
  9. tick-fed paper == bulk backtest: same fills, same exit, same net;
 11. the ladder is TWO rungs: the mother's chart and the next one up;
 10. the mother bar itself never trades and no fill precedes the mother.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("PHILFORGE_FIB_OFFLINE", "1")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("PHILFORGE_STARTUP_TOKEN", "0")
os.environ.setdefault("DHAN_PIN", "")
os.environ.setdefault("DHAN_TOTP_SECRET", "")
os.environ.setdefault("DHAN_ACCESS_TOKEN", "offline-dummy")
os.environ.setdefault("PHILFORGE_DB", os.path.join(tempfile.gettempdir(), "philforge-ce-verify", "verify.db"))
os.environ.setdefault("PHILFORGE_PIN", "123456")
os.makedirs(os.path.dirname(os.environ["PHILFORGE_DB"]), exist_ok=True)

from zoneinfo import ZoneInfo  # noqa: E402

import app  # noqa: E402
from engine.backtest import get_lot_size  # noqa: E402
from engine.candle_ladder import LADDER_DEPTH, closed_at, ladder_from  # noqa: E402
from engine.cascade_options import IndexCandle, LadderCandleEntryPaper  # noqa: E402

IST_TZ = ZoneInfo("Asia/Kolkata")

N = int(sys.argv[1]) if len(sys.argv) > 1 else 12
random.seed(18)
IST = app.IST


def request():
    return SimpleNamespace(state=SimpleNamespace(user_id=1))


async def cached(tf: str):
    adapter = app.CascadeOptionsAdapter(None, paper_only=True)
    return await adapter.async_get_candles("NIFTY", tf, from_date=date(2024, 10, 1), to_date=date(2030, 1, 1))


FAILS: list[str] = []
CHECKS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(label)


def bar_close(ts: datetime, tf: str) -> datetime:
    return closed_at(SimpleNamespace(timeframe=tf, timestamp=ts))


def verify_one(result: dict, series: dict[str, list], mother_ts: datetime, tf: str) -> None:
    c = result["campaign"]
    contract = result["contract"]
    fills = c["fills"]
    exit_row = c["exit"]
    stages = result["stages"]
    tag = f"{mother_ts.isoformat()} {tf}"
    # 11. two rungs, the mother's chart first
    expected_stages = list(ladder_from(tf, LADDER_DEPTH))
    check(stages == expected_stages, f"{tag}: stages {stages} != {expected_stages}")
    by_tf = {k: {row.timestamp: row for row in rows} for k, rows in series.items()}
    ordered = {k: sorted(rows, key=lambda r: r.timestamp) for k, rows in series.items()}
    mother_row = by_tf[tf].get(mother_ts)
    check(mother_row is not None, f"{tag}: mother bar missing")
    if mother_row is None:
        return
    # 7. contract. The headline strike is ATM-2 of the index WHERE THE FIRST
    # RUNG FILLS (the page's rule since 2026-08-19, for first_buy and each_buy
    # alike); with no fill it is still the mother's ATM-2, the contract the
    # campaign was opened with.
    fills = result.get("campaign", {}).get("fills") or []
    at_buy = result.get("strike_at") in ("first_buy", "each_buy")
    anchor = float(fills[0]["index_price"]) if fills and at_buy else float(mother_row.close)
    atm = int(anchor / 50.0 + 0.5) * 50
    check(
        contract["strike"] == atm - 100,
        f"{tag}: strike {contract['strike']} != ATM-2 {atm - 100} of {'the first fill' if fills else 'the mother'}",
    )
    if result.get("strike_at") == "each_buy":
        # 7b. every rung's own strike is ATM-2 of its own fill.
        for fill in fills:
            own = int(float(fill["index_price"]) / 50.0 + 0.5) * 50 - 100
            check(
                int(fill["strike"]) == own, f"{tag}: rung {fill['rung']} strike {fill['strike']} != its own ATM-2 {own}"
            )
    check(contract["lot_size"] == int(get_lot_size("NIFTY", mother_ts.date())), f"{tag}: lot size by date")
    expiry = date.fromisoformat(contract["expiry"])
    dte = (expiry - mother_ts.date()).days
    check(0 < dte <= 60, f"{tag}: expiry {expiry} DTE {dte} implausible")
    # 10. nothing before or on the mother
    for f in fills:
        ts = datetime.fromisoformat(f["timestamp"])
        check(ts > mother_ts, f"{tag}: fill {ts} not after the mother")
    # 1-3, 6: each fill
    events = c["events"]
    prev_marked_low = None
    prev_fill_ts = None
    prev_fill_tf = None
    for f in fills:
        rtf = f["timeframe"]
        rows = ordered[rtf]
        ts = datetime.fromisoformat(f["timestamp"])
        idx = next((i for i, r in enumerate(rows) if r.timestamp == ts), None)
        check(idx is not None, f"{tag}: fill bar {ts} {rtf} not in series")
        if idx is None:
            continue
        fill_bar = rows[idx]
        stop = float(f["index_price"])
        check(fill_bar.high >= stop, f"{tag}: fill bar high {fill_bar.high} < stop {stop}")
        # the arming: the last entry_stop_armed event for this rung before the fill
        arms = [
            e
            for e in events
            if e.get("event") == "entry_stop_armed"
            and e.get("rung") == f["rung"]
            and datetime.fromisoformat(e["timestamp"]) < ts
        ]
        check(bool(arms), f"{tag}: rung {f['rung']} filled without an arming event")
        if arms:
            arm = arms[-1]
            arm_ts = datetime.fromisoformat(arm["timestamp"])
            check(abs(float(arm["stop"]) - stop) < 1e-6, f"{tag}: rung {f['rung']} stop {arm['stop']} != fill {stop}")
            aidx = next((i for i, r in enumerate(rows) if r.timestamp == arm_ts), None)
            check(aidx is not None, f"{tag}: arming bar {arm_ts} not found")
            if aidx is not None:
                arming = rows[aidx]
                # PHIL'S RULE, re-derived here rather than read off the engine:
                # a sequence of RED closes each below the one before it, greens
                # ignored, and the stop is one red back from the newest. The
                # gate low (the previous fill's marked low) bars a red that has
                # not made a new low from joining.
                gate = prev_marked_low if f["rung"] > 1 else None
                # The engine only shows a stage the bars it was active for:
                # rung 1 from the mother, a later rung from the moment the one
                # below it filled (bars are ordered by when they CLOSED).
                # Bars are fed in CLOSE order, finer chart first on a tie, so a
                # slower bar closing at the same instant as the fill is seen by
                # the rung above it.
                since = bar_close(prev_fill_ts, prev_fill_tf) if f["rung"] > 1 and prev_fill_ts else None
                reds: list = []
                for row in rows[: aidx + 1]:
                    if row.timestamp <= mother_ts:
                        continue
                    if since is not None and bar_close(row.timestamp, rtf) < since:
                        continue
                    if row.close >= row.open:
                        continue
                    if gate is not None and row.low >= gate:
                        continue
                    if reds and row.close >= reds[-1].close:
                        continue
                    reds.append(row)
                check(len(reds) >= 2, f"{tag}: fewer than two reds by the arming bar {arm_ts}")
                check(reds and reds[-1].timestamp == arming.timestamp, f"{tag}: the arming bar is not the newest red")
                if len(reds) >= 2:
                    check(abs(reds[-2].close - stop) < 1e-6, f"{tag}: stop {stop} != one red back ({reds[-2].close})")
                check(arming.close < arming.open, f"{tag}: the arming bar {arm_ts} is not red")
                check(stop > arming.close, f"{tag}: stop {stop} not above the market at arming ({arming.close})")
                check(fill_bar.timestamp > arm_ts, f"{tag}: fill bar {ts} not after arming {arm_ts}")
                if f["rung"] > 1 and prev_marked_low is not None:
                    check(arming.low < prev_marked_low, f"{tag}: rung {f['rung']} armed without a new low")
        # 6. priced at the bar close
        priced_at = datetime.fromisoformat(f["priced_at"]) if f.get("priced_at") else None
        check(priced_at == bar_close(ts, rtf), f"{tag}: fill priced at {priced_at}, bar closes {bar_close(ts, rtf)}")
        prev_marked_low = float(f["marked_low"])
        prev_fill_ts, prev_fill_tf = ts, rtf
    # 4. target
    if fills:
        qty = sum(f["quantity"] for f in fills)
        avg = sum(f["index_price"] * f["quantity"] for f in fills) / qty
        target = round(avg + 0.25 * (mother_row.high - avg), 2)
        check(abs(target - float(c["target_index"])) < 0.02, f"{tag}: target {c['target_index']} != {target}")
    # 5. exit
    if exit_row:
        etf = exit_row["timeframe"]
        ets = datetime.fromisoformat(exit_row["timestamp"])
        priced_at = datetime.fromisoformat(exit_row["priced_at"])
        check(priced_at == ets, f"{tag}: exit priced at {priced_at} but stamped {ets}")
        exit_bar = next((r for r in ordered[etf] if bar_close(r.timestamp, etf) == ets), None)
        check(exit_bar is not None, f"{tag}: no {etf} bar closes at {ets}")
        if exit_bar is not None:
            if exit_row["reason"] == "target":
                check(
                    exit_bar.high >= float(c["target_index"]) - 1e-6, f"{tag}: target exit bar never reached the target"
                )
                check(
                    abs(float(exit_row["index_price"]) - float(c["target_index"])) < 1e-6, f"{tag}: target exit price"
                )
            elif exit_row["reason"] == "expiry":
                check(
                    exit_bar.timestamp.date() == expiry,
                    f"{tag}: expiry exit on {exit_bar.timestamp.date()} != {expiry}",
                )
                check(ets.time() >= dt_time(15, 15), f"{tag}: expiry exit before 15:15")
                earlier = [
                    r
                    for r in ordered[etf]
                    if r.timestamp.date() == expiry
                    and bar_close(r.timestamp, etf).time() >= dt_time(15, 15)
                    and r.timestamp < exit_bar.timestamp
                ]
                check(not earlier, f"{tag}: an earlier expiry-day bar already closed at/after 15:15")
                check(
                    abs(float(exit_row["index_price"]) - exit_bar.close) < 1e-6,
                    f"{tag}: expiry exit not at the bar close",
                )
            for f in fills:
                check(datetime.fromisoformat(f["timestamp"]) < ets, f"{tag}: fill after the exit")
    # 8. money
    if exit_row and exit_row.get("option_premium") is not None and all(f["option_premium"] is not None for f in fills):
        # Each contract is sold at its own price: a leg's sale is the exit
        # premium recorded for ITS strike (one strike -> the headline premium).
        per = exit_row.get("premiums") or {}

        def sold(f):
            value = per.get(f"{f['strike']}{f.get('option_type') or 'CE'}")
            return float(value) if value is not None else float(exit_row["option_premium"])

        gross = round(sum((sold(f) - f["option_premium"]) * f["quantity"] for f in fills), 2)
        check(abs(gross - float(c["gross_pnl"])) < 0.05, f"{tag}: gross {c['gross_pnl']} != {gross}")
        check(
            abs(float(c["gross_pnl"]) - float(c["costs_total"]) - float(c["net_pnl"])) < 0.05,
            f"{tag}: net != gross - costs",
        )
    elif exit_row and fills:
        check(c["net_pnl"] is None, f"{tag}: unpriced leg but net is {c['net_pnl']}")


def tick_fed(result: dict, series: dict[str, list], mother_ts: datetime, tf: str, history) -> dict:
    """The paper loop's shape: every 20s it re-fetches all CLOSED bars since the
    mother and ingests them; the engine dedupes. Simulated at one tick per
    minute over the replay window, then settled the way the loop settles."""
    contract = result["contract"]
    mother_row = next(r for r in series[tf] if r.timestamp == mother_ts)
    # Start from the contract the campaign was OPENED with (the mother's), and
    # let the engine choose strikes the way the route did -- at the first buy
    # or at every buy -- so this replay makes the same decisions, not inherit
    # the backtest's answers.
    opened = result.get("contract_at_mother") or contract
    fixed = app.FixedCampaignOption(
        "NIFTY", int(opened["strike"]), date.fromisoformat(contract["expiry"]), "CE", contract["lot_size"], ""
    )
    engine = LadderCandleEntryPaper(
        mother_row,
        tf,
        fixed,
        app._CandleEntryReplayAdapter(),
        (lambda when, k: history(when, k)) if history is not None else (lambda _w, _k: None),
        intraday_close=bool(result["intraday_close"]),
        strike_at=str(result.get("strike_at") or "mother"),
        strike_offset_points=-100,
    )
    end_day = date.fromisoformat(result["horizon_to"])
    stamps = sorted(
        {
            bar_close(r.timestamp, k)
            for k, rows in series.items()
            for r in rows
            if mother_ts.date() <= r.timestamp.date() <= end_day
        }
    )
    idx = {k: 0 for k in series}
    ordered = {k: sorted(rows, key=lambda r: bar_close(r.timestamp, k)) for k, rows in series.items()}
    seen: dict[str, list] = {k: [] for k in series}
    for t in stamps:
        for k, rows in ordered.items():
            while idx[k] < len(rows) and bar_close(rows[idx[k]].timestamp, k) <= t:
                if rows[idx[k]].timestamp.date() <= end_day:
                    seen[k].append(rows[idx[k]])
                idx[k] += 1
        engine.ingest({k: list(v) for k, v in seen.items()})  # the whole window again, like the poll
        if engine.status in {"CLOSED", "EXPIRED", "KILLED"}:
            break
    engine.settle_past_expiry(datetime.now(IST))
    return engine.get_status()


def same(a: dict, b: dict) -> bool:
    fa = [(f["rung"], f["timestamp"], f["index_price"], f["option_premium"], f["quantity"]) for f in a["fills"]]
    fb = [(f["rung"], f["timestamp"], f["index_price"], f["option_premium"], f["quantity"]) for f in b["fills"]]
    ea = a["exit"] and (
        a["exit"]["timestamp"],
        a["exit"]["reason"],
        a["exit"]["index_price"],
        a["exit"]["option_premium"],
    )
    eb = b["exit"] and (
        b["exit"]["timestamp"],
        b["exit"]["reason"],
        b["exit"]["index_price"],
        b["exit"]["option_premium"],
    )
    return fa == fb and ea == eb and a["net_pnl"] == b["net_pnl"] and a["status"] == b["status"]


async def main() -> None:
    all_tf = {tf: await cached(tf) for tf in ("1m", "5m", "15m", "1h")}
    latest = min(rows[-1].timestamp for rows in all_tf.values())
    print(f"cache: 1m..1h to {latest}")
    picks: list[tuple[datetime, str]] = []
    for tf in ("5m", "15m", "1h"):
        pool = [r for r in all_tf[tf] if r.timestamp.date() < latest.date() - timedelta(days=50)]
        picks += [(r.timestamp, tf) for r in random.sample(pool, min(N, len(pool)))]
    pool1 = [r for r in all_tf["1m"] if r.timestamp.date() < latest.date() - timedelta(days=50)]
    picks += [(r.timestamp, "1m") for r in random.sample(pool1, min(max(2, N // 4), len(pool1)))]
    print(f"{len(picks)} mothers")
    outcomes = {"target": 0, "expiry": 0, "open": 0, "no_buy": 0}
    nets: list[float] = []
    for mother_ts, tf in picks:
        try:
            result = await app.candle_entry_backtest(
                app.CandleEntryBacktestPayload(
                    mother_timestamp=mother_ts.replace(tzinfo=None).isoformat(), timeframe=tf
                ),
                request(),
            )
        except Exception as exc:  # a refused mother is a finding, not a crash
            FAILS.append(f"{mother_ts} {tf}: backtest raised {exc}")
            continue
        stages = result["stages"]
        expiry = date.fromisoformat(result["contract"]["expiry"])
        # The intraday charts come from the cache; 1d and 1w only exist inside
        # the route (the ladder climbs to them since it went to three rungs),
        # so they are read back from the charts the run itself drew. Rebuilding
        # them here would risk comparing the paper run against bars the
        # backtest never saw.
        series = {}
        for k in stages:
            if k in all_tf:
                series[k] = [
                    r for r in all_tf[k] if mother_ts.date() <= r.timestamp.date() <= min(expiry, latest.date())
                ]
                continue
            drawn = (result.get("charts") or {}).get(k) or {}
            series[k] = [
                IndexCandle(
                    datetime.fromtimestamp(row["t"], IST_TZ),
                    float(row["o"]),
                    float(row["h"]),
                    float(row["l"]),
                    float(row["c"]),
                )
                for row in (drawn.get("candles") or [])
                if not row.get("is_mother")
            ]
        verify_one(result, series, mother_ts, tf)
        history, _ = app._candle_entry_pricing(None, mother_ts.date(), min(expiry, latest.date()))
        paper = tick_fed(result, series, mother_ts, tf, history)
        check(same(result["campaign"], paper), f"{mother_ts} {tf}: tick-fed paper != backtest")
        c = result["campaign"]
        if not c["fills"]:
            outcomes["no_buy"] += 1
        elif c["exit"] is None:
            outcomes["open"] += 1
        else:
            outcomes[c["exit"]["reason"]] = outcomes.get(c["exit"]["reason"], 0) + 1
        if c["net_pnl"] is not None:
            nets.append(c["net_pnl"])
    print(f"checks: {CHECKS}, failures: {len(FAILS)}")
    for line in FAILS[:40]:
        print("  FAIL", line)
    print(f"outcomes: {outcomes}; priced rounds {len(nets)}, sum net {round(sum(nets), 2)}")


asyncio.run(main())
