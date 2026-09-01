#!/usr/bin/env python3
"""Read one real Dhan ORDER-STATUS payload, because the Auto path guesses it.

The strategy-builder Auto path (`engine/live.py`) does not use Super Orders.
It sends a plain MARKET order and a separate SL order, then asks
`GET /v2/orders/{id}` whether the entry filled -- and everything it does next
depends on keys nobody here has ever seen come back:

    Q1  Is the body an OBJECT or a single-element ARRAY? Every caller does
        `status.get(...)`, which on a list raises and reads through as a
        verification failure. `get_order_status` now unwraps an array; this
        says whether it ever needed to.

    Q2  Which fill keys are present, and when? `verify_order_fill` used to
        fall back filledQty -> tradedQuantity -> `quantity`, so a PENDING
        order carrying only the order quantity was declared FILLED at a price
        of zero, and the engine opened a position that did not exist. That
        fallback is gone. This proves whether it was ever firing, by printing
        the raw payload of the SAME order while it is pending and after it
        trades.

    Q3  Is the fill price `averagePrice` or `averageTradedPrice`? The engine
        books its entry premium from whichever it finds first.

    Q4  Is a separate SL order accepted for a bought option, and what does
        Dhan call it back? That is the leg stop the deploy modal offers, and
        it has never been placed by this path either.

It is deliberately not part of the app: no strategy is armed, no engine is
started, no gate is opened. It buys one lot, asks the four questions, prints
the exact words Dhan used, and sells the lot back.

USAGE
    python3 tools/dhan_order_probe.py                    # dry run, sends nothing
    python3 tools/dhan_order_probe.py --i-mean-it        # places ONE real order
    python3 tools/dhan_order_probe.py --i-mean-it --close-only SECURITY_ID:QTY

WHAT IT COSTS
    One lot of an ATM-2 NIFTY call, held for the few seconds the probe needs.
    The lot is read from the live chain, not assumed -- NIFTY's has changed
    four times in two years. At a 65 lot and a Rs 250 premium that is about
    Rs 16,000 of premium at risk, plus the round-trip spread and charges.

BEFORE YOU RUN IT
  * Stop PhilForge locally if it is running. A Dhan session is single-active:
    the app and a local token fight, and the token dies in two or three
    minutes.
  * Run it inside market hours, or the order is rejected and Q2 -- the one
    that matters -- goes unanswered.
  * It sells the lot back itself. If it dies in the middle, the last line it
    prints is the exact command to flatten by hand. Read that before walking
    away.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from broker.dhan import DhanClient, ScripMaster  # noqa: E402
from engine.backtest import get_option_contract_lot_size  # noqa: E402


def say(step: str, detail: object = "") -> None:
    print(f"\n=== {step} ===")
    if detail != "":
        print(detail if isinstance(detail, str) else json.dumps(detail, indent=2, default=str))


def raw_status(broker: DhanClient, order_id: str) -> object:
    """The UNNORMALISED body, which is the whole point of the probe.

    `broker.get_order_status` unwraps an array and substitutes UNKNOWN on a
    non-200. Both would hide the answer to Q1, so this goes to the endpoint
    directly.
    """
    from broker.dhan import _request_with_retry

    resp = _request_with_retry(
        "GET",
        f"{broker.base_url}/v2/orders/{order_id}",
        headers=broker.headers,
        timeout=10,
        allow_token_refresh=False,
        refresh_token_func=broker.refresh_access_token,
    )
    try:
        return {"http": resp.status_code, "body": resp.json()}
    except Exception:
        return {"http": resp.status_code, "text": resp.text[:2000]}


def describe(body: object) -> str:
    """Answer Q1-Q3 in words, so the reader does not have to squint at JSON."""
    shape = type(body).__name__
    row = body[0] if isinstance(body, list) and body else body
    if not isinstance(row, dict):
        return f"shape: {shape} — no object to read keys from"
    fill_keys = [k for k in ("filledQty", "filled_qty", "tradedQuantity", "traded_quantity") if k in row]
    price_keys = [k for k in ("averageTradedPrice", "averagePrice", "price") if k in row]
    lines = [
        f"Q1  shape ............. {shape}" + (" (ARRAY — unwrapping was needed)" if isinstance(body, list) else ""),
        f"Q2  status ............ {row.get('orderStatus', row.get('status', '?'))}",
        f"Q2  fill keys present . {fill_keys or 'NONE — this is the case the old fallback misread'}",
        f"Q2  quantity .......... {row.get('quantity', '?')}",
    ]
    for k in fill_keys:
        lines.append(f"Q2    {k} = {row.get(k)!r}")
    lines.append(f"Q3  price keys ........ {price_keys or 'none'}")
    for k in price_keys:
        lines.append(f"Q3    {k} = {row.get(k)!r}")
    return "\n".join(lines)


def first_price(payload) -> float:
    """Dig the first last_price out of a market-feed payload.

    Dhan nests these by SEGMENT and then by security id --
    {"IDX_I": {"13": {"last_price": 24500}}} -- so reading one level down finds
    a dict where a number was expected and reports no price at all. The app
    walks the same payload recursively; the super-order probe hit this first
    and was fixed the same way in 0d4a40c.
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


