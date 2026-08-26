"""What the open Fib Boundary basket is worth RIGHT NOW.

The ladder reported deployed capital and nothing else, so a running campaign
looked the same whether it was up or down -- the one number a live ladder is
watched for was the one it would not say (Phil, 2026-08-26: "Need Open trades
... with current P&L. The monitor one... It is not on all the 4 pages"). Gap
Carry and Candle Entry already marked their open legs; these pin the same
arithmetic here, including its refusal to guess.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktZm9yLXVuaXQtdGVzdHMtMzJieXRlIQ==")

import app as app_module  # noqa: E402
from engine.fib_touch_ladder import (  # noqa: E402
    FibTouchConfig,
    FibTouchLadder,
    PaperExecutor,
)

Bar = app_module.IndexCandle

BASE = datetime(2026, 8, 6, 9, 15)
ROWS = [
    (24_660, 24_780, 24_640, 24_642),
    (24_642, 24_644, 24_620, 24_622),
    (24_622, 24_624, 24_600, 24_602),
    (24_602, 24_612, 24_600, 24_610),
    (24_610, 24_620, 24_608, 24_618),
    (24_618, 24_650, 24_615, 24_645),
    (24_645, 24_700, 24_640, 24_695),
    (24_695, 24_698, 24_680, 24_682),
    (24_682, 24_684, 24_670, 24_672),
    (24_672, 24_674, 24_495, 24_510),
    (24_510, 24_512, 24_493, 24_500),
    (24_500, 24_501, 24_490, 24_494),
    (24_494, 24_508, 24_492, 24_506),
]


def _ladder(premium_lookup):
    engine = FibTouchLadder(
        FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=BASE,
            lot_size=65,
            strike_step=50.0,
        ),
        premium_lookup=premium_lookup,
        expiry_source=lambda on: [date(2026, 8, 11)],
        executor=PaperExecutor(),
    )
    for i, (o, h, low, c) in enumerate(ROWS):
        engine.on_candle(Bar(BASE + timedelta(minutes=i), o, h, low, c))
    return engine


class OpenMarkTests(unittest.TestCase):
    def test_a_held_basket_reports_what_it_is_worth(self):
        # Bought at 200, now worth 260: 60 a unit on the quantity it holds.
        engine = _ladder(lambda *a: 200.0)
        self.assertTrue(engine.fills, "fixture must hold a leg")
        quantity = sum(fill.quantity for fill in engine.fills)
        engine.premium_lookup = lambda *a: 260.0
        mark = engine.mark_open(BASE + timedelta(minutes=20))
        self.assertIsNotNone(mark)
        self.assertEqual(mark["gross_pnl"], round(60.0 * quantity, 2))
        # Charged on the round trip, so the net is below the gross but close.
        self.assertIsNotNone(mark["costs_total"])
        self.assertLess(mark["net_pnl"], mark["gross_pnl"])
        self.assertGreater(mark["net_pnl"], mark["gross_pnl"] - 500)

    def test_a_leg_with_no_quote_refuses_to_invent_a_number(self):
        engine = _ladder(lambda *a: 200.0)
        engine.premium_lookup = lambda *a: None
        mark = engine.mark_open(BASE + timedelta(minutes=20))
        self.assertIsNotNone(mark)
        self.assertIsNone(mark["net_pnl"], "an unpriced leg makes the basket a guess")
        self.assertIsNone(mark["gross_pnl"])
        # It still says what it holds, so the table can show the legs.
        self.assertTrue(mark["legs"])
        self.assertGreater(mark["deployed_inr"], 0)

    def test_nothing_held_is_not_marked(self):
        engine = _ladder(lambda *a: 200.0)
        engine.fills.clear()
        self.assertIsNone(engine.mark_open(BASE + timedelta(minutes=20)))

    def test_the_status_carries_the_mark_for_the_page(self):
        engine = _ladder(lambda *a: 200.0)
        status = engine.get_status()
        self.assertIn("mark", status)
        # Refreshed on the bar, so a status read costs no broker quote.
        self.assertIsNotNone(status["mark"], "the fixture ends holding a leg")
        self.assertEqual(status["mark"]["legs"][0]["option_type"], "CE")


if __name__ == "__main__":
    unittest.main()
