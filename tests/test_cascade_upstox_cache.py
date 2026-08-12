"""Two Upstox premium sources with different underlyings must not read each
other's cached expiry list or option chain when they share a root cache dir.

This is the regression guard for the namespace collision: expiries.json and
contracts_<expiry>.json are keyed by underlying, so without a per-underlying
folder a BankNifty source (monthly, ~55000 strikes) would read a NIFTY source's
weekly expiries and ~24000 strikes off the shared root. Everything below primes
the disk cache directly, so no token or network is required.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from data.cascade_upstox import NIFTY_INDEX_KEY, UpstoxAccessError, UpstoxPremiumSource

BANKNIFTY_KEY = "NSE_INDEX|Nifty Bank"


def _source(cache_dir: Path, underlying_key: str) -> UpstoxPremiumSource:
    # token="dummy" skips .env; no request is made because the cache is primed.
    return UpstoxPremiumSource(token="dummy", cache_dir=cache_dir, underlying_key=underlying_key)


def test_shared_cache_dir_isolates_expiries_and_contracts(tmp_path: Path) -> None:
    nifty = _source(tmp_path, NIFTY_INDEX_KEY)
    bank = _source(tmp_path, BANKNIFTY_KEY)

    # Distinct meta dirs are the whole point: NIFTY keeps the legacy root,
    # BankNifty gets its own subdir, so their cache files can never be the
    # same path.
    assert nifty._meta_dir == tmp_path
    assert bank._meta_dir != nifty._meta_dir
    # Same filename, different directory -> the two caches never share a path.
    assert (nifty._meta_dir / "expiries.json") != (bank._meta_dir / "expiries.json")

    expiry = date(2026, 7, 30)

    # Prime each source's cache with values only that underlying could have:
    # a weekly NIFTY expiry + ~24000 strikes vs. a monthly BankNifty expiry +
    # ~55000 strikes.
    (nifty._meta_dir / "expiries.json").write_text(json.dumps(["2026-07-30"]))
    (bank._meta_dir / "expiries.json").write_text(json.dumps(["2026-07-31"]))
    (nifty._meta_dir / f"contracts_{expiry.isoformat()}.json").write_text(
        json.dumps({"24000|CE": "NSE_FO|nifty_24000_ce"})
    )
    (bank._meta_dir / f"contracts_{expiry.isoformat()}.json").write_text(
        json.dumps({"55000|CE": "NSE_FO|bank_55000_ce"})
    )

    # Each source reads its own file, never the other's.
    assert nifty.available_expiries() == {date(2026, 7, 30)}
    assert bank.available_expiries() == {date(2026, 7, 31)}

    nifty_chain = nifty._contract_index(expiry)
    bank_chain = bank._contract_index(expiry)
    assert nifty_chain == {(24000, "CE"): "NSE_FO|nifty_24000_ce"}
    assert bank_chain == {(55000, "CE"): "NSE_FO|bank_55000_ce"}


def test_nifty_meta_dir_is_backward_compatible_root(tmp_path: Path) -> None:
    # NIFTY must keep writing to the root so its existing 244-file cache is
    # still found -- no re-fetch, no orphaned subdir.
    nifty = _source(tmp_path, NIFTY_INDEX_KEY)
    (tmp_path / "expiries.json").write_text(json.dumps(["2026-07-30"]))
    assert nifty.available_expiries() == {date(2026, 7, 30)}


def test_cache_only_mode_never_fetches_or_writes_a_cache_miss(tmp_path: Path) -> None:
    source = UpstoxPremiumSource(cache_only=True, cache_dir=tmp_path)
    expiry = date(2026, 7, 30)

    # No token is needed and a missing chain is an honest empty lookup, not an
    # Upstox request or a newly-created empty cache file.
    assert source._contract_index(expiry) == {}
    assert not (tmp_path / f"contracts_{expiry.isoformat()}.json").exists()
    assert source._minute_series("NSE_FO|missing", expiry) == {}
    assert not (tmp_path / "candles_NSE_FO_missing.json").exists()
    assert source.requests_made == 0

    # Coverage metadata is required for callers that ask for it; cache-only
    # mode must refuse the miss before any network request is attempted.
    with pytest.raises(UpstoxAccessError):
        source.available_expiries()
    assert source.requests_made == 0


def test_release_memory_drops_parsed_candles_but_keeps_contract_metadata(tmp_path: Path) -> None:
    source = _source(tmp_path, NIFTY_INDEX_KEY)
    expiry = date(2026, 7, 30)
    source._series["NSE_FO|contract"] = {object(): object()}
    source._contracts[expiry] = {(25000, "CE"): "NSE_FO|contract"}

    source.release_memory()

    assert source._series == {}
    assert source._contracts[expiry] == {(25000, "CE"): "NSE_FO|contract"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