def pick_contract(broker: DhanClient, *, itm_steps: int = 2) -> dict:
    """Everything READ from what Dhan is listing now, never assumed."""
    expiries = [e for e in ScripMaster.get_expiries("NIFTY") if str(e) >= date.today().isoformat()]
    if not expiries:
        raise SystemExit("Dhan is listing no NIFTY expiries; is the scrip master loaded?")
    expiry = sorted(expiries)[0]
    try:
        quote = broker.get_ltp(["13"], "IDX_I") or {}
    except Exception as exc:
        raise SystemExit(
            f"Could not read the NIFTY spot: {exc}\n\n"
            "Almost always the token, not the probe: only ONE Dhan session is active at\n"
            "a time. Nothing was sent."
        ) from exc
    spot = first_price(quote)
    if not spot:
        raise SystemExit("Could not read the NIFTY spot; refusing to choose a strike blind.")
    step = 50.0
    atm = round(spot / step) * step
    strike = int(atm - itm_steps * step)
    lot = int(get_option_contract_lot_size("NIFTY", date.fromisoformat(str(expiry))))
    security_id = ScripMaster.lookup("NIFTY", strike, str(expiry), "CE")
    if not security_id:
        raise SystemExit(f"Dhan does not list NIFTY {strike}CE {expiry}")
    return {"strike": strike, "expiry": str(expiry), "lot": lot, "spot": spot, "security_id": str(security_id)}


