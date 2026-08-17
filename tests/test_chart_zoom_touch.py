"""Chart zoom must be usable with a thumb, and reset must be findable.

Phil, 2026-08-17: "Chart zoom functions must be clear for charts in mobile."
Two things were wrong on a phone:

1. The − and + buttons were 24x20 CSS px, sitting in the plot's top-right
   corner — the same corner a thumb pans from. Both platforms ask for 44px.
2. RESET existed only as a double-click on the plot, announced in a `title`
   tooltip. A phone has no hover and no dependable double-click, so the
   function was there and could not be found. The percentage readout is now
   the reset button: the biggest, most obvious target in the cluster.

Desktop keeps the compact cluster — the chart toolbar is a settled standard
(see proj_philforge_chart_toolbar_standard) and this only adds a touch tier.
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_HERE, "static", "philforge-bench-chart.js")
_CSS = os.path.join(_HERE, "static", "philforge-app.css")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ZoomResetControlTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(_JS)

    def test_the_readout_is_a_real_button_that_resets(self):
        host = self.js.split("function _pfBenchChartHostHtml", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("data-bench-zoom-reset", host)
        self.assertIn('aria-label="Reset zoom to fit"', host)
        # Still the same readout hook, so every zoom updates it as before.
        self.assertIn("data-bench-zoom-level", host)
        self.assertNotRegex(host, r"<span[^>]*data-bench-zoom-level", "the readout must be a button, not dead text")

    def test_the_cluster_click_handler_wires_reset(self):
        handler = self.js.split("zoomBox.addEventListener('click'", 1)[1].split("});", 1)[0]
        self.assertIn("[data-bench-zoom-reset]", handler)
        # reset=true is the fit call; a factor would merely zoom by a step.
        self.assertRegex(handler, r"_pfChartCanvasZoom\(0,\s*true\)")

    def test_every_control_in_the_cluster_is_a_button(self):
        host = self.js.split("function _pfBenchChartHostHtml", 1)[1].split("\nfunction ", 1)[0]
        cluster = host.split("pf-bench-zoom", 1)[1]
        self.assertEqual(cluster.count("<button"), 3, "zoom out, reset, zoom in")


class ZoomTouchTargetTests(unittest.TestCase):
    def setUp(self):
        self.css = _read(_CSS)

    def test_a_touch_tier_exists_for_the_zoom_cluster(self):
        self.assertIn("(pointer: coarse)", self.css)
        block = self.css.split("@media (pointer: coarse), (max-width: 620px) {", 1)
        self.assertEqual(len(block), 2, "the coarse-pointer tier for .pf-bench-zoom is missing")
        self.tier = block[1].split("\n  }\n", 1)[0]

    def test_the_buttons_reach_44px_under_a_thumb(self):
        tier = self.css.split("@media (pointer: coarse), (max-width: 620px) {", 1)[1]
        button_rule = tier.split(".pf-bench-zoom button {", 1)[1].split("}", 1)[0]
        for prop in ("min-width", "min-height"):
            size = re.search(rf"{prop}:\s*(\d+)px", button_rule)
            self.assertIsNotNone(size, f"{prop} is not set on the touch tier")
            self.assertGreaterEqual(int(size.group(1)), 44, f"{prop} is below the 44px both platforms ask for")

    def test_desktop_keeps_the_compact_cluster(self):
        """The tier ADDS to the base rule; it must not rewrite it."""
        base = self.css.split(".pf-bench-zoom button {", 1)[1].split("}", 1)[0]
        self.assertIn("min-width: 24px", base)
        self.assertNotIn("44px", base)


if __name__ == "__main__":
    unittest.main()
