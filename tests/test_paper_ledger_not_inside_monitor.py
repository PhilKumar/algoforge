"""The all-time ledger must outlive the campaign it records.

Each strategy's closed-campaign table used to be nested INSIDE that tab's
`ocp-monitor`, which the page hides whenever nothing is running. So the one
panel whose entire job is to survive the next auto mother vanished with it,
and a settled -Rs 52,598.92 campaign was invisible on any day it was not
running (Phil, 2026-08-26: "again dropped"). This pins the structure: the
ledger is a SIBLING of the monitor, and its visibility belongs to the ledger
fetch alone.
"""

import re
import unittest
from pathlib import Path

MARKUP = (Path(__file__).resolve().parent.parent / "strategy.html").read_text(encoding="utf-8")

LEDGERS = {
    "candle_entry": "oc-candle-closed",
    "gap_carry": "oc-gap-closed",
    "fib_boundary": "oc-fib-closed",
}
TAG = re.compile(r"<(/?)(?:div|section|details)\b")


def _ancestor_ids(needle: str) -> list[str]:
    """Every enclosing element id, innermost first, for the panel with `needle`."""
    lines = MARKUP.split("\n")
    start = next(i for i, line in enumerate(lines) if f'id="{needle}"' in line)
    depth, ancestors = 0, []
    for i in range(start - 1, -1, -1):
        line = lines[i]
        for closing in TAG.findall(line):
            depth += 1 if closing else -1
        if depth < 0 and TAG.search(line):
            found = re.search(r'id="([^"]+)"', line)
            ancestors.append(
                found.group(1) if found else re.search(r'class="([^"]*)"', line).group(1) if 'class="' in line else "?"
            )
            depth = 0
    return ancestors


class LedgerLivesOutsideTheMonitorTests(unittest.TestCase):
    def test_every_ledger_exists(self):
        for strategy, panel_id in LEDGERS.items():
            with self.subTest(strategy=strategy):
                self.assertIn(f'id="{panel_id}"', MARKUP)

    def test_no_ledger_is_nested_in_a_campaign_monitor(self):
        # The monitors are the elements the renderers set `hidden` on.
        monitors = {"oc-candle-monitor", "oc-gap-monitor", "oc-high-monitor"}
        for strategy, panel_id in LEDGERS.items():
            with self.subTest(strategy=strategy):
                nested = monitors.intersection(_ancestor_ids(panel_id))
                self.assertFalse(
                    nested,
                    f"{panel_id} sits inside {nested}, which is hidden whenever no campaign runs",
                )

    def test_each_ledger_sits_below_both_columns(self):
        """Not in either column: beneath the form AND the monitor, full width.

        Phil, 2026-08-26: "Closed paper rounds only must be under the form and
        monitor." Inside the live column it was the monitor's neighbour; the
        archive belongs under the whole strategy.
        """
        for strategy, panel_id in LEDGERS.items():
            with self.subTest(strategy=strategy):
                ancestors = _ancestor_ids(panel_id)
                self.assertNotIn("ocp-pane ocp-pane-live", ancestors)
                self.assertNotIn("ocp-panes", ancestors)
                # Still inside the strategy's own card, not loose on the page.
                self.assertIn("card", ancestors[0])
