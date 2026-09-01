#!/usr/bin/env python3
"""Put the engines' own live order path to the real Dhan API, once, at one lot.

Every strategy's live entry is a Super Order with the stop attached, a
PLACEHOLDER target at 10x the premium, and the real target amended in later.
Around that sit two readings that a fake broker cannot check: which order book
carries a bracketed fill, and which legs to cancel when releasing it.

This script drives `OptionsLiveExecutor` -- the same object a live strategy
uses -- so what gets tested is the engine's code and not a copy of it. The
first version called the broker directly. It answered what Dhan does, and both
bugs it found lived in what the ENGINE did with those answers, which it never
touched.

    Q1  Does Dhan accept the shape the engines send -- a Super Order with a
        PLACEHOLDER target at 10x the entry premium?
    Q2  Can TARGET_LEG be amended afterwards, so the placeholder can be
        replaced by a target the rules actually chose?
    Q3  Does the executor come back with the REAL fill price? The super order
        book reports averageTradedPrice 0.0, so this only passes if it falls
        through to the ordinary book.
    Q4  Does releasing the bracket leave NOTHING working at Dhan? The first
        run of this probe left both legs PENDING over a flat position.

    Everything runs through OptionsLiveExecutor -- the same object a live
    strategy uses. An earlier version called the broker directly, which
    answered what Dhan does but never exercised what the engine does with the
    answer, and both bugs it found lived in exactly that gap.

It is deliberately not part of the app: no strategy is armed, no gate is
opened, no engine is started. It buys one lot, asks the two questions, and
gives you back the exact words Dhan used.

USAGE
    python3 tools/dhan_super_order_probe.py                  # dry run, sends nothing
    python3 tools/dhan_super_order_probe.py --i-mean-it      # places ONE real order
    python3 tools/dhan_super_order_probe.py --i-mean-it --close-only ORDER_ID

WHAT IT COSTS
    One lot, of a contract you choose with --itm-steps. The lot size, strike
    and expiry are READ from the live chain, never assumed. The dry run prints
    the exact rupees before anything is sent. On 01-Sep a 65 lot at Rs 36.65
    cost about Rs 60 all in -- spread plus charges.

BEFORE YOU RUN IT
  * Stop PhilForge locally if it is running. A local Dhan token and the app
    fight over the same session and the token dies in two or three minutes.
  * Run it inside market hours. Outside them the order is rejected and you
    learn nothing about the two questions.
  * It exits the position itself at the end, and then RE-READS the book to
    prove nothing is left working. If anything goes wrong in the middle, the
    last thing it prints is the order id and the exact command to close it.
    Read that line before walking away.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import engine.options_live_executor as executor_module  # noqa: E402
from broker.dhan import DhanClient, ScripMaster  # noqa: E402
from engine.backtest import get_option_contract_lot_size  # noqa: E402
from engine.options_live_executor import OptionsLiveExecutor, OrderRejected  # noqa: E402

IST_NOW = datetime.now


def first_price(payload) -> float:
    """Dig the first last_price out of a market-feed payload.

    Dhan nests these by SEGMENT and then by security id --
    {"IDX_I": {"13": {"last_price": 24500}}} -- so reading one level down
    finds a dict where a number was expected and quietly reports no price.
    The app walks the same payload recursively for the same reason.
    """
    if isinstance(payload, dict):
        for key in ("last_price", "ltp", "LTP"):
            if key in payload:
                try:
                    value = float(payload[key])
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
        for value in payload.values():
            found = first_price(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = first_price(value)
            if found:
                return found
    return 0.0


def say(step: str, detail: object = "") -> None:
    print(f"\n=== {step} ===")
    if detail != "":
        print(detail if isinstance(detail, str) else json.dumps(detail, indent=2, default=str))


def pick_contract(broker: DhanClient, *, itm_steps: int = 2, min_dte: int = 3) -> dict:
    """The contract this probe will buy.

    Everything here is READ, never assumed -- the strike step, the expiry and
    the lot all come from what Dhan is listing right now. Quoting a number
    from memory is how you end up sizing a trade wrong.

    NOT the nearest expiry. On its own expiry day the near contract is a
    lottery ticket with hours to live: a strike a few steps out prices at a
    rupee or two, its stop lands at the tick floor, and an order Dhan refuses
    for THAT reason would read as an answer to the questions this probe is
    asking. The strategies themselves require days to expiry; so does this.
    """
    today = date.today()
    expiries = sorted(
        e for e in ScripMaster.get_expiries("NIFTY") if (date.fromisoformat(str(e)) - today).days >= int(min_dte)
    )
    if not expiries:
        raise SystemExit(
            f"Dhan lists no NIFTY expiry at least {min_dte} days out. " "Lower --min-dte only if you know why."
        )
    expiry = expiries[0]
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
    spot = first_price(quote)
    if not spot:
        raise SystemExit(
            "Could not read the NIFTY spot; refusing to choose a strike blind.\n"
            "The index is only quoted during market hours -- if it is before 09:15 or\n"
            "after 15:30, that is all this is. Nothing was sent."
        )
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
    ap.add_argument(
        "--itm-steps",
        type=int,
        default=2,
        help="strikes IN the money (negative walks it out). Far OTM is cheap and a poor test.",
    )
    ap.add_argument("--min-dte", type=int, default=3, help="skip expiries closer than this many days")
    ap.add_argument(
        "--min-premium",
        type=float,
        default=15.0,
        help="refuse a contract cheaper than this; its stop would sit at the tick floor",
    )
    ap.add_argument("--stop-pct", type=float, default=0.70, help="stop this far under the entry, as the engines do")
    ap.add_argument("--close-only", metavar="ORDER_ID", help="skip the probe; just close this super order")
    args = ap.parse_args()

    broker = DhanClient()
    ScripMaster.ensure_loaded()

    if args.close_only:
        # THE EXIT LEGS. This is the path someone reaches for when a run has
        # already gone wrong, so it must not repeat the mistake that made it
        # go wrong: cancelling ENTRY_LEG after the entry traded returns DH-906
        # and leaves the target and stop working.
        say("CLEANUP", f"releasing the exit legs of super order {args.close_only}")
        for leg_name in ("TARGET_LEG", "STOP_LOSS_LEG"):
            try:
                print(leg_name, json.dumps(broker.cancel_super_order(args.close_only, leg_name), default=str))
            except Exception as exc:
                print(f"{leg_name} FAILED: {exc}")
        left = []
        try:
            for row in broker.get_super_orders() or []:
                if str(row.get("orderId") or "") != str(args.close_only):
                    continue
                left = [
                    str(leg.get("legName"))
                    for leg in row.get("legDetails") or []
                    if str(leg.get("orderStatus") or "").upper() == "PENDING"
                ]
        except Exception as exc:
            print(f"could not re-read the book: {exc}")
            left = ["UNKNOWN"]
        print(f"\nstill working: {left or 'nothing'}")
        print("Check your Dhan positions: if the leg is still HELD, sell it there.")
        return 0

    contract = pick_contract(broker, itm_steps=args.itm_steps, min_dte=args.min_dte)
    say("CONTRACT", contract)
    premium = first_price(broker.get_ltp([contract["security_id"]], "NSE_FNO") or {})
    if premium <= 0:
        raise SystemExit(
            f"No quote for NIFTY {contract['strike']}CE {contract['expiry']}.\n"
            "Outside market hours nothing is quoted, and a strike this far out can be\n"
            "untraded even in session -- try fewer --itm-steps. Nothing was sent."
        )
    if premium < float(args.min_premium):
        # A contract this cheap cannot answer the questions. Its stop lands at
        # or under the tick floor, and a refusal for THAT would read as an
        # answer about the order shape, which is the one thing this must not
        # get wrong.
        raise SystemExit(
            f"NIFTY {contract['strike']}CE {contract['expiry']} is quoting Rs {premium:.2f}, under the "
            f"Rs {args.min_premium:.2f} floor.\n"
            f"Its stop would sit at Rs {max(0.05, round(premium * 0.30, 2)):.2f} -- at the tick floor, where a "
            "rejection would say nothing\nabout the order shape this probe exists to test.\n\n"
            "Move the strike closer to the money (spot is "
            f"{contract['spot']:,.0f}, this strike is {contract['strike']:,}) with fewer --itm-steps,\n"
            "or lower --min-premium if you have a reason. Nothing was sent."
        )
    stop = round(max(0.05, premium * (1.0 - args.stop_pct)), 2)
    dte = (date.fromisoformat(contract["expiry"]) - date.today()).days
    say(
        "WHAT THIS WILL DO",
        f"BUY 1 lot ({contract['lot']} qty) NIFTY {contract['strike']}CE {contract['expiry']} ({dte}d to expiry)\n"
        f"  last traded premium : Rs {premium:,.2f}\n"
        f"  premium at risk     : Rs {premium * contract['lot']:,.0f}\n"
        f"  stop leg            : Rs {stop:,.2f}  ({args.stop_pct:.0%} under the entry)\n"
        f"  target leg          : Rs {premium * 10:,.2f}   (placeholder, 10x entry)",
    )

    if not args.i_mean_it:
        say("DRY RUN", "Nothing was sent. Re-run with --i-mean-it to place it for real.")
        return 0

    if input("\nType YES to place this order: ").strip() != "YES":
        print("Nothing sent.")
        return 1

    # THE ENGINE'S OWN CODE, not a copy of it. The first run of this probe
    # called broker.* directly, which answered what DHAN does but never
    # exercised what the executor does with those answers -- and both bugs it
    # found lived in the executor. So everything below goes through the same
    # object a live strategy uses.
    #
    # The gate is opened for THIS PROCESS ONLY, deliberately, on the line
    # below. Nothing is persisted and no strategy is armed; when this script
    # exits, live execution is closed again exactly as it was.
    executor_module.OPTIONS_LIVE_EXECUTION_ENABLED = True
    executor = OptionsLiveExecutor(broker, "NIFTY", armed=True, product_type="MARGIN", tag="PF_PROBE")

    # ── Q1: does the shape the engines send get accepted? ────────────────
    say("Q1 -- executor.buy() places the bracketed entry")
    try:
        receipt = executor.buy(
            when=datetime.now(),
            strike=contract["strike"],
            expiry=contract["expiry"],
            option_type="CE",
            quantity=contract["lot"],
            premium=premium,
            stop_price=stop,
        )
    except OrderRejected as exc:
        say("Q1 ANSWER: NO -- Dhan refused it", f"{exc}\n\nNothing is working. Nothing to clean up.")
        return 2
    except Exception as exc:
        say(
            "Q1 UNRESOLVED",
            f"{exc}\n\n!! An order MAY be working at Dhan. Check the order book before doing anything else.",
        )
        return 4
    say("Q1 ANSWER: YES -- accepted, and the executor resolved it", receipt)
    order_id = str(receipt.get("bracket_order_id") or receipt.get("order_id") or "")
    print(f"\n!! LIVE ORDER {order_id} -- if this probe dies, close it with:")
    print(f"   ./venv/bin/python3 tools/dhan_super_order_probe.py --i-mean-it --close-only {order_id}")

    # ── Q3: did the executor get the REAL fill price? ────────────────────
    # The super order book reports averageTradedPrice 0.0 (proven 01-Sep), so
    # a traded_premium here means the executor fell through to the ordinary
    # book and read the true price. If it comes back None, the engine would
    # book the quote instead of the fill.
    traded = receipt.get("traded_premium")
    if traded:
        say(
            "Q3 ANSWER: YES -- the executor read the real fill",
            f"quoted Rs {premium:,.2f} -> filled Rs {float(traded):,.2f} "
            f"(slippage Rs {float(traded) - premium:+,.2f} per unit)",
        )
    else:
        say("Q3 ANSWER: NO", "the executor could not price the fill; the engine would book the QUOTE. Tell Claude.")

    # ── Q2: can the placeholder target be replaced with a real one? ──────
    target = round(premium * 1.5, 2)
    say("Q2 -- executor.amend_bracket_target()", f"asking for a target of Rs {target:,.2f}")
    try:
        say("Q2 ANSWER: YES", executor.amend_bracket_target(order_id=order_id, price=target))
    except Exception as exc:
        say("Q2 ANSWER: NO", f"Dhan refused the amend:\n  {exc}\nA bracketed entry could never take a profit exit.")

    # ── Q4: does the release actually leave NOTHING working? ─────────────
    # THE ONE THE FIRST RUN GOT WRONG. Cancelling ENTRY_LEG returned DH-906
    # and left both legs PENDING over a position believed closed. This asks
    # the fixed code to do it, then checks the book rather than trusting it.
    say("Q4 -- executor.cancel_bracket() releases the exit legs")
    try:
        say("release reported", executor.cancel_bracket(order_id=order_id))
    except Exception as exc:
        say("Q4 ANSWER: NO -- the release failed", f"{exc}\n!! CHECK DHAN: legs may still be working.")

    still_working = []
    try:
        for row in broker.get_super_orders() or []:
            if str(row.get("orderId") or "") != order_id:
                continue
            for leg in row.get("legDetails") or []:
                if str(leg.get("orderStatus") or "").upper() == "PENDING":
                    still_working.append(str(leg.get("legName")))
    except Exception as exc:
        print(f"could not re-read the super order book: {exc}")
        still_working = ["UNKNOWN"]
    if still_working:
        say("Q4 ANSWER: NO", f"STILL WORKING AT DHAN: {still_working}\n!! Cancel them by hand. Tell Claude.")
    else:
        say("Q4 ANSWER: YES -- nothing left working", "the bracket released cleanly; the position can be sold safely")

    # ── the exit, through the executor too ───────────────────────────────
    say("CLEANUP -- executor.sell() closes the leg")
    try:
        exit_receipt = executor.sell(
            when=datetime.now(),
            strike=contract["strike"],
            expiry=contract["expiry"],
            option_type="CE",
            quantity=contract["lot"],
        )
        say("exit", exit_receipt)
        if str(exit_receipt.get("status")).upper() != "FILLED":
            print("\n!! THE EXIT DID NOT CONFIRM. CHECK YOUR DHAN POSITIONS AND CLOSE BY HAND.")
            return 3
    except Exception as exc:
        print(f"\n!! EXIT FAILED: {exc}")
        print("!! CHECK YOUR DHAN POSITIONS AND CLOSE THE LEG BY HAND.")
        return 3

    say("DONE", "Q1-Q4 answered above, all through the engine's own executor. Confirm flat in Dhan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
