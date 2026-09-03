"""The rule that ended the trade, drawn on the chart of the trade it ended.

Phil, 2026-09-03: "I need the super trend indicator that cuts today's trade to
an exit.. I want that in the chart as well".

It was already the exit. CE_SL15_NoMonTue carries `Supertrend_10_2.7_3` --
period 10, multiplier 2.7, on 3-minute NIFTY. On 03-Sep it turned DOWN at
10:36 and the engine, whose execution timeframe is 5m, acted at the next
boundary: 10:45. Without it on the chart that exit looks arbitrary.

Two things this had to get right, both learned the hard way today:

  * WARM-UP. Supertrend's ATR is Wilder-smoothed, so it carries across days.
    Rebuilt from 09:15 alone the bands are wrong and 03-Sep shows NO flip at
    all; warmed on prior sessions the same settings flip at 10:36, which is
    the flip the engine acted on. The window therefore starts days early.
  * TIME, NOT PRICE. The trade chart draws the OPTION's premium candles and
    this supertrend is an INDEX level. On an axis running 250-300 a value of
    23,996 is off-screen at best and misread as a premium at worst -- the same
    category error as drawing option-priced marks on NIFTY candles. The two
    series share a time axis and nothing else, so the flip is drawn as a
    vertical line at its time.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PHILFORGE_PIN", "test-pin-not-real")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("PHILFORGE_STARTUP_SCRIP_MASTER", "0")
os.environ.setdefault("PHILFORGE_STARTUP_ENGINE_RESTORE", "0")
os.environ.setdefault("DHAN_CLIENT_ID", "dummy")
os.environ.setdefault("DHAN_ACCESS_TOKEN", "dummy")

import app as app_module  # noqa: E402

CHART_JS = (ROOT / "static" / "philforge-bench-chart.js").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")


class TheSpecComesFromTheStrategy(unittest.TestCase):
    """`Supertrend_<period>_<multiplier>_<timeframe>`, wherever it sits."""

    def test_the_live_ce_strategy(self):
        cfg = {"exit_conditions": [{"left": "Supertrend_10_2.7_3", "op": "<", "right": "close"}]}
        self.assertEqual(app_module._strategy_supertrend_spec(cfg), (10, 2.7, 3))

    def test_the_live_pe_strategy(self):
        self.assertEqual(
            app_module._strategy_supertrend_spec({"exit_conditions": [{"left": "Supertrend_10_2_3"}]}),
            (10, 2.0, 3),
        )

    def test_a_strategy_without_one_gets_no_overlay(self):
        self.assertIsNone(app_module._strategy_supertrend_spec({"exit_conditions": [{"left": "RSI_14"}]}))
        self.assertIsNone(app_module._strategy_supertrend_spec(None))
        self.assertIsNone(app_module._strategy_supertrend_spec({}))

    def test_nonsense_parameters_are_refused(self):
        self.assertIsNone(app_module._strategy_supertrend_spec({"x": "Supertrend_1_2.7_3"}))
        self.assertIsNone(app_module._strategy_supertrend_spec({"x": "Supertrend_10_0_3"}))

    def test_it_is_found_however_deep_the_condition_sits(self):
        deep = {"legs": [{"exit": {"groups": [{"rules": [{"lhs": "Supertrend_7_1.5_15"}]}]}}]}
        self.assertEqual(app_module._strategy_supertrend_spec(deep), (7, 1.5, 15))


class TheFlipsAreComputedOnTheIndex(unittest.TestCase):
    """Exercised against a real Wilder-smoothed series, not a stub."""

    def _bars(self, n=400):
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(7)
        idx = pd.date_range("2026-09-01 09:15", periods=n, freq="1min")
        walk = 23900 + np.cumsum(rng.normal(0, 4, n))
        return pd.DataFrame({"open": walk, "high": walk + 6, "low": walk - 6, "close": walk, "volume": 0}, index=idx)

    def _run(self, frame, spec=(10, 2.7, 3)):
        import asyncio

        class Broker:
            def get_historical_data(self, **_kw):
                return frame

        return asyncio.run(app_module._index_supertrend_flips(Broker(), "NIFTY", spec, "2026-09-01", "2026-09-01"))

    def test_it_reports_the_settings_it_used(self):
        out = self._run(self._bars())
        self.assertEqual((out["period"], out["multiplier"], out["timeframe"]), (10, 2.7, "3m"))

    def test_every_flip_carries_a_time_and_a_direction(self):
        out = self._run(self._bars())
        self.assertTrue(out["flips"], "a random walk over 400 bars should turn at least once")
        for flip in out["flips"]:
            self.assertIn(flip["dir"], ("up", "down"))
            self.assertIsInstance(flip["t"], int)

    def test_directions_alternate(self):
        """Two 'down's in a row would mean a non-flip was reported."""
        dirs = [f["dir"] for f in self._run(self._bars())["flips"]]
        for a, b in zip(dirs, dirs[1:]):
            self.assertNotEqual(a, b)

    def test_too_little_history_returns_nothing_rather_than_a_cold_guess(self):
        self.assertIsNone(self._run(self._bars(12)))

    def test_a_broker_failure_costs_the_overlay_not_the_chart(self):
        import asyncio

        class Broken:
            def get_historical_data(self, **_kw):
                raise RuntimeError("Dhan down")

        self.assertIsNone(asyncio.run(app_module._index_supertrend_flips(Broken(), "NIFTY", (10, 2.7, 3), "a", "b")))


class TheRouteShipsIt(unittest.TestCase):
    def _route(self):
        i = APP.index('@app.get("/api/live/trade-chart")')
        return APP[i : APP.index("\n@app.", i + 1)]

    def test_the_overlay_is_returned(self):
        self.assertIn('"supertrend": supertrend_overlay', self._route())

    def test_it_is_built_from_the_running_strategy(self):
        self.assertIn('_strategy_supertrend_spec(getattr(engine, "strategy", None))', self._route())

    def test_it_shares_the_charts_frozen_window(self):
        """A window ending today would draw flips after the trade closed."""
        self.assertIn("broker_client, underlying, spec, from_date, end_day.isoformat()", self._route())

    def test_the_index_is_asked_for_one_minute_candles(self):
        """Dhan serves no 3m candle; the engine builds it from 1m and so does this."""
        i = APP.index("async def _index_supertrend_flips(")
        body = APP[i : i + 4000]
        self.assertIn('candle_type="1"', body)
        self.assertIn('resample(f"{minutes}min", label="left", closed="left")', body)


class TheChartDrawsItInTime(unittest.TestCase):
    def test_the_flip_is_a_vertical_line_not_a_price_line(self):
        self.assertIn("var st = d.supertrend;", CHART_JS)
        self.assertIn("ctx.moveTo(x, p.padT);", CHART_JS)
        self.assertIn("ctx.lineTo(x, p.padT + p.plotH);", CHART_JS)

    def test_it_is_placed_by_time(self):
        self.assertIn("var x = p.xOf(flip.t);", CHART_JS)

    def test_a_flip_outside_the_visible_window_is_skipped(self):
        self.assertIn("if (!isFinite(x) || x < p.padL || x > p.padL + p.plotW) return;", CHART_JS)

    def test_up_and_down_are_told_apart(self):
        self.assertIn("var down = String(flip.dir) === 'down';", CHART_JS)
        self.assertIn("'ST ' + (down ? 'DOWN' : 'UP')", CHART_JS)

    def test_a_chart_with_no_overlay_is_unaffected(self):
        self.assertIn("((st && Array.isArray(st.flips)) ? st.flips : [])", CHART_JS)


if __name__ == "__main__":
    unittest.main()
