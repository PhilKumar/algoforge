"""tools/fetch_index_candles.py -- index candles from Upstox, not Dhan.

Dhan is the usual source for index candles here, but it hands out ONE active
token per client: minting one locally kills the live server's, so a casual
backfill takes the running engine down with it.  Upstox's historical-candle
endpoint serves 1-minute index data for the same period (NSE and BSE), using
the token the option pricing already uses, so a new underlying can be added
without touching Dhan at all.

BSE SENSEX has no Dhan feed here in any case, which is what this was written
for:

    python3 tools/fetch_index_candles.py --key "BSE_INDEX|SENSEX" --cache SENSEX \
        --from 2024-10-01 --to 2026-07-21

Fetches month by month (the endpoint's practical window), resamples 1-minute
bars into 5m and 15m aligned to the 09:15 open, and writes them next to the
other cached series so load_bars() picks them up unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.cascade_upstox import UpstoxAccessError, UpstoxPremiumSource  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nifty_cache")
SESSION_OPEN_MINUTES = 9 * 60 + 15


def fetch_minutes(key: str, start: date, end: date) -> dict[datetime, tuple]:
    source = UpstoxPremiumSource(cache_only=False, backfill_missing=True, underlying_key=key)
    quoted = urllib.parse.quote(key, safe="")
    out: dict[datetime, tuple] = {}
    cursor = start
    while cursor <= end:
        nxt = (cursor.replace(day=28) + timedelta(days=6)).replace(day=1)
        upto = min(nxt - timedelta(days=1), end)
        try:
            body = source._get(f"/historical-candle/{quoted}/1minute/{upto.isoformat()}/{cursor.isoformat()}")
            rows = (body.get("data") or {}).get("candles") or []
        except UpstoxAccessError as exc:
            print(f"  {cursor:%Y-%m}: FAILED {exc}")
            rows = []
        for row in rows:
            stamp = datetime.fromisoformat(row[0]).replace(tzinfo=None)
            out[stamp] = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        print(f"  {cursor:%Y-%m}: {len(rows):>6} 1-min bars (total {len(out):,})")
        cursor = nxt
    return out


def resample(minutes: dict[datetime, tuple], size: int) -> list[list]:
    buckets: dict[datetime, list] = defaultdict(list)
    for stamp in sorted(minutes):
        offset = (stamp.hour * 60 + stamp.minute) - SESSION_OPEN_MINUTES
        if offset < 0:
            continue  # pre-open prints belong to no candle
        slot = SESSION_OPEN_MINUTES + (offset // size) * size
        key = stamp.replace(hour=slot // 60, minute=slot % 60, second=0, microsecond=0)
        buckets[key].append(minutes[stamp])
    rows = []
    for stamp in sorted(buckets):
        bars = buckets[stamp]
        rows.append([stamp.isoformat(), bars[0][0], max(b[1] for b in bars), min(b[2] for b in bars), bars[-1][3]])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help='Upstox instrument key, e.g. "BSE_INDEX|SENSEX"')
    parser.add_argument("--cache", required=True, help="cache prefix, e.g. SENSEX")
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--timeframes", nargs="*", default=["5m", "15m"])
    args = parser.parse_args()

    minutes = fetch_minutes(args.key, date.fromisoformat(args.start), date.fromisoformat(args.end))
    if not minutes:
        raise SystemExit("no candles returned")
    print(f"\ntotal 1-min bars: {len(minutes):,}")
    for label in args.timeframes:
        rows = resample(minutes, {"5m": 5, "15m": 15, "1h": 60}[label])
        if not rows:
            continue
        first, last = rows[0][0][:10], rows[-1][0][:10]
        path = os.path.join(CACHE_DIR, f"{args.cache}_{label}_{first}_{last}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        print(f"{label}: {len(rows):>6} bars  {first} .. {last}  -> {os.path.basename(path)}")


if __name__ == "__main__":
    main()
