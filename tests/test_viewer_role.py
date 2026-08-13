"""The read-only account: sees everything, changes nothing.

The gate is on the request METHOD rather than on a list of protected routes,
and that choice is what these tests pin. A route added next month is denied to
viewers the day it appears, without anyone having to remember. The opposite
design — annotate each mutating endpoint — fails open, and the one endpoint
nobody annotated is the one that matters.
"""

import unittest

import auth


class ViewerRoleTests(unittest.TestCase):
    def test_viewer_is_a_real_role(self):
        self.assertIn(auth.VIEWER_ROLE, auth.USER_ROLES)
        self.assertIn("admin", auth.USER_ROLES)
        self.assertIn("user", auth.USER_ROLES)

    def test_only_the_viewer_role_is_read_only(self):
        self.assertTrue(auth.is_viewer({"role": "viewer"}))
        self.assertTrue(auth.is_viewer({"role": "VIEWER"}))
        self.assertFalse(auth.is_viewer({"role": "user"}))
        self.assertFalse(auth.is_viewer({"role": "admin"}))
        self.assertFalse(auth.is_viewer(None))

    def test_reading_is_always_allowed(self):
        for path in (
            "/api/strategies",
            "/api/runs/347",
            "/api/live/status",
            "/api/terminal/cascade/status",
            "/app",
        ):
            self.assertTrue(auth.viewer_may_call("GET", path), path)
            self.assertTrue(auth.viewer_may_call("HEAD", path), path)

    def test_every_way_of_placing_a_trade_is_refused(self):
        for method, path in (
            ("POST", "/api/live/start"),
            ("POST", "/api/live/stop"),
            ("POST", "/api/live/exit-position"),
            ("POST", "/api/orders/place"),
            ("DELETE", "/api/orders/12345"),
            ("POST", "/api/terminal/order"),
            ("POST", "/api/terminal/gtt"),
            ("POST", "/api/paper/start"),
            ("POST", "/api/terminal/cascade/start"),
            ("POST", "/api/terminal/cascade/kill"),
            ("POST", "/api/fib-boundary/live/NIFTY/arm"),
            ("POST", "/api/scalp/start"),
        ):
            self.assertFalse(auth.viewer_may_call(method, path), f"{method} {path} must be refused")

    def test_data_cannot_be_changed_or_deleted(self):
        for method, path in (
            ("POST", "/api/strategies"),
            ("PUT", "/api/strategies/12"),
            ("DELETE", "/api/strategies/12"),
            ("DELETE", "/api/runs/347"),
            ("POST", "/api/backtest"),
            ("POST", "/api/admin/users"),
            ("PATCH", "/api/journals/3"),
        ):
            self.assertFalse(auth.viewer_may_call(method, path), f"{method} {path} must be refused")

    def test_an_unknown_future_route_is_refused_by_default(self):
        """The reason the gate is on the method: this is the failure mode."""
        self.assertFalse(auth.viewer_may_call("POST", "/api/something/invented/next-year"))
        self.assertFalse(auth.viewer_may_call("DELETE", "/api/not/written/yet"))

    def test_a_viewer_can_still_look_after_their_own_login(self):
        for path in (
            "/api/auth/logout",
            "/api/auth/change-password",
            "/api/auth/mfa/enroll/start",
            "/api/auth/passkeys/register/options",
            "/api/auth/passkeys/register/verify",
        ):
            self.assertTrue(auth.viewer_may_call("POST", path), path)

    def test_the_allowlist_never_reaches_trading_or_admin(self):
        for path in auth.VIEWER_WRITE_ALLOWLIST:
            self.assertFalse(
                any(word in path for word in ("order", "live", "cascade", "scalp", "admin", "paper", "backtest")),
                f"{path} is too powerful for a read-only account",
            )


if __name__ == "__main__":
    unittest.main()
