"""Three complaints from one habit: a message that does not say what happened.

- The Gap Carry banner carried "A campaign is already running. Kill it first."
  beside a perfectly healthy state, for days (Phil, 2026-08-27: "Why?"). That
  409 exists to stop a PERSON starting a second campaign; the scheduler
  meeting it just means the night is already carried.
- Every High Entry chart failure read "Chart unavailable." because the handler
  looked for `data.detail` while this app wraps refusals as
  {error: {detail}} -- so the route's reason never reached the page.
- And a dialog with its own close was getting a second from the chart strip.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktZm9yLXVuaXQtdGVzdHMtMzJieXRlIQ==")

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "philforge-app.css").read_text(encoding="utf-8")
MARKUP = (ROOT / "strategy.html").read_text(encoding="utf-8")


class GapAutoNoteTests(unittest.TestCase):
    def test_a_held_carry_is_not_recorded_as_an_error(self):
        block = APP[APP.index("payload = SimpleNamespace(**_GAP_CARRY_AUTO_RULE") :][:1800]
        self.assertIn("if exc.status_code == 409:", block)
        self.assertIn('return "already-carried"', block)
        # and it must CLEAR any older complaint rather than leave it standing
        head = block[block.index("if exc.status_code == 409:") :][:400]
        self.assertIn('setting.pop("last_error", None)', head)

    def test_a_healthy_tick_clears_a_stale_complaint(self):
        import app as app_module

        self.assertTrue(hasattr(app_module, "_GAP_CARRY_AUTO_HEALTHY_STATES"))
        healthy = app_module._GAP_CARRY_AUTO_HEALTHY_STATES
        for state in ("waiting-for-clock", "holding", "entered", "already-carried"):
            with self.subTest(state=state):
                self.assertIn(state, healthy)
        # a genuine failure is NOT healthy and keeps its message
        self.assertNotIn("start-failed", healthy)
        self.assertNotIn("exit-failed", healthy)
        self.assertIn("_GAP_CARRY_AUTO_HEALTHY_STATES", APP.split("async def _gap_carry_auto_loop")[-1])


class ChartFailureTests(unittest.TestCase):
    def test_the_chart_reads_the_error_envelope(self):
        loader = SCRIPT[SCRIPT.index("async function loadRecoveryChart(") :][:2600]
        self.assertNotIn("data.detail || 'Chart unavailable.'", loader)
        self.assertIn("_apiErrorMessage(data,", loader)
        # a body that is not JSON must not become the error itself
        self.assertIn("res.json().catch(() => null)", loader)


class OneCloseTests(unittest.TestCase):
    def test_the_strip_close_stands_down_where_a_toolbar_has_one(self):
        self.assertIn('button[aria-label="Close chart"]:not([data-strip-close])', CSS)

    def test_every_strategy_chart_carries_its_own_close(self):
        for overlay in (
            "oc-fib-chart-overlay",
            "oc-gap-chart-overlay",
            "oc-candle-chart-overlay",
            "oc-high-chart-overlay",
        ):
            with self.subTest(overlay=overlay):
                start = MARKUP.index(f'id="{overlay}"')
                block = MARKUP[start : start + 3400]
                self.assertIn('aria-label="Close chart"', block, "no way out of this dialog")


if __name__ == "__main__":
    unittest.main()
