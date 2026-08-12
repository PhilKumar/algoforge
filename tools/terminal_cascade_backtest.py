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
    TIMEFRAME_MINUTES,
    CashCascadeChain,
    CashCascadeInstrument,
    CashCascadePaperConfig,
    CashCascadePaperEngine,
)
from engine.cascade_mothers import find_mother_candles  # noqa: E402
from engine.cascade_options import CascadeError, IndexCandle  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".equity_cache")
# v3 serves NATIVE 5m/15m/1h/4h/1d for equities, so nothing is resampled.
BASE = "https://api.upstox.com/v3"

# (unit, value, max days per request, minutes per bar). The windows are Upstox's,
# found by probing: asking wider returns UDAPI1148 "Invalid date range", so a
# long span has to be stitched from chunks rather than requested whole.
TIMEFRAMES = {
    "5m": ("minutes", "5", 30, 5),
    "15m": ("minutes", "15", 30, 15),
    "1h": ("hours", "1", 90, 60),
    "4h": ("hours", "4", 90, 240),
    "1d": ("days", "1", 730, 375),
}

# A campaign is replayed at most this many of its OWN bars -- the same 800-bar
# budget the Terminal page uses for its window. Without it a 5m run replays
# every mother to the end of two years, which is quadratic and measures a
# holding period no one would sit through.
HORIZON_BARS = 800

# Upstox instrument keys. ISIN-based for equities, and NIFTYBEES is an ETF whose
# SIGNAL is the NIFTY index -- the Terminal maps that in
# REFERENCE_INDEX_BY_TRADED_SYMBOL, and the backtest has to honour it or it
# would be drawing geometry on the ETF instead of the index it tracks.
UNIVERSE = {
    # Five large caps that move slowly -- the original audit set, kept so its
    # numbers stay comparable.
    "RELIANCE": dict(key="NSE_EQ|INE002A01018", name="Reliance Industries"),
    "TCS": dict(key="NSE_EQ|INE467B01029", name="Tata Consultancy Services"),
    "HDFCBANK": dict(key="NSE_EQ|INE040A01034", name="HDFC Bank"),
    "INFY": dict(key="NSE_EQ|INE009A01021", name="Infosys"),
    "ICICIBANK": dict(key="NSE_EQ|INE090A01021", name="ICICI Bank"),
    # Four more index heavyweights, for breadth rather than temperament.
    "SBIN": dict(key="NSE_EQ|INE062A01020", name="State Bank of India"),
    "AXISBANK": dict(key="NSE_EQ|INE238A01034", name="Axis Bank"),
    "ITC": dict(key="NSE_EQ|INE154A01025", name="ITC"),
    "LT": dict(key="NSE_EQ|INE018A01030", name="Larsen & Toubro"),
    # Six that actually fall far enough to pay a quarter-of-the-fall target.
    # The cascade needs range; a stock that never gives back 8% never ladders.
    "ADANIENT": dict(key="NSE_EQ|INE423A01024", name="Adani Enterprises"),
    "ETERNAL": dict(key="NSE_EQ|INE758T01015", name="Eternal (formerly Zomato)"),
    "TATASTEEL": dict(key="NSE_EQ|INE081A01020", name="Tata Steel"),
    "HINDALCO": dict(key="NSE_EQ|INE038A01020", name="Hindalco Industries"),
    "TATAPOWER": dict(key="NSE_EQ|INE245A01021", name="Tata Power"),
    "TITAN": dict(key="NSE_EQ|INE280A01028", name="Titan Company"),
    # Not one of the fifteen: the ETF whose SIGNAL is the index it tracks.
    "NIFTYBEES": dict(key="NSE_EQ|INF204KB14I2", name="Nippon India ETF Nifty BeES", signal="NIFTY"),
}
# The fifteen the report covers. NIFTYBEES is reachable by name but sits out --
# it is an ETF on an index, not a stock, and it dilutes a stock table.
DEFAULT_SYMBOLS = [s for s in UNIVERSE if s != "NIFTYBEES"]
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


