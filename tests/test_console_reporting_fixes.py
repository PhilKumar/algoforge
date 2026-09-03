"""Four things the consoles were reporting wrongly, from one review.

Phil, 2026-09-03, reading the strategy page:

  1. "Why no frozen chart is there on the Supertrend strategy closed trades"
  2. "The old trades has to be on the bottom and new has to be on the top"
  3. "Make some meaningful campaign events on the Fib boundary strategy"
  4. "Is the gap carry tearsheet and (i) updated? Seems like they are not?"

The fourth was two different things wearing one complaint: the ⓘ and the sheet
WERE updated and deployed (verified on the box), but the strategy-picker tile
and the two auto-carry status lines still said "09:20 out" -- so the page
contradicted itself, and the contradiction is what he saw.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as db_mod  # noqa: E402

APP = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")
HTML = (ROOT / "strategy.html").read_text(encoding="utf-8")


class ASupertrendCampaignCanBeDrawn(unittest.TestCase):
    """`has_chart` is SQL over the payload; JSON-inside-JSON matches nothing.

    `save_paper_campaign` serialises the payload itself. Supertrend handed it
    `json.dumps(...)` -- already a string -- so the stored text was a JSON
    string containing JSON, `json_extract(payload, '$.engine')` found nothing,
    `has_chart` came back 0, and the Chart button never rendered on a single
    Supertrend row. Every other strategy passed a dict.
    """

    def test_the_writer_passes_a_dict(self):
        i = APP.index('"supertrend",')
        block = APP[i : i + 3000]
        self.assertIn('"payload": {"engine": row, "config": dict(config or {})},', block)
        self.assertNotIn('"payload": json.dumps(', block)

    def test_an_already_encoded_payload_is_not_encoded_twice(self):
        once = db_mod._paper_campaign_payload({"engine": {"a": 1}})
        twice = db_mod._paper_campaign_payload(json.dumps({"engine": {"a": 1}}))
        self.assertIsInstance(json.loads(once), dict)
        self.assertIsInstance(json.loads(twice), dict, "a string payload must land as an object")
        self.assertEqual(json.loads(once), json.loads(twice))

    def test_the_engine_key_survives_for_json_extract(self):
        for value in ({"engine": {"a": 1}}, json.dumps({"engine": {"a": 1}})):
            self.assertIn("engine", json.loads(db_mod._paper_campaign_payload(value)))

    def test_an_empty_payload_is_still_an_object(self):
        self.assertEqual(json.loads(db_mod._paper_campaign_payload(None)), {})

    def test_text_that_is_not_json_is_left_alone(self):
        """Better a stored oddity than a crash on the archive path."""
        self.assertEqual(db_mod._paper_campaign_payload("not json"), "not json")


class TheNewestClosedCampaignIsOnTop(unittest.TestCase):
    """High Entry filtered and never sorted, so it showed oldest first."""

    def test_the_rows_are_ordered_newest_first(self):
        self.assertIn(".sort((a, b) => _endedAt(b) - _endedAt(a))", APP_JS)

    def test_it_orders_on_the_campaign_s_own_close(self):
        self.assertIn("const _endedAt = (c) => {", APP_JS)
        self.assertIn("trades[trades.length - 1].exit_time", APP_JS)

    def test_a_campaign_that_closed_nothing_falls_back_to_its_mother(self):
        self.assertIn("last || (c.mother && c.mother.timestamp)", APP_JS)

    def test_the_shared_ledger_was_already_right(self):
        """It is the SQL that orders that one; this was a second renderer."""
        sql = (ROOT / "db.py").read_text(encoding="utf-8")
        self.assertIn("ORDER BY COALESCE(closed_at, created_at) DESC", sql)


class TheCampaignEventsReadAsEnglish(unittest.TestCase):
    def test_every_event_the_ladder_emits_has_a_sentence(self):
        """A machine name on screen means the payload went unread."""
        for name in (
            "fib_drawn",
            "level_collected",
            "turn_armed",
            "turn_stop_moved",
            "fill",
            "trail_armed",
            "premium_missing",
            "resting_exit_unpriced",
            "mother_broken",
            "intraday_close",
            "expiry_exit",
            "killed",
        ):
            self.assertIn(f"case '{name}':", APP_JS, f"{name} still prints as a machine name")

    def test_the_numbers_in_the_payload_are_used(self):
        """The whole complaint: the data was already there and unread."""
        self.assertIn("Bought ${L}: ${e.lots} lot", APP_JS)
        self.assertIn("a ${n(e.span, 0)}-point span", APP_JS)
        self.assertIn("the buy stop moved down to ${n(e.stop, 2)}", APP_JS)

    def test_an_unknown_event_still_reads(self):
        self.assertIn("const name = String(e.event || 'event').replaceAll('_', ' ');", APP_JS)

    def test_a_repeated_condition_shows_its_count_not_a_repeated_row(self):
        self.assertIn("event.repeats > 1", APP_JS)

    def test_an_unpriced_exit_is_not_dressed_up_as_a_zero(self):
        self.assertIn("booked unpriced, not as zero", APP_JS)


class ThePageAgreesWithItselfAboutGapCarry(unittest.TestCase):
    """The ⓘ said one rule and the tile beside it said another."""

    def test_the_picker_tile_says_the_current_rule(self):
        self.assertIn("15:10 in · cut 09:15 · out 09:20", HTML)
        self.assertNotIn('<span class="oc-tab-sub">15:10 in · 09:20 out</span>', HTML)

    def test_the_auto_status_lines_say_it_too(self):
        self.assertEqual(APP_JS.count("a carry that opens down is cut at 09:15"), 2)
        self.assertNotIn("15:10 in, 09:20 out, every session", APP_JS)

    def test_the_heading_strip_says_it(self):
        self.assertIn("15:10 IN &middot; CUT 09:15 &middot; OUT 09:20", HTML)

    def test_the_info_doc_carries_the_measured_set(self):
        for figure in ("2,77,173", "50.8%", "PF 2.60", "17,965"):
            self.assertIn(figure, HTML, f"the ⓘ lost {figure}")

    def test_the_superseded_figures_are_gone(self):
        for stale in ("2,11,624", "PF 1.86"):
            self.assertNotIn(stale, HTML)


if __name__ == "__main__":
    unittest.main()
