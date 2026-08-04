"""tools/terminal_cascade_backtest.py -- measure the Terminal cash Cascade.

The Terminal page has run on paper and been reasoned about, but until now the
cash-market Cascade had NO backtest at all: every other strategy in this repo
(fib space, fib cascade, two-red) has one, this one did not, so nothing about it
was measured over more than a few live days.

It drives the SAME engine the Terminal runs -- CashCascadePaperEngine, one
candle pair at a time -- so a number here is a number about the shipped code,
not about a reimplementation.

    python3 tools/terminal_cascade_backtest.py
    python3 tools/terminal_cascade_backtest.py --years 2 --capital 300000
    python3 tools/terminal_cascade_backtest.py --symbols RELIANCE,NIFTYBEES

Candles come from Upstox (daily), cached under tools/.equity_cache, so a repeat
run is offline.  Dhan is deliberately NOT used: it keeps one active token per
client id, and minting one here would kill the live server's.

WHAT IS AND IS NOT MEASURED
  * Costs ARE charged -- the engine's own delivery schedule (brokerage, STT,
    exchange, SEBI, stamp, GST), the same one the paper page uses.
  * Fills are at the engine's own trigger prices with NO slippage or spread.
    On a liquid large cap that is a small lie; on anything thin it is not.
  * Mothers are found by the 5-bar swing-pivot scanner, not hand-picked. The
    Terminal expects a HUMAN to pick the mother, so these numbers describe the
    rule applied mechanically -- historically the pessimistic reading, since
    every measured strategy here does better when Phil selects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_equity import (  # noqa: E402
    CashCascadeInstrument,
    CashCascadePaperConfig,
    CashCascadePaperEngine,
)
from engine.cascade_mothers import find_mother_candles  # noqa: E402
from engine.cascade_options import CascadeError, IndexCandle  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".equity_cache")
BASE = "https://api.upstox.com/v2"

# Upstox instrument keys. ISIN-based for equities, and NIFTYBEES is an ETF whose
# SIGNAL is the NIFTY index -- the Terminal maps that in
# REFERENCE_INDEX_BY_TRADED_SYMBOL, and the backtest has to honour it or it
# would be drawing geometry on the ETF instead of the index it tracks.
UNIVERSE = {
    "RELIANCE": dict(key="NSE_EQ|INE002A01018", name="Reliance Industries"),
    "TCS": dict(key="NSE_EQ|INE467B01029", name="Tata Consultancy Services"),
    "HDFCBANK": dict(key="NSE_EQ|INE040A01034", name="HDFC Bank"),
    "INFY": dict(key="NSE_EQ|INE009A01021", name="Infosys"),
    "ICICIBANK": dict(key="NSE_EQ|INE090A01021", name="ICICI Bank"),
    "NIFTYBEES": dict(key="NSE_EQ|INF204KB14I2", name="Nippon India ETF Nifty BeES", signal="NIFTY"),
}
SIGNAL_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50"}

# The same pivot width the fib-space design settled on: 3 promotes every minor
# bounce in a downtrend, and Phil rejected those on sight.
MOTHER_PIVOT_BARS = 5


def _session():
    import requests  # local import: the cached path needs no network stack

    return requests.Session()


def _token() -> str:
    from dotenv import load_dotenv

    load_dotenv()
    token = (os.getenv("UPSTOX_ACCESS_TOKEN") or "").strip()
    if not token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN is not set; cannot fetch candles.")
    return token


def fetch_daily(instrument_key: str, start: date, end: date, *, cache_only: bool = False) -> list[list]:
    """Daily candles, cached. Returns Upstox rows: [ts, o, h, l, c, vol, oi]."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    slug = instrument_key.replace("|", "_").replace(" ", "_")
    path = os.path.join(CACHE_DIR, f"{slug}_{start}_{end}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    if cache_only:
        raise SystemExit(f"No cached candles for {instrument_key} and --cache-only was set.")

    session, token = _session(), _token()
    from urllib.parse import quote

    url = f"{BASE}/historical-candle/{quote(instrument_key, safe='')}/day/{end}/{start}"
    for attempt in range(4):
        response = session.get(
            url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}, timeout=45
        )
        if response.status_code == 200:
            rows = (response.json().get("data") or {}).get("candles") or []
            # Upstox returns newest first; every consumer here wants oldest first.
            rows = sorted(rows, key=lambda r: r[0])
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(rows, handle)
            return rows
        if response.status_code in (429, 500, 502, 503, 504):
            time.sleep(1.5 * (attempt + 1))
            continue
        raise SystemExit(f"Upstox {response.status_code} for {instrument_key}: {response.text[:200]}")
    raise SystemExit(f"Upstox kept failing for {instrument_key}")


