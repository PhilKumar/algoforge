"""tools/fib_offline/upstox_listed.py -- premiums for contracts that have NOT expired.

The Upstox option archive (data/cascade_upstox) holds only EXPIRED contracts,
so a local replay of a mother from the last week could resolve no expiry
("no expiry at least 4 days after 2026-08-17", 1,798 times on Phil's screen)
and bought nothing where the live server, reading Dhan, would have. Upstox's
own historical-candle API serves LISTED contracts as well, given the
instrument key from its NSE instrument master. This module joins the two:

    source = ListedPremiumSource(archive)      # archive = UpstoxPremiumSource
    source.expiries()                          # archive ∪ listed
    source.lookup(when, contract) -> open or None

Listed-contract days are cached under tools/.nifty_cache/upstox_listed/ so a
sweep does not refetch the same contract-day. Read-only token from .env.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import date, datetime
from typing import Any, Optional

import requests

_log = logging.getLogger("philforge.fib_offline.listed")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_CACHE = os.path.join(_ROOT, "tools", ".nifty_cache", "upstox_listed")
_MASTER = os.path.join(_CACHE, "nse_master.json")
_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_ENV = os.path.join(_ROOT, ".env")


def _token() -> str:
    try:
        for line in open(_ENV):
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get("UPSTOX_ACCESS_TOKEN", "")


def _expiry_day(value: Any) -> Optional[date]:
    """Upstox stamps expiries as epoch millis; older dumps as ISO text."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value) / 1000).date()
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class ListedPremiumSource:
    def __init__(self, archive: Any, *, symbol: str = "NIFTY") -> None:
        self.archive = archive
        self.symbol = symbol.upper()
        self._archive_expiries = set(archive.available_expiries())
        self._keys: dict[tuple[date, float, str], str] = {}
        self._listed_expiries: set[date] = set()
        self._days: dict[tuple[str, date], dict[str, float]] = {}
        self._token = _token()
        os.makedirs(_CACHE, exist_ok=True)
        self._load_master()

    # -- instrument master ------------------------------------------------
    def _load_master(self) -> None:
        rows = None
        try:
            if os.path.exists(_MASTER) and time.time() - os.path.getmtime(_MASTER) < 24 * 3600:
                rows = json.load(open(_MASTER))
        except Exception:
            rows = None
        if rows is None:
            try:
                r = requests.get(_MASTER_URL, timeout=90)
                r.raise_for_status()
                data = json.loads(gzip.decompress(r.content))
                rows = [
                    {
                        "expiry": d.get("expiry"),
                        "strike": d.get("strike_price"),
                        "type": d.get("instrument_type"),
                        "key": d.get("instrument_key"),
                    }
                    for d in data
                    if d.get("segment") == "NSE_FO"
                    and d.get("underlying_symbol") == self.symbol
                    and d.get("instrument_type") in ("CE", "PE")
                ]
                json.dump(rows, open(_MASTER, "w"))
            except Exception as exc:
                _log.warning("[FIB OFFLINE] Upstox instrument master unavailable: %s", exc)
                rows = []
        for d in rows:
            day = _expiry_day(d.get("expiry"))
            if day is None:
                continue
            self._keys[(day, float(d.get("strike") or 0), str(d.get("type")))] = str(d.get("key"))
            self._listed_expiries.add(day)

    # -- public --------------------------------------------------------------
    def expiries(self) -> list[date]:
        return sorted(self._archive_expiries | self._listed_expiries)

    def lookup(self, when: datetime, contract: Any) -> Optional[float]:
        """The contract's OPEN at ``when``'s minute, or None. Archive first;
        a listed contract from Upstox's live historical API."""
        expiry = contract.expiry
        if expiry in self._archive_expiries:
            bar = self.archive.lookup(when, contract)
            return float(bar.open) if bar is not None and bar.open > 0 else None
        key = self._keys.get((expiry, float(contract.strike), str(contract.option_type).upper()))
        if key is None or not self._token:
            return None
        day = self._day(key, when.date())
        price = day.get(when.strftime("%Y-%m-%dT%H:%M"))
        return float(price) if price and price > 0 else None

    def _day(self, key: str, day: date) -> dict[str, float]:
        k = (key, day)
        if k in self._days:
            return self._days[k]
        path = os.path.join(_CACHE, f"{key.replace('|', '_')}_{day.isoformat()}.json")
        series: dict[str, float] = {}
        if os.path.exists(path):
            try:
                series = json.load(open(path))
            except Exception:
                series = {}
        else:
            url = f"https://api.upstox.com/v3/historical-candle/{key.replace('|', '%7C')}/minutes/1/{day.isoformat()}/{day.isoformat()}"
            try:
                r = requests.get(
                    url, headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"}, timeout=30
                )
                if r.status_code == 200:
                    for c in r.json().get("data", {}).get("candles", []):
                        series[str(c[0])[:16]] = float(c[1])
                    # Only a COMPLETED day is worth caching on disk; today's is still growing.
                    if day < date.today():
                        json.dump(series, open(path, "w"))
                else:
                    _log.warning("[FIB OFFLINE] Upstox %s %s: HTTP %s", key, day, r.status_code)
            except Exception as exc:
                _log.warning("[FIB OFFLINE] Upstox %s %s: %s", key, day, exc)
        self._days[k] = series
        return series
