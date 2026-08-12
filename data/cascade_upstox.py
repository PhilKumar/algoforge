"""
data/cascade_upstox.py -- Layer 2 premium source for the NIFTY option cascade.

The cascade backtest (tools/cascade_backtest_nifty.py) computes every decision
from index candles alone and then asks one question of a premium source:

    option_lookup(timestamp, contract) -> OptionCandle | None

This module answers that question with REAL 1-minute premiums for the exact
fixed strike and expiry the engine chose, pulled from Upstox's expired-
instruments history. A held call at 24000 CE on a specific weekly expiry is one
instrument with one price series; Dhan's rolling ATM feed cannot price it (see
data/cascade_dhan.py, which correctly refuses to pretend otherwise). Upstox can.

Design rules, each of which is a way a premium backtest lies if you skip it:

  * A strike/type the exchange never listed for that expiry is a GAP, not a
    zero. lookup() returns None; the engine records a data gap and leaves that
    leg unpriced rather than inventing a fill.
  * A minute with no candle (near-ATM empty-but-200 responses were reported for
    exactly this endpoint) is also a gap, never a zero.
  * Nothing here decides anything. It only prices what the index engine already
    decided. It reads no index candles and holds no strategy state.

Every contract list and every 1-minute series is cached under tools/.upstox_cache
so a second run is offline and the numbers move only when the rules do. Requires
UPSTOX_ACCESS_TOKEN (the read-only Analytics Token) in .env. See
tools/upstox_probe.py for the live-access check.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from data.option_archive import OptionDataArchive
from engine.cascade_options import Contract, OptionCandle

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
CACHE_DIR = REPO_ROOT / "tools" / ".upstox_cache"

BASE = "https://api.upstox.com/v2"
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


def _underlying_slug(underlying_key: str) -> str:
    """A filesystem-safe folder name unique to one underlying, e.g.
    "NSE_INDEX|Nifty Bank" -> "nse_index_nifty_bank". Used to namespace the
    per-underlying caches (expiry list, option chains) so two underlyings can
    share a root cache dir without reading each other's contracts."""
    return re.sub(r"[^a-z0-9]+", "_", underlying_key.lower()).strip("_")


# How far back from expiry one contract's minute history is fetched.
#
# This was 20 days, sized for the WEEKLY cascade's 7-13 DTE window, and it
# silently truncated every MONTHLY 15-45 DTE replay: an entry 34 days before
# expiry fell outside the fetched range, so the leg came back unpriceable even
# though the strike was listed and liquid.  That alone made ~75% of the
# converging-space grid unpriceable (2026-08-02), and the failure looked like
# "no bar at that minute" rather than "we never asked for that date".
#
# The 3000-bar-per-call limit the old comment cited does not hold: measured
# against 24050CE/28-11-2024, a 20-day call returns 4,872 bars and a 60-day
# call returns 9,320 back to 2024-09-30.  60 days covers the whole 15-45 DTE
# window with room to spare; a contract with less history simply returns less.
_HISTORY_LOOKBACK_DAYS = int(os.environ.get("UPSTOX_HISTORY_LOOKBACK_DAYS", "60"))


class UpstoxAccessError(RuntimeError):
    """Raised when the token is missing or Upstox refuses a request outright."""


def _read_token() -> str:
    if not ENV_PATH.exists():
        raise UpstoxAccessError(f"No {ENV_PATH}; put UPSTOX_ACCESS_TOKEN in it first.")
    for line in ENV_PATH.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "UPSTOX_ACCESS_TOKEN":
            cleaned = value.strip().strip('"').strip("'")
            if cleaned:
                return cleaned
    raise UpstoxAccessError(
        f"UPSTOX_ACCESS_TOKEN missing or empty in {ENV_PATH}. Copy it from "
        "https://account.upstox.com/developer/apps -> Analytics."
    )


def _minute_key(moment: datetime) -> datetime:
    """Naive IST minute. Upstox stamps +05:30 and the index engine works in IST,
    so both collapse to the same wall-clock minute once tzinfo is dropped."""
    return moment.replace(tzinfo=None, second=0, microsecond=0)


