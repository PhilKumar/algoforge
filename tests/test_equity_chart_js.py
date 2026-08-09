"""The equity curve's maths, run as the browser runs it.

The chart itself is Canvas and cannot be asserted here, but everything that
decides WHAT gets drawn is pure and worth pinning, because each of these was
wrong in the version this replaced and each one hid something:

  * ticks came straight off min/max, so the axis read ₹2,44,209 / ₹1,84,212
  * the series was smoothed with a Catmull-Rom spline at tension 0.5, which
    invents equity between trades and overshoots at every turn — one big
    winner came out as a gentle ramp instead of the single step it was
  * timestamps went unparsed, so x was the array index and a trade a day and
    a trade a quarter apart occupied the same width

The functions are extracted from static/philforge-app.js and run under Node,
not reimplemented — a reimplementation would happily agree with the bug.
"""

import json
import os
import shutil
import subprocess
import unittest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_JS = os.path.join(_HERE, "static", "philforge-app.js")
_NODE = shutil.which("node")

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');

function grab(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start === -1) throw new Error('missing ' + name);
  let i = src.indexOf('{', start), depth = 0;
  for (;; i++) { if (src[i] === '{') depth++; else if (src[i] === '}') { depth--; if (!depth) break; } }
  return src.slice(start, i + 1);
}
for (const fn of ['_pfEquityNiceStep', '_pfEquityAlpha', '_pfEquityParseTime', '_pfEquityDateLabel']) {
  eval(grab(fn));
}

const call = JSON.parse(process.argv[2]);
const fns = { _pfEquityNiceStep, _pfEquityAlpha, _pfEquityParseTime, _pfEquityDateLabel };
console.log(JSON.stringify(fns[call.fn].apply(null, call.args)));
"""


@unittest.skipIf(_NODE is None, "node is not installed")
class EquityChartHelperTests(unittest.TestCase):
    def call(self, fn, *args):
        proc = subprocess.run(
            [_NODE, "-e", _HARNESS, "--", _APP_JS, json.dumps({"fn": fn, "args": list(args)})],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            self.fail(f"node failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)

    def test_axis_steps_are_round_numbers(self):
        """₹2,44,209 is exact and unreadable; ₹2,50,000 is neither."""
        for rough, want in ((48000, 50000), (23000, 25000), (12000, 20000), (9000, 10000), (1.4, 2)):
            with self.subTest(rough=rough):
                self.assertEqual(self.call("_pfEquityNiceStep", rough), want)

    def test_a_degenerate_range_cannot_produce_a_zero_step(self):
        """A flat series would otherwise divide by zero and loop forever."""
        for rough in (0, -5, None):
            with self.subTest(rough=rough):
                self.assertEqual(self.call("_pfEquityNiceStep", rough), 1)

    def test_palette_colours_convert_to_translucent_fills(self):
        """The shared palette spells colours both ways, and the gradient needs
        an alpha on whichever it is handed."""
        self.assertEqual(self.call("_pfEquityAlpha", "#3fae56", 0.2), "rgba(63,174,86,0.2)")
        self.assertEqual(self.call("_pfEquityAlpha", "#fff", 0.5), "rgba(255,255,255,0.5)")
        self.assertEqual(self.call("_pfEquityAlpha", "rgba(148,163,184,0.55)", 0.1), "rgba(148,163,184,0.1)")

    def test_an_unreadable_colour_falls_back_instead_of_painting_nothing(self):
        self.assertEqual(self.call("_pfEquityAlpha", "not-a-colour", 0.3), "rgba(148,163,184,0.3)")

    def test_the_backtest_timestamp_parses(self):
        """The engine writes "YYYY-MM-DD HH:MM"; Date is inconsistent about the
        bare space, and an unparsed time silently costs the whole x-axis."""
        self.assertIsInstance(self.call("_pfEquityParseTime", "2024-10-09 11:50"), int)

    def test_a_missing_or_bad_timestamp_is_null_not_nan(self):
        """null makes the renderer fall back to even spacing. NaN would place
        every point at the same x and draw a vertical line."""
        for bad in ("rubbish", "", None):
            with self.subTest(bad=bad):
                self.assertIsNone(self.call("_pfEquityParseTime", bad))

    def test_date_labels_coarsen_as_the_window_widens(self):
        stamp = "2025-05-12 09:20"
        ms = self.call("_pfEquityParseTime", stamp)
        day = 86400000
        self.assertEqual(self.call("_pfEquityDateLabel", ms, 600 * day), "May '25")
        self.assertEqual(self.call("_pfEquityDateLabel", ms, 30 * day), "12 May")
        self.assertEqual(self.call("_pfEquityDateLabel", ms, 0), "12 May 09:20")


class EquityChartSourceTests(unittest.TestCase):
    """Two properties of the renderer that are cheap to assert and expensive to
    lose again."""

    def setUp(self):
        self.src = open(_APP_JS, encoding="utf-8").read()
        start = self.src.index("function renderEquityChart(")
        self.body = self.src[start : self.src.index("\nfunction _pfEquityHover", start)]

    def test_the_curve_is_not_smoothed(self):
        """Equity does not move between trades. A spline through it draws money
        that was never made, and at tension 0.5 it overshoots every turn."""
        self.assertNotIn("bezierCurveTo", self.body)
        self.assertNotIn("Catmull", self.body.replace("Catmull-Rom spline", ""))

    def test_the_observers_are_disconnected_on_teardown(self):
        """A ResizeObserver left watching a detached canvas is how the bench
        chart leaked one observer per refresh."""
        teardown = self.src[self.src.index("function _pfEquityTeardown(") :][:600]
        self.assertIn("ro.disconnect()", teardown)
        self.assertIn("themeObserver.disconnect()", teardown)
        self.assertIn("removeEventListener", teardown)


if __name__ == "__main__":
    unittest.main()
