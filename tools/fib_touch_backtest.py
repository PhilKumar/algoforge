#!/usr/bin/env python3
"""Run the swing touch ladder over cached history, offline.

The console's Backtest button does this through Dhan and a live Upstox token.
This runs the SAME engine (`engine.fib_touch_ladder.FibTouchLadder`) against
the local caches instead, so a measurement can be taken without a network call
-- and, more importantly, without minting a Dhan token, which would kill the
one the live server is trading with.

Sources, both cache-only and both refusing rather than guessing:
  * index candles   tools/.nifty_cache/NIFTY_1m_*.json  (1m, naive IST)
  * option minutes  tools/.upstox_cache/                (expired contracts only)

Because Upstox records a contract's minutes only AFTER it expires, this can
only measure a mother whose contract has already expired. A recent mother has
to go through the console, which can reach Dhan for a still-listed strike.

    python3 tools/fib_touch_backtest.py --mother 2026-07-17T14:15 --timeframe 15m
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.cascade_upstox import UpstoxPremiumSource  # noqa: E402
from engine.fib_touch_ladder import (  # noqa: E402
    FibTouchConfig,
    FibTouchLadder,
    symbol_terms,
)

INDEX_CACHE = ROOT / "tools" / ".nifty_cache"


class Bar:
    __slots__ = ("timestamp", "open", "high", "low", "close")

    def __init__(self, timestamp, o, h, low, c):
        self.timestamp, self.open, self.high, self.low, self.close = timestamp, o, h, low, c


def load_index_1m(symbol: str) -> list[Bar]:
    """Every cached 1m bar for the symbol, oldest first, de-duplicated."""
    files = sorted(INDEX_CACHE.glob(f"{symbol}_1m_*.json"))
    if not files:
        raise SystemExit(f"No cached 1m {symbol} candles in {INDEX_CACHE}")
    seen: dict[datetime, Bar] = {}
    for path in files:
        for row in json.loads(path.read_text()):
            stamp = datetime.fromisoformat(row[0])
            if stamp.tzinfo is not None:
                stamp = stamp.replace(tzinfo=None)
            seen[stamp] = Bar(stamp, float(row[1]), float(row[2]), float(row[3]), float(row[4]))
    return [seen[k] for k in sorted(seen)]


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    """1m bars folded into NSE-aligned buckets of `minutes`.

    A session opens at 09:15, so buckets are measured from that offset -- not
    from the hour -- or a 15m bar would start at 09:00 and never match a mother
    the console would accept.
    """
    if minutes == 1:
        return bars
    out: list[Bar] = []
    current: Bar | None = None
    bucket_at: datetime | None = None
    for bar in bars:
        session_open = bar.timestamp.replace(hour=9, minute=15, second=0, microsecond=0)
        if bar.timestamp < session_open:
            continue
        offset = int((bar.timestamp - session_open).total_seconds() // 60)
        start = session_open + timedelta(minutes=(offset // minutes) * minutes)
        if bucket_at != start:
            if current is not None:
                out.append(current)
            current = Bar(start, bar.open, bar.high, bar.low, bar.close)
            bucket_at = start
        else:
            assert current is not None
            current.high = max(current.high, bar.high)
            current.low = min(current.low, bar.low)
            current.close = bar.close
    if current is not None:
        out.append(current)
    return out


def build_premium_lookup(underlying_key: str, symbol: str):
    """(when, strike, expiry, side) -> a real recorded trade, or None.

    Cache-only, and it never fabricates: `UpstoxPremiumSource.lookup` returns
    the exact minute's bar or nothing. A minute the option did not trade is
    searched forward up to ten minutes -- what an order resting at the level
    would actually get -- and then given up on as a gap.
    """
    source = UpstoxPremiumSource(underlying_key=underlying_key, cache_only=True)
    dead: set[tuple] = set()

    def lookup(when: datetime, strike: float, expiry: date, side: str):
        key = (float(strike), expiry, str(side).upper())
        if key in dead:
            return None
        contract = SimpleNamespace(
            symbol=symbol,
            underlying=symbol,
            strike=float(strike),
            expiry=expiry,
            option_type=str(side).upper(),
        )
        minute = when.replace(second=0, microsecond=0)
        for step in range(0, 11):
            try:
                bar = source.lookup(minute + timedelta(minutes=step), contract)
            except Exception:
                dead.add(key)
                return None
            if bar is not None:
                return float(bar.open)
        return None

    return lookup, source


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--mother", required=True, help="IST, e.g. 2026-07-17T14:15")
    ap.add_argument("--side", default="CE", choices=["CE", "PE"])
    ap.add_argument("--timeframe", default="15m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--cap", type=float, default=75_000.0)
    ap.add_argument("--itm-steps", type=int, default=2)
    ap.add_argument("--min-dte", type=int, default=4)
    ap.add_argument("--horizon-days", type=int, default=10)
    args = ap.parse_args()

    terms = symbol_terms(args.symbol)
    mother_at = datetime.fromisoformat(args.mother)
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[args.timeframe]

    every = load_index_1m(terms.symbol)
    horizon = mother_at + timedelta(days=args.horizon_days)
    window = [b for b in every if mother_at <= b.timestamp <= horizon]
    if not window:
        raise SystemExit(f"No cached 1m candles between {mother_at} and {horizon}")
    geometry = resample(window, tf_minutes)
    if not any(b.timestamp == mother_at for b in geometry):
        near = [b.timestamp for b in geometry[:6]]
        raise SystemExit(f"No {args.timeframe} candle opens at {mother_at}. Nearby: {near}")

    lookup, source = build_premium_lookup(
        f"NSE_INDEX|Nifty {'Bank' if terms.symbol == 'BANKNIFTY' else '50'}", terms.symbol
    )
    expiries = sorted(source.available_expiries())
    if not expiries:
        raise SystemExit("The Upstox cache holds no expiries; nothing can be priced offline.")

    config = FibTouchConfig(
        symbol=terms.symbol,
        side=args.side,
        mother_timestamp=mother_at,
        lot_size=65 if terms.symbol == "NIFTY" else terms.lot_size,
        strike_step=terms.strike_step,
        timeframe=args.timeframe,
        capital_cap_inr=args.cap,
        itm_steps=args.itm_steps,
        min_dte=args.min_dte,
    )
    engine = FibTouchLadder(config, premium_lookup=lookup, expiry_source=lambda on: expiries)
    if args.timeframe != "1m":
        for bar in geometry:
            engine.on_geometry_candle(bar)
    for bar in window:
        engine.on_candle(bar)
        if engine.status in {"CLOSED", "EXPIRED", "MOTHER_BROKEN"}:
            break

    status = engine.get_status()
    print(json.dumps(status, indent=2, default=str))


if __name__ == "__main__":
    main()
