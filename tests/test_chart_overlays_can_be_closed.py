"""Every chart dialog must have a way out.

The campaign chart -- shared by the live chart and the frozen one a closed
campaign opens -- shipped with no close control, no Escape handler and no
backdrop click. Opening it was a one-way door (Phil, 2026-08-26: "Frozen chart
not having close button? - Not able to come out").
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKUP = (ROOT / "strategy.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")

# Every chart overlay on the options-cascade page, with the action that closes it.
OVERLAYS = {
    "oc-candle-chart-overlay": "closeCandleEntryChart",
    "oc-high-chart-overlay": "closeRecoveryChart",
}


class ChartOverlayExitTests(unittest.TestCase):
    def test_each_overlay_carries_a_close_control(self):
        for overlay_id, action in OVERLAYS.items():
            with self.subTest(overlay=overlay_id):
                start = MARKUP.find(f'id="{overlay_id}"')
                self.assertGreater(start, -1, f"{overlay_id} is missing")
                # The control must live inside the dialog, not somewhere later.
                block = MARKUP[start : start + 4000]
                self.assertIn(
                    f'data-pf-action="{action}"',
                    block,
                    f"{overlay_id} has no close button — it cannot be left",
                )

    def test_the_close_actions_are_allowlisted_and_defined(self):
        for action in OVERLAYS.values():
            with self.subTest(action=action):
                self.assertIn(f"'{action}'", SCRIPT, "not in the pf-action allowlist")
                self.assertIsNotNone(
                    re.search(rf"function {action}\s*\(", SCRIPT),
                    "allowlisted but never defined — the button would do nothing",
                )

    def test_escape_closes_an_open_chart_overlay(self):
        self.assertIn("pf-cascade-chart-overlay.is-open", SCRIPT)
        self.assertRegex(SCRIPT, r"event\.key !== 'Escape'[\s\S]{0,400}oc-candle-chart-overlay")

    def test_a_click_on_the_backdrop_closes_it(self):
        # `event.target !== overlay` is what keeps a click INSIDE the dialog
        # from closing it; without that line the chart would shut on any click.
        self.assertIn("event.target !== overlay", SCRIPT)


if __name__ == "__main__":
    unittest.main()
