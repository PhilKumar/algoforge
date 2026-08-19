"""tools/candle_entry_offline/audit.py -- check a sweep run without asking the engine.

Phil, 2026-08-19: "check again at least 5 times whether this is the correct
results without any bugs or corrections later."  So this reads the per-campaign
CSV a sweep wrote and re-derives everything from the raw candle cache and the
raw option archive, with code written from the RULE rather than shared with
TwoRedLadder.  Five independent passes:

  1. MOTHER      -- every mother really does make the high (CE) or the low (PE)
                    of the last N bars on its own chart, at its own bar.
  2. GEOMETRY    -- an independent replay of the two-red rule reproduces every
                    fill: same bar, same price, same rung, same chart.
  3. PRICES      -- every premium is a trade the archive actually recorded, at
                    the minute claimed or inside the documented fallback.
  4. NO PEEKING  -- nothing is stamped before the bar that justified it: fills
                    strictly after their arming bar, the exit strictly after
                    the last fill and on the campaign's own chart.
  5. MONEY       -- gross, costs and net recomputed from the legs alone.

    python3 tools/candle_entry_offline/audit.py run.csv [--bars 278] [--pos 0.25]
                                                        [--side ce] [--depth 3]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools", "fib_offline"))

import sweep as SW  # noqa: E402

from cascade_costs import OptionCostFill, calculate_nifty_option_basket_round_costs  # noqa: E402
from engine.candle_ladder import ladder_from  # noqa: E402
from engine.cascade_options import FixedCampaignOption  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
FAILS: list[str] = []
COUNTS: dict[str, int] = {}


def check(pass_name: str, ok: bool, detail: str) -> None:
    COUNTS[pass_name] = COUNTS.get(pass_name, 0) + 1
    if not ok:
        FAILS.append(f"[{pass_name}] {detail}")


MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 375, "1w": 6135}


def closed_at_local(timeframe: str, opened: datetime) -> datetime:
    """When that bar closed -- written out here rather than imported."""
    span = MINUTES[timeframe]
    closed = opened + timedelta(minutes=span)
    if span < MINUTES["1d"]:
        session_end = opened.replace(hour=15, minute=30, second=0, microsecond=0)
        if closed > session_end >= opened:
            closed = session_end
    return closed


def replay_entries(bars_by_tf, stages, mother, side, box_bars, box_pos, depth, watch_from=None):
    """The two-red rule, written out again from Phil's words.

    Reds (greens on a PE) whose CLOSES step down (up), any number of other
    candles between them; on the second one a stop goes at the FIRST one's
    close; it fills when a LATER bar trades through that price. Then the next
    rung watches the next chart up, and may only count bars that go beyond the
    extreme reached when the last rung filled. Nothing counts while price sits
    outside its half of the box.
    """
    ce = side == "CE"
    fills = []
    rung = 0
    gate = None  # the extreme at the last fill
    extreme = mother.low if ce else mother.high
    reds: list = []
    stop = None
    armed_at = None
    # One stream, ordered the way the engine orders it: by the moment each bar
    # closed, finest chart first.
    stream = []
    for tf in stages:
        for row in bars_by_tf.get(tf, []):
            if row.timestamp <= mother.timestamp or (watch_from is not None and row.timestamp < watch_from):
                continue
            stream.append((closed_at_local(tf, row.timestamp), stages.index(tf), tf, row))
    stream.sort(key=lambda item: (item[0], item[1]))

    box = list(bars_by_tf["__box__"])  # the N bars up to and including the mother
    for _closed, _order, tf, row in stream:
        extreme = min(extreme, row.low) if ce else max(extreme, row.high)
        if rung >= min(depth, len(stages)):
            break
        if tf != stages[rung]:
            if tf == stages[0] and box_bars:
                box.append(row)
                del box[:-box_bars]
            continue
        if stop is not None and armed_at is not None and row.timestamp > armed_at:
            hit = row.high >= stop if ce else row.low <= stop
            if hit:
                fills.append({"t": row.timestamp, "tf": tf, "price": stop, "rung": rung + 1, "closed": _closed})
                gate = extreme
                rung += 1
                reds, stop, armed_at = [], None, None
                if tf == stages[0] and box_bars:
                    box.append(row)
                    del box[:-box_bars]
                continue
        coloured = row.close < row.open if ce else row.close > row.open
        gate_ok = True
        if box_bars:
            if len(box) < box_bars:
                gate_ok = False
            else:
                hi = max(b.high for b in box)
                lo = min(b.low for b in box)
                gate_ok = (row.close <= lo + box_pos * (hi - lo)) if ce else (row.close >= hi - box_pos * (hi - lo))
        beyond = True if gate is None else (row.low < gate if ce else row.high > gate)
        if coloured and gate_ok and beyond:
            stepped = True if not reds else (row.close < reds[-1].close if ce else row.close > reds[-1].close)
            if stepped:
                reds.append(row)
                if len(reds) >= 2:
                    stop = reds[-2].close
                    armed_at = row.timestamp
        if tf == stages[0] and box_bars:
            box.append(row)
            del box[:-box_bars]
    return fills


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--bars", type=int, default=278)
    ap.add_argument("--pos", type=float, default=0.25)
    ap.add_argument("--side", default="ce")
    ap.add_argument("--depth", type=int, default=3)
    args = ap.parse_args()
    side = "PE" if args.side.upper() == "PE" else "CE"

    rows = list(csv.DictReader(open(args.csv)))
    tf = rows[0]["tf"]
    stages = ladder_from(tf, args.depth)
    series = {k: SW.load(k) for k in stages if k in ("1m", "5m", "15m", "1h")}
    base = series[list(series)[0]]
    for slow in [k for k in stages if k in ("1d", "1w")]:
        series[slow] = SW.regroup(base if slow == "1d" else SW.regroup(base, "1d"), slow)
    src = SW._listed_source()
    own = series[tf]
    index_of = {row.timestamp: i for i, row in enumerate(own)}

    for row in rows:
        mother_ts = datetime.fromisoformat(row["mother"])
        legs = json.loads(row["legs"] or "[]")
        exit_detail = json.loads(row["exit_detail"] or "{}")
        mi = index_of[mother_ts]
        mother = own[mi]
        # A retried campaign (Phil's "history mother") watches from `watch_from`
        # and reads the box as it stood THEN; an ordinary one from the mother.
        watch_from = datetime.fromisoformat(row["watch_from"]) if row.get("watch_from") else None
        if watch_from is not None and watch_from <= mother_ts:
            watch_from = None
        starts = watch_from or mother_ts
        si = max(i for i, b in enumerate(own) if b.timestamp <= starts)
        window = own[si - args.bars + 1 : si + 1]

        # 1. MOTHER -- it made the N-bar extreme at ITS OWN bar (its trailing
        # window), and on a retry it is still the extreme of the box as it
        # stood where the watch started (or the retry would have had a newer
        # mother).
        # An ORDINARY mother made a NEW N-bar extreme at its own bar. A RETRY's
        # mother is the extreme of the box as it stood where the watch started
        # -- it need not have been a new high when it printed (an older, higher
        # bar may have rolled out of the window since).
        own_window = own[mi - args.bars + 1 : mi + 1]
        if side == "CE":
            if watch_from is None:
                check(
                    "mother",
                    mother.high >= max(b.high for b in own_window),
                    f"{mother_ts}: not the {args.bars}-bar high",
                )
            check("mother", mother.high >= max(b.high for b in window), f"{mother_ts}: not the box high at {starts}")
        else:
            if watch_from is None:
                check(
                    "mother", mother.low <= min(b.low for b in own_window), f"{mother_ts}: not the {args.bars}-bar low"
                )
            check("mother", mother.low <= min(b.low for b in window), f"{mother_ts}: not the box low at {starts}")

        # 2. GEOMETRY -- independent replay
        expiry = datetime.fromisoformat(row["expiry"] + "T15:30:00+05:30").date()
        batches = {
            k: [c for c in rowset if starts.date() <= c.timestamp.date() <= expiry] for k, rowset in series.items()
        }
        batches["__box__"] = window
        mine = replay_entries(batches, stages, mother, side, args.bars, args.pos, args.depth, watch_from)
        theirs = [
            {"t": datetime.fromisoformat(f["t"]), "tf": f["tf"], "price": f["index"], "rung": f["rung"]} for f in legs
        ]
        # The engine stops buying once it exits, and a rung is only acted on
        # when its bar CLOSES -- an hourly bar opening at 09:15 is not a buy
        # until 10:15, so an exit at 09:20 comes first. Comparing the bar's
        # OPEN here reported a phantom third rung on 2026-04-01.
        if exit_detail.get("timestamp"):
            cutoff = datetime.fromisoformat(exit_detail["timestamp"])
            mine = [f for f in mine if f["closed"] <= cutoff]
        check(
            "geometry",
            len(mine) == len(theirs)
            and [(f["t"], f["tf"], round(f["price"], 2)) for f in mine]
            == [(f["t"], f["tf"], round(f["price"], 2)) for f in theirs],
            f"{mother_ts}: independent replay differs\n  mine   {mine[:4]}\n  theirs {theirs[:4]}",
        )

        # 3. PRICES + 4. NO PEEKING + 5. MONEY
        contract = FixedCampaignOption("NIFTY", int(row["strike"]), expiry, side, int(row["lot"]), "")
        buys = []
        last_fill_ts = None
        for leg in legs:
            # A leg carries its own strike when the rule picks one per buy.
            leg_contract = (
                FixedCampaignOption("NIFTY", int(leg["strike"]), expiry, side, int(row["lot"]), "")
                if leg.get("strike") is not None
                else contract
            )
            priced_at = datetime.fromisoformat(leg["priced_at"]).replace(tzinfo=None)
            found = src.lookup(priced_at, leg_contract)
            if found is None:  # the documented fallback: forward 15, back 30, same day
                for delta in [timedelta(minutes=m) for m in range(1, 16)] + [
                    timedelta(minutes=-m) for m in range(1, 31)
                ]:
                    probe = priced_at + delta
                    if probe.date() != priced_at.date() or not (dt_time(9, 15) <= probe.time() <= dt_time(15, 30)):
                        continue
                    if src.lookup(probe, leg_contract) == leg["premium"]:
                        found = leg["premium"]
                        break
            check(
                "prices",
                leg["premium"] is None or found == leg["premium"],
                f"{mother_ts}: leg at {leg['priced_at']} claims {leg['premium']}, archive says {found}",
            )
            fill_ts = datetime.fromisoformat(leg["t"])
            check("no_peeking", fill_ts > mother_ts, f"{mother_ts}: a leg at {fill_ts} is not after the mother")
            last_fill_ts = fill_ts if last_fill_ts is None else max(last_fill_ts, fill_ts)
            if leg["premium"] is not None:
                buys.append(
                    (int(leg.get("strike") or row["strike"]), OptionCostFill(float(leg["premium"]), int(leg["qty"]), 1))
                )

        if exit_detail.get("timestamp"):
            check(
                "no_peeking",
                exit_detail["timeframe"] == tf,
                f"{mother_ts}: exit read on {exit_detail['timeframe']}, not the campaign's {tf}",
            )
            if last_fill_ts is not None:
                check(
                    "no_peeking",
                    datetime.fromisoformat(exit_detail["timestamp"]) > last_fill_ts,
                    f"{mother_ts}: exit at {exit_detail['timestamp']} is not after the last buy",
                )

        if buys and len(buys) == len(legs) and exit_detail.get("option_premium") and row["net"]:
            # Each contract is sold at ITS OWN recorded price at the exit minute
            # and charged on its own; the engine's per-contract sells are in
            # exit_detail["premiums"] and are re-read from the archive here.
            exit_at = datetime.fromisoformat(exit_detail["priced_at"]).replace(tzinfo=None)
            net = 0.0
            ok = True
            for strike in sorted({k for k, _ in buys}):
                group = [f for k, f in buys if k == strike]
                sell = src.lookup(exit_at, FixedCampaignOption("NIFTY", strike, expiry, side, int(row["lot"]), ""))
                if sell is None:
                    reported = (exit_detail.get("premiums") or {}).get(f"{strike}{side}")
                    sell = float(reported) if reported is not None else None
                if sell is None:
                    ok = False
                    break
                qty = sum(f.quantity for f in group)
                costs = calculate_nifty_option_basket_round_costs(
                    buys=group, sell_price=float(sell), sell_lots=qty // int(row["lot"]), sell_quantity=qty
                )
                net += float(sell) * qty - sum(f.price * f.quantity for f in group) - costs.total
            check(
                "money",
                (not ok) or abs(net - float(row["net"])) < 0.51,
                f"{mother_ts}: recomputed net {net:.2f} vs reported {row['net']}",
            )

    print(f"campaigns: {len(rows)}")
    for name in ("mother", "geometry", "prices", "no_peeking", "money"):
        print(f"  {name:<11} {COUNTS.get(name, 0):>5} checks")
    print(f"failures: {len(FAILS)}")
    for line in FAILS[:15]:
        print("  " + line)


if __name__ == "__main__":
    main()
