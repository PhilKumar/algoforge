"""The chart toolbar is built once per host, not once per load.

`pfChartStrip` (static/philforge-bench-chart.js) creates a fresh div and
APPENDS it -- it never clears the host. So a caller that re-runs on every
chart load stacks a toolbar per load. High Entry did exactly that: opening the
chart, clicking a timeframe and hitting refresh each added a row, and Phil
sent a screenshot on 2026-09-02 with the strip four deep.

Every other caller on the site already guarded with `childElementCount`;
High Entry was the one that did not. This pins the RULE rather than the one
instance, so the next chart to grow a strip cannot repeat it.

Two callers are deliberately exempt: the scalp and live-entry overlays build a
brand new overlay element and query the host out of it, so the host is fresh
every time and there is nothing to stack onto.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")
BENCH_JS = (ROOT / "static" / "philforge-bench-chart.js").read_text(encoding="utf-8")

# How far back to look for the guard. The callers put it on the `if` directly
# above the call, sometimes with a comment in between.
_LOOKBACK_LINES = 8


class ChartStripBuiltOnceTests(unittest.TestCase):
    def test_pf_chart_strip_still_appends_without_clearing(self):
        """The premise. If this ever stops being true the guards are moot."""
        body = BENCH_JS[BENCH_JS.index("function pfChartStrip(") :][:2000]
        self.assertIn("document.createElement('div')", body)
        # No wholesale clear of the host anywhere in the builder.
        self.assertNotIn("host.innerHTML = ''", body)

    def test_every_persistent_host_guards_against_a_second_strip(self):
        lines = APP_JS.splitlines()
        unguarded = []
        for index, line in enumerate(lines):
            if "pfChartStrip(" not in line or "function pfChartStrip" in line:
                continue
            # Overlays rebuilt from scratch each time cannot stack.
            if "overlay.querySelector(" in line:
                continue
            window = "\n".join(lines[max(0, index - _LOOKBACK_LINES) : index + 1])
            if "childElementCount" not in window:
                unguarded.append(f"line {index + 1}: {line.strip()}")
        self.assertEqual(
            unguarded,
            [],
            "pfChartStrip appends without clearing, so a persistent host needs a "
            "childElementCount guard or the toolbar stacks one row per load:\n" + "\n".join(unguarded),
        )

    def test_high_entry_rebuilds_when_the_campaign_changes_its_stages(self):
        """Closing that overlay only HIDES it.

        A bare emptiness check would leave the previous campaign's timeframes
        on screen, so High Entry keys the strip on its stage list as well.
        """
        block = APP_JS[APP_JS.index("const strip = byId('oc-high-chart-strip');") :][:1200]
        self.assertIn("stripKey", block)
        self.assertIn("strip.dataset.stripKey", block)
        # It clears before rebuilding, or the rebuild is itself a second strip.
        self.assertIn("strip.innerHTML = ''", block)
        self.assertLess(
            block.index("strip.innerHTML = ''"),
            block.index("pfChartStrip(strip"),
            "the host must be cleared BEFORE the new strip is appended",
        )


if __name__ == "__main__":
    unittest.main()
