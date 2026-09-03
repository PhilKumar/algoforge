"""A trade is announced on Telegram once, however many times the app restarts.

Phil, 2026-09-03: "Why again and again I getting this telegram message for the
exit trade?"

Because the alert tracker was an in-memory dict holding a COUNT:

    _alert_state: Dict[str, dict] = {}          # {"in_trade", "closed_count"}
    ...
    if new_count > prev["closed_count"]:
        for t in closed_trades[prev["closed_count"]:]:
            alerter.alert("Trade Exit", ...)

Nothing persisted it. Every process restart emptied the dict, the restored
engine came back with the day's `closed_trades` still in its list, and the
whole list read as new -- so each deploy re-announced every exit that had
already happened. There were sixteen process starts on 03-Sep, and the 10:45
exit went out again on the 11:40 restart.

A count is the wrong thing to remember for a second reason: it assumes the list
only ever grows, in order. What is remembered now is WHICH trades have been
announced, by their own identity, in `app_state`.
"""

import asyncio
import json
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

EXIT = {
    "id": 1,
    "symbol": "NIFTY 23750 CE",
    "exit_time": "2026-09-03T10:45:02",
    "exit_reason": "EXIT_SIGNAL",
    "pnl": -1723.80,
}


class AlertHarness(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.store = {}
        self._alert = app_module.alerter.alert
        self._get = app_module._db_mod.get_app_state
        self._set = app_module._db_mod.set_app_state

        app_module.alerter.alert = lambda title, body, level="error": self.sent.append((title, body))

        async def _get(key):
            return self.store.get(key)

        async def _set(key, value):
            self.store[key] = value

        app_module._db_mod.get_app_state = _get
        app_module._db_mod.set_app_state = _set
        app_module._alert_state.clear()

    def tearDown(self):
        app_module.alerter.alert = self._alert
        app_module._db_mod.get_app_state = self._get
        app_module._db_mod.set_app_state = self._set
        app_module._alert_state.clear()

    def fire(self, event, *, restart=False):
        if restart:
            app_module._alert_state.clear()  # what a process restart does
        asyncio.run(app_module._check_trade_alerts("PE_NoTarget", "Auto (LIVE)", dict(event), user_id=1))

    def event(self, *trades, in_trade=False):
        return {
            "type": "trade_closed",
            "in_trade": in_trade,
            "closed_trades": list(trades),
            "positions": [],
            "total_pnl": sum(t.get("pnl", 0) for t in trades),
        }

    def exits(self):
        return [b for t, b in self.sent if t == "Trade Exit"]


class OneExitIsOneMessage(AlertHarness):
    def test_the_exit_is_announced(self):
        self.fire(self.event(EXIT))
        self.assertEqual(len(self.exits()), 1)

    def test_further_status_events_in_the_same_process_say_nothing(self):
        for _ in range(4):
            self.fire(self.event(EXIT))
        self.assertEqual(len(self.exits()), 1)

    def test_sixteen_restarts_do_not_make_sixteen_messages(self):
        """03-Sep had sixteen process starts; the 10:45 exit went out twice."""
        self.fire(self.event(EXIT))
        for _ in range(16):
            self.fire(self.event(EXIT), restart=True)
        self.assertEqual(len(self.exits()), 1, "a restart must not re-announce a trade already sent")

    def test_a_genuinely_new_exit_still_gets_through(self):
        """The fix must not buy silence by suppressing real trades."""
        self.fire(self.event(EXIT))
        second = {
            "id": 2,
            "symbol": "NIFTY 23800 PE",
            "exit_time": "2026-09-03T14:10:00",
            "exit_reason": "STOP_LOSS",
            "pnl": -410.0,
        }
        self.fire(self.event(EXIT, second), restart=True)
        self.assertEqual(len(self.exits()), 2)
        self.assertIn("23800 PE", self.exits()[1])

    def test_a_reordered_list_does_not_replay_it(self):
        """A count assumed the list only grows, in order. This does not."""
        second = {"id": 2, "symbol": "NIFTY 23800 PE", "exit_time": "2026-09-03T14:10:00", "pnl": -410.0}
        self.fire(self.event(EXIT, second))
        self.assertEqual(len(self.exits()), 2)
        self.fire(self.event(second, EXIT), restart=True)  # same two, other order
        self.assertEqual(len(self.exits()), 2)


class TheEntryAlertToo(AlertHarness):
    def test_an_open_position_is_not_re_announced_on_restart(self):
        """`in_trade and not prev["in_trade"]` had the same amnesia."""
        self.fire(self.event(in_trade=True))
        self.fire(self.event(in_trade=True), restart=True)
        self.assertEqual(len([t for t, _ in self.sent if t == "Trade Entry"]), 1)


class WhatIsWrittenDown(AlertHarness):
    def test_it_is_persisted_under_its_own_key(self):
        self.fire(self.event(EXIT))
        self.assertIn("trade_alerts:1:PE_NoTarget", self.store)

    def test_the_record_names_the_trades_not_a_count(self):
        self.fire(self.event(EXIT))
        stored = json.loads(self.store["trade_alerts:1:PE_NoTarget"])
        self.assertNotIn("closed_count", stored)
        self.assertEqual(len(stored["seen"]), 1)
        self.assertIn("EXIT_SIGNAL", stored["seen"][0])

    def test_the_set_cannot_grow_without_bound(self):
        cap = app_module._ALERT_SEEN_CAP
        many = [dict(EXIT, id=i, exit_time=f"t{i}") for i in range(cap + 40)]
        self.fire(self.event(*many))
        stored = json.loads(self.store["trade_alerts:1:PE_NoTarget"])
        self.assertEqual(len(stored["seen"]), cap)

    def test_a_database_failure_costs_an_alert_not_the_run(self):
        async def _boom(*_a, **_k):
            raise RuntimeError("db down")

        app_module._db_mod.get_app_state = _boom
        app_module._db_mod.set_app_state = _boom
        self.fire(self.event(EXIT))  # must not raise
        self.assertEqual(len(self.exits()), 1)


class TheCallersAwaitIt(unittest.TestCase):
    def test_no_call_site_was_left_synchronous(self):
        """A forgotten `await` here is a coroutine that never runs: silence."""
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("await _check_trade_alerts("), 4)
        self.assertNotIn("\n    _check_trade_alerts(", src)
        self.assertNotIn("\n        _check_trade_alerts(", src)


if __name__ == "__main__":
    unittest.main()
