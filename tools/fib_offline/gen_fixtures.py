"""Call the REAL fib-boundary routes in-process with an offline broker/adapter.

Produces the exact JSON the page receives for: backtest, paper start, status,
chart. Zero Dhan calls (cached candles), zero Upstox calls (local archive).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SCRATCH = os.environ.get("FIB_OFFLINE_SCRATCH") or os.path.join(tempfile.gettempdir(), "fib_offline")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, SCRATCH)
os.chdir(ROOT)

TEST_DB = Path(f"{SCRATCH}/fixtures.db")
TEST_USER_DATA = Path(f"{SCRATCH}/fixtures-data")
os.environ["PHILFORGE_PIN"] = "123456"
os.environ["PHILFORGE_DB"] = str(TEST_DB)
os.environ["PHILFORGE_USER_DATA_ROOT"] = str(TEST_USER_DATA)
os.environ["PHILFORGE_SKIP_STARTUP_JOBS"] = "1"
os.environ["ENCRYPTION_KEY"] = "QmG8YWqLPtWFDn7gCAiHJXoX7zH5zi89kUnkkMvibU="
os.environ["DHAN_PIN"] = ""
os.environ["DHAN_TOTP_SECRET"] = ""
os.environ["DHAN_ACCESS_TOKEN"] = "offline-dummy"

from fib_replay import load  # noqa: E402

import app as app_module  # noqa: E402
from data.cascade_upstox import UpstoxPremiumSource  # noqa: E402

MOTHER = sys.argv[1] if len(sys.argv) > 1 else "2026-07-23T11:15"
TF = sys.argv[2] if len(sys.argv) > 2 else "5m"
SIDE = sys.argv[3] if len(sys.argv) > 3 else "CE"
MODE = sys.argv[4] if len(sys.argv) > 4 else "levels"
OUT = Path(sys.argv[5] if len(sys.argv) > 5 else f"{SCRATCH}/fixtures")
OUT.mkdir(exist_ok=True)

_cache = {}


def _candles(tf):
    if tf not in _cache:
        _cache[tf] = load(tf)
    return _cache[tf]


class OfflineAdapter:
    def __init__(self, *_a, **_k):
        pass

    async def async_get_candles(self, _symbol, timeframe="5m", *, from_date=None, to_date=None, now=None):
        tf = str(timeframe).lower()
        rows = [
            app_module.IndexCandle(c.timestamp.replace(tzinfo=app_module.IST), c.open, c.high, c.low, c.close)
            for c in _candles(tf)
            if (from_date is None or c.timestamp.date() >= from_date)
            and (to_date is None or c.timestamp.date() <= to_date)
        ]
        return rows


src = UpstoxPremiumSource(cache_only=True, backfill_missing=False)
EXPIRIES = sorted(src.available_expiries())


def offline_history(_broker, _symbol, _from, _to):
    def lookup(when, contract):
        bar = src.lookup(when, contract)
        return float(bar.open) if bar is not None and bar.open > 0 else None

    lookup.source_failures = []
    lookup.stale_fills = []
    return lookup


def offline_expiries(_broker, _symbol):
    def source(on):
        return [e for e in EXPIRIES if e >= on]

    return source


class DummyRequest:
    def __init__(self, user_id=11):
        self.state = SimpleNamespace(user_id=user_id)


async def main():
    if TEST_DB.exists():
        TEST_DB.unlink()
    if TEST_USER_DATA.exists():
        shutil.rmtree(TEST_USER_DATA)
    app_module.config.DB_PATH = str(TEST_DB)
    app_module.config.USER_DATA_ROOT = str(TEST_USER_DATA)
    app_module._USER_DATA_ROOT = str(TEST_USER_DATA)
    app_module._db_mod.config.DB_PATH = str(TEST_DB)
    app_module._db_mod.config.USER_DATA_ROOT = str(TEST_USER_DATA)
    app_module._db_mod._initialized = False
    await app_module._db_mod.init_db()

    broker = SimpleNamespace(get_option_ltp=lambda *a, **k: None)
    with (
        patch.object(app_module, "CascadeOptionsAdapter", OfflineAdapter),
        patch.object(app_module, "_request_broker_context", AsyncMock(return_value=({"id": 11}, broker, "user"))),
        patch.object(app_module, "_fib_touch_history_lookup", offline_history),
        patch.object(app_module, "_fib_touch_expiry_source", offline_expiries),
        patch(
            "broker.dhan.ScripMaster.get_expiries",
            lambda *_a, **_k: [e.isoformat() for e in EXPIRIES if e >= date(2026, 7, 1)],
        ),
        patch("broker.dhan.ScripMaster.get_lot_size", lambda *_a, **_k: 65),
    ):
        # ---- backtest
        bt_payload = app_module.FibTouchBacktestPayload(
            symbol="NIFTY",
            side=SIDE,
            mother_timestamp=MOTHER,
            timeframe=TF,
            capital_cap_inr=75000,
            itm_steps=2,
            min_dte=4,
            horizon_days=10,
            buy_mode=MODE,
            intraday_close=True,
        )
        bt = await app_module.fib_boundary_backtest(bt_payload, DummyRequest())
        (OUT / "backtest.json").write_text(json.dumps(bt, default=str))
        c = bt["campaign"]
        print(
            "BACKTEST",
            c["status"],
            c.get("exit_reason"),
            "net",
            c.get("net_pnl"),
            "fibs",
            len(c.get("fibs") or []),
            "fills",
            len(c.get("fills") or []),
            "rounds",
            len(c.get("rounds") or []),
            "gaps",
            c.get("data_gaps"),
        )
        print("  premium_failures", bt["premium_failures"], "stale", bt["premium_stale_fills"][:3])

        # ---- paper start (a past mother replays to today's date through the same engine)
        st_payload = app_module.FibTouchStartPayload(
            symbol="NIFTY",
            side=SIDE,
            mother_timestamp=MOTHER,
            timeframe=TF,
            capital_cap_inr=75000,
            itm_steps=2,
            min_dte=4,
            mode="paper",
            buy_mode=MODE,
            intraday_close=True,
        )
        started = await app_module.fib_boundary_paper_start(st_payload, DummyRequest())
        (OUT / "paper_start.json").write_text(json.dumps(started, default=str))
        camp = started["campaign"]
        print(
            "PAPER START",
            camp["status"],
            camp.get("exit_reason"),
            "net",
            camp.get("net_pnl"),
            "running",
            camp["running"],
            "fibs",
            len(camp.get("fibs") or []),
            "rounds",
            len(camp.get("rounds") or []),
        )
        # let the loop tick once (it will fetch from the offline adapter) then stop it
        await asyncio.sleep(0.5)
        status = await app_module.fib_boundary_paper_status(DummyRequest())
        (OUT / "paper_status.json").write_text(json.dumps(status, default=str))
        print("PAPER STATUS", [(x["symbol"], x["status"], x["running"]) for x in status["campaigns"]])
        for ladders in app_module._fib_boundary_engines.values():
            for rt in ladders.values():
                rt.running = False
                if rt.task:
                    rt.task.cancel()

        # ---- chart
        chart = await app_module.fib_boundary_paper_chart(
            MOTHER, DummyRequest(), symbol="NIFTY", side=SIDE, timeframe=TF, base_timeframe=TF, buy_mode=MODE
        )
        (OUT / "chart.json").write_text(json.dumps(chart, default=str))
        print(
            "CHART candles",
            len(chart["candles"]),
            "fibs",
            len(chart.get("fibs") or []),
            "levels",
            len(chart.get("levels") or []),
        )

        # ---- the sanity check Phil cares about: same mother, same engine -> backtest and paper agree
        same = (
            c["status"] == camp["status"]
            and c.get("net_pnl") == camp.get("net_pnl")
            and len(c.get("rounds") or []) == len(camp.get("rounds") or [])
        )
        print("BACKTEST == PAPER on this mother:", same)


asyncio.run(main())
