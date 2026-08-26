"""Rebuild the first live Candle Entry campaign, which its successor destroyed.

Mother 3 Aug 15:25, NIFTY 24400 CE expiring 25 Aug 2026. It bought twice and
held to expiry -- the tail the backtest said existed and had never once hit in
34 campaigns. It settled on 25 Aug and, within the hour, the auto mother opened
the next campaign and OVERWROTE the only row that held it. Phil, that evening:
"Where is that trade that is completed today with expiry?"

Nothing on the box holds its settlement: the option archive stops at 20 Aug
(the days Dhan's data subscription had lapsed), and the only backup that day
was taken at 09:08, before expiry. So the exit is REBUILT, not recovered:

  * The two fills come from the 09:08 backup, which is a live capture.
  * The exit is the definition of expiry settlement -- a call settles at
    max(0, settlement - strike) -- against NIFTY's own 25 Aug settlement of
    24,334.55, read back from Dhan's index history.
  * Charges come from the engine's own cost model, not an estimate.

The row is written with source='rebuilt' so the page can say so on its face.
Idempotent: it upserts on (user, strategy, mother), so running it twice is safe.

    python3 tools/rebuild_lost_candle_campaign.py --user 1 [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cascade_costs import calculate_nifty_option_round_costs  # noqa: E402

MOTHER = "2026-08-03T15:25:00+05:30"
EXPIRY_SETTLEMENT = 24334.55  # NIFTY, 25 Aug 2026, from Dhan index history
CLOSED_AT = "2026-08-25T15:30:00+05:30"
FILLS = [
    {"timestamp": "2026-08-11T09:25:00+05:30", "strike": 24400, "quantity": 65, "lots": 1, "option_premium": 309.0},
    {"timestamp": "2026-08-12T10:15:00+05:30", "strike": 24300, "quantity": 130, "lots": 2, "option_premium": 283.6},
]


def build_row() -> dict:
    deployed = value = gross = charges = 0.0
    legs = []
    for fill in FILLS:
        sell = max(0.0, EXPIRY_SETTLEMENT - float(fill["strike"]))
        dep = float(fill["option_premium"]) * int(fill["quantity"])
        val = sell * int(fill["quantity"])
        cost = calculate_nifty_option_round_costs(
            buy_price=float(fill["option_premium"]),
            sell_price=sell,
            quantity=int(fill["quantity"]),
            lots_bought=int(fill["lots"]),
            lots_sold=int(fill["lots"]),
        )
        deployed += dep
        value += val
        gross += val - dep
        charges += cost.total
        legs.append({**fill, "exit_premium": round(sell, 2), "exit_value": round(val, 2)})
    return {
        "campaign_key": MOTHER,
        "symbol": "NIFTY",
        "contract": "24400 CE",
        "opened_at": FILLS[0]["timestamp"],
        "closed_at": CLOSED_AT,
        "status": "EXPIRED",
        "exit_reason": "expiry",
        "buys": len(FILLS),
        "deployed_inr": round(deployed, 2),
        "gross_pnl": round(gross, 2),
        "costs_total": round(charges, 2),
        "net_pnl": round(gross - charges, 2),
        "source": "rebuilt",
        "payload": {
            "mother": {"timestamp": MOTHER},
            "contract": {"underlying": "NIFTY", "strike": 24400, "option_type": "CE", "expiry": "2026-08-25"},
            "exit": {"timestamp": CLOSED_AT, "reason": "expiry", "index_settlement": EXPIRY_SETTLEMENT},
            "fills": legs,
            "rounds": [],
            "rebuilt_note": (
                "Fills captured live (09:08 backup, 25 Aug). Exit rebuilt from NIFTY's "
                "25 Aug settlement of 24,334.55; the option archive has no bars after "
                "20 Aug because Dhan's data subscription had lapsed."
            ),
        },
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--apply", action="store_true", help="write it; otherwise just print")
    args = ap.parse_args()

    row = build_row()
    print("Rebuilt campaign")
    print("  mother   :", row["campaign_key"])
    print("  contract :", row["contract"], "expiry 2026-08-25")
    for leg in row["payload"]["fills"]:
        print(
            "    %s CE x%s @ %.2f -> settles %.2f"
            % (leg["strike"], leg["quantity"], leg["option_premium"], leg["exit_premium"])
        )
    print("  deployed :", row["deployed_inr"])
    print("  gross    :", row["gross_pnl"])
    print("  costs    :", row["costs_total"])
    print("  NET      :", row["net_pnl"])
    if not args.apply:
        print("\n(dry run — pass --apply to write it)")
        return 0
    import db as db_mod

    await db_mod.init_db()
    fresh = await db_mod.save_paper_campaign(args.user, "candle_entry", row)
    print("\nwritten:", "new row" if fresh else "updated the existing row")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
