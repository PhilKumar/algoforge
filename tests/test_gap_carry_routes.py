"""The Gap Carry routes' rules that do not need a broker.

Everything here is a refusal or a shape: which charts the page may ask for,
which clock times can actually be reached, that the live gate holds, that Start
and Backtest ask the same questions, and that a strike offset can never be
pointed at the out-of-the-money wing.
"""

import base64
import os
import unittest
from datetime import date, time, timedelta
from types import SimpleNamespace

os.environ.setdefault("PHILFORGE_PIN", "123456")
os.environ.setdefault("PHILFORGE_DB", "/tmp/philforge-gap-carry-routes.db")
os.environ.setdefault("PHILFORGE_USER_DATA_ROOT", "/tmp/philforge-gap-carry-routes-data")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from fastapi import HTTPException  # noqa: E402

import app as app_module  # noqa: E402

IST = app_module.IST


def _payload(**kw):
    base = dict(
        timeframe="5m",
        rsi_threshold=70.0,
        strike_offset_steps=4,
        lots=1,
        entry_time="15:10",
        exit_time="09:20",
        expiry_rule="weekly",
        mode="paper",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class ChartTests(unittest.TestCase):
    """The page may only offer charts the adapter can actually fetch."""

    def test_the_two_fetchable_charts_are_accepted(self):
        for tf in ("5m", "15m"):
            self.assertEqual(app_module._gap_carry_timeframe(tf), tf)

    def test_a_chart_the_adapter_cannot_source_is_refused(self):
        """The engine was measured on 10m too, but CascadeOptionsAdapter cannot
        fetch it. Offering it would fail at the first candle request."""
        for tf in ("10m", "30m", "1m", "1h"):
            with self.assertRaises(HTTPException) as ctx:
                app_module._gap_carry_timeframe(tf)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("5m", str(ctx.exception.detail))

    def test_the_offered_list_is_a_subset_of_what_the_engine_allows(self):
        import engine.gap_carry as gap_carry

        self.assertTrue(set(app_module._GAP_CARRY_TIMEFRAMES) <= set(gap_carry.TIMEFRAMES))


class ClockTests(unittest.TestCase):
    """A time the background loop never wakes for is not a setting, it is a trap."""

    def test_the_measured_clocks_are_accepted(self):
        self.assertEqual(app_module._gap_carry_clock("15:10", label="Entry time"), time(15, 10))
        self.assertEqual(app_module._gap_carry_clock("09:20", label="Exit time"), time(9, 20))

    def test_a_time_after_the_close_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            app_module._gap_carry_clock("15:40", label="Entry time")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("session", str(ctx.exception.detail))

    def test_a_time_before_the_open_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            app_module._gap_carry_clock("09:05", label="Exit time")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_junk_is_refused_with_the_field_named(self):
        with self.assertRaises(HTTPException) as ctx:
            app_module._gap_carry_clock("half past three", label="Entry time")
        self.assertIn("Entry time", str(ctx.exception.detail))


class ExpiryRuleTests(unittest.TestCase):
    def test_weekly_and_monthly_are_the_choices(self):
        self.assertEqual(app_module._gap_carry_expiry_rule("weekly"), "weekly")
        self.assertEqual(app_module._gap_carry_expiry_rule("monthly"), "monthly")

    def test_anything_else_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            app_module._gap_carry_expiry_rule("biweekly")
        self.assertEqual(ctx.exception.status_code, 400)


class LiveGateTests(unittest.TestCase):
    """Live is refused at the same gate as its two siblings on this page."""

    def test_paper_is_allowed(self):
        self.assertEqual(app_module._gap_carry_trade_mode("paper"), "paper")

    def test_live_is_refused_with_the_reason(self):
        original = app_module._FIB_TOUCH_LIVE_EXECUTION_ENABLED
        app_module._FIB_TOUCH_LIVE_EXECUTION_ENABLED = False
        try:
            with self.assertRaises(HTTPException) as ctx:
                app_module._gap_carry_trade_mode("live")
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIn("partial-fill handling", str(ctx.exception.detail))
        finally:
            app_module._FIB_TOUCH_LIVE_EXECUTION_ENABLED = original

    def test_a_nonsense_mode_is_a_400_not_a_503(self):
        with self.assertRaises(HTTPException) as ctx:
            app_module._gap_carry_trade_mode("wishful")
        self.assertEqual(ctx.exception.status_code, 400)


class ConfigTests(unittest.TestCase):
    def test_a_valid_payload_builds_the_engines_own_config(self):
        config = app_module._gap_carry_config(_payload(lots=3, rsi_threshold=72.0))
        self.assertEqual(config.timeframe, "5m")
        self.assertEqual(config.lots, 3)
        self.assertEqual(config.rsi_threshold, 72.0)
        self.assertEqual(config.rsi_ceiling_for_put, 28.0)
        self.assertEqual(config.entry_time, time(15, 10))

    def test_the_engines_refusal_reaches_the_user_as_a_400(self):
        with self.assertRaises(HTTPException) as ctx:
            app_module._gap_carry_config(_payload(rsi_threshold=99.0))
        self.assertEqual(ctx.exception.status_code, 400)


class StrikeTests(unittest.TestCase):
    """The offset is applied AGAINST the trade's direction, always."""

    def test_a_call_is_bought_below_spot_and_a_put_above(self):
        import engine.gap_carry as gap_carry

        config = app_module._gap_carry_config(_payload(strike_offset_steps=4))
        self.assertEqual(gap_carry.strike_for(24580, "CE", config), 24400)
        self.assertEqual(gap_carry.strike_for(24580, "PE", config), 24800)

    def test_the_payload_cannot_express_an_out_of_the_money_offset(self):
        field = app_module.GapCarryPaperStartPayload.model_fields["strike_offset_steps"]
        lows = [m for m in field.metadata if getattr(m, "ge", None) is not None]
        self.assertTrue(lows and lows[0].ge == 0, "a negative offset would buy the cheap OTM wing")


class PayloadShapeTests(unittest.TestCase):
    def test_backtest_asks_the_same_questions_as_start(self):
        """If these drift, the replay stops describing what Start would trade."""
        start = set(app_module.GapCarryPaperStartPayload.model_fields)
        back = set(app_module.GapCarryBacktestPayload.model_fields)
        self.assertEqual(start - {"mode"}, back - {"lookback_days"})

    def test_defaults_are_the_measured_configuration(self):
        p = app_module.GapCarryPaperStartPayload()
        self.assertEqual(p.timeframe, "5m")
        self.assertEqual(p.rsi_threshold, 70.0)
        self.assertEqual(p.strike_offset_steps, 4)
        self.assertEqual(p.lots, 1)
        self.assertEqual(p.entry_time, "15:10")
        self.assertEqual(p.exit_time, "09:20")

    def test_lots_are_bounded_on_the_wire(self):
        import pydantic

        with self.assertRaises(pydantic.ValidationError):
            app_module.GapCarryPaperStartPayload(lots=0)
        with self.assertRaises(pydantic.ValidationError):
            app_module.GapCarryPaperStartPayload(lots=99)


class AutoRuleTests(unittest.TestCase):
    def test_automation_is_pinned_to_the_measured_configuration(self):
        """The console must not be able to leave an unattended loop on a setting
        nobody has replayed."""
        rule = app_module._GAP_CARRY_AUTO_RULE
        self.assertEqual(rule["timeframe"], "5m")
        self.assertEqual(rule["rsi_threshold"], 70.0)
        self.assertEqual(rule["strike_offset_steps"], 4)
        self.assertEqual(rule["entry_time"], "15:10")
        self.assertEqual(rule["exit_time"], "09:20")

    def test_the_auto_rule_is_itself_a_legal_payload(self):
        config = app_module._gap_carry_config(SimpleNamespace(**rule_ns()))
        self.assertEqual(config.timeframe, "5m")

    def test_underscore_keys_never_reach_the_page(self):
        public = app_module._gap_carry_auto_public({"enabled": True, "_seq": 4, "state": "holding"})
        self.assertEqual(public, {"enabled": True, "state": "holding"})


def rule_ns() -> dict:
    return dict(app_module._GAP_CARRY_AUTO_RULE)


class RegistrationTests(unittest.TestCase):
    """Miss one of these and a campaign is dropped by a kill, a save or a restart."""

    def test_every_route_is_registered(self):
        paths = {r.path for r in app_module.app.routes if "gap-carry" in getattr(r, "path", "")}
        self.assertEqual(
            paths,
            {
                "/api/gap-carry/paper/status",
                "/api/gap-carry/paper/start",
                "/api/gap-carry/paper/kill",
                "/api/gap-carry/backtest",
                "/api/gap-carry/backtests/latest",
                "/api/gap-carry/auto",
            },
        )

    def test_the_registry_joins_the_runtime_owners(self):
        app_module._gap_carry_engines[4242] = object()
        try:
            self.assertIn(4242, app_module._runtime_owner_ids())
        finally:
            app_module._gap_carry_engines.pop(4242, None)

    def test_the_control_summary_reports_the_strategy(self):
        summary = app_module._runtime_control_summary(999999)
        self.assertIn("gap_carry_running", summary)

    def test_a_viewer_may_read_but_the_writes_are_not_allowlisted(self):
        import auth

        self.assertIn("/api/gap-carry/", auth.VIEWER_SHARED_READ_PREFIXES)
        self.assertFalse(any("gap-carry" in str(p) for p in auth.VIEWER_WRITE_ALLOWLIST))


class ExpiryLookupTests(unittest.TestCase):
    """A contract settling tonight cannot carry an overnight position."""

    class _Broker:
        pass

    def _lookup(self, rows, rule="weekly"):
        original = app_module._fib_touch_expiry_source
        app_module._fib_touch_expiry_source = lambda _b, _s: (lambda _on: rows)
        try:
            return app_module._gap_carry_expiry_lookup(self._Broker(), rule)
        finally:
            app_module._fib_touch_expiry_source = original

    def test_tonights_expiry_is_skipped_for_the_next_one(self):
        session = date(2026, 3, 10)
        lookup = self._lookup([session, session + timedelta(days=7)])
        self.assertEqual(lookup(session), session + timedelta(days=7))

    def test_nothing_far_enough_out_is_none(self):
        session = date(2026, 3, 10)
        self.assertIsNone(self._lookup([session])(session))


if __name__ == "__main__":
    unittest.main()
