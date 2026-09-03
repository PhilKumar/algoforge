"""A Gap Carry already under water at 09:15 is sold there, not at 09:20.

Phil, 2026-09-03, asked what the book looks like sold at the opening minute
instead of 09:20. Sold wholesale it is WORSE: it wins overall only because of
2026 and loses in four of the six years. Split by what the market did
overnight, the reason is plain -- across 5.6 years of the archive, those five
minutes were worth:

    +Rs 1,068 per trade to a carry that opened DOWN   (it kept falling)
    -Rs   569 per trade to a carry that opened UP     (it kept rising)

Losers keep falling and winners keep rising, so cutting everything early throws
the winners away and holding everything gives the losers back. Cutting ONLY the
losers turns Rs 215,052 into Rs 277,173 over the same 179 trades, improving
five of the six years.

JUDGED ON THE PREMIUM, NOT THE INDEX. The index tested better (+Rs 77,991
against +Rs 62,121, and six years of six) but it is not reliably in hand at
09:15: the opening candle has not closed, so the engine's `last_index_close` is
still yesterday's and the test would read flat every morning. The premium is
fetched on the same tick that takes the decision.

OFF BY DEFAULT. This changes when real money is sold.
"""

import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.gap_carry import GapCarryConfig, GapCarryError, GapCarryPosition, SignalReading  # noqa: E402
from engine.gap_carry_paper import GapCarryPaper  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class TheRuleIsOffUntilAsked(unittest.TestCase):
    def test_a_fresh_config_sells_at_the_old_time(self):
        cfg = GapCarryConfig()
        self.assertFalse(cfg.cut_losers_at_open)
        self.assertEqual(cfg.exit_time, time(9, 20))

    def test_the_early_minute_defaults_to_the_open(self):
        self.assertEqual(GapCarryConfig().early_exit_time, time(9, 15))

    def test_an_early_exit_after_the_exit_is_refused(self):
        cfg = GapCarryConfig(early_exit_time=time(9, 25))
        with self.assertRaises(GapCarryError):
            cfg.validate()

    def test_an_early_exit_before_the_open_is_refused(self):
        cfg = GapCarryConfig(early_exit_time=time(8, 0))
        with self.assertRaises(GapCarryError):
            cfg.validate()


class TheLiveEngineCutsOnlyLosers(unittest.TestCase):
    """`mark()` is what moves real money; these are its four cases."""

    def _engine(self, *, enabled: bool, entry_premium: float = 200.0):
        engine = GapCarryPaper(GapCarryConfig(cut_losers_at_open=enabled))
        engine.position = _position(entry_premium)
        engine._status = "HOLDING"
        return engine

    def _mark(self, engine, hhmm, premium):
        now = datetime(2026, 9, 4, hhmm[0], hhmm[1], tzinfo=IST)
        engine.mark(now, premium=premium)
        return engine.position is None

    def test_a_losing_carry_is_cut_at_the_open(self):
        engine = self._engine(enabled=True)
        self.assertTrue(self._mark(engine, (9, 15), 150.0), "a carry below its cost must be sold at 09:15")
        self.assertEqual(engine.history[-1].exit_reason, "MORNING_CUT")

    def test_a_winning_carry_is_left_alone_at_the_open(self):
        engine = self._engine(enabled=True)
        self.assertFalse(self._mark(engine, (9, 15), 260.0), "a carry in profit must run to 09:20")

    def test_the_winner_still_sells_at_the_normal_time(self):
        engine = self._engine(enabled=True)
        self._mark(engine, (9, 15), 260.0)
        self.assertTrue(self._mark(engine, (9, 20), 255.0))
        self.assertEqual(engine.history[-1].exit_reason, "MORNING_EXIT")

    def test_with_the_rule_off_a_loser_is_carried_to_the_exit(self):
        engine = self._engine(enabled=False)
        self.assertFalse(self._mark(engine, (9, 15), 150.0), "nothing may change while the rule is off")
        self.assertTrue(self._mark(engine, (9, 20), 150.0))
        self.assertEqual(engine.history[-1].exit_reason, "MORNING_EXIT")

    def test_it_never_fires_on_the_day_the_carry_was_bought(self):
        """15:10 entry, and 09:15 the SAME afternoon is not a morning."""
        engine = self._engine(enabled=True)
        now = datetime(2026, 9, 3, 9, 15, tzinfo=IST)  # the session it was bought
        engine.mark(now, premium=150.0)
        self.assertIsNotNone(engine.position, "the cut belongs to the NEXT morning")

    def test_a_missing_quote_cuts_nothing(self):
        engine = self._engine(enabled=True)
        self.assertFalse(self._mark(engine, (9, 15), None))
        self.assertFalse(self._mark(engine, (9, 16), 0.0))


class TheRuleSurvivesARestart(unittest.TestCase):
    """A flag that is not saved is a rule that quietly reverts on deploy."""

    def _round_trip(self, enabled):
        engine = GapCarryPaper(GapCarryConfig(cut_losers_at_open=enabled, early_exit_time=time(9, 16)))
        return GapCarryPaper.from_dict(engine.to_dict())

    def test_on_stays_on(self):
        back = self._round_trip(True)
        self.assertTrue(back.config.cut_losers_at_open)
        self.assertEqual(back.config.early_exit_time, time(9, 16))

    def test_off_stays_off(self):
        self.assertFalse(self._round_trip(False).config.cut_losers_at_open)

    def test_an_old_saved_state_reads_as_off(self):
        """Every engine already on disk predates this flag."""
        engine = GapCarryPaper(GapCarryConfig())
        raw = engine.to_dict()
        raw["config"].pop("cut_losers_at_open", None)
        raw["config"].pop("early_exit_time", None)
        self.assertFalse(GapCarryPaper.from_dict(raw).config.cut_losers_at_open)

    def test_the_panel_can_see_which_rule_is_running(self):
        status = GapCarryPaper(GapCarryConfig(cut_losers_at_open=True)).get_status()
        self.assertTrue(status["rule"]["cut_losers_at_open"])
        self.assertEqual(status["rule"]["early_exit_time"], "09:15")


def _position(entry_premium: float) -> GapCarryPosition:
    """The engine's OWN position type, bought yesterday at 15:10.

    A hand-rolled stub was missing `is_open` and would have gone on missing
    whatever the engine grew next -- a test that passes because the fake is
    incomplete proves nothing about the code that runs.
    """
    return GapCarryPosition(
        session=date(2026, 9, 3),
        side="CE",
        strike=24000,
        expiry=date(2026, 9, 8),
        lot_size=65,
        lots=1,
        signal=SignalReading(
            timestamp=datetime(2026, 9, 3, 15, 10, tzinfo=IST),
            close=24000.0,
            ema=23900.0,
            rsi=72.0,
            side="CE",
            reason="test",
        ),
        entry_timestamp=datetime(2026, 9, 3, 15, 10, tzinfo=IST),
        entry_spot=24000.0,
        entry_premium=entry_premium,
    )


if __name__ == "__main__":
    unittest.main()
