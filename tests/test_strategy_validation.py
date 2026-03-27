import unittest

from engine.strategy_contract import validate_strategy_contract


class StrategyContractValidationTests(unittest.TestCase):
    def test_contract_infers_daily_cpr_from_prefixed_condition_field(self):
        contract = validate_strategy_contract(
            ["EMA_20_5m"],
            entry_conditions=[{"left": "current_close", "operator": "is_below", "right": "CPR_BC"}],
            exit_conditions=[],
        )

        self.assertTrue(contract["valid"])
        self.assertIn("CPR_0.2_0.5", contract["normalized_indicators"])

    def test_contract_infers_daily_cpr_from_generic_cpr_alias(self):
        contract = validate_strategy_contract(
            [],
            entry_conditions=[{"left": "current_close", "operator": "is_above", "right": "bc"}],
            exit_conditions=[],
        )

        self.assertTrue(contract["valid"])
        self.assertIn("CPR_0.2_0.5", contract["normalized_indicators"])

    def test_contract_rejects_unknown_left_field(self):
        contract = validate_strategy_contract(
            ["EMA_20_5m"],
            entry_conditions=[{"left": "Made_Up_Field", "operator": "is_above", "right": "EMA_20_5m"}],
            exit_conditions=[],
        )

        self.assertFalse(contract["valid"])
        self.assertIn("unsupported left-hand field", contract["errors"][0])

    def test_contract_rejects_invalid_time_literal(self):
        contract = validate_strategy_contract(
            [],
            entry_conditions=[{"left": "Time_Of_Day", "operator": "is_below", "right": "time", "right_time": "25:61"}],
            exit_conditions=[],
        )

        self.assertFalse(contract["valid"])
        self.assertTrue(any("invalid time value" in err for err in contract["errors"]))

    def test_contract_rejects_empty_day_selection(self):
        contract = validate_strategy_contract(
            [],
            entry_conditions=[{"left": "Day_Of_Week", "operator": "contains", "right": "days", "right_days": []}],
            exit_conditions=[],
        )

        self.assertFalse(contract["valid"])
        self.assertTrue(any("select at least one day" in err for err in contract["errors"]))

    def test_contract_rejects_boolean_operator_on_numeric_field(self):
        contract = validate_strategy_contract(
            [],
            entry_conditions=[{"left": "current_close", "operator": "is_true", "right": "true"}],
            exit_conditions=[],
        )

        self.assertFalse(contract["valid"])
        self.assertTrue(any("only valid for boolean fields" in err for err in contract["errors"]))

    def test_contract_accepts_supertrend_dir_with_supertrend_indicator(self):
        contract = validate_strategy_contract(
            ["Supertrend_10_2.7_5m"],
            entry_conditions=[{"left": "supertrend_dir", "operator": "==", "right": "number", "right_number_value": 1}],
            exit_conditions=[],
        )

        self.assertTrue(contract["valid"])

    def test_contract_rejects_supertrend_dir_without_supertrend_indicator(self):
        contract = validate_strategy_contract(
            [],
            entry_conditions=[{"left": "supertrend_dir", "operator": "==", "right": "number", "right_number_value": 1}],
            exit_conditions=[],
        )

        self.assertFalse(contract["valid"])
        self.assertTrue(any("unsupported left-hand field 'supertrend_dir'" in err for err in contract["errors"]))


if __name__ == "__main__":
    unittest.main()
