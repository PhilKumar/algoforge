#!/usr/bin/env python3
"""Prove the two Dhan Super Order behaviours the live engines depend on.

Every strategy's live path now sends its entry as a Super Order with the stop
attached, and it does that with **targetPrice 0** -- because at entry there is
no honest target to name yet -- then amends TARGET_LEG once one can be
measured. Both of those are ASSUMPTIONS. They are written that way in the
engines, they are covered by tests against a fake broker, and neither has ever
been put to the real API.

This puts them to the real API, once, with one lot, and nothing else running.

    Q1  Does Dhan accept a Super Order whose target leg is 0?
    Q2  Can TARGET_LEG be amended after the entry has filled?

It is deliberately not part of the app: no strategy is armed, no gate is
opened, no engine is started. It buys one lot, asks the two questions, and
gives you back the exact words Dhan used.

USAGE
    python3 tools/dhan_super_order_probe.py                  # dry run, sends nothing
    python3 tools/dhan_super_order_probe.py --i-mean-it      # places ONE real order
    python3 tools/dhan_super_order_probe.py --i-mean-it --close-only ORDER_ID

WHAT IT COSTS
    One lot of an ATM-2 NIFTY call. The lot is read from the live chain, not
    assumed -- at a 65 lot and a Rs 250 premium that is about Rs 16,000 of
    premium, at risk for the couple of minutes the probe holds it.

BEFORE YOU RUN IT
  * Stop PhilForge locally if it is running. A local Dhan token and the app
    fight over the same session and the token dies in two or three minutes.
  * Run it inside market hours. Outside them the order is rejected and you
    learn nothing about the two questions.
  * It exits the position itself at the end. If anything goes wrong in the
    middle, the last thing it prints is the order id and the exact command to
    close it by hand. Read that line before walking away.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from broker.dhan import DhanClient, ScripMaster  # noqa: E402
from engine.backtest import get_option_contract_lot_size  # noqa: E402

IST_NOW = datetime.now


def say(step: str, detail: object = "") -> None:
    print(f"\n=== {step} ===")
    if detail != "":
        print(detail if isinstance(detail, str) else json.dumps(detail, indent=2, default=str))


def pick_contract(broker: DhanClient, *, itm_steps: int = 2) -> dict:
    """The contract this probe will buy: ATM-2 CE on the nearest weekly.

    Everything here is READ, never assumed -- the strike step, the expiry and
    the lot all come from what Dhan is listing right now. Quoting a number
    from memory is how you end up sizing a trade wrong.
    """
    expiries = [e for e in ScripMaster.get_expiries("NIFTY") if str(e) >= date.today().isoformat()]
    if not expiries:
        raise SystemExit("Dhan is listing no NIFTY expiries; is the scrip master loaded?")
    expiry = sorted(expiries)[0]
    # NIFTY 50's index security id is 13 on IDX_I -- the same one
    # `get_nifty_intraday` uses, so this reads the level the engines read.
    try:
        quote = broker.get_ltp(["13"], "IDX_I") or {}
    except Exception as exc:
        # By far the most likely first failure, and it says nothing useful on
        # its own. A Dhan session is single-active: the running app holds it,
        # and a local token dies two or three minutes after the app takes it.
        raise SystemExit(
            f"Could not read the NIFTY spot: {exc}\n\n"
            "This is almost always the token, not the probe. Only ONE Dhan session is\n"
            "active at a time -- stop PhilForge locally (ports 8000/8001), refresh the\n"
            "token, and run this again. Nothing was sent."
        ) from exc
    spot = 0.0
    for value in (quote or {}).values():
        try:
            spot = float(value if not isinstance(value, dict) else value.get("last_price") or 0)
        except (TypeError, ValueError):
            spot = 0.0
        if spot:
            break
    if not spot:
        raise SystemExit("Could not read the NIFTY spot; refusing to choose a strike blind.")
    step = 50.0
    atm = round(spot / step) * step
    strike = int(atm - itm_steps * step)  # in the money for a CE
    lot = int(get_option_contract_lot_size("NIFTY", date.fromisoformat(str(expiry))))
    security_id = ScripMaster.lookup("NIFTY", strike, str(expiry), "CE")
    if not security_id:
        raise SystemExit(f"Dhan does not list NIFTY {strike}CE {expiry}")
    return {"strike": strike, "expiry": str(expiry), "lot": lot, "spot": spot, "security_id": security_id}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--i-mean-it", action="store_true", help="actually place the order (default: dry run)")
    ap.add_argument("--itm-steps", type=int, default=2)
    ap.add_argument("--stop-pct", type=float, default=0.70, help="stop this far under the entry, as the engines do")
    ap.add_argument("--close-only", metavar="ORDER_ID", help="skip the probe; just close this super order")
    args = ap.parse_args()

    broker = DhanClient()
    ScripMaster.ensure_loaded()

    if args.close_only:
        say("CLEANUP", f"cancelling super order {args.close_only}")
        print(json.dumps(broker.cancel_super_order(args.close_only, "ENTRY_LEG"), indent=2, default=str))
        print("\nCheck your Dhan positions: if a leg is still held, sell it there.")
        return 0

    contract = pick_contract(broker, itm_steps=args.itm_steps)
    say("CONTRACT", contract)
    premium = 0.0
    quote = broker.get_ltp([contract["security_id"]], "NSE_FNO") or {}
    for value in quote.values():
        try:
            premium = float(value if not isinstance(value, dict) else value.get("last_price") or 0)
        except (TypeError, ValueError):
            premium = 0.0
        if premium:
            break
    stop = round(max(0.05, premium * (1.0 - args.stop_pct)), 2)
    say(
        "WHAT THIS WILL DO",
        f"BUY 1 lot ({contract['lot']} qty) NIFTY {contract['strike']}CE {contract['expiry']}\n"
        f"  last traded premium : Rs {premium:,.2f}\n"
        f"  premium at risk     : Rs {premium * contract['lot']:,.0f}\n"
        f"  stop leg            : Rs {stop:,.2f}  ({args.stop_pct:.0%} under the entry)\n"
        f"  target leg          : 0   <-- QUESTION 1 is whether Dhan accepts this",
    )

    if not args.i_mean_it:
        say("DRY RUN", "Nothing was sent. Re-run with --i-mean-it to place it for real.")
        return 0

    if input("\nType YES to place this order: ").strip() != "YES":
        print("Nothing sent.")
        return 1

    # ── Q1: a super order with no target leg ─────────────────────────────
    say("Q1 -- placing the super order with targetPrice 0")
    try:
        placed = broker.place_super_order(
            underlying="NIFTY",
            strike_price=contract["strike"],
            option_type="CE",
            expiry=contract["expiry"],
            transaction_type="BUY",
            quantity=contract["lot"],
            target_price=0.0,
            stop_loss_price=stop,
            order_type="MARKET",
            product_type="MARGIN",
            tag="PF_PROBE",
        )
    except Exception as exc:
        say("Q1 ANSWER: NO", f"Dhan refused a zero target leg:\n  {exc}\n\nNothing is working. Nothing to clean up.")
        say("WHAT THIS MEANS", "The engines must name a target at entry, or place the stop as a separate order.")
        return 2
    say("Q1 ANSWER: YES -- Dhan accepted it", placed)
    order_id = str((placed or {}).get("orderId") or "")
    print(f"\n!! LIVE ORDER {order_id} -- if this probe dies, close it with:")
    print(f"   python3 tools/dhan_super_order_probe.py --i-mean-it --close-only {order_id}")

    say("waiting for the entry to fill")
    print(json.dumps(broker.verify_order_fill(order_id, max_wait_sec=30), indent=2, default=str))

    # ── Q2: amend the target leg after the entry filled ──────────────────
    target = round(premium * 1.5, 2) if premium else 500.0
    say("Q2 -- amending TARGET_LEG", f"asking for a target of Rs {target:,.2f}")
    try:
        amended = broker.modify_super_order(order_id, "TARGET_LEG", target_price=target)
        say("Q2 ANSWER: YES -- the target leg can be set after the fill", amended)
    except Exception as exc:
        say("Q2 ANSWER: NO", f"Dhan refused the amend:\n  {exc}")
        say("WHAT THIS MEANS", "A bracketed entry can never get a profit exit; the engines must rest one separately.")

    say("the super order as Dhan now sees it")
    try:
        print(
            json.dumps(
                [o for o in broker.get_super_orders() if str(o.get("orderId")) == order_id], indent=2, default=str
            )
        )
    except Exception as exc:
        print(f"could not read the super order book: {exc}")

    # ── cleanup ──────────────────────────────────────────────────────────
    say("CLEANUP -- cancelling the super order and selling the leg")
    try:
        print(json.dumps(broker.cancel_super_order(order_id, "ENTRY_LEG"), indent=2, default=str))
    except Exception as exc:
        print(f"cancel failed: {exc}")
    try:
        sold = broker.place_option_order(
            underlying="NIFTY",
            strike_price=float(contract["strike"]),
            option_type="CE",
            expiry=contract["expiry"],
            transaction_type="SELL",
            quantity=contract["lot"],
            product_type="MARGIN",
            tag="PF_PROBE_EXIT",
        )
        print(json.dumps(sold, indent=2, default=str))
        print(json.dumps(broker.verify_order_fill(str(sold.get("orderId")), max_wait_sec=30), indent=2, default=str))
    except Exception as exc:
        print(f"\n!! EXIT FAILED: {exc}")
        print("!! CHECK YOUR DHAN POSITIONS AND CLOSE THE LEG BY HAND.")
        return 3

    say("DONE", "Both questions answered above. Nothing should be left open -- confirm in Dhan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
