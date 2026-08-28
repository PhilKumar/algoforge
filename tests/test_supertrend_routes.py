"""The Supertrend routes, checked as source facts.

Standing the whole app up needs a broker, so these assert the wiring that
silently rots instead: the live gate, the registrations a new strategy must
make in five separate places, and the fact that nothing here shares a key or a
registry with Gap Carry.
"""

from __future__ import annotations

import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (_REPO / "app.py").read_text()
AUTH = (_REPO / "auth.py").read_text()
HTML = (_REPO / "strategy.html").read_text(encoding="utf-8")
JS = (_REPO / "static" / "philforge-app.js").read_text(encoding="utf-8")


class RouteTests(unittest.TestCase):
    def test_every_route_the_panel_calls_exists(self):
        for path, verb in [
            ("/api/supertrend/paper/status", "get"),
            ("/api/supertrend/paper/start", "post"),
            ("/api/supertrend/paper/kill", "post"),
            ("/api/supertrend/backtest", "post"),
            ("/api/supertrend/backtests/latest", "get"),
            ("/api/supertrend/backtests/latest", "delete"),
            ("/api/supertrend/backtests/latest/chart", "get"),
            ("/api/supertrend/backtests/latest/export.csv", "get"),
            ("/api/supertrend/paper/chart", "get"),
            ("/api/supertrend/auto", "post"),
        ]:
            self.assertIn(f'@app.{verb}("{path}")', APP, f"{verb.upper()} {path} is missing")

    def test_the_panel_only_calls_routes_that_exist(self):
        called = set(re.findall(r"fetch\('(/api/supertrend/[^'?]+)", JS))
        for path in called:
            self.assertIn(f'"{path}"', APP, f"the console calls {path}, which app.py does not serve")


class LiveGateTests(unittest.TestCase):
    def test_live_is_refused_with_503_like_every_other_strategy(self):
        block = APP[APP.index("def _supertrend_trade_mode") : APP.index("def _supertrend_timeframe")]
        self.assertIn("_FIB_TOUCH_LIVE_EXECUTION_ENABLED", block)
        self.assertIn("status_code=503", block)

    def test_start_and_auto_both_pass_through_the_gate(self):
        start = APP[APP.index("async def supertrend_paper_start") : APP.index("async def supertrend_paper_kill")]
        self.assertIn("_supertrend_trade_mode(payload.mode)", start)
        auto = APP[APP.index("async def supertrend_auto(") : APP.index("async def _supertrend_auto_step")]
        self.assertIn("_supertrend_trade_mode(payload.mode)", auto)

    def test_the_panel_is_told_whether_live_is_available(self):
        status = APP[APP.index("async def supertrend_paper_status") : APP.index("async def supertrend_paper_start")]
        self.assertIn('"live_available"', status)


class WiringTests(unittest.TestCase):
    def test_it_is_registered_in_the_shared_paper_ledger(self):
        self.assertIn(
            '"supertrend"', APP[APP.index("_PAPER_LEDGER_STRATEGIES") : APP.index("_PAPER_LEDGER_STRATEGIES") + 200]
        )

    def test_the_auto_loop_starts_at_boot(self):
        block = APP[APP.index("def _ensure_auto_loops_running") : APP.index("def _ensure_auto_loops_running") + 900]
        self.assertIn('"supertrend": _run_supertrend_auto_loop', block)

    def test_open_campaigns_are_restored_saved_and_shut_down(self):
        self.assertIn("_restore_supertrend_open_state(user_id, broker_client, activate=True)", APP)
        self.assertIn("await _save_supertrend_open_state(owner_id, force=True)", APP)
        self.assertIn("Saved Supertrend campaign", APP)

    def test_the_save_state_route_labels_it_correctly(self):
        """A copy-pasted label made every strategy report as gap-carry once."""
        block = APP[APP.index("for owner_id in list(_supertrend_engines):") :][:300]
        self.assertIn('saved.append(f"supertrend:{owner_id}")', block)
        self.assertNotIn('saved.append(f"gap-carry:{owner_id}")', block)

    def test_viewers_may_read_it(self):
        self.assertIn('"/api/supertrend/",', AUTH)

    def test_nothing_is_shared_with_gap_carry(self):
        for key in ("supertrend_open:", "supertrend_auto:", "supertrend_backtest_latest:"):
            self.assertIn(key, APP)
        self.assertIn("_supertrend_engines: Dict[int, _CascadeRuntime] = {}", APP)


