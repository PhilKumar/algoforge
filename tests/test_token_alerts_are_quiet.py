"""A token that refreshes itself does not need to wake anybody.

Phil, 2026-09-02: "I am getting this telegram alerts often for Dhan token
expired and token renewed.... Annoying". Read on prod, all four alerts in
24 hours were FAILURES, and three of them landed exactly on a deploy:

    12:58:56,151  Generating new token...
    12:58:56,227  Generating new token...      <- 76ms later, a second caller
    12:58:56,236  New token generated, expires 2026-09-03
    12:58:56,256  "Token can be generated once every 2 minutes."  -> alert

Two startup paths ask for a token -- app.py's bootstrap and broker/dhan.py's
refresh -- and they raced. The first won, the second collected Dhan's own rate
limit, and that refusal was reported as "FAILED, manual intervention may be
needed" about a token minted twenty milliseconds earlier. Nothing was wrong.

So: success is silent, the vendor's rate limit is not a failure, and a genuine
failure buzzes once an hour rather than once a poll.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PHILFORGE_PIN", "test-pin-not-real")
os.environ.setdefault("PHILFORGE_SKIP_STARTUP_JOBS", "1")
os.environ.setdefault("DHAN_CLIENT_ID", "dummy")
os.environ.setdefault("DHAN_ACCESS_TOKEN", "dummy")

import broker.dhan as dhan  # noqa: E402


class TheVendorsRateLimitIsNotAFailure(unittest.TestCase):
    def test_dhans_own_two_minute_message_is_recognised(self):
        self.assertTrue(dhan._is_rate_limited("Token can be generated once every 2 minutes."))
        self.assertTrue(dhan._is_rate_limited("TOKEN CAN BE GENERATED ONCE EVERY 2 MINUTES"))

    def test_a_real_failure_is_not_swallowed_by_it(self):
        for reason in ("invalid TOTP secret", "DHAN_PIN not set", "connection refused", ""):
            self.assertFalse(dhan._is_rate_limited(reason), reason)


class SuccessSaysNothing(unittest.TestCase):
    def test_a_successful_refresh_sends_no_message(self):
        with patch("broker.dhan.requests.post") as post:
            dhan._notify_token_event(True)
            dhan._notify_token_event(True, "refreshed")
        post.assert_not_called()


class FailuresAreRationed(unittest.TestCase):
    def setUp(self):
        dhan._last_token_alert = 0.0

    def tearDown(self):
        dhan._last_token_alert = 0.0

    def test_the_rate_limit_refusal_never_alerts(self):
        with patch("broker.dhan.config.TELEGRAM_ALERTS_ENABLED", True), patch("broker.dhan.requests.post") as post:
            dhan._notify_token_event(False, "Token can be generated once every 2 minutes.")
        post.assert_not_called()

    def test_a_real_failure_alerts_once_then_holds_for_an_hour(self):
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        with (
            patch("broker.dhan.config.TELEGRAM_ALERTS_ENABLED", True),
            patch.dict(os.environ, env),
            patch("broker.dhan.requests.post") as post,
        ):
            for _ in range(5):
                dhan._notify_token_event(False, "invalid TOTP secret")
        self.assertEqual(post.call_count, 1, "a broken token must not buzz once per poll")


class TheCooldownClearsTheVendorsWindow(unittest.TestCase):
    def test_the_refresh_cooldown_is_longer_than_dhans_two_minutes(self):
        """30s let this ask again INSIDE Dhan's own window and collect the
        refusal it then reported as a failure."""
        import inspect

        src = inspect.getsource(dhan._reserve_refresh_slot)
        self.assertIn("cooldown_sec: float = 150.0", src)


if __name__ == "__main__":
    unittest.main()
