"""On "auto" the instrument decides what trading costs, not the payload.

`execution_profile: "auto"` was a LABEL. The numbers behind it lived only in
the browser, were written into the strategy by whatever the instrument select
happened to hold, and were then trusted forever -- `restoreExecutionSettings`
painted the stored basis points back over the ones the profile would have
applied, so one bad moment ratcheted in permanently.

It did. On 2026-08-31 all three running strategies were under-costed:
My_First_Run_PE and _CE said "auto" and carried 12/6/8 (the Cash Equity
fallback) while both are instrument 26000, whose row is 18/10/14. PE_NoTarget
was "custom" 0/0/0 with the capital check off, so its paper book filled every
trade at the exact quoted price and would have ordered live without ever
asking Dhan for funds.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.execution_profiles import resolve_execution_costs  # noqa: E402
from engine.live import LiveEngine  # noqa: E402
from engine.paper_trading import PaperTradingEngine  # noqa: E402

NIFTY = "26000"


class AutoIgnoresWhateverThePayloadCarries(unittest.TestCase):
    def test_the_stale_cash_equity_row_is_overridden(self):
        """The exact shape of My_First_Run_PE: auto, NIFTY, carrying 12/6/8."""
        out = resolve_execution_costs(
            {
                "execution_profile": "auto",
                "instrument": NIFTY,
                "spread_bps": 12,
                "entry_slippage_bps": 6,
                "exit_slippage_bps": 8,
            }
        )
        self.assertEqual((out["spread_bps"], out["entry_slippage_bps"], out["exit_slippage_bps"]), (18.0, 10.0, 14.0))
        self.assertEqual(out["profile_label"], "NIFTY 50")

    def test_auto_turns_the_capital_check_on_whatever_was_stored(self):
        out = resolve_execution_costs({"execution_profile": "auto", "instrument": NIFTY, "enforce_capital": False})
        self.assertTrue(out["enforce_capital"])
        self.assertEqual(out["capital_buffer_pct"], 5.0)

    def test_sensex_gets_its_own_row(self):
        out = resolve_execution_costs({"execution_profile": "auto", "instrument": "1"})
        self.assertEqual(out["spread_bps"], 34.0)

    def test_an_unknown_instrument_falls_back_to_cash_equity(self):
        out = resolve_execution_costs({"execution_profile": "auto", "instrument": "99999"})
        self.assertEqual(out["spread_bps"], 12.0)
        self.assertEqual(out["profile_label"], "Cash Equity")


class CustomStillMeansCustom(unittest.TestCase):
    def test_a_deliberate_zero_is_respected(self):
        out = resolve_execution_costs(
            {"execution_profile": "custom", "instrument": NIFTY, "spread_bps": 0, "enforce_capital": False}
        )
        self.assertEqual(out["spread_bps"], 0.0)
        self.assertFalse(out["enforce_capital"])

    def test_a_missing_profile_is_custom_not_auto(self):
        """A strategy saved before profiles existed must not silently acquire
        today's costs -- its recorded results were measured without them."""
        out = resolve_execution_costs({"instrument": NIFTY, "spread_bps": 3})
        self.assertEqual(out["execution_profile"], "custom")
        self.assertEqual(out["spread_bps"], 3.0)


class BothEnginesUseIt(unittest.TestCase):
    def _configure(self, cls, strategy):
        with tempfile.TemporaryDirectory() as tmp:
            engine = cls(dhan=object(), run_id="profile-check", state_dir=tmp)
            engine.configure(strategy=strategy, entry_conditions=[], exit_conditions=[])
            return engine

    def test_paper_charges_the_instrument_row_on_auto(self):
        engine = self._configure(
            PaperTradingEngine,
            {"run_name": "p", "instrument": NIFTY, "execution_profile": "auto", "spread_bps": 12},
        )
        self.assertEqual(engine._spread_bps, 18.0)
        self.assertEqual(engine._exit_slippage_bps, 14.0)
        self.assertTrue(engine._enforce_capital)

    def test_live_takes_the_capital_rules_from_the_same_row(self):
        engine = self._configure(
            LiveEngine,
            {"run_name": "l", "instrument": NIFTY, "execution_profile": "auto", "enforce_capital": False},
        )
        self.assertTrue(engine._enforce_capital, "live must not place orders it never checked funds for")
        self.assertEqual(engine._capital_buffer_pct, 5.0)

    def test_custom_survives_configure_on_both(self):
        strategy = {
            "run_name": "c",
            "instrument": NIFTY,
            "execution_profile": "custom",
            "spread_bps": 0,
            "enforce_capital": False,
        }
        self.assertEqual(self._configure(PaperTradingEngine, strategy)._spread_bps, 0.0)
        self.assertFalse(self._configure(LiveEngine, strategy)._enforce_capital)

    def test_live_configure_survives_a_missing_deploy_config(self):
        """It used to raise on the fill-timeout lookup: self.deploy_config was
        defaulted, the local name was not."""
        engine = self._configure(LiveEngine, {"run_name": "l", "instrument": NIFTY, "execution_profile": "auto"})
        self.assertEqual(engine.deploy_config, {})


if __name__ == "__main__":
    unittest.main()


class ADeployIsPersistedBeforeAnythingCanRestartIt(unittest.TestCase):
    """The window that made PE_NoTarget keep reverting.

    `_paper_start_impl` replaces an engine by calling `old_engine.stop()`,
    which writes the OLD engine's dying in-memory config to the state file,
    then configures the new one -- and used to start the task without saving.
    Between those two moments the file on disk described the run that had just
    been replaced, so any restart in that window restored the old config and
    silently undid the deploy. `live_start` always persisted here; paper did
    not.
    """

    def test_paper_start_saves_state_after_configuring_the_new_engine(self):
        import inspect

        import app as app_module

        src = inspect.getsource(app_module._paper_start_impl)
        start = src.index("paper_task_bucket[run_id] = asyncio.create_task")
        self.assertIn(
            "_save_state()",
            src[start:],
            "paper_start must persist the new engine's config before returning, "
            "or a restart restores the engine it just replaced",
        )
