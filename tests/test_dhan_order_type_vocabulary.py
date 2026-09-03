"""An order type Dhan does not know is caught here, not at the broker.

Phil, 2026-09-03 09:20 IST, from the live log:

    [ENTRY] ✅ Entry succeeded on attempt 1
    [ERROR] CRITICAL: SL order FAILED for Leg 1 (Async order placement failed
    (400): Missing required fields, bad values for parameters etc.)
    - the position is UNPROTECTED at the broker. Place the stop manually.

The entry filled -- NIFTY 23750CE, 130 qty at Rs 294.73 -- and the stop meant
to guard it was rejected. The position ran unprotected for eighty-five minutes
until the strategy's own exit signal closed it at 10:45 (Rs 282.38, a realised
loss of Rs 1,723.80). Had it gapped instead, nothing at the broker would have
stopped it.

The cause was vocabulary. Dhan v2 accepts exactly four order types, and
`app.py`'s manual-order route has validated against that set all along:

    app.py:4611  _ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"}

The automated stop path sent "SL" -- the word most brokers use -- straight
into the payload, and Dhan answered DH-905 for every one. In thirty days of
logs that was the FIRST stop this box ever attempted, which is why nothing had
caught it: the vocabulary was never right, it had simply never been exercised.

Three engines pass "SL" (engine/live.py, engine/fib_touch_ladder.py,
engine/cascade_equity_live.py), so the translation belongs in the payload
builders they share, not in whichever one is being debugged today.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broker.dhan import _DHAN_ORDER_TYPES, _dhan_order_type  # noqa: E402


class TheBrokersOwnVocabulary(unittest.TestCase):
    def test_the_four_dhan_accepts(self):
        self.assertEqual(set(_DHAN_ORDER_TYPES), {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"})

    def test_it_agrees_with_the_route_that_already_validated(self):
        """Two sets for one fact is how the automated path drifted off."""
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('_ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"}', app)

    def test_the_word_that_cost_the_position(self):
        self.assertEqual(_dhan_order_type("SL"), "STOP_LOSS")

    def test_the_market_stop_alias(self):
        for spelling in ("SL-M", "SLM", "SL_M"):
            self.assertEqual(_dhan_order_type(spelling), "STOP_LOSS_MARKET", spelling)

    def test_dhans_own_names_pass_through_unchanged(self):
        for name in _DHAN_ORDER_TYPES:
            self.assertEqual(_dhan_order_type(name), name)

    def test_case_and_padding_do_not_decide_whether_a_stop_exists(self):
        self.assertEqual(_dhan_order_type("  sl  "), "STOP_LOSS")

    def test_an_unknown_type_fails_here_rather_than_at_the_broker(self):
        """A 400 from Dhan is generic; this names the value."""
        for bad in ("BRACKET", "SL-X", "", None):
            with self.assertRaises(ValueError) as caught:
                _dhan_order_type(bad)
            self.assertIn("orderType", str(caught.exception))


class EveryOrderPayloadIsTranslated(unittest.TestCase):
    """Six payloads reach Dhan; a raw `order_type` in any of them is the bug."""

    def _source(self) -> str:
        return (ROOT / "broker" / "dhan.py").read_text(encoding="utf-8")

    def test_no_payload_passes_the_callers_word_through_untranslated(self):
        src = self._source()
        self.assertNotIn('"orderType": order_type,', src)
        self.assertNotIn('payload["orderType"] = order_type\n', src)

    def test_all_six_go_through_the_translation(self):
        """Counted as PAYLOAD writes; the two SL-M price checks call it too."""
        src = self._source()
        payload_writes = src.count('"orderType": _dhan_order_type(order_type),') + src.count(
            'payload["orderType"] = _dhan_order_type(order_type)'
        )
        self.assertEqual(payload_writes, 6)

    def test_the_market_stop_price_check_reads_the_translated_name(self):
        """Comparing the caller's spelling missed Dhan's own."""
        src = self._source()
        self.assertNotIn('if order_type == "SL-M":', src)
        self.assertEqual(src.count('if _dhan_order_type(order_type) == "STOP_LOSS_MARKET":'), 2)


class TheEnginesThatPassSL(unittest.TestCase):
    """Named so a future reader sees why this could not be fixed at one site."""

    def test_the_three_callers_still_speak_their_own_word(self):
        for rel in ("engine/live.py", "engine/fib_touch_ladder.py", "engine/cascade_equity_live.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn('"SL"', src, f"{rel} no longer passes SL — check the translation is still needed")


if __name__ == "__main__":
    unittest.main()