class UpstoxPremiumSource:
    """Fixed-strike 1-minute premium lookups, cached, gap-honest.

    Pass ``.lookup`` (a bound method matching ``OptionLookup``) straight to the
    backtest's ``option_lookup`` argument.
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        cache_dir: Path = CACHE_DIR,
        underlying_key: str = NIFTY_INDEX_KEY,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        cache_only: bool = False,
        backfill_missing: bool = False,
    ) -> None:
        # A completed cache is useful long after Upstox's daily token expires.
        # Backtests using this mode must not silently fall through to the
        # network: a cache miss is reported as a pricing gap instead.
        self._cache_only = bool(cache_only)
        self._backfill_missing = bool(backfill_missing) and not self._cache_only
        self._token = token or ("" if self._cache_only else _read_token())
        self._underlying = underlying_key
        self._timeout = timeout
        self._session = session or requests.Session()
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # The expiry list and option chains are keyed by underlying, so they must
        # be namespaced or a second underlying sharing this root would read the
        # first's contracts (NIFTY weekly vs. BankNifty monthly; ~24000 vs.
        # ~55000 strikes). The default NIFTY keeps the root so its existing
        # cache needs no re-fetch; every other underlying gets its own subdir.
        # Candles are NOT namespaced here -- their filename carries the globally
        # unique instrument_key, so they stay at the root cache dir.
        if self._underlying == NIFTY_INDEX_KEY:
            self._meta_dir = self._cache_dir
        else:
            self._meta_dir = self._cache_dir / _underlying_slug(self._underlying)
            self._meta_dir.mkdir(parents=True, exist_ok=True)

        # In-memory memoisation on top of the disk cache.
        self._contracts: dict[date, dict[tuple[int, str], str]] = {}
        self._series: dict[str, dict[datetime, OptionCandle]] = {}
        self._expiries: Optional[set[date]] = None
        self._refreshed_contracts: set[date] = set()
        self._refreshed_series: set[str] = set()
        archive_root = None if self._cache_dir.resolve() == CACHE_DIR.resolve() else self._cache_dir / "option_archive"
        self._archive = OptionDataArchive(archive_root)
        self._archive_series: dict[tuple[str, date, int, str], dict[datetime, dict]] = {}
        self._archive_written: set[str] = set()

        # Observability: the engine already counts gaps, but knowing *why* a run
        # is thin (strike never listed vs. minute missing) is worth keeping.
        self.missing_contracts = 0
        self.missing_minutes = 0
        self.requests_made = 0

    # ── HTTP ──────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """One GET with a small retry on 429/5xx. Raises on a hard failure so a
        broken run stops loudly instead of silently pricing nothing."""
        if self._cache_only:
            raise UpstoxAccessError(f"Upstox cache has no entry for {path}; offline pricing will not fetch it.")
        url = f"{BASE}{path}"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        last: Optional[str] = None
        for attempt in range(4):
            self.requests_made += 1
            response = self._session.get(url, headers=headers, params=params or {}, timeout=self._timeout)
            if response.status_code == 200:
                body = response.json()
                if isinstance(body, dict) and body.get("status") == "success":
                    return body
                raise UpstoxAccessError(f"{path}: 200 but not success: {str(body)[:300]}")
            if response.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {response.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            raise UpstoxAccessError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
        raise UpstoxAccessError(f"{path}: gave up after retries ({last})")

    # ── expiry coverage ───────────────────────────────────────────

    def available_expiries(self) -> set[date]:
        """The expiries Upstox still holds history for. Anything outside this
        set can never be priced -- the caller should know before a long run."""
        if self._expiries is None:
            cache = self._meta_dir / "expiries.json"
            cached_raw = json.loads(cache.read_text()) if cache.exists() else None
            raw = cached_raw
            if self._cache_only and cached_raw is None:
                raise UpstoxAccessError(
                    f"Upstox cache has no expiry coverage for {self._underlying}; offline pricing cannot continue."
                )
            cache_is_fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < (6 * 60 * 60)
            if not self._cache_only and not cache_is_fresh:
                try:
                    body = self._get("/expired-instruments/expiries", {"instrument_key": self._underlying})
                except UpstoxAccessError:
                    if cached_raw is None:
                        raise
                else:
                    raw = [str(x) for x in (body.get("data") or [])]
                    cache.write_text(json.dumps(raw))
            if raw is None:
                raw = []
            self._expiries = {date.fromisoformat(x) for x in raw}
        return self._expiries

    # ── contract resolution ───────────────────────────────────────

    def _contract_index(self, expiry: date, *, refresh: bool = False) -> dict[tuple[int, str], str]:
        """{(strike, 'CE'|'PE') -> upstox instrument key} for one expiry."""
        if expiry in self._contracts and not refresh:
            return self._contracts[expiry]

        cache = self._meta_dir / f"contracts_{expiry.isoformat()}.json"
        cached_raw = json.loads(cache.read_text()) if cache.exists() else None
        if cached_raw is not None and not refresh:
            raw = cached_raw
        elif self._cache_only:
            self._contracts[expiry] = {}
            return self._contracts[expiry]
        else:
            try:
                body = self._get(
                    "/expired-instruments/option/contract",
                    {"instrument_key": self._underlying, "expiry_date": expiry.isoformat()},
                )
            except UpstoxAccessError:
                # A failed refresh must never erase previously cached history.
                # A brand-new expiry must fail loudly instead of turning an
                # expired token into thousands of silent pricing gaps.
                if cached_raw is None:
                    raise
                raw = cached_raw
            else:
                raw = {}
                for contract in body.get("data") or []:
                    strike = contract.get("strike_price")
                    side = str(contract.get("instrument_type") or "").upper()
                    key = contract.get("expired_instrument_key") or contract.get("instrument_key")
                    if strike is None or side not in {"CE", "PE"} or not key:
                        continue
                    raw[f"{int(round(float(strike)))}|{side}"] = key
                cache.write_text(json.dumps(raw))

        index = {(int(k.split("|")[0]), k.split("|")[1]): v for k, v in raw.items()}
        self._contracts[expiry] = index
        return index

    # ── 1-minute premium series ───────────────────────────────────

    def _minute_series(
        self, instrument_key: str, expiry: date, *, refresh: bool = False
    ) -> dict[datetime, OptionCandle]:
        """{naive-IST minute -> OptionCandle} for one instrument, whole life."""
        if instrument_key in self._series and not refresh:
            return self._series[instrument_key]

        safe = instrument_key.replace("|", "_").replace("/", "_")
        cache = self._cache_dir / f"candles_{safe}.json"
        cached_rows = json.loads(cache.read_text()) if cache.exists() else None
        if cached_rows is not None and not refresh:
            rows = cached_rows
        elif self._cache_only:
            self._series[instrument_key] = {}
            return self._series[instrument_key]
        else:
            to_date = expiry.isoformat()
            from_date = (expiry - timedelta(days=_HISTORY_LOOKBACK_DAYS)).isoformat()
            path = f"/expired-instruments/historical-candle/{instrument_key}/1minute/{to_date}/{from_date}"
            try:
                body = self._get(path)
                rows = (body.get("data") or {}).get("candles") or []
            except UpstoxAccessError:
                # Keep a good cached series when an attempted backfill fails.
                # With no cached series, propagate the provider failure. The
                # caller can report an invalid token immediately rather than
                # spend hours producing an empty backtest.
                if cached_rows is None:
                    raise
                rows = cached_rows
            else:
                cache.write_text(json.dumps(rows))

        series: dict[datetime, OptionCandle] = {}
        for row in rows:
            # [ts, open, high, low, close, volume, open_interest], newest-first.
            moment = _minute_key(datetime.fromisoformat(row[0]))
            series[moment] = OptionCandle(
                timestamp=moment,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            )
        self._series[instrument_key] = series
        return series

    def release_series(self, instrument_key: str) -> None:
        """Release one parsed contract after a strike-selection probe."""
        self._series.pop(instrument_key, None)

    def release_memory(self) -> None:
        """Drop parsed candle objects while retaining the durable disk cache.

        A long premium-target replay visits many weekly expiries. Keeping each
        contract's one-minute candles as Python objects grows until the web
        worker reaches its memory-reclaim ceiling. The selector releases the
        previous expiry here; a later lookup can reopen the same cached JSON
        without another Upstox request.
        """
        self._series.clear()

    # ── the one method the engine calls ───────────────────────────

    def lookup(self, timestamp: datetime, contract: Contract) -> Optional[OptionCandle]:
        """The exact premium bar for ``contract`` at ``timestamp``'s minute, or
        None if that strike/expiry was never listed or that minute has no data.
        Never fabricates a price."""
        archive_key = (
            str(contract.symbol).upper(),
            contract.expiry,
            int(round(contract.strike)),
            str(contract.option_type).upper(),
        )
        if archive_key not in self._archive_series:
            self._archive_series[archive_key] = self._archive.load(
                provider="upstox",
                underlying=archive_key[0],
                expiry=contract.expiry,
                strike=archive_key[2],
                option_type=archive_key[3],
            )
        archived = self._archive_series[archive_key].get(_minute_key(timestamp))
        if archived is not None:
            return OptionCandle(
                timestamp=_minute_key(timestamp),
                open=float(archived["open"]),
                high=float(archived["high"]),
                low=float(archived["low"]),
                close=float(archived["close"]),
            )

        index = self._contract_index(contract.expiry)
        instrument_key = index.get((int(round(contract.strike)), str(contract.option_type).upper()))
        if instrument_key is None and self._backfill_missing and contract.expiry not in self._refreshed_contracts:
            self._refreshed_contracts.add(contract.expiry)
            index = self._contract_index(contract.expiry, refresh=True)
            instrument_key = index.get((int(round(contract.strike)), str(contract.option_type).upper()))
        if instrument_key is None:
            self.missing_contracts += 1
            return None

        series = self._minute_series(instrument_key, contract.expiry)
        bar = series.get(_minute_key(timestamp))
        refreshed = False
        if bar is None and self._backfill_missing and instrument_key not in self._refreshed_series:
            self._refreshed_series.add(instrument_key)
            series = self._minute_series(instrument_key, contract.expiry, refresh=True)
            bar = series.get(_minute_key(timestamp))
            refreshed = True
        if series and (instrument_key not in self._archive_written or refreshed):
            self._archive.store(
                provider="upstox",
                underlying=archive_key[0],
                expiry=contract.expiry,
                strike=archive_key[2],
                option_type=archive_key[3],
                bars=series,
                instrument_key=instrument_key,
            )
            persisted_series = dict(self._archive_series[archive_key])
            persisted_series.update(
                {
                    minute: {
                        "timestamp": minute.isoformat(timespec="minutes"),
                        "open": row.open,
                        "high": row.high,
                        "low": row.low,
                        "close": row.close,
                    }
                    for minute, row in series.items()
                }
            )
            self._archive_series[archive_key] = persisted_series
            self._archive_written.add(instrument_key)
        if bar is None:
            self.missing_minutes += 1
            return None
        return bar
