"""Nothing in the page scripts may call a function that no longer exists.

Born 2026-08-16: removing the Fib Space tab deleted `_fsxFlushHeldPaints`, and
three chart-close handlers went on calling it -- so closing the chart on the
Live, Scalp and Terminal pages threw "ReferenceError: _fsxFlushHeldPaints is not
defined" and put a crash screen over a working app. The sweep meant to catch it
grepped for "fsx-" with a hyphen, which that name does not contain.

So: no call may survive a helper this app has retired. Comments, strings and
regex literals are stripped first, because a pattern that mentions a name is not
a call to it.
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = [
    os.path.join(_HERE, "static", "philforge-app.js"),
    os.path.join(_HERE, "static", "philforge-bench-chart.js"),
    os.path.join(_HERE, "static", "philforge-two-red.js"),
]

_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_STRINGS = re.compile(
    r"'(?:\\.|[^'\\\n])*'" r'|"(?:\\.|[^"\\\n])*"' r"|`(?:\\.|[^`\\])*`",
    re.S,
)
# A regex literal, recognised by what may legally precede one.
_REGEX = re.compile(r"(?<=[(,=:!&|?{;\[])\s*/(?![/*])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+/[gimsuy]*")

_CALL = re.compile(r"(?<![\w.$])(_[A-Za-z][\w$]*)\s*\(")
_DEF = re.compile(r"(?:function\s+|const\s+|let\s+|var\s+|window\.)(_[A-Za-z][\w$]*)")


def _strip_literals(text: str) -> str:
    """Comments, strings and regex literals are not code that calls anything."""
    text = _COMMENTS.sub(" ", text)
    text = _STRINGS.sub('""', text)
    return _REGEX.sub(" ", text)


class DeadCallTests(unittest.TestCase):
    def setUp(self):
        raw = ""
        for path in _SCRIPTS:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    raw += handle.read() + "\n"
        self.raw = raw
        self.source = _strip_literals(raw)

    def test_the_scripts_were_actually_read(self):
        """A parse that found nothing would pass every other test."""
        self.assertGreater(len(self.source), 100_000)
        self.assertIn("_pfChartCanvasDraw", self.source)

    def test_the_stripper_drops_what_is_not_code(self):
        self.assertNotIn("/*", self.source)
        self.assertNotIn("_copy", self.source)
        self.assertIn("_copy", self.raw, "and the raw file really does mention it")

    def test_no_call_survives_a_deleted_helper(self):
        """The precise form of the bug: a helper is deleted and its callers are
        not. Checked against the names this app has actually retired, because a
        blanket "every underscore name must be defined" reports 45 false alarms
        -- definitions written as object methods, destructured imports and
        assignments this regex cannot see -- and a guard that cries wolf is
        worse than no guard. Add a name here whenever one is removed.
        """
        retired = ("_fsx", "_renderFibSpace", "_fibSpace")
        called = set(_CALL.findall(self.source))
        for prefix in retired:
            offenders = sorted(name for name in called if name.startswith(prefix))
            self.assertEqual(offenders, [], f"{prefix}* was removed but is still called")

    def test_the_removed_fib_space_panel_left_nothing_behind(self):
        self.assertNotIn("_fsx", self.raw)
        self.assertNotIn("FibSpace", self.raw)
