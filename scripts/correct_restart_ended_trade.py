#!/usr/bin/env python3
"""Re-book a paper trade that a RESTART ended, at the exit its rules would have given.

Phil, 2026-08-17. A deploy restarted the app at 09:58 IST while his PE run held a
position. The paper engine force-closed it at the last quote it happened to hold
and booked it `ENGINE_STOP`. His strategy did not end that trade -- the deploy
did -- so the record shows an exit that his rules never produced.

This corrects exactly that: one trade, named by its entry time, whose exit reason
is ENGINE_STOP. It writes the true exit and KEEPS THE ORIGINAL BESIDE IT, so the
record shows both what was booked and why it changed. It never invents a price:
the exit premium is given on the command line, read off the option chart at the
moment the rule fired.

    # look first -- prints the change, writes nothing
    ./venv/bin/python scripts/correct_restart_ended_trade.py \
        --run-id My_First_Run_PE --entry-time 09:20:01 \
        --exit-time 10:45:00 --exit-premium 268.40 \
        --exit-reason CPR_S2_CROSS

    # then write it
    ... --apply

Run it on the host, where the state files live. Nothing here talks to a broker.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_ROOT = os.environ.get("PHILFORGE_USER_DATA_ROOT", "user_data")


def _state_files(root: str, run_id: str) -> list[str]:
    hits = []
    for path in glob.glob(os.path.join(root, "**", "paper_state_*.json"), recursive=True):
        try:
            with open(path, encoding="utf-8") as handle:
                state = json.load(handle)
        except Exception:
            continue
        if not run_id or run_id in os.path.basename(path) or state.get("run_id") == run_id:
            hits.append(path)
    return hits


def _matches(trade: dict, entry_time: str) -> bool:
    stamp = str(trade.get("entry_time") or "")
    return entry_time in stamp and str(trade.get("exit_reason") or "").upper() == "ENGINE_STOP"


def correct(trade: dict, *, exit_time: str, exit_premium: float, reason: str) -> dict:
    """The corrected trade, with the restart's version kept alongside it."""
    entry = float(trade.get("entry_premium") or 0.0)
    qty = int(trade.get("quantity") or trade.get("qty") or 0)
    if not qty:
        lots = int(trade.get("lots") or 0)
        lot_size = int(trade.get("lot_size") or 0)
        qty = lots * lot_size
    direction = 1 if str(trade.get("transaction_type", "BUY")).upper() == "BUY" else -1

    fixed = dict(trade)
    # The restart's version, preserved. A corrected P&L with no trace of what it
    # replaced is indistinguishable from a number somebody made up.
    fixed["restart_exit"] = {
        "exit_time": trade.get("exit_time"),
        "exit_premium": trade.get("exit_premium"),
        "exit_quote_premium": trade.get("exit_quote_premium"),
        "pnl": trade.get("pnl"),
        "exit_reason": trade.get("exit_reason"),
        "corrected_at": datetime.now().isoformat(timespec="seconds"),
        "note": "closed by an app restart, not by the strategy",
    }
    fixed["exit_time"] = exit_time
    fixed["exit_premium"] = float(exit_premium)
    fixed["exit_quote_premium"] = float(exit_premium)
    fixed["exit_reason"] = reason
    fixed["pnl"] = round((float(exit_premium) - entry) * direction * qty, 2)
    fixed["corrected"] = True
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", default="", help="run id, or part of the state filename")
    ap.add_argument("--entry-time", required=True, help="the trade's entry time, e.g. 09:20:01")
    ap.add_argument("--exit-time", required=True, help="when the rule actually fired, e.g. 10:45:00")
    ap.add_argument("--exit-premium", required=True, type=float, help="the option's price at that moment")
    ap.add_argument("--exit-reason", default="EXIT_CORRECTED", help="e.g. CPR_S2_CROSS")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="user_data root")
    ap.add_argument("--apply", action="store_true", help="write it; without this, only print")
    args = ap.parse_args()

    files = _state_files(args.root, args.run_id)
    if not files:
        print(f"No paper state file found under {args.root} for run {args.run_id or '(any)'}")
        return 1

    touched = 0
    for path in files:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        trades = state.get("closed_trades") or []
        for i, trade in enumerate(trades):
            if not _matches(trade, args.entry_time):
                continue
            fixed = correct(
                trade,
                exit_time=args.exit_time,
                exit_premium=args.exit_premium,
                reason=args.exit_reason,
            )
            print(f"\n{os.path.basename(path)}  trade #{i + 1}  {trade.get('symbol', '')}")
            print(f"   was : exit {trade.get('exit_time')}  @ {trade.get('exit_premium')}  P&L {trade.get('pnl')}")
            print(
                f"   now : exit {fixed['exit_time']}  @ {fixed['exit_premium']}  P&L {fixed['pnl']}  ({fixed['exit_reason']})"
            )
            trades[i] = fixed
            touched += 1
        if touched and args.apply:
            shutil.copy2(path, path + ".bak")
            state["closed_trades"] = trades
            # The day's P&L is the sum of its trades; leaving it stale would put
            # a corrected trade under an uncorrected total.
            state["daily_pnl"] = round(sum(float(t.get("pnl") or 0) for t in trades), 2)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2)
            print(f"   written · original kept at {os.path.basename(path)}.bak")

    if not touched:
        print(f"No ENGINE_STOP trade entered at {args.entry_time} was found.")
        return 1
    if not args.apply:
        print("\nNothing was written. Re-run with --apply once the numbers above look right.")
    else:
        print("\nRestart the run (or the app) so the panel reloads the corrected state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
