"""The book poll must not rebuild DOM that has not changed.

The fib-space book polls every 3 seconds and rewrote the summary, the campaign
table and the open trade sheet on every tick, even though a paper run only
decides something once a minute at most. Each assignment destroys and rebuilds
those nodes, and the chart overlay sitting above them is glass
(`backdrop-filter: blur(8px)`), so the browser re-composites the blurred
backdrop every time. That is what "the chart is flickering" was: the chart
never redrew, the page underneath it did, three times a minute.

The guard is one function, so it is worth pinning that it really is a guard —
an `innerHTML` written unconditionally would pass any test that only checked
the final markup.

The function is extracted from static/philforge-app.js and run under Node, not
reimplemented: a reimplementation would happily agree with the bug.
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
// _fsxPaint leans on two neighbours: the held-paint map, and the probe that
// asks whether a chart overlay is up. The probe is grabbed from the real file
// too -- stubbing it here would let the guard rot without a test noticing.
// Only the browser globals it reads are stood up.
let _overlayOpen = false;
const document = {
  body: { classList: { contains: (name) => _overlayOpen && name === 'terminal-cascade-chart-open' } },
};
const _fsxHeldPaints = new Map();
eval(grab('_fsxChartOverlayOpen'));
eval(grab('_fsxPaint'));

// A node that counts how many times the markup was actually assigned. A real
// browser rebuilds its children on every assignment; the count is the thing
// that matters, not the final value.
function makeNode(initial) {
  const node = { writes: 0, _html: initial };
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._html; },
    set(value) { this.writes += 1; this._html = value; },
  });
  return node;
}

const call = JSON.parse(process.argv[2]);
_overlayOpen = !!call.overlayOpen;
const node = call.node === null ? null : makeNode(call.node);
for (const html of call.paints) _fsxPaint(node, html);
console.log(JSON.stringify(node === null ? {writes: 0, html: null} : {writes: node.writes, html: node.innerHTML}));
"""


@unittest.skipIf(_NODE is None, "node is not installed")
class FibSpaceRepaintTests(unittest.TestCase):
    def paint(self, initial, *paints, overlay_open=False):
        call = {"node": initial, "paints": list(paints), "overlayOpen": overlay_open}
        proc = subprocess.run(
            [_NODE, "-e", _HARNESS, "--", _APP_JS, json.dumps(call)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            self.fail(f"node failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)

    def test_an_unchanged_poll_does_not_touch_the_dom(self):
        """The flicker itself: same markup, three polls, zero rebuilds."""
        result = self.paint("<tr>same</tr>", "<tr>same</tr>", "<tr>same</tr>", "<tr>same</tr>")
        self.assertEqual(result["writes"], 0)

    def test_a_real_change_still_paints(self):
        """Guarding must not mean going stale — a fill has to show up."""
        result = self.paint("<tr>0 fills</tr>", "<tr>1 fill</tr>")
        self.assertEqual(result["writes"], 1)
        self.assertEqual(result["html"], "<tr>1 fill</tr>")

    def test_only_the_changed_poll_repaints(self):
        result = self.paint("a", "a", "a", "b", "b", "c")
        self.assertEqual(result["writes"], 2)
        self.assertEqual(result["html"], "c")

    def test_a_missing_element_is_not_an_error(self):
        """Every call site passes a getElementById result straight in."""
        self.assertEqual(self.paint(None, "anything")["writes"], 0)

    def test_an_open_chart_holds_the_paint(self):
        """The page under the glass must not move while a chart is being read."""
        result = self.paint("<tr>0 fills</tr>", "<tr>1 fill</tr>", overlay_open=True)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["html"], "<tr>0 fills</tr>")


if __name__ == "__main__":
    unittest.main()
