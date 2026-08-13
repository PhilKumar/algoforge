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


class ViewerSharedDataTests(unittest.TestCase):
    """Which reads come from the owner's account, and which stay the viewer's.

    A viewer has no trading of their own, so without sharing the whole feature
    shows an empty site. Sharing is an allowlist because the failure mode of
    the inverse is handing out the owner's broker credentials.
    """

    def test_the_pages_a_viewer_is_meant_to_watch_show_the_owners_data(self):
        for path in (
            "/api/strategies",
            "/api/runs",
            "/api/runs/347",
            "/api/live/status",
            "/api/positions",
            "/api/orders",
            "/api/paper/status",
            "/api/portfolio/history",
            "/api/terminal/cascade/status",
            "/api/scalp/status",
            "/api/journal/2026-08-13",
            "/api/engines/all",
            "/api/broker/trades",
            "/api/two-red/status",
            "/api/fib-space/paper/status",
        ):
            self.assertTrue(auth.viewer_reads_owner_data("GET", path), path)

    def test_who_you_are_is_never_borrowed_from_the_owner(self):
        for path in (
            "/api/auth/status",
            "/api/auth/passkeys",
            "/api/user/profile",
            "/api/user/execution-ip-status",
            "/api/admin/users",
        ):
            self.assertFalse(auth.viewer_reads_owner_data("GET", path), path)

    def test_sharing_is_reads_only(self):
        """Redirecting a WRITE at the owner's account would be catastrophic."""
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertFalse(auth.viewer_reads_owner_data(method, "/api/strategies"))
            self.assertFalse(auth.viewer_reads_owner_data(method, "/api/live/start"))

    def test_an_unknown_future_read_is_not_shared(self):
        """Fails closed: a new route shows a viewer nothing, never too much."""
        self.assertFalse(auth.viewer_reads_owner_data("GET", "/api/something/invented/next-year"))

    def test_the_balance_is_refused_outright(self):
        """Not merely unshared -- these fall back to the admin's broker."""
        self.assertFalse(auth.viewer_may_read("/api/funds"))
        self.assertFalse(auth.viewer_may_read("/api/portfolio/summary"))

    def test_everything_else_is_still_readable(self):
        for path in ("/api/portfolio/history", "/api/positions", "/api/live/status", "/app"):
            self.assertTrue(auth.viewer_may_read(path), path)

    def test_no_shared_prefix_ever_reaches_a_private_one(self):
        for private in auth.VIEWER_PRIVATE_READS:
            self.assertFalse(
                any(private.startswith(shared) for shared in auth.VIEWER_SHARED_READ_PREFIXES),
                f"{private} is reachable through the shared allowlist",
            )


if __name__ == "__main__":
    unittest.main()
