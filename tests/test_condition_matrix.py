import unittest

import pandas as pd

from engine.backtest import debug_condition_group, eval_condition, inspect_condition_group


class ConditionMatrixTests(unittest.TestCase):
    def test_numeric_operator_matrix(self):
        prev_row = pd.Series(
            {
                "current_close": 100.0,
                "EMA_2_5m": 101.0,
                "level": 101.0,
                "open": 99.0,
                "high": 101.5,
                "low": 98.5,
                "close": 100.0,
            },
            name=pd.Timestamp("2026-03-27 09:20"),
        )
        row = pd.Series(
            {
                "current_close": 102.0,
                "EMA_2_5m": 101.0,
                "level": 101.0,
                "open": 100.5,
                "high": 103.0,
                "low": 100.0,
                "close": 102.0,
            },
            name=pd.Timestamp("2026-03-27 09:25"),
        )

        cases = [
            ({"left": "current_close", "operator": "is_above", "right": "EMA_2_5m"}, True),
            ({"left": "current_close", "operator": "is_below", "right": "EMA_2_5m"}, False),
            ({"left": "current_close", "operator": ">=", "right": "number", "right_number_value": 102}, True),
            ({"left": "current_close", "operator": "<=", "right": "number", "right_number_value": 102}, True),
            ({"left": "current_close", "operator": "==", "right": "number", "right_number_value": 102}, True),
            ({"left": "current_close", "operator": "crosses_above", "right": "level"}, True),
            ({"left": "current_close", "operator": "crosses_below", "right": "level"}, False),
            ({"left": "current_high", "operator": "touches", "right": "number", "right_number_value": 102.5}, True),
        ]

        for cond, expected in cases:
            with self.subTest(operator=cond["operator"], right=cond["right"]):
                self.assertEqual(eval_condition(row, cond, prev_row), expected)

    def test_boolean_operator_matrix(self):
        row = pd.Series(
            {"CPR_is_wide": True, "ORB_is_breakout_up": False, "close": 100.0},
            name=pd.Timestamp("2026-03-27 09:25"),
        )

        cases = [
            ({"left": "CPR_is_wide", "operator": "is_true", "right": "true"}, True),
            ({"left": "CPR_is_wide", "operator": "is_false", "right": "true"}, False),
            ({"left": "CPR_is_wide", "operator": "==", "right": "true"}, True),
            ({"left": "ORB_is_breakout_up", "operator": "==", "right": "false"}, True),
        ]

        for cond, expected in cases:
            with self.subTest(operator=cond["operator"], right=cond["right"]):
                self.assertEqual(eval_condition(row, cond), expected)

    def test_time_of_day_operator_matrix(self):
        prev_row = pd.Series({"close": 100.0}, name=pd.Timestamp("2026-03-27 09:25:00"))
        row = pd.Series({"close": 101.0}, name=pd.Timestamp("2026-03-27 09:30:00"))

        cases = [
            ({"left": "Time_Of_Day", "operator": "is_above", "right": "time", "right_time": "09:20"}, True),
            ({"left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "09:45"}, True),
            ({"left": "Time_Of_Day", "operator": ">=", "right": "time", "right_time": "09:30"}, True),
            ({"left": "Time_Of_Day", "operator": "crosses_above", "right": "time", "right_time": "09:27"}, True),
            ({"left": "Time_Of_Day", "operator": "crosses_below", "right": "time", "right_time": "09:27"}, False),
        ]

        for cond, expected in cases:
            with self.subTest(operator=cond["operator"], time=cond["right_time"]):
                self.assertEqual(eval_condition(row, cond, prev_row), expected)

    def test_day_of_week_operator_matrix(self):
        row = pd.Series({"close": 101.0}, name=pd.Timestamp("2026-03-27 09:30:00"))

        cases = [
            ({"left": "Day_Of_Week", "operator": "contains", "right": "days", "right_days": ["Friday"]}, True),
            ({"left": "Day_Of_Week", "operator": "contains", "right": "days", "right_days": ["Monday"]}, False),
            (
                {"left": "Day_Of_Week", "operator": "not_contains", "right": "days", "right_days": ["Monday"]},
                True,
            ),
        ]

        for cond, expected in cases:
            with self.subTest(operator=cond["operator"], days=cond["right_days"]):
                self.assertEqual(eval_condition(row, cond), expected)

    def test_debug_condition_group_reports_all_results_for_mixed_conditions(self):
        prev_row = pd.Series(
            {"close": 101.0, "EMA_2_5m": 102.0, "cpr_is_wide": False},
            name=pd.Timestamp("2026-03-27 09:25:00"),
        )
        row = pd.Series(
            {"close": 100.0, "EMA_2_5m": 101.0, "cpr_is_wide": False},
            name=pd.Timestamp("2026-03-27 09:30:00"),
        )
        conditions = [
            {"left": "close", "operator": "is_below", "right": "EMA_2_5m", "logic": "IF"},
            {"left": "cpr_is_wide", "operator": "is_false", "right": "true", "logic": "AND"},
            {"left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "10:00", "logic": "AND"},
        ]

        overall, details = debug_condition_group(row, conditions, prev_row)

        self.assertTrue(overall)
        self.assertEqual(len(details), 3)
        self.assertTrue(all("condition" in item and "result" in item for item in details))

    def test_inspect_condition_group_reports_missing_runtime_indicator_data(self):
        row = pd.Series(
            {"close": 100.0, "EMA_2_5m": 101.0, "CPR_BC": float("nan")},
            name=pd.Timestamp("2026-03-27 09:30:00"),
        )
        conditions = [
            {"left": "current_close", "operator": "is_below", "right": "CPR_BC", "logic": "IF"},
            {"left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "10:00", "logic": "AND"},
        ]

        overall, details, missing_fields = inspect_condition_group(row, conditions)

        self.assertFalse(overall)
        self.assertEqual(missing_fields, ["CPR_BC"])
        self.assertEqual(details[0]["missing_fields"], ["CPR_BC"])
        self.assertNotIn("missing_fields", details[1])

    def test_inspect_condition_group_reports_missing_previous_candle_data_for_cross(self):
        prev_row = pd.Series(
            {"close": 100.0, "EMA_2_5m": float("nan")},
            name=pd.Timestamp("2026-03-27 09:25:00"),
        )
        row = pd.Series(
            {"close": 102.0, "EMA_2_5m": 101.0},
            name=pd.Timestamp("2026-03-27 09:30:00"),
        )
        conditions = [
            {"left": "current_close", "operator": "crosses_above", "right": "EMA_2_5m", "logic": "IF"},
        ]

        overall, details, missing_fields = inspect_condition_group(row, conditions, prev_row)

        self.assertFalse(overall)
        self.assertEqual(missing_fields, ["EMA_2_5m (prev)"])
        self.assertEqual(details[0]["missing_fields"], ["EMA_2_5m (prev)"])


if __name__ == "__main__":
    unittest.main()
