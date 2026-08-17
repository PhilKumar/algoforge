"""A 3-second poll must not yank the Live panel's scroll back to the top.

Phil, 2026-08-17, trying to read the CPR rows of a 71-field table on the Live
page: "These scroll reset in 2/3 sec.. make it stay". `renderLivePanel` rebuilds
the whole panel with one `innerHTML` write on every status poll, and an
innerHTML write destroys every node -- with them, the scroll position.

The nodes cannot survive, so a scrolled box is remembered by its POSITION IN THE
TREE: the same template rebuilds the same shape, so the child-index path leads
back to the same element. This runs the real functions out of the shipped file
under Node against a rebuilt tree -- a reimplementation would happily agree with
a bug.
"""

import os
import shutil
import subprocess
import unittest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_JS = os.path.join(_HERE, "static", "philforge-app.js")
_NODE = shutil.which("node")

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const grab = (name) => {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`${name} is not in the shipped file`);
  let depth = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (!depth) return src.slice(i, k + 1); }
  }
};
eval(grab('_pfScrollPath')); eval(grab('_pfCaptureScroll')); eval(grab('_pfRestoreScroll'));

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.scrollTop = 0; this.scrollLeft = 0; this.parentElement = null; }
  add(child) { child.parentElement = this; this.children.push(child); return child; }
  querySelectorAll() { const out = []; (function walk(n) { n.children.forEach(c => { out.push(c); walk(c); }); })(this); return out; }
}
const build = () => {
  const root = new El('div');
  const col = root.add(new El('div'));
  const card = col.add(new El('div'));
  card.add(new El('table'));
  const scroller = card.add(new El('div'));
  const other = root.add(new El('div'));
  const events = other.add(new El('div'));
  return { root, scroller, events };
};

const before = build();
before.scroller.scrollTop = 420;
before.events.scrollTop = 96;
const saved = _pfCaptureScroll(before.root);

const after = build();                 // what innerHTML leaves: same shape, new nodes
const untouched = after.scroller.scrollTop;
_pfRestoreScroll(after.root, saved);

console.log(JSON.stringify({
  captured: saved.length,
  untouched,
  restored: after.scroller.scrollTop,
  restoredSecond: after.events.scrollTop,
}));
"""


@unittest.skipIf(_NODE is None, "node is not installed")
class LivePanelScrollTests(unittest.TestCase):
    def setUp(self):
        result = subprocess.run([_NODE, "-e", _HARNESS, _APP_JS], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json

        self.out = json.loads(result.stdout.strip().splitlines()[-1])

    def test_a_rebuilt_panel_gets_its_scroll_back(self):
        self.assertEqual(self.out["untouched"], 0, "the rebuilt tree really did start at the top")
        self.assertEqual(self.out["restored"], 420)

    def test_every_scrolled_box_is_remembered_not_just_the_first(self):
        self.assertEqual(self.out["captured"], 2)
        self.assertEqual(self.out["restoredSecond"], 96)


if __name__ == "__main__":
    unittest.main()