def to_candles(rows: list[list]) -> list[IndexCandle]:
    """Upstox rows -> IndexCandles, stamped at the session open.

    A daily bar arrives at 00:00; the engine treats a bar as spanning
    DAILY_BAR_MINUTES from 09:15, and is_nse_cash_session rejects midnight, so
    an unrestamped series would be silently dropped bar by bar.
    """
    out = []
    for row in rows:
        stamp = datetime.fromisoformat(str(row[0]))
        stamp = stamp.replace(tzinfo=None, hour=9, minute=15, second=0, microsecond=0)
        out.append(IndexCandle(stamp, float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return out


def run_symbol(symbol: str, *, years: float, capital: float, cache_only: bool) -> dict:
    spec = UNIVERSE[symbol]
    end = date.today()
    start = end - timedelta(days=int(365 * years) + 20)

    trade_candles = to_candles(fetch_daily(spec["key"], start, end, cache_only=cache_only))
    signal_symbol = spec.get("signal") or symbol
    if signal_symbol != symbol:
        signal_candles = to_candles(fetch_daily(SIGNAL_KEYS[signal_symbol], start, end, cache_only=cache_only))
    else:
        signal_candles = trade_candles

    # Geometry runs on the SIGNAL series, so mothers are found there too -- for
    # NIFTYBEES that is the NIFTY index, which is the whole point of the
    # reference-index mode.
    by_stamp = {c.timestamp: c for c in trade_candles}
    signal_by_stamp = {c.timestamp: c for c in signal_candles}
    shared = sorted(set(by_stamp) & set(signal_by_stamp))
    if len(shared) < 120:
        return {"symbol": symbol, "error": f"only {len(shared)} shared sessions", "campaigns": []}

    aligned_signal = [signal_by_stamp[s] for s in shared]
    mothers = find_mother_candles(aligned_signal, left_bars=MOTHER_PIVOT_BARS, right_bars=MOTHER_PIVOT_BARS)

    instrument = CashCascadeInstrument(
        symbol=symbol,
        name=spec["name"],
        security_id="",
        signal_symbol=signal_symbol,
        signal_security_id="",
        signal_instrument_type="INDEX" if signal_symbol != symbol else "EQUITY",
    )

    campaigns = []
    for mother in mothers:
        index = mother.index
        if index + 20 >= len(shared):
            continue
        # A campaign may only act from the bar that CONFIRMED the pivot; a swing
        # high is not knowable at the high itself.
        armed_at = index + MOTHER_PIVOT_BARS
        try:
            engine = CashCascadePaperEngine(
                aligned_signal[index],
                by_stamp[shared[index]],
                instrument,
                CashCascadePaperConfig(capital_inr=capital, timeframe="1d"),
            )
        except CascadeError as exc:
            return {"symbol": symbol, "error": str(exc), "campaigns": []}

        for position in range(armed_at, len(shared)):
            stamp = shared[position]
            engine.on_candle(signal_by_stamp[stamp], by_stamp[stamp])
            if engine.status in {"ENDED", "COMPLETED"}:
                break

        status = engine.get_status()
        rounds = status.get("rounds") or []
        if not rounds and not (status.get("open_fills") or []):
            continue
        # MARK THE UNSOLD STOCK. A round only enters the P&L when it CLOSES, so
        # counting closed rounds alone reports the winners and quietly parks the
        # losers as "still holding" -- which is how a 99% win rate appears out of
        # nothing. Whatever is still held is valued at the last close.
        open_quantity = int(status.get("open_quantity") or 0)
        invested = float(status.get("open_invested_inr") or 0.0)
        last_close = float(by_stamp[shared[-1]].close)
        campaigns.append(
            {
                "mother": aligned_signal[index].timestamp.date().isoformat(),
                "rounds": rounds,
                "open_quantity": open_quantity,
                "open_invested_inr": invested,
                "open_value_inr": round(open_quantity * last_close, 2),
                "unrealised_inr": round(open_quantity * last_close - invested, 2),
                "status": status.get("status"),
            }
        )
    return {"symbol": symbol, "signal": signal_symbol, "sessions": len(shared), "campaigns": campaigns}


def summarise(result: dict) -> dict:
    rounds = [r for c in result.get("campaigns", []) for r in c.get("rounds", [])]
    nets = [float(r.get("net_pnl") or 0.0) for r in rounds]
    gross = [float(r.get("gross_pnl") or 0.0) for r in rounds]
    costs = [float((r.get("costs") or {}).get("total") or 0.0) for r in rounds]
    wins = [n for n in nets if n > 0]
    stranded = sum(1 for c in result.get("campaigns", []) if (c.get("open_quantity") or 0) > 0)
    return {
        "symbol": result["symbol"],
        "sessions": result.get("sessions", 0),
        "campaigns": len(result.get("campaigns", [])),
        "rounds": len(rounds),
        "wins": len(wins),
        "win_rate": (100.0 * len(wins) / len(rounds)) if rounds else 0.0,
        "net": sum(nets),
        "gross": sum(gross),
        "costs": sum(costs),
        "best": max(nets) if nets else 0.0,
        "worst": min(nets) if nets else 0.0,
        "still_holding": stranded,
        "open_invested": sum(float(c.get("open_invested_inr") or 0.0) for c in result.get("campaigns", [])),
        "unrealised": sum(float(c.get("unrealised_inr") or 0.0) for c in result.get("campaigns", [])),
        "error": result.get("error"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--capital", type=float, default=300000.0)
    parser.add_argument("--symbols", default=",".join(UNIVERSE))
    parser.add_argument("--cache-only", action="store_true")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in UNIVERSE]
    if unknown:
        raise SystemExit(f"unknown symbol(s): {', '.join(unknown)}")

    print(f"[backtest] Terminal cash Cascade · daily · {args.years}y · Rs {args.capital:,.0f} per campaign")
    print(f"[backtest] mothers: {MOTHER_PIVOT_BARS}-bar swing pivots, applied mechanically\n")

    rows = []
    for symbol in symbols:
        result = run_symbol(symbol, years=args.years, capital=args.capital, cache_only=args.cache_only)
        row = summarise(result)
        rows.append(row)
        if row["error"]:
            print(f"  {symbol:<11} ERROR: {row['error']}")
            continue
        print(
            f"  {symbol:<11}{row['campaigns']:>3} camp {row['rounds']:>3} rnd  win {row['win_rate']:>5.1f}%  "
            f"banked Rs {row['net']:>9,.0f}   held {row['still_holding']:>2} "
            f"({row['unrealised']:>+11,.0f})   TRUE Rs {row['net'] + row['unrealised']:>11,.0f}"
        )

    traded = [r for r in rows if not r["error"]]
    banked = sum(r["net"] for r in traded)
    unreal = sum(r["unrealised"] for r in traded)
    held_cost = sum(r["open_invested"] for r in traded)
    total_rounds = sum(r["rounds"] for r in traded)
    print("\n" + "=" * 92)
    print(
        f"  BANKED       {total_rounds} closed rounds, "
        f"win {100.0 * sum(r['wins'] for r in traded) / max(total_rounds, 1):.1f}%   Rs {banked:>12,.0f}"
    )
    print(
        f"  STILL HELD   {sum(r['still_holding'] for r in traded)} campaigns, "
        f"Rs {held_cost:,.0f} invested, marked at last close   Rs {unreal:>12,.0f}"
    )
    print(f"  {'-' * 88}")
    print(f"  TRUE TOTAL   banked + unrealised{'':<38}Rs {banked + unreal:>12,.0f}")
    print("=" * 92)
    # The win rate above counts CLOSED rounds only. A cascade that never sells
    # never books a loss, so that number rises as the open pile grows -- which
    # is why the held line sits next to it and the true total sums both.
    print("\n  Costs charged. NO slippage or spread. Mothers mechanical, not picked.")
    print("  Win rate counts CLOSED rounds only — the held pile is where losses hide.\n")


if __name__ == "__main__":
    main()
