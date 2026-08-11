"""Every data-pf-action button must actually be wired to something.

The click delegate fires only when BOTH are true:

    PF_DELEGATED_ACTIONS.has(action) && typeof window[action] === 'function'

so a button whose action is missing from the allowlist, or whose function was
never exported onto window, does nothing at all when clicked. No console error,
no failed request — the button is simply dead, and it looks identical to a
working one. That is the whole failure mode this pins.

Both halves are easy to forget because they live hundreds of lines apart from
the markup and from each other.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_APP_JS = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")
_SOURCES = {
    "strategy.html": (ROOT / "strategy.html").read_text(encoding="utf-8"),
    "static/philforge-app.js": _APP_JS,
}

_ACTION_RE = re.compile(r"""data-pf-action=\\?["']([A-Za-z0-9_]+)\\?["']""")
_WINDOW_EXPORT_RE = re.compile(r"^\s*window\.([A-Za-z0-9_]+)\s*=", re.MULTILINE)
# philforge-app.js is loaded as a CLASSIC script, not a module, so a function
# declared at the top level IS window.thatName — which is why most actions need
# no explicit export and only the ones nested inside a block do. Both count.
_TOP_LEVEL_FN_RE = re.compile(r"^(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\(", re.MULTILINE)


def _allowlisted() -> set:
    """The PF_DELEGATED_ACTIONS set literal, read from the source."""
    start = _APP_JS.index("PF_DELEGATED_ACTIONS")
    body = _APP_JS[start : _APP_JS.index("]", start)]
    return set(re.findall(r"['\"]([A-Za-z0-9_]+)['\"]", body))


class ActionWiringTests(unittest.TestCase):
    def setUp(self):
        self.allowed = _allowlisted()
        self.exported = set(_WINDOW_EXPORT_RE.findall(_APP_JS)) | set(_TOP_LEVEL_FN_RE.findall(_APP_JS))
        self.used = {(action, where) for where, text in _SOURCES.items() for action in _ACTION_RE.findall(text)}

    def test_the_allowlist_was_actually_found(self):
        """A parse that silently found nothing would pass every other test."""
        self.assertGreater(len(self.allowed), 20)
        self.assertIn("loadFibSpaceChart", self.allowed)

    def test_every_button_action_is_allowlisted(self):
        missing = sorted({f"{action} ({where})" for action, where in self.used if action not in self.allowed})
        self.assertEqual(missing, [], "not in PF_DELEGATED_ACTIONS — the click is ignored")

    def test_every_button_action_is_reachable_as_window_dot_name(self):
        missing = sorted({f"{action} ({where})" for action, where in self.used if action not in self.exported})
        self.assertEqual(missing, [], "no window.<name> to call — the click is ignored")

    def test_a_nested_function_is_not_mistaken_for_a_global(self):
        """The guard's own blind spot, pinned.

        Top-level declarations land on window; ones nested inside a block or
        another function do not, and those are exactly the ones that need an
        explicit window.<name> = line. Indented declarations must therefore NOT
        satisfy the export check on their own.
        """
        self.assertNotIn("_fsxLoadCampaignDetail", _TOP_LEVEL_FN_RE.findall("  async function _fsxNested() {"))
        self.assertEqual(_TOP_LEVEL_FN_RE.findall("  function indented() {"), [])
        self.assertEqual(_TOP_LEVEL_FN_RE.findall("function topLevel() {"), ["topLevel"])

    def test_the_delete_button_this_was_written_for_is_wired(self):
        self.assertIn("deleteFibSpaceCampaign", self.allowed)
        self.assertIn("deleteFibSpaceCampaign", self.exported)
        self.assertIn('data-pf-action="deleteFibSpaceCampaign"', _SOURCES["strategy.html"])


if __name__ == "__main__":
    unittest.main()
