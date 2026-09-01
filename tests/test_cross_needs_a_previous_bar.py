"""A cross that cannot be evaluated is not a cross.

`crosses_above` / `crosses_below` need the previous bar. When one was not
available they fell back to a plain `>` / `<` -- which answers a DIFFERENT
question, and answers it wrongly. "crossed above S1" became "is above S1", so
every level UNDER the current price reported a cross that never happened.

Seen live on 2026-09-01: a NIFTY 24300PE exit journal showed CPR_S1, CPR_S2
and CPR_S3 all crossed on one bar, with the index at 24,021 sitting above all
three (24,006 / 23,932 / 23,871) and never having dipped below any of them. A
genuine S1 cross gives ONE tick, not three -- which is what pins the fallback
as the cause. The same call decides the real exit, so that trade closed on a
rule nobody wrote.
"""

import os
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest import drain_cross_skips, eval_condition_group  # noqa: E402

# Another test in this suite repoints tempfile.tempdir at a scratch directory
# and then deletes it, so a bare TemporaryDirectory() fails for everything that
# runs afterwards. Naming the parent keeps these tests independent of run order.
_TMP_ROOT = "/tmp/philforge-engine-tests"


def _tmpdir():
    os.makedirs(_TMP_ROOT, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=_TMP_ROOT)


# The bar from the journal.
S1, S2, S3 = 24006.43, 23932.47, 23871.33
CURRENT = pd.Series({"close": 24021.2, "CPR_S1": S1, "CPR_S2": S2, "CPR_S3": S3})


def _cross(level: str, op: str = "crosses_above"):
    return [{"left": "current_close", "operator": op, "right": level}]


def _verdicts(prev, op="crosses_above"):
    drain_cross_skips()
    out = [bool(eval_condition_group(CURRENT, _cross(lv, op), prev)) for lv in ("CPR_S1", "CPR_S2", "CPR_S3")]
    return out, drain_cross_skips()


class ThePriceWasNeverBelowThoseLevels(unittest.TestCase):
    def test_no_previous_bar_is_not_a_cross(self):
        verdicts, skips = _verdicts(None)
        self.assertEqual(verdicts, [False, False, False], "a level test is not a cross")
        self.assertTrue(skips)
        self.assertIn("no previous bar", skips[0]["reason"])

    def test_a_previous_bar_missing_the_level_is_not_a_cross(self):
        verdicts, skips = _verdicts(pd.Series({"close": 24015.0}))
        self.assertEqual(verdicts, [False, False, False])
        self.assertIn("no right value", skips[0]["reason"])

    def test_a_nan_on_the_previous_bar_is_not_a_cross(self):
        prev = pd.Series({"close": float("nan"), "CPR_S1": S1, "CPR_S2": S2, "CPR_S3": S3})
        verdicts, skips = _verdicts(prev)
        self.assertEqual(verdicts, [False, False, False])
        self.assertIn("NaN", skips[0]["reason"])

    def test_crosses_below_gets_the_same_treatment(self):
        verdicts, skips = _verdicts(None, op="crosses_below")
        self.assertEqual(verdicts, [False, False, False])
        self.assertTrue(skips)


class ARealCrossStillFires(unittest.TestCase):
    def test_one_level_crossed_gives_exactly_one_tick(self):
        """The shape that proves the fix: dipping under S1 and closing back
        above it crosses S1 and NOTHING else."""
        prev = pd.Series({"close": 24000.0, "CPR_S1": S1, "CPR_S2": S2, "CPR_S3": S3})
        verdicts, skips = _verdicts(prev)
        self.assertEqual(verdicts, [True, False, False])
        self.assertEqual(skips, [], "a decidable cross records no diagnostic")

    def test_staying_above_the_level_is_not_a_cross(self):
        prev = pd.Series({"close": 24015.0, "CPR_S1": S1, "CPR_S2": S2, "CPR_S3": S3})
        verdicts, _ = _verdicts(prev)
        self.assertEqual(verdicts, [False, False, False])

    def test_crosses_below_fires_on_a_real_downward_cross(self):
        prev = pd.Series({"close": 24010.0, "CPR_S1": S1})
        row = pd.Series({"close": 24000.0, "CPR_S1": S1})
        self.assertTrue(eval_condition_group(row, _cross("CPR_S1", "crosses_below"), prev))
        self.assertEqual(drain_cross_skips(), [])

    def test_touching_the_level_exactly_counts_as_crossing_up(self):
        prev = pd.Series({"close": S1, "CPR_S1": S1})
        row = pd.Series({"close": S1 + 0.05, "CPR_S1": S1})
        self.assertTrue(eval_condition_group(row, _cross("CPR_S1"), prev))


class TheDiagnosticStaysBounded(unittest.TestCase):
    def test_notes_are_capped_and_drain_clears_them(self):
        for _ in range(500):
            eval_condition_group(CURRENT, _cross("CPR_S1"), None)
        first = drain_cross_skips()
        self.assertLessEqual(len(first), 64, "the recorder must not grow without limit")
        self.assertEqual(drain_cross_skips(), [], "draining clears")


class BothEnginesReportIt(unittest.TestCase):
    def test_engines_log_a_line_when_a_cross_cannot_be_decided(self):
        from engine.live import LiveEngine
        from engine.paper_trading import PaperTradingEngine

        for cls in (PaperTradingEngine, LiveEngine):
            with _tmpdir() as tmp:
                engine = cls(dhan=object(), run_id="x", state_dir=tmp)
                engine.event_log = []
                drain_cross_skips()
                eval_condition_group(CURRENT, _cross("CPR_S1"), None)
                engine._log_cross_skips("exit")
                messages = " ".join(e["message"] for e in engine.event_log)
                self.assertIn("could not be decided", messages, f"{cls.__name__} stayed silent")
                self.assertIn("no previous bar", messages)


if __name__ == "__main__":
    unittest.main()
