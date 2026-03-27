import unittest
from unittest.mock import patch

import pandas as pd

from engine.backtest import debug_condition_group
from engine.live import LiveEngine
from engine.paper_trading import PaperTradingEngine


def _make_two_day_intraday_ohlcv() -> pd.DataFrame:
    index = pd.to_datetime(
        [
            "2026-03-26 09:15",
            "2026-03-26 09:20",
            "2026-03-26 09:25",
            "2026-03-26 09:30",
            "2026-03-26 09:35",
            "2026-03-26 09:40",
            "2026-03-26 09:45",
            "2026-03-26 09:50",
            "2026-03-27 09:15",
            "2026-03-27 09:20",
            "2026-03-27 09:25",
            "2026-03-27 09:30",
            "2026-03-27 09:35",
            "2026-03-27 09:40",
            "2026-03-27 09:45",
            "2026-03-27 09:50",
        ]
    )
    opens = [
        100.0,
        101.0,
        102.0,
        103.0,
        102.5,
        101.5,
        100.5,
        99.5,
        98.5,
        98.0,
        97.5,
        97.0,
        96.5,
        96.0,
        95.5,
        95.0,
    ]
    closes = [
        101.0,
        102.0,
        103.0,
        102.5,
        101.5,
        100.5,
        99.5,
        99.0,
        98.0,
        97.5,
        97.0,
        96.5,
        96.0,
        95.5,
        95.0,
        94.5,
    ]
    highs = [max(op, cl) + 0.6 for op, cl in zip(opens, closes)]
    lows = [min(op, cl) - 0.6 for op, cl in zip(opens, closes)]
    volumes = [100, 120, 110, 130, 115, 125, 135, 140, 145, 150, 160, 155, 165, 170, 175, 180]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=index,
    )


class DummyBroker:
    async def async_get_funds(self):
        return {"availabelBalance": 100000.0}


