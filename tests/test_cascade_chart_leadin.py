"""The campaign chart must show candles BEFORE the mother.

Phil, 2026-08-12, on the KALYANKJIL campaign chart: *"Is this the way you
render the chart with the mother candle? At the low? Chart standards not
followed again."* The mother sat on the chart's left edge with nothing before
it -- and a mother with no run-up behind it cannot be judged at all: the same
picture is drawn whether the candle was a real high or a bar in the middle of
a climb.

The standard was already set once, on the scanner (CHART_LEAD_BARS, after
*"I need candles at least from that high and some candles before as well"*).
These tests hold the CAMPAIGN chart to it too, and pin the boundary that
matters: the lead is picture only -- the replay must never see a pre-mother
candle, because feeding one would let the geometry anchor on history the
campaign never owned.
"""

import base64
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-test-chart-leadin.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-test-chart-leadin-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
# Computed, not a literal, so the secret scanner doesn't flag a dummy test key.
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

import app as app_module  # noqa: E402
from engine.cascade_equity import CashCascadeInstrument, CashCascadePaperConfig, CashCascadePaperEngine  # noqa: E402
from engine.cascade_options import IndexCandle  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _session_candles(first_day: date, sessions: int) -> list[IndexCandle]:
    """15m NSE bars, mildly falling so the engine has an ordinary tape."""
    rows: list[IndexCandle] = []
    price = 600.0
    for day in range(sessions):
        start = datetime(first_day.year, first_day.month, first_day.day, 9, 15, tzinfo=IST) + timedelta(days=day)
        for slot in range(25):
            open_ = price
            close = price - 0.3
            rows.append(IndexCandle(start + timedelta(minutes=15 * slot), open_, open_ + 1.0, close - 1.0, close))
            price = close
    return rows


class CampaignChartLeadInTests(unittest.TestCase):
    def setUp(self):
        self.first_day = date(2026, 7, 20)
        self.candles = _session_candles(self.first_day, 6)
        # A mid-session mother on the third day: plenty of bars on both sides.
        self.mother_at = datetime(2026, 7, 22, 11, 15, tzinfo=IST)
        self.mother = next(row for row in self.candles if row.timestamp == self.mother_at)
        self.requested: list[tuple[str, date]] = []

        candles = self.candles
        requested = self.requested

        async def fake_load(broker, instrument, timeframe, *, from_date, to_date):
            requested.append((str(instrument.get("kind")), from_date))
            return [row for row in candles if from_date <= row.timestamp.date() <= to_date]

        self._real_load = app_module._terminal_cascade_load_candles
        app_module._terminal_cascade_load_candles = fake_load

    def tearDown(self):
        app_module._terminal_cascade_load_candles = self._real_load

    def _run(self):
        instrument = CashCascadeInstrument(symbol="KALYANKJIL", name="Kalyan", security_id="1")
        engine = CashCascadePaperEngine(
            self.mother, self.mother, instrument, CashCascadePaperConfig(capital_inr=100000, timeframe="15m")
        )
        import asyncio

        rows = (
            asyncio.get_event_loop_policy()
            .new_event_loop()
            .run_until_complete(
                app_module._terminal_cascade_replay_with_candles(
                    None, engine, {"kind": "signal"}, {"kind": "trade"}, self.mother_at
                )
            )
        )
        return engine, rows

    def test_the_chart_reaches_back_before_the_mother(self):
        _engine, rows = self._run()
        before = [row for row in rows if row.timestamp < self.mother_at]
        self.assertEqual(len(before), app_module.CASCADE_CHART_LEAD_BARS)
        # And the mother itself is still in the picture, right after the lead.
        self.assertEqual(rows[len(before)].timestamp, self.mother_at)

    def test_the_replay_never_sees_a_pre_mother_candle(self):
        engine, _rows = self._run()
        # history[0] is the mother the engine was built with; everything fed
        # afterwards must be strictly later. One pre-mother candle here and the
        # geometry could cut a leg from a fall the campaign never owned.
        fed = engine.geometry.history[1:]
        self.assertTrue(fed, "replay fed nothing -- the fixture is broken")
        self.assertTrue(all(row.timestamp > self.mother_at for row in fed))

    def test_only_the_signal_fetch_reaches_back(self):
        self._run()
        asked = dict(self.requested)
        self.assertLess(asked["signal"], self.mother_at.date())
        # The trade side prices fills from the mother onward; candles before it
        # are spend against the Dhan rate budget that nothing ever reads.
        self.assertEqual(asked["trade"], self.mother_at.date())

    def test_a_mother_on_the_first_available_bar_still_draws(self):
        self.mother_at = self.candles[0].timestamp
        self.mother = self.candles[0]
        _engine, rows = self._run()
        self.assertEqual(rows[0].timestamp, self.mother_at)


if __name__ == "__main__":
    unittest.main()
