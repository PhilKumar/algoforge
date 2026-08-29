"""High Entry's replay: the same rules its Start button paper-trades.

Phil, 2026-08-29, on finding it was the one strategy that could not answer
"what would this have done": "Yes build the backtest for High Entry".

It was also the reason the Test Bench kept being asked to grow a fifth
strategy. The point of these tests is that the replay is not a second
implementation of the rule -- it builds the SAME `CandleRecoveryHost` the live
tab builds and calls the same replay its poll calls, and differs only in two
things that are about HISTORY rather than rules:

* prices come from the recorded archive, because a live chain cannot quote a
  contract that expired last March, and
* the lot size is the one listed on the mother's own date, because NIFTY has
  been 75, then 50, then 25, and pricing an old campaign at today's lot
  silently rewrites its money.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = pathlib.Path(__file__).resolve().parent.parent
APP = (_REPO / "app.py").read_text()
HTML = (_REPO / "strategy.html").read_text(encoding="utf-8")
JS = (_REPO / "static" / "philforge-app.js").read_text(encoding="utf-8")
CSS = (_REPO / "static" / "philforge-app.css").read_text(encoding="utf-8")


def _route_body(path: str, verb: str = "post") -> str:
    start = APP.index(f'@app.{verb}("{path}")')
    tail = APP[start:]
    nxt = re.search(r"\n@app\.(get|post|delete|put)\(", tail[10:])
    return tail[: nxt.start() + 10] if nxt else tail


class RouteTests(unittest.TestCase):
    def test_every_route_the_panel_calls_exists(self):
        for path, verb in [
            ("/api/recovery/backtest", "post"),
            ("/api/recovery/backtests/latest", "get"),
            ("/api/recovery/backtests/latest", "delete"),
        ]:
            self.assertIn(f'@app.{verb}("{path}")', APP, f"{verb.upper()} {path} is missing")

    def test_the_panel_only_calls_routes_that_exist(self):
        for path in set(re.findall(r"fetch\('(/api/recovery/[^'?]+)", JS)):
            self.assertIn(f'"{path}"', APP, f"the console calls {path}, which app.py does not serve")

    def test_it_is_registered_for_the_viewer_door(self):
        auth = (_REPO / "auth.py").read_text()
        self.assertIn('"/api/recovery/', auth, "the recovery prefix is not in the shared-read list")


class SameEngineTests(unittest.TestCase):
    """The whole value of the replay is that it is not a second rule."""

    BODY = _route_body("/api/recovery/backtest")

    def test_it_builds_the_live_host_rather_than_its_own(self):
        self.assertIn("_build_recovery_host(", self.BODY)

    def test_it_replays_through_the_hosts_own_named_mother_path(self):
        """`start_named_mother` is what the live '+ Run this mother' calls, and
        it replays through `_replay` -- the same function the poll runs."""
        self.assertIn("start_named_mother(", self.BODY)

    def test_it_never_reimplements_the_recovery_engine(self):
        for forbidden in ("TwoRedRecovery(", "FibZoneEntry(", "RecoveryConfig("):
            self.assertNotIn(
                forbidden,
                self.BODY,
                f"the replay constructs {forbidden} itself instead of letting the host do it",
            )

    def test_the_host_builder_defaults_to_the_live_quote(self):
        """A missing override must not silently give a live run recorded prices."""
        builder = APP[APP.index("def _build_recovery_host(") :]
        builder = builder[: builder.index("\nasync def ")]
        self.assertIn("_cascade_premium_lookup(broker) if premium_source is None else premium_source", builder)
        self.assertIn("if lot_size_override", builder)


class OfflineParityTests(unittest.TestCase):
    """The rule was proven offline before it had a button.

    `tools/candle_recovery_sweep.py` is where High Entry's book was measured, so
    a replay in the app that disagreed with it would make the published numbers
    unreachable from the screen. Both drive the same engine over the same
    window; these pin the two places that could silently diverge.
    """

    HOST = (_REPO / "engine" / "candle_recovery_host.py").read_text()
    SWEEP = (_REPO / "tools" / "candle_recovery_sweep.py").read_text()

    def test_both_pick_the_engine_the_same_way(self):
        for source, name in ((self.HOST, "host"), (self.SWEEP, "offline sweep")):
            self.assertIn(
                "FibZoneEntry if",
                source,
                f"the {name} no longer chooses between the two rules the same way",
            )
            self.assertIn("else TwoRedRecovery", source, name)

    def test_both_start_the_watch_after_the_mother_never_on_it(self):
        """Replaying the mother's own bar lets a campaign fill on a bar a live
        system had not been told about yet -- the lookahead the sweep calls out
        by name."""
        self.assertIn("b.timestamp > campaign.mother.timestamp", self.HOST)
        self.assertIn("row.timestamp > watch_from", self.SWEEP)


class HonestyTests(unittest.TestCase):
    BODY = _route_body("/api/recovery/backtest")

    def test_it_prices_from_the_record_not_a_live_chain(self):
        self.assertIn("_candle_entry_pricing", self.BODY)
        self.assertIn("premium_source=history", self.BODY)

    def test_no_recorded_history_is_refused_rather_than_replayed_at_zero(self):
        self.assertIn("if history is None:", self.BODY)
        self.assertIn("503", self.BODY)

    def test_the_lot_is_the_one_listed_on_the_mothers_own_date(self):
        self.assertIn("_nifty_lot_size_on(mother_timestamp.date())", self.BODY)

    def test_it_is_paper_only(self):
        self.assertIn("paper_only=True", self.BODY)

    def test_a_future_mother_is_refused(self):
        self.assertIn("Mother timestamp cannot be in the future", self.BODY)

    def test_the_panel_says_when_a_replay_was_not_fully_priced(self):
        """A replay missing a leg's premium is not a book, and the badge is the
        one place that difference is visible."""
        self.assertIn("premium_failures", self.BODY)
        self.assertIn("fully_priced", JS[JS.index("function _renderRecoveryBacktest") :][:2600])
        self.assertIn("not a book", JS[JS.index("function _renderRecoveryBacktest") :][:2600])


class PanelTests(unittest.TestCase):
    def test_the_tab_has_a_backtest_button_wired_three_ways(self):
        self.assertIn('id="oc-high-backtest-btn"', HTML)
        self.assertIn('data-pf-action="runRecoveryBacktest"', HTML)
        for action in ("runRecoveryBacktest", "deleteRecoveryBacktest"):
            self.assertIn(f"  '{action}',", JS, f"{action} is not in the delegated allowlist")
            self.assertIn(f"function {action}(", JS, f"{action} has no function")
            self.assertIn(f"window.{action} = {action};", JS, f"{action} is not exported")

    def test_every_class_the_panel_uses_is_a_real_one(self):
        """An invented class renders as bare text. This has happened before."""
        panel = HTML[HTML.index('<section id="oc-high-backtest"') :]
        panel = panel[: panel.index("</section>")]
        for cls in sorted({c for m in re.findall(r'class="([^"]+)"', panel) for c in m.split()}):
            self.assertRegex(CSS, rf"\.{re.escape(cls)}\b", f"{cls} is not defined in the stylesheet")

    def test_the_saved_replay_comes_back_with_the_tab(self):
        self.assertIn("_loadRecoveryBacktest()", JS)
        self.assertIn("tab === 'recovery'", JS)

    def test_start_and_replay_read_one_form(self):
        """Two readers of the same controls drift; one cannot."""
        self.assertIn("function _recoveryFormPayload()", JS)
        start = JS[JS.index("async function recoveryStart()") :][:800]
        self.assertIn("_recoveryFormPayload()", start)
        replay = JS[JS.index("async function runRecoveryBacktest()") :][:1200]
        self.assertIn("_recoveryFormPayload()", replay)


if __name__ == "__main__":
    unittest.main()
