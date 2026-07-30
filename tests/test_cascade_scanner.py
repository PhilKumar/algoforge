"""Tests for the Cascade instrument scanner."""

import unittest

from engine.cascade_scanner import ScanInput, rungs_fundable, scan


def row(symbol, *, last, strength_from=None, high=None, sessions=80, etf=False):
    """A synthetic history that ends at `last` and peaked at `high`."""
    start = strength_from if strength_from is not None else last
    closes = [start + (last - start) * i / (sessions - 1) for i in range(sessions)]
    highs = list(closes)
    if high is not None:
        highs[-10] = high
    return ScanInput(symbol=symbol, name=symbol, closes=closes, highs=highs, last_price=last, etf=etf)


class ScannerTests(unittest.TestCase):
    def test_cheap_scrips_are_dropped(self):
        found, rejected = scan([row("PENNY", last=150.0, strength_from=100.0, high=180.0)], capital_inr=10_000)
        self.assertEqual(found, [])
        self.assertIn("below Rs 200", rejected[0].reason)

    def test_bees_etfs_skip_the_min_price_gate(self):
        """GOLDBEES trades near Rs 80; the Rs 200 floor is a stock heuristic."""
        found, _ = scan([row("GOLDBEES", last=80.0, strength_from=64.0, high=90.0, etf=True)], capital_inr=100_000)
        self.assertEqual([c.symbol for c in found], ["GOLDBEES"])
        self.assertTrue(found[0].etf)

    def test_an_etf_still_needs_a_trend_and_a_discount(self):
        found, rejected = scan(
            [row("FLATBEES", last=80.0, strength_from=100.0, high=90.0, etf=True)], capital_inr=100_000
        )
        self.assertEqual(found, [])
        self.assertIn("trend is", rejected[0].reason)

    def test_a_falling_trend_is_dropped_however_deep_the_discount(self):
        """The Cascade bets the trend survives; a knife satisfies every rule."""
        found, rejected = scan([row("WEAK", last=400.0, strength_from=800.0, high=460.0)], capital_inr=100_000)
        self.assertEqual(found, [])
        self.assertIn("trend is", rejected[0].reason)

    def test_a_name_at_its_high_offers_no_discount_yet(self):
        found, rejected = scan([row("TOPPED", last=500.0, strength_from=400.0, high=501.0)], capital_inr=100_000)
        self.assertEqual(found, [])
        self.assertIn("no discount yet", rejected[0].reason)

    def test_too_deep_a_fall_is_treated_as_a_broken_trend(self):
        found, rejected = scan(
            [row("BROKEN", last=400.0, strength_from=390.0, high=800.0)],
            capital_inr=100_000,
        )
        self.assertEqual(found, [])
        self.assertIn("trend likely broken", rejected[0].reason)

    def test_deeper_discount_ranks_higher_at_equal_strength(self):
        found, _ = scan(
            [
                row("SHALLOW", last=500.0, strength_from=400.0, high=525.0),
                row("DEEPER", last=500.0, strength_from=400.0, high=560.0),
            ],
            capital_inr=100_000,
        )
        self.assertEqual([c.symbol for c in found], ["DEEPER", "SHALLOW"])

    def test_unaffordable_names_are_dropped_not_ranked(self):
        found, rejected = scan([row("PRICEY", last=3000.0, strength_from=2400.0, high=3300.0)], capital_inr=1_000)
        self.assertEqual(found, [])
        self.assertIn("does not buy one share", rejected[0].reason)

    def test_small_capital_reports_how_few_rungs_can_actually_fire(self):
        """With Rs 1,000 and a Rs 250 share, most rungs cannot reach one share."""
        found, _ = scan([row("MIDCAP", last=250.0, strength_from=200.0, high=280.0)], capital_inr=1_000)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].affordable_shares, 4)
        # 20% of 1,000 is 200 -- under one share. Only the 30% and 50% slices reach it.
        self.assertEqual(found[0].rungs_fundable, 2)

    def test_fundability_scales_the_score(self):
        cheap, _ = scan([row("A", last=250.0, strength_from=200.0, high=280.0)], capital_inr=100_000)
        tight, _ = scan([row("A", last=250.0, strength_from=200.0, high=280.0)], capital_inr=1_000)
        self.assertEqual(cheap[0].rungs_fundable, 3)
        self.assertGreater(cheap[0].score, tight[0].score)

    def test_rungs_fundable_is_measured_against_the_engines_own_split(self):
        self.assertEqual(rungs_fundable(1_000, 250.0), 2)  # 200 no, 300 yes, 500 yes
        self.assertEqual(rungs_fundable(1_000, 600.0), 0)
        self.assertEqual(rungs_fundable(10_000, 250.0), 3)

    def test_the_shortlist_is_capped(self):
        rows = [row(f"S{i}", last=500.0 + i, strength_from=400.0, high=560.0 + i) for i in range(40)]
        found, _ = scan(rows, capital_inr=1_000_000, limit=25)
        self.assertEqual(len(found), 25)


if __name__ == "__main__":
    unittest.main()