class LivePaperParityTests(unittest.TestCase):
    def test_live_and_paper_prepare_same_mixed_indicator_frame(self):
        raw = _make_two_day_intraday_ohlcv()
        indicators = [
            "EMA_2_5m",
            "RSI_2_15m",
            "MACD_2_3_2_5m",
            "BB_2_2_5m",
            "ADX_2_5m",
            "StochRSI_2_5m",
            "Supertrend_2_2_5m",
            "CPR_0.2_0.5",
            "ORB_15min",
            "Previous_Day",
        ]
        now = pd.Timestamp("2026-03-27 09:55:01").to_pydatetime()

        live_engine = LiveEngine(dhan=DummyBroker(), run_id="live-parity")
        live_engine.configure(
            strategy={"instrument": "26000", "timeframe_minutes": 5, "indicators": indicators},
            entry_conditions=[],
            exit_conditions=[],
            deploy_config={},
        )
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            paper_engine = PaperTradingEngine(dhan=DummyBroker(), run_id="paper-parity")
        paper_engine.configure(
            strategy={"instrument": "26000", "timeframe_minutes": 5, "indicators": indicators},
            entry_conditions=[],
            exit_conditions=[],
        )

        live_frame = live_engine._prepare_ws_strategy_frame(raw, live_engine.strategy["indicators"], 5, 5, now)
        paper_frame = paper_engine._prepare_ws_strategy_frame(raw, paper_engine.strategy["indicators"], 5, 5, now)

        pd.testing.assert_index_equal(live_frame.index, paper_frame.index)
        columns = [
            "EMA_2_5m",
            "RSI_2_15m",
            "MACD_2_3_2_5m_signal",
            "BB_2_2_5m_upper",
            "ADX_2_5m_plus_di",
            "StochRSI_2_5m_D",
            "supertrend_dir",
            "CPR_BC",
            "ORB_High",
            "Yesterday_High",
            "Day_of_Week",
            "Hour",
            "Minute",
        ]
        for column in columns:
            with self.subTest(column=column):
                self.assertIn(column, live_frame.columns)
                self.assertIn(column, paper_frame.columns)
                self.assertEqual(pd.isna(live_frame.iloc[-1][column]), pd.isna(paper_frame.iloc[-1][column]))
                if not pd.isna(live_frame.iloc[-1][column]):
                    self.assertAlmostEqual(
                        float(live_frame.iloc[-1][column]), float(paper_frame.iloc[-1][column]), places=8
                    )

    def test_live_and_paper_debug_condition_results_match(self):
        raw = _make_two_day_intraday_ohlcv()
        indicators = ["EMA_2_5m", "CPR_0.2_0.5", "ORB_15min", "Previous_Day"]
        conditions = [
            {"left": "current_close", "operator": "is_below", "right": "EMA_2_5m", "logic": "IF"},
            {"left": "current_close", "operator": "is_below", "right": "CPR_BC", "logic": "AND"},
            {"left": "Day_Of_Week", "operator": "contains", "right": "days", "right_days": ["Friday"], "logic": "AND"},
            {"left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "10:00", "logic": "AND"},
        ]
        now = pd.Timestamp("2026-03-27 09:55:01").to_pydatetime()

        live_engine = LiveEngine(dhan=DummyBroker(), run_id="live-parity-debug")
        live_engine.configure(
            strategy={"instrument": "26000", "timeframe_minutes": 5, "indicators": indicators},
            entry_conditions=conditions,
            exit_conditions=[],
            deploy_config={},
        )
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            paper_engine = PaperTradingEngine(dhan=DummyBroker(), run_id="paper-parity-debug")
        paper_engine.configure(
            strategy={"instrument": "26000", "timeframe_minutes": 5, "indicators": indicators},
            entry_conditions=conditions,
            exit_conditions=[],
        )

        live_frame = live_engine._prepare_ws_strategy_frame(raw, live_engine.strategy["indicators"], 5, 5, now)
        paper_frame = paper_engine._prepare_ws_strategy_frame(raw, paper_engine.strategy["indicators"], 5, 5, now)
        live_latest = live_frame.iloc[-1]
        paper_latest = paper_frame.iloc[-1]
        live_prev = live_frame.iloc[-2]
        paper_prev = paper_frame.iloc[-2]

        live_overall, live_details = debug_condition_group(live_latest, conditions, live_prev)
        paper_overall, paper_details = debug_condition_group(paper_latest, conditions, paper_prev)

        self.assertEqual(live_overall, paper_overall)
        self.assertEqual(len(live_details), len(paper_details))
        for live_item, paper_item in zip(live_details, paper_details):
            self.assertEqual(live_item["condition"], paper_item["condition"])
            self.assertEqual(live_item["result"], paper_item["result"])
            self.assertEqual(live_item["left_value"], paper_item["left_value"])
            self.assertEqual(live_item["right_value"], paper_item["right_value"])

    def test_live_and_paper_gate_missing_condition_data_instead_of_silent_false(self):
        conditions = [
            {"left": "current_close", "operator": "is_below", "right": "CPR_BC", "logic": "IF"},
            {"left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "11:00", "logic": "AND"},
        ]
        now = pd.Timestamp("2026-03-27 10:35:00").to_pydatetime()
        latest_row = pd.Series(
            {"close": 23009.4, "EMA_20_5m": 23040.9312, "CPR_BC": float("nan")},
            name=pd.Timestamp("2026-03-27 10:35:00"),
        )
        prev_row = pd.Series(
            {"close": 23006.75, "EMA_20_5m": 23042.0, "CPR_BC": float("nan")},
            name=pd.Timestamp("2026-03-27 10:30:00"),
        )

        live_engine = LiveEngine(dhan=DummyBroker(), run_id="live-missing-condition-data")
        live_engine.configure(
            strategy={"instrument": "26000", "timeframe_minutes": 5, "indicators": ["EMA_20_5m", "CPR_0.2_0.5"]},
            entry_conditions=conditions,
            exit_conditions=[],
            deploy_config={},
        )
        with patch.object(PaperTradingEngine, "_load_state", autospec=True, return_value=None):
            paper_engine = PaperTradingEngine(dhan=DummyBroker(), run_id="paper-missing-condition-data")
        paper_engine.configure(
            strategy={"instrument": "26000", "timeframe_minutes": 5, "indicators": ["EMA_20_5m", "CPR_0.2_0.5"]},
            entry_conditions=conditions,
            exit_conditions=[],
        )

        live_overall, live_debug = live_engine._evaluate_entry_conditions_with_debug(latest_row, prev_row, now)
        paper_overall, paper_debug = paper_engine._evaluate_entry_conditions_with_debug(latest_row, prev_row, now)

        self.assertFalse(live_overall)
        self.assertFalse(paper_overall)
        self.assertEqual(live_debug["gate"], "missing_condition_data (CPR_BC)")
        self.assertEqual(paper_debug["gate"], "missing_condition_data (CPR_BC)")
        self.assertEqual(live_debug["missing_fields"], ["CPR_BC"])
        self.assertEqual(paper_debug["missing_fields"], ["CPR_BC"])
        self.assertFalse(live_debug["overall"])
        self.assertFalse(paper_debug["overall"])
        self.assertTrue(live_debug["conditions"][1]["result"])
        self.assertTrue(paper_debug["conditions"][1]["result"])


if __name__ == "__main__":
    unittest.main()
