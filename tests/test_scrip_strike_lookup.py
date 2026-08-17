"""A float strike must find its contract.

Phil, 2026-08-17, market open, holding a NIFTY 24350 CE on a Fib Boundary
ladder: "Not able to kill the live Nifty trade". Kill answered 409 --
"Current option quote or broker exit unavailable" -- because the leg could not
be priced, and it could not be priced because the scrip lookup missed:

    [SCRIP] ⚠ Security ID not found: NIFTY_24350.0_2026-08-18_CE

The cache stores whole strikes without decimals ("24350"). Every Fib Boundary
fill carries a FLOAT strike, because `atm_strike` multiplies the step, so every
live quote for that ladder asked for "24350.0" and got nothing. The old ".0"
fallback only ever rescued a caller holding an int.

The same lookup sits under `place_order`, so this was never only a pricing
nuisance: a live order for a float strike had no security id either.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.dhan import ScripMaster  # noqa: E402


class StrikeKeyTests(unittest.TestCase):
    def test_a_whole_number_loses_its_decimals(self):
        self.assertEqual(ScripMaster.strike_key(24350.0), "24350")
        self.assertEqual(ScripMaster.strike_key(24350), "24350")
        self.assertEqual(ScripMaster.strike_key("24350.0"), "24350")

    def test_a_real_fraction_is_left_alone(self):
        """SENSEX and a few series do carry half strikes; do not round them away."""
        self.assertEqual(ScripMaster.strike_key(24350.5), "24350.5")

    def test_something_unparseable_is_passed_through_not_crashed(self):
        self.assertEqual(ScripMaster.strike_key("ATM"), "ATM")
        self.assertEqual(ScripMaster.strike_key(None), "None")


class LookupTests(unittest.TestCase):
    """Against the cache spelled exactly as the scrip master builds it."""

    def setUp(self):
        self._cache = ScripMaster._options_cache
        self._loaded = ScripMaster._loaded_date
        ScripMaster._options_cache = {"NIFTY_24350_2026-08-18_CE": "54321"}
        # Loaded today, so `ensure_loaded` does not go to the network.
        from datetime import datetime

        ScripMaster._loaded_date = datetime.now().strftime("%Y-%m-%d")

    def tearDown(self):
        ScripMaster._options_cache = self._cache
        ScripMaster._loaded_date = self._loaded

    def test_the_float_strike_that_could_not_be_killed_now_resolves(self):
        self.assertEqual(ScripMaster.lookup("NIFTY", 24350.0, "2026-08-18", "CE"), "54321")

    def test_an_int_strike_still_resolves(self):
        self.assertEqual(ScripMaster.lookup("NIFTY", 24350, "2026-08-18", "CE"), "54321")

    def test_a_cache_that_stores_the_decimal_form_is_still_reachable(self):
        ScripMaster._options_cache = {"NIFTY_24350.0_2026-08-18_CE": "99999"}
        self.assertEqual(ScripMaster.lookup("NIFTY", 24350.0, "2026-08-18", "CE"), "99999")
        self.assertEqual(ScripMaster.lookup("NIFTY", 24350, "2026-08-18", "CE"), "99999")

    def test_a_contract_that_really_is_absent_still_returns_nothing(self):
        self.assertEqual(ScripMaster.lookup("NIFTY", 24400.0, "2026-08-18", "CE"), "")


if __name__ == "__main__":
    unittest.main()
