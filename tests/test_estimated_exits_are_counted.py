"""An exit priced from the index is an ESTIMATE, and every book must say so.

Three of the five published books settle an exit they cannot find a print for
by computing intrinsic value off the index. That was documented as a floor --
"it UNDERSTATES the exit rather than inventing a price" -- and the claim is
false in one direction. Measured on Gap Carry, 2026-02-02: intrinsic said
952.25 while the contract was printing at 896.80, so the estimate invented
about Rs 3,600 of profit on a single trade. A deep in-the-money option is
illiquid and does trade below intrinsic.

It matters because the estimated trades are not marginal. Three of Gap Carry's
179 exits are estimated and they carry 46% of its net. Supertrend's put book
reports +Rs 36,371 and is -Rs 1,53,521 on printed prices alone.

So the rule these tests hold: a round settled on an estimate is FLAGGED, and
the flag reaches the report. A book may still publish the estimated number --
it just may not publish only that number.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PHILFORGE_PIN", "test-pin-not-real")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")

LADDER = (ROOT / "engine" / "fib_touch_ladder.py").read_text(encoding="utf-8")
SWEEP = (ROOT / "tools" / "fib_offline" / "fib_sweep.py").read_text(encoding="utf-8")
GAP_CARRY = (ROOT / "engine" / "gap_carry.py").read_text(encoding="utf-8")


class FibBoundaryCountsItsEstimates(unittest.TestCase):
    def test_a_round_records_whether_every_leg_sold_at_a_printed_price(self):
        self.assertIn('"exit_priced": self._floored_this_exit == 0', LADDER)
        self.assertIn('"floored_legs": self._floored_this_exit', LADDER)

    def test_the_counter_is_cleared_per_round_not_per_campaign(self):
        """A mother that banks twice must not carry the first round's estimate
        into the second round's honesty."""
        settle = LADDER[LADDER.index("def _settle(") :]
        settle = settle[: settle.index("def mark_open(")]
        self.assertIn("self._floored_this_exit = 0", settle)
        # Cleared AFTER the round is appended, or the flag it just stamped
        # would always read clean.
        self.assertLess(
            settle.index('"exit_priced"'),
            settle.index("self._floored_this_exit = 0"),
            "the round must be stamped before the counter is cleared",
        )

    def test_the_false_claim_about_intrinsic_is_gone(self):
        """The comment used to promise the estimate could only understate."""
        self.assertNotIn("UNDERSTATES the exit rather than inventing a price", LADDER)
        self.assertNotIn("understates profit", LADDER)

    def test_the_sweep_carries_the_split_into_the_report(self):
        self.assertIn('"priced_net"', SWEEP)
        self.assertIn('"floored_rounds"', SWEEP)
        self.assertIn('r.get("exit_priced")', SWEEP)


class GapCarryAlreadyFlagsItsEstimates(unittest.TestCase):
    def test_an_estimated_exit_is_marked_and_reasoned(self):
        """`exit_priced` is the flag the CSV's `priced` column is written from."""
        self.assertIn("exit_priced", GAP_CARRY)
        self.assertIn("MORNING_EXIT_AT_INTRINSIC", GAP_CARRY)

    def test_an_out_of_the_money_exit_with_no_quote_is_dropped_not_zeroed(self):
        """Booking it as zero would invent a total loss out of a missing tick."""
        self.assertIn("dropped, not zeroed", GAP_CARRY)


class CandleEntryIsNotAffected(unittest.TestCase):
    """It uses intrinsic ONLY at the contract's own expiry, where intrinsic is
    the exact settlement value rather than an estimate. Anything else it cannot
    price leaves the campaign unpriced and OUT of the book, which is already
    the honest treatment -- so it must not be 'fixed' into flooring."""

    def test_intrinsic_is_reached_only_at_expiry(self):
        source = (ROOT / "engine" / "cascade_options.py").read_text(encoding="utf-8")
        block = source[source.index("An option at its own expiry is worth intrinsic") :][:1200]
        self.assertIn("entry.contract.expiry <= candle.timestamp.date()", block)
        # The non-expiry branch appends None rather than an estimate.
        self.assertIn("prices.append(None)", block)


if __name__ == "__main__":
    unittest.main()
