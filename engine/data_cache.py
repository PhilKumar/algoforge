"""
engine/data_cache.py — Async Historical Data Downloader & Cache
Caches downloaded candle data to local Parquet/JSON files to avoid
re-downloading the same date ranges from Dhan API.
"""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta

import pandas as pd

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".data_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


def _cache_key(
    security_id: str, exchange_segment: str, instrument_type: str, candle_type: str, from_date: str, to_date: str
) -> str:
    """Generate a deterministic cache key for given parameters."""
    raw = f"{security_id}:{exchange_segment}:{instrument_type}:{candle_type}:{from_date}:{to_date}"
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"{key}.parquet")


def _meta_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"{key}.meta.json")


def get_cached(
    security_id: str,
    exchange_segment: str,
    instrument_type: str,
    candle_type: str,
    from_date: str,
    to_date: str,
    max_age_hours: float = 12.0,
) -> pd.DataFrame | None:
    """Return cached DataFrame if it exists and is fresh, else None."""
    key = _cache_key(security_id, exchange_segment, instrument_type, candle_type, from_date, to_date)
    cache_file = _cache_path(key)
    meta_file = _meta_path(key)

    if not os.path.exists(cache_file) or not os.path.exists(meta_file):
        return None

    try:
        with open(meta_file) as f:
            meta = json.load(f)
        cached_at = datetime.fromisoformat(meta["cached_at"])
        if (datetime.now() - cached_at).total_seconds() > max_age_hours * 3600:
            return None  # stale
        df = pd.read_parquet(cache_file)
        if df.index.name != "timestamp":
            df.set_index("timestamp", inplace=True)
        print(f"[CACHE] ✅ Hit: {len(df)} candles ({from_date} → {to_date})")
        return df
    except Exception as e:
        print(f"[CACHE] Read error: {e}")
        return None


def save_to_cache(
    df: pd.DataFrame,
    security_id: str,
    exchange_segment: str,
    instrument_type: str,
    candle_type: str,
    from_date: str,
    to_date: str,
):
    """Save DataFrame to cache."""
    key = _cache_key(security_id, exchange_segment, instrument_type, candle_type, from_date, to_date)
    cache_file = _cache_path(key)
    meta_file = _meta_path(key)

    try:
        df.to_parquet(cache_file, engine="pyarrow")
        with open(meta_file, "w") as f:
            json.dump(
                {
                    "cached_at": datetime.now().isoformat(),
                    "rows": len(df),
                    "security_id": security_id,
                    "exchange_segment": exchange_segment,
                    "candle_type": candle_type,
                    "from_date": from_date,
                    "to_date": to_date,
                },
                f,
            )
        print(f"[CACHE] 💾 Saved {len(df)} candles to cache")
    except Exception as e:
        print(f"[CACHE] Write error: {e}")


async def get_historical_cached(
    dhan_client,
    security_id: str,
    exchange_segment: str,
    instrument_type: str,
    candle_type: str = "5",
    from_date: str = None,
    to_date: str = None,
    max_age_hours: float = 12.0,
) -> pd.DataFrame:
    """
    Get historical data with local file cache.
    1. Check cache → return if fresh
    2. Download from Dhan API (via asyncio.to_thread)
    3. Save to cache
    """
    if not from_date:
        from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    if not to_date:
        to_date = datetime.now().strftime("%Y-%m-%d")

    # Check cache
    cached = get_cached(security_id, exchange_segment, instrument_type, candle_type, from_date, to_date, max_age_hours)
    if cached is not None:
        return cached

    # Download
    print(f"[CACHE] Miss — downloading {security_id} {candle_type} {from_date}→{to_date}")
    df = await asyncio.to_thread(
        dhan_client.get_historical_data,
        security_id=security_id,
        exchange_segment=exchange_segment,
        instrument_type=instrument_type,
        from_date=from_date,
        to_date=to_date,
        candle_type=candle_type,
    )

    # Save to cache
    if df is not None and len(df) > 0:
        save_to_cache(df, security_id, exchange_segment, instrument_type, candle_type, from_date, to_date)

    return df


def clear_cache(max_age_days: int = 7):
    """Remove cache files older than max_age_days."""
    cutoff = datetime.now() - timedelta(days=max_age_days)
    removed = 0
    for f in os.listdir(_CACHE_DIR):
        fpath = os.path.join(_CACHE_DIR, f)
        if os.path.isfile(fpath):
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            if mtime < cutoff:
                os.unlink(fpath)
                removed += 1
    if removed:
        print(f"[CACHE] 🧹 Removed {removed} stale cache files")
    return removed
