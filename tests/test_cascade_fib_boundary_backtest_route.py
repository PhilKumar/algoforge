"""The Backtest button must price the ladder the Start button trades.

`/api/fib-boundary/backtest` used to run the auto-geometry engine -- trendline,
touch, detected legs -- while the panel above it draws two fixed lines measured
straight off the mother the user typed.  The amber warning under the button was
apologising for exactly that.  Phil asked for real historical P&L on the typed
levels (2026-08-01), so the route now runs `FibBoundaryCascade`.

These tests cover the seams that swap introduced -- the serializer the panel
reads, the chart payload the Canvas renderer draws, and the rupee/bar mismatch
between the premium lookup and the engine -- without needing Dhan or Upstox.
"""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _nifty_lot_size_on, _serialize_fib_boundary_backtest  # noqa: E402
from engine.cascade_fib_boundary import FibBoundaryCascade, FibBoundaryConfig  # noqa: E402
from engine.cascade_options import Candle, NiftyContractResolver, OptionCandle  # noqa: E402
from engine.ladder_reporting import fib_boundary_chart  # noqa: E402

# 08-25 is August's monthly at 27 DTE from a 07-29 mother; the two weeklies are
# there so a resolver that ignored monthly_only would be caught picking one.
EXPIRIES = [date(2026, 8, 4), date(2026, 8, 11), date(2026, 8, 25)]
MOTHER_AT = datetime(2026, 7, 29, 9, 10)


def _c(hh, mm, o, h, low, c):
    return Candle(datetime(2026, 7, 29, hh, mm), o, h, low, c)


# The same walk tests/test_cascade_fib_boundary.py replays: down through L4
# (23,660), on down through L8 (23,140), then a snap back through the target.
CANDLES = [
    _c(9, 15, 24040, 24050, 23690, 23700),
    _c(9, 20, 23700, 23710, 23600, 23640),
    _c(9, 25, 23640, 23680, 23630, 23670),  # fills L4
    _c(9, 30, 23670, 23675, 23380, 23400),
    _c(9, 35, 23400, 23410, 23050, 23100),
    _c(9, 40, 23100, 23160, 23090, 23150),  # fills L8
    _c(9, 45, 23150, 23560, 23150, 23540),  # target
]
PREMIUMS = {(9, 25): 150.0, (9, 40): 90.0, (9, 45): 210.0}


def _config(**overrides):
    return FibBoundaryConfig(MOTHER_AT, 24180.0, 24050.0, timeframe="5m", **overrides)


def _run(lookup=None):
    config = _config()
    resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=75)
    if lookup is None:

        def lookup(timestamp, _contract):
            value = PREMIUMS.get((timestamp.hour, timestamp.minute))
            return None if value is None else OptionCandle(timestamp, value, value, value, value)

    return config, FibBoundaryCascade(config, resolver, lookup).run(CANDLES)


class FibBoundarySerializerTests(unittest.TestCase):
    def test_the_panel_gets_a_finished_rupee_pnl(self):
        config, result = _run()
        payload = _serialize_fib_boundary_backtest(result, config.lot_size)
        self.assertEqual(payload["status"], "closed")
        self.assertEqual(payload["exit_reason"], "target")
        self.assertTrue(payload["fully_priced"])
        self.assertIsNotNone(payload["net_pnl"])
        # Costs are charged, so net must sit below gross rather than equal it.
        self.assertLess(payload["net_pnl"], payload["gross_pnl"])
        self.assertGreater(payload["costs_total"], 0)

    def test_every_entry_names_the_fib_line_it_sat_on(self):
        _config_, result = _run()
        payload = _serialize_fib_boundary_backtest(result, 75)
        self.assertEqual([row["level"] for row in payload["entries"]], [4, 8])
        # 1/2/3 lots, and each rung re-selects ATM-2 at ITS depth, so the deeper
        # CE strike is the lower one.
        self.assertEqual([row["lots"] for row in payload["entries"]], [1, 2])
        strikes = [row["strike"] for row in payload["entries"]]
        self.assertLess(strikes[1], strikes[0])
        for row in payload["entries"]:
            self.assertIsNotNone(row["spend_inr"])

    def test_the_campaign_buys_the_monthly_never_a_weekly(self):
        # The whole reason the locked config exists: weeklies died at expiry.
        _config_, result = _run()
        payload = _serialize_fib_boundary_backtest(result, 75)
        self.assertEqual({row["expiry"] for row in payload["entries"]}, {"2026-08-25"})
        self.assertEqual(payload["contract"]["expiry"], "2026-08-25")

    def test_an_unpriced_leg_is_a_gap_not_a_free_trade(self):
        def blind(_timestamp, _contract):
            return None

        _config_, result = _run(lookup=blind)
        payload = _serialize_fib_boundary_backtest(result, 75)
        self.assertFalse(payload["fully_priced"])
        for row in payload["entries"]:
            self.assertIsNone(row["spend_inr"])

    def test_an_exit_with_no_print_settles_at_the_intrinsic_floor(self):
        # The deep ITM leg is exactly the one that stops printing (2026-07-27:
        # a gap-up hit the target on the opening candle before the deep strike
        # traded).  Its exit settles at intrinsic against the index — the same
        # floor the expiry exit uses — disclosed, and the P&L stands.
        def lookup(timestamp, contract):
            value = PREMIUMS.get((timestamp.hour, timestamp.minute))
            if value is None:
                return None
            if (timestamp.hour, timestamp.minute) == (9, 45) and contract.strike <= 23_000:
                return None  # the deep strike never printed near the exit
            return OptionCandle(timestamp, value, value, value, value)

        config = _config()
        resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=75)
        result = FibBoundaryCascade(config, resolver, lookup).run(CANDLES)

        self.assertEqual(result.status, "closed")
        self.assertEqual(result.data_gaps, [])
        self.assertEqual(len(result.pricing_notes), 1)
        self.assertIn("intrinsic", result.pricing_notes[0])
        self.assertIsNotNone(result.net_pnl)
        # The exit candle closed at 23,540; the deep leg's floor is close - strike.
        deep = next(entry for entry in result.entries if entry.contract.strike <= 23_000)
        floor = 23_540.0 - deep.contract.strike
        priced_exit = result.exit_option_prices[result.entries.index(deep)]
        self.assertAlmostEqual(priced_exit, floor)
        payload = _serialize_fib_boundary_backtest(result, 75)
        self.assertTrue(payload["fully_priced"])
        self.assertIsNotNone(payload["net_pnl"])

    def test_a_mother_that_never_traded_serializes_without_a_contract(self):
        config = _config()
        resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=75)
        flat = [_c(10, 0, 24100, 24110, 24090, 24100)] * 3
        result = FibBoundaryCascade(config, resolver, lambda *_: None).run(flat)
        payload = _serialize_fib_boundary_backtest(result, 75)
        self.assertEqual(payload["entries"], [])
        self.assertIsNone(payload["contract"])
        self.assertIsNone(payload["net_pnl"])


