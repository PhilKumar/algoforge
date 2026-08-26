"""Gap Carry leads the strategy tabs, and every tab shows its closed paper trades.

Phil, 2026-08-25: "Get the Gap carry to the first strategy before Fib boundary,
Recovery next and so on" and "The closed paper trades are not displayed... add
that to all the strategies".

Two of the four tabs already listed their settled rounds (Gap Carry keeps a
`history` table, Fib Boundary folds banked rounds back into its fills). The
other two dropped them: Candle Entry never rendered the `rounds` its own engine
reports, and High Entry returned early whenever the loop was not running, which
threw the entire book off the screen the moment Stop was pressed.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "strategy.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")

ORDER = ["gapcarry", "fib", "recovery", "candle", "bench"]


class TabOrderTests(unittest.TestCase):
    def test_the_tab_buttons_are_in_phils_order(self):
        found = re.findall(r'<button id="oc-tabbtn-([a-z]+)"', HTML)
        self.assertEqual(found, ORDER)

    def test_the_js_tab_list_matches_the_markup(self):
        self.assertIn(
            "const _OC_TABS = ['gapcarry', 'fib', 'recovery', 'candle', 'bench'];",
            APP_JS,
        )

    def test_gap_carry_is_the_one_tab_selected_on_arrival(self):
        active = re.findall(r'<button id="oc-tabbtn-([a-z]+)" class="oc-tab is-active"', HTML)
        self.assertEqual(active, ["gapcarry"])
        selected = re.findall(r'<button id="oc-tabbtn-([a-z]+)"[^>]*aria-selected="true"', HTML)
        self.assertEqual(selected, ["gapcarry"])

    def test_the_gap_carry_panel_is_the_one_visible_before_js_runs(self):
        """A panel left visible in the markup flashes the wrong tab on load."""
        for name in ORDER:
            m = re.search(rf'<div id="oc-tab-{name}" class="oc-tab-panel"[^>]*>', HTML)
            self.assertIsNotNone(m, name)
            hidden = "display:none" in m.group(0)
            self.assertEqual(hidden, name != "gapcarry", f"{name} visibility is wrong")


class ClosedPaperTradesTests(unittest.TestCase):
    def test_candle_entry_has_a_closed_rounds_table(self):
        self.assertIn('id="candle-entry-closed-rounds"', HTML)
        self.assertIn("Closed paper rounds", HTML)

    def test_the_closed_table_reads_the_ledger_not_the_live_engine(self):
        """The engine holds only the CURRENT campaign; the next mother wipes it.

        Reading `campaign.rounds` meant a settled trade survived exactly as long
        as its successor took to start -- which is how Phil's first live Candle
        Entry campaign vanished on 25 Aug 2026. The rows come from the archive.
        """
        self.assertIn("async function _refreshPaperLedger(strategy)", APP_JS)
        self.assertNotIn("function _renderCandleEntryClosedRounds", APP_JS)
        body = APP_JS.split("async function _refreshPaperLedger(strategy)")[1].split("\n}")[0]
        self.assertIn("/api/paper-campaigns/", body)
        self.assertIn("net_pnl", body)
        # A rebuilt row must never pass as a live capture.
        self.assertIn("rebuilt", body)

    def test_every_strategy_tab_has_a_closed_table_wired_to_the_ledger(self):
        for prefix in ("candle-entry", "fibx", "gap-carry"):
            self.assertIn(f'id="{prefix}-closed-rounds"', HTML, prefix)
            self.assertIn(f'id="{prefix}-closed-wrap"', HTML, prefix)
        for strategy in ("candle_entry", "fib_boundary", "gap_carry"):
            self.assertIn(f"_refreshPaperLedger('{strategy}')", APP_JS, strategy)

    def test_high_entry_keeps_its_book_after_the_run_is_stopped(self):
        body = APP_JS.split("function renderRecovery(data)")[1].split("\n}")[0]
        self.assertIn("if (!running && !campaigns.length) {", body)
        self.assertNotIn("if (!running) {", body, "a stopped run must not discard its closed trades")

    def test_the_two_tabs_that_already_listed_theirs_still_do(self):
        """Guards the pair I did not touch, so this change cannot quietly cost them."""
        self.assertIn("const history = Array.isArray(campaign.history) ? campaign.history : [];", APP_JS)
        self.assertIn("const rounds = Array.isArray(campaign.rounds) ? campaign.rounds : [];", APP_JS)


if __name__ == "__main__":
    unittest.main()
