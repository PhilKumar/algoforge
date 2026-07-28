#!/usr/bin/env python3
"""Find out what Upstox will actually give us for expired NIFTY options.

Two things are unknown and worth one cheap request each rather than an
assumption baked into a fetcher:

  1. How far back the expiry list really goes. Community reports in July 2026
     say about six months; the docs say nothing. Whatever this prints is the
     real ceiling on a fully-priced backtest.
  2. Whether an Analytics Token reaches the expired-instruments endpoints at
     all. It is documented for "Historical Data", but expired instruments are
     not named in that list, and there are open reports of some market-data
     endpoints returning 404 or UDAPI100050 for this token type.

Nothing here is assumed. Every call prints its status and, on failure, the
body, because a wrong guess about a URL shape is indistinguishable from a
permissions problem unless you can see the error.

    python3 tools/upstox_probe.py

Read-only. Places no order, writes no state.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

BASE = "https://api.upstox.com/v2"
NIFTY = "NSE_INDEX|Nifty 50"


def token() -> str:
    """Read the Analytics Token out of .env without pulling in dotenv."""
    if not ENV_PATH.exists():
        sys.exit(f"No {ENV_PATH}. Put UPSTOX_ACCESS_TOKEN in it first.")
    for line in ENV_PATH.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "UPSTOX_ACCESS_TOKEN":
            cleaned = value.strip().strip('"').strip("'")
            if cleaned:
                return cleaned
    sys.exit(
        f"UPSTOX_ACCESS_TOKEN is missing or empty in {ENV_PATH}.\n"
        "Copy it from https://account.upstox.com/developer/apps -> Analytics."
    )


def get(path: str, params: Optional[dict] = None) -> tuple[int, Any]:
    """One GET, returning status and parsed body. Never raises on HTTP error."""
    response = requests.get(
        f"{BASE}{path}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token()}"},
        params=params or {},
        timeout=30,
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text[:400]


def show(label: str, status: int, body: Any) -> bool:
    """Print an outcome. Returns whether it looked like a success."""
    ok = status == 200 and isinstance(body, dict) and body.get("status") == "success"
    mark = "ok  " if ok else "FAIL"
    print(f"  [{mark}] {label} -> HTTP {status}")
    if not ok:
        text = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
        print("        " + text[:600].replace("\n", "\n        "))
    return ok


def main() -> int:
    print("\n1. Which expiries does Upstox still hold for NIFTY?")
    status, body = get("/expired-instruments/expiries", {"instrument_key": NIFTY})
    if not show("expiries", status, body):
        print(
            "\n  If this is 404 or UDAPI100050, the Analytics Token does not reach\n"
            "  expired instruments and we need the OAuth app after all\n"
            "  (tools/upstox_login.py is already written for that path).\n"
        )
        return 1

    expiries = sorted(str(x) for x in (body.get("data") or []))
    if not expiries:
        print("  The call succeeded but returned no expiries at all.")
        return 1

    oldest, newest = expiries[0], expiries[-1]
    try:
        months = (date.fromisoformat(newest) - date.fromisoformat(oldest)).days / 30.44
        span = f"{months:.1f} months"
    except ValueError:
        span = "unparseable dates"
    print(f"  {len(expiries)} expiries, {oldest} to {newest}  ({span})")
    print(f"  >> A fully-priced backtest cannot start before {oldest}.")

    a_year_ago = (date.today() - timedelta(days=365)).isoformat()
    print(f"  >> One year ago is {a_year_ago}: " + ("covered." if oldest <= a_year_ago else "NOT covered."))

    print(f"\n2. Can we list contracts for the oldest expiry ({oldest})?")
    status, body = get(
        "/expired-instruments/option/contract",
        {"instrument_key": NIFTY, "expiry_date": oldest},
    )
    if not show("option contracts", status, body):
        return 1

    contracts = body.get("data") or []
    print(f"  {len(contracts)} contracts returned.")
    if not contracts:
        return 1

    # A mid-list strike stands in for ATM-2 well enough to prove the pipe. The
    # near-ATM strikes are exactly where empty-but-200 responses were reported,
    # so this is the interesting case, not a soft one.
    sample = contracts[len(contracts) // 2]
    key = sample.get("expired_instrument_key") or sample.get("instrument_key")
    print(f"  Sampling {sample.get('trading_symbol') or key}")

    print("\n3. Do 1-minute candles come back for that contract?")
    from_date = (date.fromisoformat(oldest) - timedelta(days=10)).isoformat()
    status, body = get(f"/expired-instruments/historical-candle/{key}/1minute/{oldest}/{from_date}")
    if not show("historical candles", status, body):
        return 1

    candles = (body.get("data") or {}).get("candles") or []
    print(f"  {len(candles)} candles for {from_date} to {oldest}.")
    if not candles:
        print("  >> Empty-but-successful. This is the reported near-ATM gap; the")
        print("     fetcher must record it as a gap, never price it as zero.")
        return 1

    print(f"  First: {candles[0]}")
    print(f"  Last:  {candles[-1]}")
    print("\nAll three worked. The fetcher can be built against this shape.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