def flatten(broker: DhanClient, contract: dict, qty: int) -> None:
    say("CLEANUP", f"selling back {qty} qty of NIFTY {contract['strike']}CE {contract['expiry']}")
    sold = broker.place_option_order(
        underlying="NIFTY",
        strike_price=contract["strike"],
        option_type="CE",
        expiry=contract["expiry"],
        transaction_type="SELL",
        quantity=qty,
        order_type="MARKET",
        product_type="INTRADAY",
        tag="PF_PROBE_X",
    )
    print(json.dumps(sold, indent=2, default=str))
    exit_id = str(sold.get("orderId") or "")
    if exit_id:
        time.sleep(2)
        say("EXIT ORDER, RAW", raw_status(broker, exit_id))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--i-mean-it", action="store_true", help="actually place the order (default: dry run)")
    ap.add_argument("--itm-steps", type=int, default=2)
    ap.add_argument("--sl-pct", type=float, default=20.0, help="leg stop, as the deploy modal offers it")
    ap.add_argument("--close-only", metavar="STRIKE:QTY", help="skip the probe; just sell this back")
    args = ap.parse_args()

    broker = DhanClient()
    ScripMaster.ensure_loaded()

    contract = pick_contract(broker, itm_steps=args.itm_steps)

    if args.close_only:
        strike, _, qty = args.close_only.partition(":")
        contract["strike"] = int(strike)
        flatten(broker, contract, int(qty))
        return 0

    say("CONTRACT", contract)
    premium = 0.0
    premium = first_price(broker.get_ltp([contract["security_id"]], "NSE_FNO") or {})
    if premium <= 0:
        raise SystemExit(
            f"No quote for NIFTY {contract['strike']}CE {contract['expiry']}.\n"
            "Nothing is quoted outside market hours, and a strike this far out can be\n"
            "untraded even in session -- try fewer --itm-steps. Nothing was sent."
        )
    trigger = round(max(0.05, premium * (1 - args.sl_pct / 100)), 2)
    say(
        "WHAT THIS WILL DO",
        f"BUY 1 lot ({contract['lot']} qty) NIFTY {contract['strike']}CE {contract['expiry']} at MARKET\n"
        f"  last traded premium : Rs {premium:,.2f}\n"
        f"  premium at risk     : Rs {premium * contract['lot']:,.0f}\n"
        f"  then place an SL order at trigger Rs {trigger:,.2f}, read both back, cancel the SL,\n"
        f"  and sell the lot at MARKET.",
    )

    if not args.i_mean_it:
        say("DRY RUN", "Nothing was sent. Re-run with --i-mean-it to place it for real.")
        return 0
    if input("\nType YES to send a REAL order: ").strip() != "YES":
        say("ABORTED", "Nothing was sent.")
        return 1

    qty = contract["lot"]
    entry = broker.place_option_order(
        underlying="NIFTY",
        strike_price=contract["strike"],
        option_type="CE",
        expiry=contract["expiry"],
        transaction_type="BUY",
        quantity=qty,
        order_type="MARKET",
        product_type="INTRADAY",
        tag="PF_PROBE_E",
    )
    say("ENTRY SUBMIT RESPONSE", entry)
    order_id = str(entry.get("orderId") or "")
    if not order_id:
        say("NO ORDER ID", "Dhan answered without one. Nothing to read back; check the order book by hand.")
        return 1

    print(
        f"\n!! If this dies from here on, flatten with:\n   python3 tools/dhan_order_probe.py "
        f"--i-mean-it --close-only {contract['strike']}:{qty}\n"
    )

    sl_order_id = ""
    try:
        # Q2's whole point: read it IMMEDIATELY, while it may still be pending.
        say("ENTRY, READ BACK AT ONCE (raw)", raw_status(broker, order_id))
        first = raw_status(broker, order_id)
        say("ENTRY, READ BACK AT ONCE (answers)", describe(first.get("body") if isinstance(first, dict) else first))

        time.sleep(4)
        settled = raw_status(broker, order_id)
        say("ENTRY, AFTER 4s (raw)", settled)
        say("ENTRY, AFTER 4s (answers)", describe(settled.get("body") if isinstance(settled, dict) else settled))

        say("WHAT verify_order_fill MAKES OF IT", broker.verify_order_fill(order_id, max_wait_sec=10))

        # Q4 — the leg stop this path offers and has never placed.
        say("SL ORDER", f"placing SL, trigger Rs {trigger:,.2f}, limit Rs {round(trigger * 0.99, 2):,.2f}")
        sl = broker.place_sl_order(
            underlying="NIFTY",
            strike_price=contract["strike"],
            option_type="CE",
            expiry=contract["expiry"],
            transaction_type="SELL",
            quantity=qty,
            trigger_price=trigger,
            price=round(trigger * 0.99, 2),
            product_type="INTRADAY",
            order_type="SL",
            tag="PF_PROBE_SL",
        )
        say("SL SUBMIT RESPONSE", sl)
        sl_order_id = str(sl.get("orderId") or "")
        if sl_order_id:
            time.sleep(2)
            say("SL, READ BACK (raw)", raw_status(broker, sl_order_id))
    finally:
        if sl_order_id:
            try:
                say("CANCEL SL", broker.cancel_order(sl_order_id))
            except Exception as exc:
                say("CANCEL SL FAILED", f"{exc}\nCancel it by hand before the position is sold.")
        try:
            flatten(broker, contract, qty)
        except Exception as exc:
            say(
                "COULD NOT FLATTEN",
                f"{exc}\n\nSELL {qty} qty NIFTY {contract['strike']}CE {contract['expiry']} IN DHAN NOW.",
            )
            return 1

    say("DONE", "Position closed. The four answers are in the raw payloads above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