class NiftyLotSizeTests(unittest.TestCase):
    """One hardcoded lot size cannot be right for a replay that spans a change.

    The dates below are ground truth from Upstox's real listed expired contract
    chains, recorded in data/nse_contract_rules.json: NIFTY carried lot 25
    through the 26-Dec-2024 expiry, 75 from 02-Jan-2025, and 65 from 06-Jan-2026.

    This test previously asserted 50 for Nov-2024, matching a LOT_SIZES table
    that ran 50 straight through to Nov-2024. Anything sized off that table
    between Apr and Dec 2024 was sized at twice the real contract.
    """

    def test_the_lot_size_follows_the_mother_date(self):
        self.assertEqual(_nifty_lot_size_on(date(2024, 3, 1)), 50)
        self.assertEqual(_nifty_lot_size_on(date(2024, 11, 19)), 25)
        self.assertEqual(_nifty_lot_size_on(date(2024, 12, 26)), 25)
        self.assertEqual(_nifty_lot_size_on(date(2025, 1, 2)), 75)
        self.assertEqual(_nifty_lot_size_on(date(2025, 12, 31)), 75)
        self.assertEqual(_nifty_lot_size_on(date(2026, 1, 1)), 65)

    def test_todays_lot_is_the_one_phil_confirmed(self):
        self.assertEqual(_nifty_lot_size_on(date(2026, 8, 1)), 65)

    def test_quantity_is_the_dated_lot_times_the_ladder(self):
        # The whole point of getting the lot right: it multiplies straight into
        # quantity, and quantity multiplies straight into rupees.
        config = FibBoundaryConfig(MOTHER_AT, 24180.0, 24050.0, timeframe="5m", lot_size=65)
        resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=65)

        def lookup(timestamp, _contract):
            value = PREMIUMS.get((timestamp.hour, timestamp.minute))
            return None if value is None else OptionCandle(timestamp, value, value, value, value)

        result = FibBoundaryCascade(config, resolver, lookup).run(CANDLES)
        payload = _serialize_fib_boundary_backtest(result, 65)
        self.assertEqual([row["quantity"] for row in payload["entries"]], [65, 130])


class FibBoundaryChartTests(unittest.TestCase):
    def test_the_chart_draws_the_typed_lines_not_trendlines(self):
        config, result = _run()
        chart = fib_boundary_chart(config, result, CANDLES, timeframe="5m")
        # A typed ladder has no legs and no trendlines to draw; its boundaries
        # ride in as plain labelled lines.
        self.assertEqual(chart["trendlines"], [])
        self.assertEqual(chart["legs"], [])
        self.assertEqual([line["label"] for line in chart["lines"]], ["L4", "L8"])
        self.assertEqual([line["price"] for line in chart["lines"]], [23660.0, 23140.0])
        self.assertTrue(all(line["filled"] for line in chart["lines"]))
        self.assertTrue(all(line["inr_notional"] > 0 for line in chart["lines"]))

    def test_the_chart_speaks_epoch_seconds_and_carries_the_mother_band(self):
        config, result = _run()
        chart = fib_boundary_chart(config, result, CANDLES, timeframe="5m")
        self.assertEqual(chart["mother"], {"high": 24180.0, "low": 24050.0})
        self.assertTrue(all(isinstance(row["t"], int) for row in chart["candles"]))
        self.assertEqual(len(chart["entries"]), 2)
        self.assertEqual(len(chart["exits"]), 1)
        self.assertEqual(chart["tp_label"], "TARGET HIT")

    def test_an_untraded_line_is_drawn_unfilled(self):
        # Price reaches L4 and stops. L8 must still appear -- a rung that never
        # filled is information, not something to hide.
        config = _config()
        resolver = NiftyContractResolver(EXPIRIES, strike_step=50.0, lot_size=75)

        def lookup(timestamp, _contract):
            value = PREMIUMS.get((timestamp.hour, timestamp.minute))
            return None if value is None else OptionCandle(timestamp, value, value, value, value)

        result = FibBoundaryCascade(config, resolver, lookup).run(CANDLES[:3])
        chart = fib_boundary_chart(config, result, CANDLES[:3], timeframe="5m")
        self.assertEqual([line["filled"] for line in chart["lines"]], [True, False])
        self.assertEqual(chart["lines"][1]["inr_notional"], 0.0)
        self.assertEqual(chart["tp_label"], "TARGET (open — watching)")


if __name__ == "__main__":
    unittest.main()
