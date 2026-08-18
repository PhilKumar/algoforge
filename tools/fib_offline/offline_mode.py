"""tools/fib_offline/offline_mode.py -- run the Fib Boundary page with NO broker.

Phil, 2026-08-18: "Give me a page locally so that I can make modifications."
The page's routes want a Dhan client for index candles and a live token for
premiums; neither exists on a laptop, and minting a Dhan token locally kills
the live server's (there is ONE per client). So with

    PHILFORGE_FIB_OFFLINE=1

`app.py` calls :func:`install` at import and the three Fib Boundary routes --
backtest, Start (paper) and the chart -- read index candles from
``tools/.nifty_cache`` (real Dhan candles, plus the Upstox-fetched extension)
and premiums from the local Upstox option archive (back-filling an expired
contract from Upstox when the archive lacks it, with the read-only token in
``.env``). It is the same code path the live server runs, on the same
`FibTouchLadder`; only the data sources are swapped. Nothing here is imported
unless the flag is set, and the deploy never sets it.

Start with ``tools/run_fib_offline_server.sh`` (port 8769, admin / 123456).
"""

from __future__ import annotations

import glob
import json
import logging
import os
from datetime import date, datetime
from typing import Any, Optional

_log = logging.getLogger("philforge.fib_offline")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
CACHE_DIR = os.path.join(_ROOT, "tools", ".nifty_cache")

_candles: dict[str, list] = {}


def _load(symbol: str, tf: str, index_candle_cls, tz) -> list:
    """Every cached bar for a symbol/timeframe, merged across files (the cache
    is split by year, and taking one file silently returns the wrong year --
    see the memory note on the widest-file trap), session hours only, IST-aware."""
    key = f"{symbol}:{tf}"
    if key in _candles:
        return _candles[key]
    rows: dict[str, list] = {}
    for path in glob.glob(os.path.join(CACHE_DIR, f"{symbol}_{tf}_*.json")):
        try:
            data = json.load(open(path))
        except Exception as exc:  # a broken file is skipped, not fatal
            _log.warning("[FIB OFFLINE] unreadable cache %s: %s", path, exc)
            continue
        for row in data if isinstance(data, list) else data.get("candles") or data.get("data") or []:
            rows[row[0]] = row
    out = []
    for stamp in sorted(rows):
        r = rows[stamp]
        ts = datetime.fromisoformat(r[0])
        if not ("09:15:00" <= r[0][11:19] < "15:30:00"):
            continue
        out.append(index_candle_cls(ts.replace(tzinfo=tz), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    _candles[key] = out
    _log.info(
        "[FIB OFFLINE] %s %s: %d cached bars (%s .. %s)",
        symbol,
        tf,
        len(out),
        out[0].timestamp if out else None,
        out[-1].timestamp if out else None,
    )
    return out


class OfflineBroker:
    """A broker that answers nothing live: no LTP, no orders. Anything the
    Fib Boundary routes do not touch raises plainly instead of pretending."""

    def get_option_ltp(self, *_a, **_k):
        return None

    def __getattr__(self, name):
        def _missing(*_a, **_k):
            raise RuntimeError(f"offline mode: broker.{name} is not available")

        return _missing


def install(app_module: Any) -> None:
    """Swap the Fib Boundary data sources on an imported ``app`` module."""
    from data.cascade_upstox import UpstoxAccessError, UpstoxPremiumSource

    tz = app_module.IST
    index_candle_cls = app_module.IndexCandle
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}

    class OfflineAdapter:
        def __init__(self, *_a, **_k) -> None:
            pass

        async def async_get_candles(self, symbol, timeframe="5m", *, from_date=None, to_date=None, now=None):
            tf = str(timeframe).lower()
            current = now or datetime.now(tz)
            rows = _load(str(symbol).upper(), tf, index_candle_cls, tz)
            out = []
            for c in rows:
                d = c.timestamp.date()
                if from_date is not None and d < from_date:
                    continue
                if to_date is not None and d > to_date:
                    continue
                # Only CLOSED bars, like the real adapter.
                width = 15 if tf == "1h" and c.timestamp.hour == 15 else tf_minutes.get(tf, 5)
                if c.timestamp + app_module.timedelta(minutes=width) > current:
                    continue
                out.append(c)
            return out

    try:
        source = UpstoxPremiumSource(cache_only=False, backfill_missing=True)
    except UpstoxAccessError as exc:
        _log.warning("[FIB OFFLINE] Upstox token unusable, archive only: %s", exc)
        source = UpstoxPremiumSource(cache_only=True, backfill_missing=False)
    expiries = sorted(source.available_expiries())

    def history_lookup(_broker, _symbol, _from, _to):
        def lookup(when: datetime, contract) -> Optional[float]:
            stamp = when.replace(tzinfo=None) if when.tzinfo is not None else when
            bar = source.lookup(stamp, contract)
            return float(bar.open) if bar is not None and bar.open > 0 else None

        lookup.source_failures = []
        lookup.stale_fills = []
        return lookup

    def expiry_source(_broker, _symbol):
        def on(day: date):
            return [e for e in expiries if e >= day]

        return on

    async def broker_context(request):
        return ({"id": app_module._request_user_id(request)}, OfflineBroker(), "offline")

    app_module.CascadeOptionsAdapter = OfflineAdapter
    app_module._request_broker_context = broker_context
    app_module._fib_touch_history_lookup = history_lookup
    app_module._fib_touch_expiry_source = expiry_source
    try:
        from broker.dhan import ScripMaster

        ScripMaster.get_expiries = classmethod(lambda cls, *_a, **_k: [e.isoformat() for e in expiries])
        ScripMaster.get_lot_size = classmethod(lambda cls, symbol, expiry, *_a, **_k: int(_lot(str(symbol), expiry)))
    except Exception as exc:
        _log.warning("[FIB OFFLINE] ScripMaster not patched: %s", exc)
    _log.warning(
        "[FIB OFFLINE] Fib Boundary routes are reading the LOCAL cache and archive -- no broker, no live token."
    )


def _lot(symbol: str, expiry) -> int:
    from engine.backtest import get_lot_size

    day = date.fromisoformat(str(expiry)[:10]) if expiry else date.today()
    return int(get_lot_size(symbol.upper(), day))
