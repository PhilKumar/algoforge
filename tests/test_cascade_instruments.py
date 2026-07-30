import unittest
from datetime import date

from engine.cascade_instruments import (
    InstrumentError,
    expiry_rhythm,
    index_spec,
    instrument_facts,
)


class StubScripMaster:
    """Stands in for Dhan's scrip master with a realistic option chain."""

    def __init__(self, symbol="NIFTY", expiry="2026-08-04", step=50, lot=65, expiries=None, strikes=None):
        self.symbol = symbol
        self.expiry = expiry
        self.lot = lot
        self._expiries = expiries or ["2026-08-04", "2026-08-11", "2026-08-18"]
        if strikes is None:
            # A real chain: tight near the money, twice as wide at the wings.
            near = [24000 + step * i for i in range(20)]
            far = [near[-1] + step * 2 * i for i in range(1, 5)]
            strikes = near + far
        self._strikes = strikes

    def get_lot_size(self, symbol, expiry):
        return self.lot

    def get_strikes(self, symbol, expiry):
        return sorted(float(s) for s in self._strikes)

    def get_strike_step(self, symbol, expiry):
        strikes = self.get_strikes(symbol, expiry)
        gaps = [round(b - a, 4) for a, b in zip(strikes, strikes[1:]) if b > a]
        return max(set(gaps), key=gaps.count)

    def get_expiries(self, symbol):
        return list(self._expiries)


class IndexSpecTests(unittest.TestCase):
    def test_known_index_reports_its_candle_source(self):
        spec = index_spec("banknifty")
        self.assertEqual(spec.symbol, "BANKNIFTY")
        self.assertEqual(spec.exchange_segment, "IDX_I")

    def test_unknown_index_names_what_is_known(self):
        with self.assertRaises(InstrumentError) as caught:
            index_spec("NOSUCHINDEX")
        self.assertIn("NIFTY", str(caught.exception))

    def test_sensex_reaches_its_own_id_not_the_live_feed_one(self):
        # Confirmed against Dhan: id 51 returns SENSEX at ~77,600. The live
        # feed reaches SENSEX through id "1" in a different id space, and
        # asking the historical API for "1" returns a healthy ~23,000 series
        # for another index without erroring. Pin 51 so nobody "fixes" this
        # back to match the feed map.
        spec = index_spec("SENSEX")
        self.assertEqual(spec.security_id, "51")
        self.assertEqual(spec.exchange_segment, "IDX_I")

    def test_an_unverified_index_refuses_rather_than_guessing(self):
        # The guard that kept SENSEX honest until its id was confirmed. A
        # wrong id returns candles rather than an error, so an unverified
        # entry must never reach a fetch.
        from engine.cascade_instruments import INDEX_SPECS, IndexSpec

        unproven = IndexSpec("TESTIDX", "999", "IDX_I", verified=False, note="not confirmed against Dhan.")
        INDEX_SPECS["TESTIDX"] = unproven
        try:
            with self.assertRaises(InstrumentError) as caught:
                index_spec("TESTIDX")
            self.assertIn("not confirmed", str(caught.exception))
        finally:
            INDEX_SPECS.pop("TESTIDX", None)


class ExpiryRhythmTests(unittest.TestCase):
    def test_several_expiries_a_month_reads_as_weekly(self):
        weekly = [
            "2026-08-04",
            "2026-08-11",
            "2026-08-18",
            "2026-08-25",
            "2026-09-01",
            "2026-09-08",
            "2026-09-15",
            "2026-09-22",
            "2026-10-06",
        ]
        self.assertEqual(expiry_rhythm(weekly), "weekly")

    def test_one_expiry_a_month_reads_as_monthly(self):
        monthly = ["2026-07-28", "2026-08-25", "2026-09-29", "2026-10-27"]
        self.assertEqual(expiry_rhythm(monthly), "monthly")

    def test_too_few_dates_says_unknown_rather_than_guessing(self):
        self.assertEqual(expiry_rhythm(["2026-08-04", "2026-08-11"]), "unknown")
        self.assertEqual(expiry_rhythm([]), "unknown")


class InstrumentFactsTests(unittest.TestCase):
    def test_facts_come_from_the_scrip_master_not_a_table(self):
        stub = StubScripMaster(lot=65, step=50)
        facts = instrument_facts("NIFTY", "2026-08-04", scrip_master=stub)
        self.assertEqual(facts.lot_size, 65)
        self.assertEqual(facts.strike_step, 50.0)
        self.assertEqual(facts.security_id, "13")
        self.assertEqual(facts.expiry, date(2026, 8, 4))

    def test_a_different_index_gets_its_own_measured_ladder(self):
        # BankNifty's 100-point ladder and 30-unit lot are read from Dhan, so
        # nothing in the cascade needs to know they differ from NIFTY's.
        stub = StubScripMaster(lot=30, step=100)
        facts = instrument_facts("BANKNIFTY", "2026-08-25", scrip_master=stub)
        self.assertEqual(facts.lot_size, 30)
        self.assertEqual(facts.strike_step, 100.0)

    def test_wing_strikes_do_not_corrupt_the_measured_step(self):
        # The chain widens to 100 at the wings; the step is still 50.
        stub = StubScripMaster(step=50)
        self.assertEqual(instrument_facts("NIFTY", "2026-08-04", scrip_master=stub).strike_step, 50.0)

    def test_a_bad_lot_size_is_refused(self):
        stub = StubScripMaster(lot=0)
        with self.assertRaises(InstrumentError):
            instrument_facts("NIFTY", "2026-08-04", scrip_master=stub)

    def test_monthly_rhythm_is_reported_from_the_expiry_list(self):
        stub = StubScripMaster(
            lot=30,
            step=100,
            expiries=["2026-07-28", "2026-08-25", "2026-09-29", "2026-10-27"],
        )
        facts = instrument_facts("BANKNIFTY", "2026-08-25", scrip_master=stub)
        self.assertEqual(facts.rhythm, "monthly")


if __name__ == "__main__":
    unittest.main()