class PanelTests(unittest.TestCase):
    def test_the_tab_and_panel_ids_agree(self):
        self.assertIn('data-oc-tab="supertrend"', HTML)
        self.assertIn('id="oc-tab-supertrend"', HTML)
        self.assertIn("'supertrend'", JS[JS.index("const _OC_TABS") : JS.index("const _OC_TABS") + 120])

    def test_every_action_is_allowlisted_defined_and_exported(self):
        """Three separate registrations, or the button dies silently."""
        actions = sorted(set(re.findall(r'data-pf-action="(\w*[Ss]upertrend\w*)"', HTML)))
        self.assertTrue(actions, "the panel declares no supertrend actions")
        allowlist = JS[JS.index("PF_DELEGATED_ACTIONS") : JS.index("PF_DELEGATED_ACTIONS") + 4000]
        for action in actions:
            self.assertIn(f"'{action}'", allowlist, f"{action} is not in PF_DELEGATED_ACTIONS")
            self.assertIn(f"function {action}(", JS, f"{action} has no handler")
            self.assertIn(f"window.{action} = {action};", JS, f"{action} is not exported on window")

    def test_the_info_doc_carries_both_languages_equally(self):
        block = HTML[HTML.index('id="oc-st-info"') : HTML.index("ocp-confirmed-link", HTML.index('id="oc-st-info"'))]
        english = block[block.index('data-pf-lang="en"') : block.index('data-pf-lang="ta"')]
        tamil = block[block.index('data-pf-lang="ta"') :]
        self.assertEqual(english.count("<h5>"), tamil.count("<h5>"), "the two languages must mirror each other")
        for half in (english, tamil):
            self.assertEqual(half.count("pf-info-lede"), 1)
            self.assertEqual(half.count("pf-info-warn"), 1)

    def test_the_panel_states_the_decay_rather_than_burying_it(self):
        block = HTML[HTML.index('id="oc-st-info"') : HTML.index("ocp-confirmed-link", HTML.index('id="oc-st-info"'))]
        self.assertIn("2024", block)
        self.assertIn("24,000", block, "the recent run-rate belongs on the panel, not only in the tearsheet")

    def test_the_assets_pill_has_its_own_outline_colour(self):
        """Every tearsheet pill carries `--tearsheet-pill` in BOTH themes. A doc
        without one falls back to grey, and the strip stops saying which sheet
        is open at a glance -- the exact complaint that made the outline a
        border rather than a fill in the first place."""
        css = (_REPO / "static" / "philforge-app.css").read_text()
        docs = set(re.findall(r'data-doc="(\w+)"', HTML))
        for doc in docs:
            self.assertIn(
                f'.pf-tearsheet-doc[data-doc="{doc}"] {{ --tearsheet-pill:',
                css,
                f"the {doc} pill has no dark-theme outline colour",
            )
            self.assertIn(
                f'html[data-theme="light"] .pf-tearsheet-doc[data-doc="{doc}"] {{ --tearsheet-pill:',
                css,
                f"the {doc} pill has no light-theme outline colour",
            )

    def test_the_ledger_table_is_registered_for_the_archive(self):
        self.assertIn("supertrend: { wrap: 'oc-st-closed'", JS)

    def test_the_poll_includes_it(self):
        self.assertIn("refreshSupertrendStatus()", JS[JS.index("_fibBoundaryPollTimer = setInterval") :][:600])


if __name__ == "__main__":
    unittest.main()