def fetch_candles(
    instrument_key: str, start: date, end: date, timeframe: str, *, cache_only: bool = False
) -> list[list]:
    """Candles for one instrument and timeframe, cached and chunk-stitched.

    Upstox caps the span per request (30 days on 5m, 90 on 1h, 730 on daily), so
    anything longer is fetched in slices and merged. Slices are de-duplicated by
    timestamp because the seams overlap by a day.
    """
    unit, value, max_days, _minutes = TIMEFRAMES[timeframe]
    os.makedirs(CACHE_DIR, exist_ok=True)
    slug = instrument_key.replace("|", "_").replace(" ", "_")
    path = os.path.join(CACHE_DIR, f"{slug}_{timeframe}_{start}_{end}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    if cache_only:
        raise SystemExit(f"No cached candles for {instrument_key} and --cache-only was set.")

    session, token = _session(), _token()
    from urllib.parse import quote

    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    key = quote(instrument_key, safe="")
    by_stamp: dict = {}
    slice_end = end
    while slice_end > start:
        # THE WINDOW IS NOT A FIXED NUMBER OF DAYS. Upstox answers a 30-day 5m
        # request for recent dates and rejects the same width in March with
        # UDAPI1148 "Invalid date range" -- the real rule appears to be calendar
        # based, and is not documented here. So the width ADAPTS: ask wide, and
        # on a range rejection halve and retry rather than encoding a guess that
        # breaks on some other month.
        width = max_days
        while True:
            slice_start = max(start, slice_end - timedelta(days=width))
            url = f"{BASE}/historical-candle/{key}/{unit}/{value}/{slice_end}/{slice_start}"
            response = None
            for attempt in range(4):
                response = session.get(url, headers=headers, timeout=60)
                if response.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            if response is not None and response.status_code == 200:
                for row in (response.json().get("data") or {}).get("candles") or []:
                    by_stamp[row[0]] = row
                break
            rejected_range = response is not None and "UDAPI1148" in response.text
            if rejected_range and width > 5:
                width = max(5, width // 2)
                continue
            raise SystemExit(
                f"Upstox {getattr(response, 'status_code', '?')} for {instrument_key} {timeframe} "
                f"{slice_start}..{slice_end}: {getattr(response, 'text', '')[:200]}"
            )
        slice_end = slice_start
        time.sleep(0.25)  # the budget is shared; do not hammer it

    rows = [by_stamp[k] for k in sorted(by_stamp)]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    return rows


def to_candles(rows: list[list], timeframe: str = "1d") -> list[IndexCandle]:
    """Upstox rows -> IndexCandles in naive IST.

    A DAILY bar arrives stamped 00:00. The engine spans a bar from its stamp and
    is_nse_cash_session rejects midnight, so an unrestamped daily series is
    dropped bar by bar. Intraday bars already carry their real session time and
    must be left exactly as they are.
    """
    out = []
    for row in rows:
        stamp = datetime.fromisoformat(str(row[0])).replace(tzinfo=None)
        if timeframe == "1d":
            stamp = stamp.replace(hour=9, minute=15, second=0, microsecond=0)
        out.append(IndexCandle(stamp, float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return out


def prepare(symbol: str, *, years: float, cache_only: bool, timeframe: str) -> dict:
    """Everything both runners need: aligned series, mothers, instrument.

    Split out of run_symbol so the chain runner reads the SAME candles and the
    SAME mothers -- otherwise a difference between the two modes could be a
    difference in the data rather than in the strategy.
    """
    spec = UNIVERSE[symbol]
    end = date.today()
    start = end - timedelta(days=int(365 * years) + 20)

    trade_candles = to_candles(fetch_candles(spec["key"], start, end, timeframe, cache_only=cache_only), timeframe)
    signal_symbol = spec.get("signal") or symbol
    if signal_symbol != symbol:
        signal_candles = to_candles(
            fetch_candles(SIGNAL_KEYS[signal_symbol], start, end, timeframe, cache_only=cache_only), timeframe
        )
    else:
        signal_candles = trade_candles

    # Geometry runs on the SIGNAL series, so mothers are found there too -- for
    # NIFTYBEES that is the NIFTY index, which is the whole point of the
    # reference-index mode.
    by_stamp = {c.timestamp: c for c in trade_candles}
    signal_by_stamp = {c.timestamp: c for c in signal_candles}
    shared = sorted(set(by_stamp) & set(signal_by_stamp))
    if len(shared) < 120:
        return {"symbol": symbol, "error": f"only {len(shared)} shared sessions"}

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
    return {
        "symbol": symbol,
        "signal": signal_symbol,
        "shared": shared,
        "by_stamp": by_stamp,
        "signal_by_stamp": signal_by_stamp,
        "aligned_signal": aligned_signal,
        "mothers": mothers,
        "instrument": instrument,
    }


def run_symbol(symbol: str, *, years: float, capital: float, cache_only: bool, timeframe: str = "1d") -> dict:
    data = prepare(symbol, years=years, cache_only=cache_only, timeframe=timeframe)
    if data.get("error"):
        return {"symbol": symbol, "error": data["error"], "campaigns": []}
    shared = data["shared"]
    by_stamp = data["by_stamp"]
    signal_by_stamp = data["signal_by_stamp"]
    aligned_signal = data["aligned_signal"]
    mothers = data["mothers"]
    instrument = data["instrument"]
    signal_symbol = data["signal"]

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
                CashCascadePaperConfig(capital_inr=capital, timeframe=timeframe),
            )
        except CascadeError as exc:
            return {"symbol": symbol, "error": str(exc), "campaigns": []}

        for position in range(armed_at, min(len(shared), index + HORIZON_BARS)):
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


def _rung_census(chain: CashCascadeChain) -> dict[str, int]:
    """How many generations ended up on each rung of the escalation ladder."""
    census: dict[str, int] = {}
    for engine in chain.all_engines:
        rung = getattr(engine, "structure_timeframe", None) or engine.config.timeframe
        census[rung] = census.get(rung, 0) + 1
    return census


def _highest_rung(chain: CashCascadeChain) -> str:
    reached = [getattr(e, "structure_timeframe", None) or e.config.timeframe for e in chain.all_engines]
    return max(reached, key=lambda tf: TIMEFRAME_MINUTES.get(tf, 0), default="")


def run_symbol_chain(symbol: str, *, years: float, capital: float, cache_only: bool, timeframe: str = "1d") -> dict:
    """The same rule with the successor mechanism switched on.

    `run_symbol` above replays what the Terminal page actually does today: one
    campaign per mother, and when that mother breaks or is retested the geometry
    is finished -- it can draw no further leg, so the ladder stops laddering
    while the stock keeps falling. `CashCascadeChain` is the missing half,
    already written and matching CryptoForge's `_auto_restart`: the broken
    generation stops entering and keeps working its own position, a successor
    starts on a fresh mother, and the ladder walks down with the price.

    Nothing calls the chain in the shipped app, which is exactly why the two
    modes are reported side by side: the difference IS the value of wiring it.

    One chain per symbol, started at the first confirmed mother and run to the
    end of the window -- a continuous cascade, not a campaign per pivot, so
    generations are never counted twice.
    """
    data = prepare(symbol, years=years, cache_only=cache_only, timeframe=timeframe)
    if data.get("error"):
        return {"symbol": symbol, "error": data["error"], "campaigns": []}
    shared = data["shared"]
    by_stamp = data["by_stamp"]
    signal_by_stamp = data["signal_by_stamp"]
    aligned_signal = data["aligned_signal"]
    mothers = data["mothers"]

    seed = next((m for m in mothers if m.index + MOTHER_PIVOT_BARS + 20 < len(shared)), None)
    if seed is None:
        return {"symbol": symbol, "error": "no usable mother", "campaigns": []}

    index = seed.index
    try:
        chain = CashCascadeChain(
            aligned_signal[index],
            by_stamp[shared[index]],
            data["instrument"],
            CashCascadePaperConfig(capital_inr=capital, timeframe=timeframe),
        )
    except CascadeError as exc:
        return {"symbol": symbol, "error": str(exc), "campaigns": []}

    # PEAK DEPLOYMENT is the number the return is measured against, not the
    # Rs 3L allocation. The size gate keeps a cascade's working capital far
    # below its pot, so a profit quoted against the allocation understates the
    # rule and a profit quoted against nothing is unreadable.
    peak_invested = 0.0
    for position in range(index + MOTHER_PIVOT_BARS, len(shared)):
        stamp = shared[position]
        chain.on_candle(signal_by_stamp[stamp], by_stamp[stamp])
        peak_invested = max(peak_invested, chain.open_invested_inr)

    last_close = float(by_stamp[shared[-1]].close)
    open_quantity = chain.open_quantity
    invested = chain.open_invested_inr
    # A generation is only a "campaign" for reporting if it did something --
    # banked a round or still holds stock. A generation that was born, broke and
    # retired without drawing a fib is chain bookkeeping, not a trade.
    live = [e for e in chain.all_engines if e.rounds or e.open_fills]
    return {
        "symbol": symbol,
        "signal": data["signal"],
        "sessions": len(shared),
        "generations": len(chain.all_engines),
        "chain_stopped": chain.chain_stopped_reason,
        "peak_invested": round(peak_invested, 2),
        # PROOF THE LADDER ACTUALLY CLIMBED. A run can look healthy while every
        # campaign quietly sits on the rung it started on, so the rung each
        # generation reached is counted rather than assumed.
        "rungs": _rung_census(chain),
        "highest_rung": _highest_rung(chain),
        "campaigns": [
            {
                "mother": seed and aligned_signal[index].timestamp.date().isoformat(),
                "rounds": [row.to_dict() if hasattr(row, "to_dict") else row for e in live for row in e.rounds],
                "open_quantity": open_quantity,
                "open_invested_inr": invested,
                "open_value_inr": round(open_quantity * last_close, 2),
                "unrealised_inr": round(open_quantity * last_close - invested, 2),
                "status": chain.chain_stopped_reason or "RUNNING",
            }
        ],
    }


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
        "peak_invested": float(result.get("peak_invested") or 0.0),
        "generations": int(result.get("generations") or 0),
        "rungs": result.get("rungs") or {},
        "highest_rung": result.get("highest_rung") or "",
        "error": result.get("error"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--capital", type=float, default=300000.0)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--tf", default="1d", choices=sorted(TIMEFRAMES))
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument(
        "--chain",
        action="store_true",
        help="drive CashCascadeChain (successor campaigns) instead of one frozen campaign per mother",
    )
    parser.add_argument("--json", dest="json_out", default="", help="write the per-symbol rows to this file")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in UNIVERSE]
    if unknown:
        raise SystemExit(f"unknown symbol(s): {', '.join(unknown)}")

    runner = run_symbol_chain if args.chain else run_symbol
    mode = "CHAIN (successors on)" if args.chain else "SHIPPED (one campaign per mother)"
    print(f"[backtest] Terminal cash Cascade · {mode} · {args.tf} · {args.years}y · Rs {args.capital:,.0f}")
    print(f"[backtest] mothers: {MOTHER_PIVOT_BARS}-bar swing pivots, applied mechanically\n")

    rows = []
    for symbol in symbols:
        result = runner(symbol, years=args.years, capital=args.capital, cache_only=args.cache_only, timeframe=args.tf)
        row = summarise(result)
        rows.append(row)
        if row["error"]:
            print(f"  {symbol:<11} ERROR: {row['error']}")
            continue
        true = row["net"] + row["unrealised"]
        if args.chain:
            peak = row["peak_invested"]
            # Return is measured against the most money the chain ever had at
            # work, annualised over the window -- not against the allocation.
            per_year = (100.0 * true / peak / args.years) if peak else 0.0
            print(
                f"  {symbol:<11}{row['generations']:>3} gen {row['rounds']:>3} rnd  "
                f"banked Rs {row['net']:>8,.0f}   held Rs {row['unrealised']:>+9,.0f}   "
                f"peak Rs {peak:>8,.0f}   TRUE Rs {true:>9,.0f}  {per_year:>+6.1f}%/yr"
                f"   top {row['highest_rung'] or '--':>3}"
            )
        else:
            print(
                f"  {symbol:<11}{row['campaigns']:>3} camp {row['rounds']:>3} rnd  win {row['win_rate']:>5.1f}%  "
                f"banked Rs {row['net']:>9,.0f}   held {row['still_holding']:>2} "
                f"({row['unrealised']:>+11,.0f})   TRUE Rs {true:>11,.0f}"
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

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"mode": mode, "tf": args.tf, "years": args.years, "capital": args.capital, "rows": rows}, handle)
        print(f"  rows -> {args.json_out}\n")


if __name__ == "__main__":
    main()
