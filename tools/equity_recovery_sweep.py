"""tools/equity_recovery_sweep.py -- the recovery rules on CASH, not options.

Same engine as tools/candle_recovery_sweep.py, same rules, one difference that
is the whole point: a share does not expire and does not decay.  The option
runs kept losing to theta and to the per-round statutory charge; on cash the
strategy's own assumption -- "hold and wait for the retrace" -- is finally
true.

What changes from the option runner:

  * the traded instrument IS the signal (or the reference index for a BEES, so
    NIFTYBEES takes its geometry from NIFTY, exactly as the Terminal does);
  * "premium" is the share price, read off the traded series;
  * size is a rupee budget per trade, converted to whole shares at the fill --
    lots_schedule 1,2 means the second trade commits twice the first;
  * costs are DELIVERY charges (STT both sides, stamp on the buy), injected
    through RecoveryConfig.cost_model.

    python3 tools/equity_recovery_sweep.py --tf 15m --mother-rule run --years 2
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.candle_recovery import (  # noqa: E402
    TIMEFRAME_MINUTES,
    FibZoneEntry,
    RecoveryBar,
    RecoveryConfig,
    TwoRedRecovery,
    mirror_bar,
    mirror_bars,
)
from engine.cascade_equity import CashMarketCostSchedule, calculate_cash_round_costs  # noqa: E402
from engine.cascade_mothers import find_mother_candles, find_run_mothers  # noqa: E402
from tools.terminal_cascade_backtest import (  # noqa: E402
    SIGNAL_KEYS,
    UNIVERSE,
    fetch_candles,
    to_candles,
)

SCHEDULE = CashMarketCostSchedule()

# INTRADAY, NOT DELIVERY.  These campaigns square off at the day's last bar, so
# they are charged as intraday: STT is 0.025% on the SELL only (delivery is
# 0.1% on both sides -- four times as much and on twice the turnover), and
# stamp duty is 0.003% rather than 0.015%.  Charging delivery rates on an
# intraday strategy added roughly Rs 90 to every round.
INTRADAY_BROKERAGE_PCT = 0.0003
INTRADAY_BROKERAGE_CAP = 20.0
INTRADAY_STT_SELL_PCT = 0.00025
INTRADAY_EXCHANGE_PCT = 0.0000297
INTRADAY_SEBI_PCT = 0.000001
INTRADAY_STAMP_BUY_PCT = 0.00003
GST_PCT = 0.18


def intraday_costs(*, entry: float, exit_price: float, quantity: int, lots: int) -> float:
    """Charges for one intraday buy-and-sell round on cash equity."""
    buy_turnover = float(entry) * int(quantity)
    sell_turnover = float(exit_price) * int(quantity)
    total = buy_turnover + sell_turnover
    brokerage = min(buy_turnover * INTRADAY_BROKERAGE_PCT, INTRADAY_BROKERAGE_CAP) + min(
        sell_turnover * INTRADAY_BROKERAGE_PCT, INTRADAY_BROKERAGE_CAP
    )
    stt = sell_turnover * INTRADAY_STT_SELL_PCT
    exchange = total * INTRADAY_EXCHANGE_PCT
    sebi = total * INTRADAY_SEBI_PCT
    stamp = buy_turnover * INTRADAY_STAMP_BUY_PCT
    gst = (brokerage + exchange + sebi) * GST_PCT
    return brokerage + stt + exchange + sebi + stamp + gst


def delivery_costs(*, entry: float, exit_price: float, quantity: int, lots: int) -> float:
    """Delivery charges, for a run that is allowed to hold overnight."""
    return calculate_cash_round_costs(
        buys=[(entry, quantity)], sell_price=exit_price, sell_quantity=quantity, schedule=SCHEDULE
    ).total


def load(symbol: str, timeframe: str, years: float):
    spec = UNIVERSE[symbol]
    end = date.today()
    start = end - timedelta(days=int(365 * years) + 20)
    trade = to_candles(fetch_candles(spec["key"], start, end, timeframe), timeframe)
    signal_symbol = spec.get("signal") or symbol
    signal = (
        trade
        if signal_symbol == symbol
        else to_candles(fetch_candles(SIGNAL_KEYS[signal_symbol], start, end, timeframe), timeframe)
    )
    by_t = {c.timestamp: c for c in trade}
    by_s = {c.timestamp: c for c in signal}
    shared = sorted(set(by_t) & set(by_s))
    return shared, by_t, by_s, signal_symbol


def run_symbol(
    symbol, timeframe, years, *, mode, side, mother_rule, run_bars, trade_inr, config_kwargs, cost_model=intraday_costs
):
    shared, by_t, by_s, signal_symbol = load(symbol, timeframe, years)
    if len(shared) < 50:
        return None
    signal_rows = [by_s[s] for s in shared]
    scan_rows = signal_rows
    if side == "PE":
        # a PE campaign hangs off a swing LOW; the scanners only know highs
        scan_rows = [RecoveryBar(r.timestamp, -r.open, -r.low, -r.high, -r.close) for r in signal_rows]
    if mother_rule == "run":
        mothers = find_run_mothers(scan_rows, run=run_bars, min_separation_bars=0)
    else:
        mothers = find_mother_candles(scan_rows, left_bars=3, right_bars=3, min_range_atr=0.8)

    # One "lot" is whatever `trade_inr` buys at the typical price, so a 1,2
    # schedule really does commit twice as much on the second trade.
    typical = median([by_t[s].close for s in shared]) or 1.0
    lot_size = max(1, int(round(trade_inr / typical)))

    step = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    price_book: dict = {}
    for stamp in shared:
        close = float(by_t[stamp].close)
        price_book[stamp] = close  # the EOD square-off asks at bar OPEN
        price_book[stamp + step] = close  # fills and targets ask at bar CLOSE

    engines = []
    for mother in mothers:
        index = shared.index(mother.timestamp)
        if index + 20 >= len(shared):
            continue
        row = by_s[mother.timestamp]
        mother_bar = RecoveryBar(row.timestamp, row.open, row.high, row.low, row.close)
        if side == "PE":
            mother_bar = mirror_bar(mother_bar)
        config = RecoveryConfig(timeframe=timeframe, cost_model=cost_model, **config_kwargs)
        engine_cls = FibZoneEntry if mode == "fib-zone" else TwoRedRecovery

        # THE TRADED PRICE IS THE PREMIUM. No strike, no expiry, no decay.
        # The engine asks at BAR-CLOSE moments (fills, targets) and at bar-open
        # moments (the EOD square-off), so both resolve to the close of the bar
        # they belong to. Keying only by open silently priced every entry a
        # whole bar late.
        def price_at(when, _strike, _expiry, _book=price_book):
            value = _book.get(when)
            return float(value) if value is not None else None

        engine = engine_cls(
            mother_bar,
            config,
            contract_for=lambda when, index_price: (0, date(2099, 1, 1)),
            premium_lookup=price_at,
            lot_size=lot_size,
        )
        watch_from = getattr(mother, "confirmed_at", None) or mother.timestamp
        bars = [
            RecoveryBar(by_s[s].timestamp, by_s[s].open, by_s[s].high, by_s[s].low, by_s[s].close)
            for s in shared
            if s > watch_from
        ]
        engine.run(mirror_bars(bars) if side == "PE" else bars)
        engines.append(engine)
    return engines, lot_size, typical, len(mothers)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tf", dest="timeframe", default="15m", choices=["5m", "15m", "1h"])
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--mode", choices=["ladder", "fib-zone"], default="ladder")
    ap.add_argument("--side", choices=["CE", "PE"], default="CE")
    ap.add_argument("--mother-rule", choices=["pivot", "run"], default="run")
    ap.add_argument("--run", type=int, default=5)
    ap.add_argument("--trade-inr", type=float, default=50_000.0, help="rupees committed by a 1-lot trade")
    ap.add_argument("--min-profit", type=float, default=500.0)
    ap.add_argument("--horizon-sessions", type=int, default=10)
    ap.add_argument("--only", default=None, help="one symbol instead of the whole universe")
    args = ap.parse_args()

    config_kwargs = dict(
        lots_schedule=(1, 2),
        min_profit_inr=args.min_profit,
        sl_source="entry",
        horizon_sessions=args.horizon_sessions,
    )
    print(
        f"[equity recovery] {args.timeframe} · {args.side} · {args.mode} · {args.mother_rule} mothers · "
        f"Rs {args.trade_inr:,.0f} per lot · {args.years}y\n"
    )
    header = (
        f"  {'symbol':<11}{'moth':>5}{'camp':>6}{'trades':>8}{'green':>7}{'NET':>13}{'worst':>11}{'shares/lot':>12}"
    )
    print(header)

    grand_net = 0.0
    grand_trades = 0
    for symbol in [args.only] if args.only else list(UNIVERSE):
        got = run_symbol(
            symbol,
            args.timeframe,
            args.years,
            mode=args.mode,
            side=args.side,
            mother_rule=args.mother_rule,
            run_bars=args.run,
            trade_inr=args.trade_inr,
            config_kwargs=config_kwargs,
        )
        if not got:
            print(f"  {symbol:<11} no data")
            continue
        engines, lot_size, typical, mother_count = got
        nets: list[float] = []
        trades = 0
        for engine in engines:
            if not engine.trades:
                continue
            priced = [t for t in engine.trades if t.entry_time is not None]
            if not priced or not engine.fully_priced:
                continue
            trades += len(priced)
            nets.append(engine.booked_net)
        if not nets:
            print(f"  {symbol:<11}{mother_count:>5}{0:>6}")
            continue
        net = sum(nets)
        green = 100.0 * sum(1 for n in nets if n > 0) / len(nets)
        print(
            f"  {symbol:<11}{mother_count:>5}{len(nets):>6}{trades:>8}{green:>6.0f}%"
            f"{net:>13,.0f}{min(nets):>11,.0f}{lot_size:>12}"
        )
        grand_net += net
        grand_trades += trades

    print("  " + "-" * 74)
    print(f"  {'TOTAL':<11}{'':>5}{'':>6}{grand_trades:>8}{'':>7}{grand_net:>13,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
