"""Brokerage, STT, exchange fees, GST and stamp duty on a round trip.

Neither the paper engine nor the live one charged a rupee of this before
2026-09-01. The execution profile already modelled SLIPPAGE -- spread and
entry/exit slip, fixed onto all three running strategies in 8259257 -- but
the statutory half was simply absent, so every reported number was better
than the account would ever be.

It matters more than it sounds. The five-year replay of PE_NoTarget's config
came out at roughly Rs 216/trade across 2021-2025, against about Rs 200/trade
of these charges. Subtracting them is the difference between a thin edge and
no edge at all in those years.
"""

from __future__ import annotations

import unittest

from engine.live import statutory_round_charges as live_charges
from engine.paper_trading import statutory_round_charges as paper_charges


class ChargeTests(unittest.TestCase):
    def test_an_option_round_trip_costs_real_money(self):
        cost = paper_charges(entry_premium=250.0, exit_premium=300.0, quantity=65, lots=1, option_type="CE")
        self.assertGreater(cost, 0)
        # The order of magnitude is the point: ~Rs 200/trade is what the
        # five-year replay was measured against.
        self.assertLess(cost, 500)

    def test_both_engines_charge_the_same_schedule(self):
        args = dict(entry_premium=22.15, exit_premium=22.05, quantity=65, lots=1, option_type="PE")
        self.assertEqual(paper_charges(**args), live_charges(**args))

    def test_a_cash_equity_leg_is_not_charged_option_rates(self):
        """The schedule is NSE's OPTION schedule. Charging it to an equity leg
        would invent a different wrong number instead of the missing one."""
        self.assertEqual(
            paper_charges(entry_premium=100.0, exit_premium=101.0, quantity=50, lots=1, option_type=None), 0.0
        )

    def test_a_bigger_premium_costs_more(self):
        small = paper_charges(entry_premium=20.0, exit_premium=20.0, quantity=65, lots=1, option_type="CE")
        large = paper_charges(entry_premium=400.0, exit_premium=400.0, quantity=65, lots=1, option_type="CE")
        self.assertGreater(large, small)

    def test_it_never_reports_zero_cost_by_accident(self):
        """A cost model that cannot answer must not read as 'free'. It returns
        0.0 only for the cases that really are outside the schedule."""
        self.assertGreater(paper_charges(entry_premium=1.0, exit_premium=1.0, quantity=65, lots=1, option_type="CE"), 0)


class ZeroCostWarningTests(unittest.TestCase):
    """A custom profile of zeros is free trading, and it used to say nothing.

    PE_NoTarget ran seven trades at spread 0 / slip 0 and its +Rs 13,975 was
    read as though the market had charged for them.
    """

    def test_the_engine_says_so_when_nothing_is_being_charged(self):
        import inspect

        import engine.paper_trading as pt

        source = inspect.getsource(pt)
        self.assertIn("ZERO execution costs", source)
        self.assertIn("self._spread_bps or self._entry_slippage_bps or self._exit_slippage_bps", source)


if __name__ == "__main__":
    unittest.main()


class RecordedPriceFallbackTests(unittest.TestCase):
    """Every live book must be able to price an exit it did not see live.

    `*_premium_lookup(broker, history)` serves a recent minute from Dhan's
    live quote and anything older from `history`. Three of the five live paths
    passed history=None, so an exit noticed more than a few minutes after its
    bar closed had NO price. Supertrend then floored it at intrinsic -- zero
    for anything out of the money -- and booked a Rs 14,501 loss on a leg
    worth about Rs 170 (2026-09-01). Gap Carry instead refused to exit at all,
    holding an overnight carry on nothing but a missing quote.
    """

    def _source(self):
        import inspect

        import app as app_module

        return inspect.getsource(app_module)

    def test_supertrend_live_paths_are_given_recorded_prices(self):
        src = self._source()
        self.assertIn("broker_client, await _supertrend_history_lookup(broker_client)", src)
        self.assertIn("_supertrend_premium_lookup(broker, await _supertrend_history_lookup(broker))", src)

    def test_gap_carry_live_paths_are_given_recorded_prices(self):
        src = self._source()
        self.assertIn("_gap_carry_premium_lookup(broker, await _gap_carry_history_lookup(broker))", src)
        self.assertIn("broker_client, await _gap_carry_history_lookup(broker_client)", src)

    def test_candle_entry_restore_is_given_recorded_prices(self):
        """The start route always built this; only the restore went without."""
        src = self._source()
        self.assertNotIn("_candle_entry_premium_lookup(broker, None)", src)
        self.assertIn("_candle_entry_restore_history", src)

    def test_none_of_them_still_hardcode_a_missing_history(self):
        src = self._source()
        for call in (
            "_supertrend_premium_lookup(broker)",
            "_supertrend_premium_lookup(broker_client)",
            "_gap_carry_premium_lookup(broker)",
            "_gap_carry_premium_lookup(broker_client)",
        ):
            with self.subTest(call=call):
                self.assertNotIn(f"{call},", src, f"{call} passes no history — an exit it cannot quote has no price")
