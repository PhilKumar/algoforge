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

ORDER = ["gapcarry", "fib", "recovery", "candle", "supertrend", "bench"]


class TabOrderTests(unittest.TestCase):
    def test_the_tab_buttons_are_in_phils_order(self):
        found = re.findall(r'<button id="oc-tabbtn-([a-z]+)"', HTML)
        self.assertEqual(found, ORDER)

    def test_the_js_tab_list_matches_the_markup(self):
        """Spelled out of ORDER, so a sixth strategy is one edit here, not two.

        The list the JS iterates and the buttons in the markup are separate
        sources of truth; a tab present in one and absent from the other shows
        up as a button that selects nothing."""
        expected = ", ".join(f"'{name}'" for name in ORDER)
        self.assertIn(f"const _OC_TABS = [{expected}];", APP_JS)

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


class HeroCopyTests(unittest.TestCase):
    """The page header describes what the tabs beneath it do, so adding a tab
    can make it lie. Phil, 2026-08-29, on seeing Supertrend land: "So now this
    cascade becomes false correct?" -- it did, and Gap Carry had already broken
    the same sentence before that.
    """

    HERO = HTML[HTML.index('class="oc-hero-copy"') : HTML.index('id="options-cascade-live-gate"')]

    def _bench_strategies(self):
        """What Test Bench can actually replay, read off its own selector."""
        panel = re.search(r'<div id="oc-tab-bench".*', HTML, re.S).group(0)
        selector = re.search(r'<select[^>]*id="bench-strategy"[^>]*>(.*?)</select>', panel, re.S)
        if selector is None:  # fall back to the first strategy-shaped select on the tab
            selector = re.search(r'<option value="fib".*?</select>', panel, re.S)
            return set(re.findall(r'<option value="(\w+)"', selector.group(0))) if selector else set()
        return set(re.findall(r'<option value="(\w+)"', selector.group(1)))

    def test_the_hero_does_not_promise_a_replay_the_bench_cannot_run(self):
        """Test Bench replays two rules, and only one of them is a tab on this
        page. Phil, 2026-08-29: "This is not true... It has only 2 strategies".
        A blanket "past mothers replay in Test Bench" is wrong for the other
        mother-taking tabs, whose rules the bench has never been able to run."""
        bench = self._bench_strategies()
        wants, _ = self._tabs_wanting_a_mother()
        if not bench >= wants:
            for blanket in ("Past mothers replay in", "past mother instead"):
                self.assertNotIn(
                    blanket,
                    self.HERO,
                    f"the header sends every mother to Test Bench, which only replays {sorted(bench)}",
                )

    def _tabs_wanting_a_mother(self):
        wants, all_tabs = set(), [t for t in ORDER if t != "bench"]
        for name in all_tabs:
            panel = re.search(rf'<div id="oc-tab-{name}".*?(?=<div id="oc-tab-|<div id="oc-chart-overlay)', HTML, re.S)
            if panel and 'type="datetime-local"' in panel.group(0):
                wants.add(name)
        return wants, set(all_tabs)

    def test_it_does_not_claim_every_strategy_takes_a_mother(self):
        wants, every = self._tabs_wanting_a_mother()
        if wants != every:
            missing = ", ".join(sorted(every - wants))
            self.assertNotIn(
                "Every strategy takes a mother candle",
                self.HERO,
                f"the header says every strategy takes a mother, but {missing} do not",
            )

    def test_step_three_does_not_quote_one_tab_s_button(self):
        """Each strategy words its own Start button, so naming one of them in the
        steps is wrong on the other four."""
        labels = set()
        for bid in ("oc-gap-start", "oc-fib-start", "oc-high-start", "oc-candle-start", "oc-st-start"):
            m = re.search(rf'id="{bid}".*?>(.*?)</button>', HTML, re.S)
            if m:
                labels.add(re.sub(r"&#\d+;|\s+", " ", m.group(1)).strip())
        for label in labels:
            if len(labels) > 1:
                self.assertNotIn(
                    f"<strong>{label}</strong>",
                    self.HERO,
                    f"the steps quote {label!r}, which only one of the five buttons says",
                )


class ClosedPaperTradesTests(unittest.TestCase):
    def test_candle_entry_has_a_closed_campaigns_table(self):
        self.assertIn('id="oc-candle-closed-rows"', HTML)
        self.assertIn("Closed paper campaigns", HTML)

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
        for prefix in ("oc-candle", "oc-fib", "oc-gap"):
            self.assertIn(f'id="{prefix}-closed-rows"', HTML, prefix)
            self.assertIn(f'id="{prefix}-closed"', HTML, prefix)
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
