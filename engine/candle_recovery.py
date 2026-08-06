"""The two-red recovery buy WITH a stop loss, repeated until the book is green.

Phil's rules (2026-08-05), stated for one named mother candle on one timeframe
(1m, 5m, 15m or 1H -- the campaign never changes charts):

1.  Wait for two red candles to close, and the second must close below the
    FIRST red's LOW -- a lower close is not enough, the fall has to have eaten
    the whole prior candle.
2.  Place the buy entry on the SECOND red candle's HIGH.  (The older ladder
    armed at the first red's close; this is the stated change.)  Price must
    rise back through it -- a recovery is bought, never a falling knife.
3.  Stop loss: the LOW OF THE ENTRY CANDLE, marked at the fill.  The trade is
    stopped when a candle CLOSES below that level -- a wick through it is
    survivable, a close is not.
4.  After a stop, the same pattern repeats: the low is already broken (the
    stop candle closed under it), so watch for the next two-red pair and buy
    its recovery again.  Repeat until the target.
5.  THE TARGET IS A RECOVERY TARGET.  Each stopped trade books a real loss,
    costs included.  The open trade's exit is the premium at which its own net
    P&L pays back every booked loss AND clears a minimum profit on top --
    "if I have lost Rs 3,000 already, the latest trade has to go above
    Rs 3,500".
6.  SIZE ESCALATES: 1 lot on the first trade, 2 from the second (the
    lots_schedule), because by then the fall has proven itself and the ledger
    needs the bigger recovery.
7.  INTRADAY ONLY.  No carry forward: the day's last bar squares off any open
    trade (its result joins the ledger), an armed-but-unfilled trigger dies
    with the day, and a red pair never spans the overnight gap.  The CAMPAIGN
    continues next day -- the ledger it must recover carries, positions do not.

Every trade picks its own contract at its own fill: ATM minus `itm_steps`
strikes off the index, nearest expiry at least `min_dte` days out (the resolver
already rolls to the next week when the current one is too close).

This module owns index-space signals and paper fills.  Premiums come from a
lookup callable and are never fabricated: a missing premium defers the check to
the next bar rather than inventing a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from cascade_costs import OptionCostFill, calculate_nifty_option_basket_round_costs

TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}

# The last bar of the session a trade may still be held through, per timeframe.
# A bar OPENING at or after this moment is end-of-day: no new fill is taken on
# it and any open trade is squared off at its close.  (Dhan's series carry a
# 15:30 settlement stub on 5m/15m -- it is EOD by this rule too, so it can
# never arm, fill, or stop anything.)
EOD_OPEN_MINUTES: dict[str, int] = {
    "1m": 15 * 60 + 29,
    "5m": 15 * 60 + 25,
    "15m": 15 * 60 + 15,
    "1h": 15 * 60 + 15,
}

# A premium bar can be missing for a minute or two around the one asked for
# (illiquid strike, exchange pause). The lookup walks forward this far before
# giving up -- the same tolerance the fib-space pricer uses.
PREMIUM_FORWARD_MINUTES = 5


def round_costs(*, entry: float, exit_price: float, quantity: int, lots: int, model=None) -> float:
    """Charges for one buy-and-sell round, option basket unless told otherwise."""
    if model is not None:
        return float(model(entry=entry, exit_price=exit_price, quantity=quantity, lots=lots))
    return calculate_nifty_option_basket_round_costs(
        buys=[OptionCostFill(price=entry, quantity=quantity, lots=lots)],
        sell_price=exit_price,
        sell_quantity=quantity,
        sell_lots=lots,
    ).total


@dataclass(frozen=True)
class RecoveryBar:
    """One closed index candle of the campaign's timeframe."""

    timestamp: datetime  # bar OPEN time, naive IST
    open: float
    high: float
    low: float
    close: float

    @property
    def is_red(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class RecoveryConfig:
    timeframe: str = "15m"
    # Lots per trade, by trade number: the first trade takes lots_schedule[0],
    # the second lots_schedule[1], and every deeper one the last entry.  Phil's
    # sizing: 1 lot to probe, 2 lots once the fall has proven itself.
    lots_schedule: tuple[int, ...] = (1, 2)
    # The margin the recovering trade must clear ON TOP of the booked losses.
    min_profit_inr: float = 500.0
    # Where the stop sits, marked at the fill and never trailed:
    #   "entry"    the fill bar's own low
    #   "previous" the low of the bar BEFORE the fill -- usually the second red,
    #              so a wider stop that survives the entry bar's own wobble
    #   "ultimate" the lowest low since the mother (the original reading)
    sl_source: str = "entry"
    # ATM minus this many strike steps (CE), per the locked options work.
    itm_steps: int = 2
    min_dte: int = 4
    max_dte: int = 45
    # WHAT A ROUND COSTS. Defaults to the NIFTY option basket schedule. Cash
    # equities are charged differently (delivery STT, no lot concept), so the
    # equity runner injects its own -- the rules are the same either way, and
    # only the tax on them changes.
    cost_model: Optional[Callable[..., float]] = None
    # Safety valve: a mother that never recovers must not ladder losses forever.
    max_trades: int = 12
    # And a campaign must end: sessions counted from the mother, inclusive.
    horizon_sessions: int = 15

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAME_MINUTES:
            raise ValueError(f"timeframe must be one of {sorted(TIMEFRAME_MINUTES)}")
        if not self.lots_schedule or any(int(lots) <= 0 for lots in self.lots_schedule):
            raise ValueError("lots_schedule must be non-empty positive lots")
        if self.max_trades <= 0 or self.horizon_sessions <= 0:
            raise ValueError("max_trades and horizon_sessions must be positive")
        if self.sl_source not in {"entry", "previous", "ultimate"}:
            raise ValueError("sl_source must be entry, previous or ultimate")

    def lots_for_trade(self, trade_no: int) -> int:
        return int(self.lots_schedule[min(trade_no - 1, len(self.lots_schedule) - 1)])


@dataclass
class RecoveryTrade:
    """One buy and its one exit."""

    trade_no: int
    armed_at: datetime  # close of the qualifying second red
    trigger: float  # the second red's high, in index points
    entry_time: Optional[datetime] = None
    entry_index: Optional[float] = None
    sl_level: Optional[float] = None  # ultimate low since mother, at entry
    strike: Optional[int] = None
    expiry: Optional[date] = None
    lots: int = 0
    quantity: int = 0
    entry_premium: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_index: Optional[float] = None
    exit_premium: Optional[float] = None
    exit_reason: Optional[str] = None  # stop | target | expiry | end
    gross_pnl: Optional[float] = None
    costs: float = 0.0
    net_pnl: Optional[float] = None

    @property
    def open(self) -> bool:
        return self.entry_time is not None and self.exit_time is None

    @property
    def priced(self) -> bool:
        return self.entry_premium is not None

    # Set by the engine at construction so a trade can charge the right tax.
    cost_model: Optional[Callable[..., float]] = None

    def close_out(self, when: datetime, index_price: float, premium: Optional[float], reason: str) -> None:
        self.exit_time = when
        self.exit_index = index_price
        self.exit_premium = premium
        self.exit_reason = reason
        if premium is not None and self.entry_premium is not None and self.quantity:
            gross = round((premium - self.entry_premium) * self.quantity, 2)
            self.gross_pnl = gross
            self.costs = round(
                round_costs(
                    entry=self.entry_premium,
                    exit_price=premium,
                    quantity=self.quantity,
                    lots=self.lots,
                    model=self.cost_model,
                ),
                2,
            )
            self.net_pnl = round(gross - self.costs, 2)


class TwoRedRecovery:
    """Replay Phil's stop-loss recovery rules over one mother's aftermath.

    Pure over its inputs: index bars in, trades out.  ``contract_for`` picks the
    (strike, expiry) for a fill; ``premium_lookup`` prices a (minute, strike,
    expiry) or returns None.
    """

    def __init__(
        self,
        mother: RecoveryBar,
        config: RecoveryConfig,
        *,
        contract_for: Callable[[datetime, float], Optional[tuple[int, date]]],
        premium_lookup: Callable[[datetime, int, date], Optional[float]],
        lot_size: int,
    ) -> None:
        if mother.high <= mother.low:
            raise ValueError("mother candle must have range")
        self.mother = mother
        self.config = config
        self.contract_for = contract_for
        self.premium_lookup = premium_lookup
        self.lot_size = int(lot_size)
        self.trades: list[RecoveryTrade] = []
        self.status = "WATCHING"  # WATCHING | ARMED | IN_TRADE | RECOVERED | ABANDONED | ENDED
        self.end_reason: Optional[str] = None
        # The rule says the ultimate low AFTER the mother candle -- the mother's
        # own low is where the fall is measured FROM, never a level to stop at.
        self.ultimate_low = float("inf")
        self.events: list[dict[str, Any]] = []
        self._prev: Optional[RecoveryBar] = None
        self._pending: Optional[RecoveryTrade] = None
        self._sessions: set[date] = {mother.timestamp.date()}
        self._last_date: date = mother.timestamp.date()
        # THE NEW-LOW GATE. While the mother stands, every entry AFTER a
        # completed trade must come by breaking lower: the arming red has to
        # close below the standing ultimate low. Buying a bounce off a low that
        # nothing has closed under is buying over value -- it is how the 30 Mar
        # campaign paid 22,629.80 for a low of 22,470.15. Survives the night
        # with the mother; only the position is intraday, not the geometry.
        self._need_new_low = False
        self._low_before = float("inf")

    # ── the ledger ──────────────────────────────────────────────────────────

    @property
    def booked_net(self) -> float:
        """Net rupees of every CLOSED trade so far (losses are negative)."""
        return round(sum(t.net_pnl for t in self.trades if t.net_pnl is not None and not t.open), 2)

    @property
    def required_recovery(self) -> float:
        """What the open trade must net for the campaign to end green."""
        return round(max(0.0, -self.booked_net) + self.config.min_profit_inr, 2)

    @property
    def open_trade(self) -> Optional[RecoveryTrade]:
        return next((t for t in self.trades if t.open), None)

    @property
    def fully_priced(self) -> bool:
        return all(t.priced for t in self.trades if t.entry_time is not None)

    def _log(self, when: datetime, event: str, **payload: Any) -> None:
        self.events.append({"timestamp": when.isoformat(), "event": event, **payload})

    # ── premium plumbing ────────────────────────────────────────────────────

    def _premium(self, when: datetime, trade: RecoveryTrade) -> Optional[float]:
        if trade.strike is None or trade.expiry is None:
            return None
        for offset in range(PREMIUM_FORWARD_MINUTES + 1):
            price = self.premium_lookup(when + timedelta(minutes=offset), trade.strike, trade.expiry)
            if price is not None:
                return float(price)
        return None

    def _bar_close_time(self, bar: RecoveryBar) -> datetime:
        return bar.timestamp + timedelta(minutes=TIMEFRAME_MINUTES[self.config.timeframe])

    # ── the replay ──────────────────────────────────────────────────────────

    def on_bar(self, bar: RecoveryBar) -> None:
        if self.status in {"RECOVERED", "ABANDONED", "ENDED"}:
            return
        if bar.timestamp <= self.mother.timestamp:
            return

        self._sessions.add(bar.timestamp.date())
        if len(self._sessions) > self.config.horizon_sessions:
            self._end(bar, "horizon")
            return

        # INTRADAY, NO CARRY FORWARD.  A new session starts clean: yesterday's
        # half-formed red pair and any armed-but-unfilled trigger die with the
        # day -- the overnight gap makes both meaningless.
        if bar.timestamp.date() != self._last_date:
            self._last_date = bar.timestamp.date()
            self._prev = None
            if self._pending is not None and self._pending.entry_time is None:
                self.trades.remove(self._pending)
                self._pending = None
                if self.status == "ARMED":
                    self.status = "WATCHING"

        minute = bar.timestamp.hour * 60 + bar.timestamp.minute
        is_eod = minute >= EOD_OPEN_MINUTES[self.config.timeframe]

        # Fills and stops read the bar first; the ultimate low then absorbs it,
        # so an entry's SL includes its own bar's low but never a later one.
        if self.status == "ARMED" and not is_eod:
            self._try_fill(bar)
        if self.status == "IN_TRADE":
            if is_eod:
                self._square_off_eod(bar)
            else:
                self._manage_open(bar)

        # The gate must judge this bar against the lows that came BEFORE it --
        # ultimate_low is updated here, so the pre-bar value is kept for it.
        self._low_before = self.ultimate_low
        self.ultimate_low = min(self.ultimate_low, float(bar.low))

        if self.status in {"WATCHING", "ARMED"} and not is_eod:
            self._watch_reds(bar)
        self._prev = bar

    def run(self, bars: list[RecoveryBar]) -> "TwoRedRecovery":
        for bar in sorted(bars, key=lambda row: row.timestamp):
            self.on_bar(bar)
        if self.status not in {"RECOVERED", "ABANDONED", "ENDED"} and bars:
            self._end(bars[-1], "end_of_data")
        return self

    # ── arming ──────────────────────────────────────────────────────────────

    def _watch_reds(self, bar: RecoveryBar) -> None:
        prev = self._prev
        if prev is None or not (prev.is_red and bar.is_red):
            return
        # THE QUALIFIER: the second red must close below the first red's LOW.
        if bar.close >= prev.low:
            return
        if self._need_new_low and bar.close >= getattr(self, "_low_before", self.ultimate_low):
            return  # over value: this pair has not broken the standing low
        if len(self.trades) >= self.config.max_trades and self._pending is None:
            self._end(bar, "max_trades")
            return
        when = self._bar_close_time(bar)
        if self._pending is None:
            trade = RecoveryTrade(
                trade_no=len(self.trades) + 1,
                armed_at=when,
                trigger=float(bar.high),
                cost_model=self.config.cost_model,
            )
            self.trades.append(trade)
            self._pending = trade
            self.status = "ARMED"
            self._need_new_low = False
            self._log(when, "armed", trade=trade.trade_no, trigger=trade.trigger)
        else:
            # A newer qualifying pair re-arms at ITS second red's high -- the
            # trigger follows the fall down, exactly like the older ladder.
            self._pending.armed_at = when
            self._pending.trigger = float(bar.high)
            self._log(when, "rearmed", trade=self._pending.trade_no, trigger=self._pending.trigger)

    # ── filling ─────────────────────────────────────────────────────────────

    def _stop_for(self, bar: RecoveryBar) -> float:
        """Where this fill's stop sits. Marked once, never trailed.

        Default is the ENTRY candle's low -- Phil, 2026-08-05: "The SL has to be
        the low of the entry candle." The ultimate low since the mother sat far
        below the fill and let a trade bleed a long way before stopping; the
        previous bar's low is the middle setting. A wick through the level
        survives it; only a CLOSE below stops the trade.
        """
        source = self.config.sl_source
        if source == "ultimate":
            return round(min(self.ultimate_low, float(bar.low)), 2)
        if source == "previous" and self._prev is not None:
            return round(float(self._prev.low), 2)
        return round(float(bar.low), 2)

    def _try_fill(self, bar: RecoveryBar) -> None:
        trade = self._pending
        if trade is None or bar.high < trade.trigger:
            return
        # A gap straight over the trigger fills at the open, not at a price the
        # market never traded on the way up.
        entry_index = float(max(trade.trigger, bar.open))
        picked = self.contract_for(bar.timestamp, entry_index)
        if picked is None:
            trade.exit_reason = "no_contract"
            self._log(bar.timestamp, "no_contract", trade=trade.trade_no)
            self.trades.remove(trade)
            self._pending = None
            self.status = "WATCHING"
            return
        trade.strike, trade.expiry = int(picked[0]), picked[1]
        trade.entry_time = bar.timestamp
        trade.entry_index = entry_index
        trade.lots = self.config.lots_for_trade(trade.trade_no)
        trade.quantity = trade.lots * self.lot_size
        # The SL is marked AT ENTRY: the lowest low since the mother, including
        # the fall that armed this trade but not anything after the fill.
        trade.sl_level = self._stop_for(bar)
        # Priced at the fill bar's CLOSE minute, deliberately the conservative
        # side. The trigger is touched mid-bar after the index has already
        # risen; the bar-open premium is from BEFORE that rise, so open-minute
        # pricing buys every entry cheaper than any real order could.
        trade.entry_premium = self._premium(self._bar_close_time(bar), trade)
        self._pending = None
        self.status = "IN_TRADE"
        self._log(
            bar.timestamp,
            "filled",
            trade=trade.trade_no,
            entry_index=entry_index,
            sl=trade.sl_level,
            strike=trade.strike,
            expiry=trade.expiry.isoformat(),
            premium=trade.entry_premium,
        )

    # ── managing the open trade ─────────────────────────────────────────────

    def _manage_open(self, bar: RecoveryBar) -> None:
        trade = self.open_trade
        if trade is None:  # the fill bar itself never manages
            return
        if bar.timestamp <= trade.entry_time:
            return
        when = self._bar_close_time(bar)

        # Expiry square-off comes first: past 15:30 on expiry day there is no
        # contract left to hold whatever the chart says.
        past_expiry = trade.expiry is not None and (
            bar.timestamp.date() > trade.expiry
            or (bar.timestamp.date() == trade.expiry and when.hour * 60 + when.minute >= 15 * 60 + 30)
        )
        if past_expiry:
            premium = self._premium(bar.timestamp, trade)
            trade.close_out(when, float(bar.close), premium, "expiry")
            self._need_new_low = True
            self._log(when, "expiry_close", trade=trade.trade_no, net=trade.net_pnl)
            self.status = "WATCHING"
            return

        # THE STOP: a CLOSE below the marked low ends the trade; a wick does not.
        if trade.sl_level is not None and bar.close < trade.sl_level:
            premium = self._premium(when, trade)
            trade.close_out(when, float(bar.close), premium, "stop")
            self._need_new_low = True
            self._log(when, "stopped", trade=trade.trade_no, close=bar.close, sl=trade.sl_level, net=trade.net_pnl)
            # The low is broken by construction, so the watch resumes at once --
            # this stop bar may already be the first red of the next pair.
            self.status = "WATCHING"
            return

        # THE RECOVERY TARGET, in rupees on the premium, costs included.
        premium = self._premium(when, trade)
        if premium is None or trade.entry_premium is None:
            return
        gross = (premium - trade.entry_premium) * trade.quantity
        net = gross - round_costs(
            entry=trade.entry_premium,
            exit_price=premium,
            quantity=trade.quantity,
            lots=trade.lots,
            model=self.config.cost_model,
        )
        if net >= self.required_recovery:
            trade.close_out(when, float(bar.close), premium, "target")
            self.status = "RECOVERED"
            self.end_reason = "target"
            self._log(when, "target", trade=trade.trade_no, net=trade.net_pnl, booked=self.booked_net)

    def _square_off_eod(self, bar: RecoveryBar) -> None:
        """No carry forward: the day's last bar closes any open trade.

        Priced at the bar's OPEN minute -- the last minutes that really traded;
        the 15:30 settlement print is not a price an order could get.
        """
        trade = self.open_trade
        if trade is None:
            return
        when = self._bar_close_time(bar)
        premium = self._premium(bar.timestamp, trade)
        trade.close_out(when, float(bar.close), premium, "eod")
        self._need_new_low = True
        self._log(when, "eod_square_off", trade=trade.trade_no, net=trade.net_pnl)
        self.status = "WATCHING"

    # ── endings ─────────────────────────────────────────────────────────────

    def _end(self, bar: RecoveryBar, reason: str) -> None:
        when = self._bar_close_time(bar)
        trade = self.open_trade
        if trade is not None:
            premium = self._premium(when, trade)
            trade.close_out(when, float(bar.close), premium, "end")
            self._log(when, "end_close", trade=trade.trade_no, net=trade.net_pnl)
        if self._pending is not None and self._pending.entry_time is None:
            self.trades.remove(self._pending)
            self._pending = None
        self.status = "ABANDONED" if self.booked_net < 0 else "ENDED"
        self.end_reason = reason
        self._log(when, "campaign_over", reason=reason, booked=self.booked_net, trades=len(self.trades))


__all__ = [
    "RecoveryBar",
    "RecoveryConfig",
    "RecoveryTrade",
    "TwoRedRecovery",
    "TIMEFRAME_MINUTES",
]


# ── the 5m fib-zone mode ────────────────────────────────────────────────────
#
# Phil's rule 10, decoded 2026-08-05: the recovery ladder churns on 5m, so on
# that chart the FIBS decide where entries live and there are exactly two.
#
#   * the SELLER swing is mother high -> the first swing low after the mother;
#   * the BUYER swing is the bounce's highest high -> the SAME low, and it only
#     exists once price BREAKS that low (a bounce that holds needs no buying);
#   * each fib extends downward, level k at high - k x (high - low).  The two
#     level-2s bracket the 2-2 ZONE, the level-4s the 4-4 ZONE -- the buyer fib
#     sits above the seller fib because its high anchor is lower;
#   * entry 1 takes 1 lot at the 2-2 zone, entry 2 takes 2 lots at the 4-4
#     zone.  Inside a zone the mechanism is unchanged: two reds with the second
#     closing below the zone's first (upper) boundary, buy the recovery at the
#     second red's high, stop on a CLOSE below the ultimate low.
#
# One entry per zone -- a stopped zone is spent, there is no re-arm inside it.
# Exit is the ledger target: when booked plus open P&L clears min_profit_inr,
# everything leaves together.  Intraday: EOD squares off, the fibs persist.

# BUYER INVOLVEMENT is what anchors the swing low: the first run of this many
# consecutive green candles after the mother.  Phil, on being shown the wrong
# chart: "There is already a buyer involvement here... 2 layers, specifically 2
# or more green candles."  A 3-bar pivot instead looked right past it -- on the
# 25 Mar mother it took the low 273 points lower and two sessions later, which
# is what turned the 2-2 zone into a 312-point barn door.
BUYER_INVOLVEMENT_GREENS = 2


@dataclass(frozen=True)
class FibZone:
    level: int
    upper: float  # buyer fib's level -- reached first on the way down
    lower: float  # seller fib's level
    lots: int


class FibZoneEntry:
    """The two-fib zone variant: entries at the 2-2 and 4-4 zones, 1 and 2 lots."""

    def __init__(
        self,
        mother: RecoveryBar,
        config: RecoveryConfig,
        *,
        contract_for: Callable[[datetime, float], Optional[tuple[int, date]]],
        premium_lookup: Callable[[datetime, int, date], Optional[float]],
        lot_size: int,
    ) -> None:
        if mother.high <= mother.low:
            raise ValueError("mother candle must have range")
        self.mother = mother
        self.config = config
        self.contract_for = contract_for
        self.premium_lookup = premium_lookup
        self.lot_size = int(lot_size)
        self.trades: list[RecoveryTrade] = []
        self.status = "AWAIT_LOW"  # AWAIT_LOW | AWAIT_BREAK | ZONES | RECOVERED | ABANDONED | ENDED
        self.end_reason: Optional[str] = None
        self.swing_low: Optional[float] = None
        self.buyer_high: Optional[float] = None
        self.zones: list[FibZone] = []
        self.zone_index = 0  # the next zone allowed to arm
        self.ultimate_low = float("inf")
        self.events: list[dict[str, Any]] = []
        self._green_run = 0  # consecutive green closes, for buyer involvement
        self._low_so_far = float("inf")  # lowest low since the mother
        # THE NEW-LOW GATE.  After a trade closes, the next one may not arm
        # until a candle CLOSES below the standing ultimate low.  Without it a
        # gap-down that bounces produces two reds INSIDE the bounce, and the
        # entry gets bought over value with no new low -- which is exactly how
        # the 30 Mar campaign bought 22,629.80 when the low was 22,470.15 and
        # nothing had closed under it.  CryptoForge has always refused this.
        self._need_new_low = False
        self._prev: Optional[RecoveryBar] = None
        self._pending: Optional[RecoveryTrade] = None
        self._pending_zone: Optional[FibZone] = None
        self._sessions: set[date] = {mother.timestamp.date()}
        self._last_date: date = mother.timestamp.date()

    booked_net = TwoRedRecovery.booked_net
    open_trade = TwoRedRecovery.open_trade
    fully_priced = TwoRedRecovery.fully_priced
    _log = TwoRedRecovery._log
    _premium = TwoRedRecovery._premium
    _bar_close_time = TwoRedRecovery._bar_close_time

    @property
    def open_trades(self) -> list[RecoveryTrade]:
        return [t for t in self.trades if t.open]

    @property
    def required_recovery(self) -> float:
        return round(max(0.0, -self.booked_net) + self.config.min_profit_inr, 2)

    # ── geometry ────────────────────────────────────────────────────────────

    def _watch_buyer_involvement(self, bar: RecoveryBar) -> None:
        """Freeze the swing low at the FIRST buyer involvement after the mother.

        Buyer involvement is BUYER_INVOLVEMENT_GREENS consecutive green closes.
        The low it anchors is the lowest low printed from the mother up to and
        including that run -- the bottom of the first fall, which is where the
        buying answered.  A doji or the exchange's flat 15:30 settlement stub
        closes level, so `close > open` (strict) keeps it out of the run.
        """
        self._low_so_far = min(getattr(self, "_low_so_far", float("inf")), float(bar.low))
        if bar.close > bar.open:
            self._green_run = getattr(self, "_green_run", 0) + 1
        else:
            self._green_run = 0
            return
        if self._green_run < BUYER_INVOLVEMENT_GREENS:
            return
        self.swing_low = round(self._low_so_far, 2)
        self.buyer_high = float(bar.high)  # grows with the rest of the bounce
        self.status = "AWAIT_BREAK"
        self._log(bar.timestamp, "buyer_involvement", low=self.swing_low, greens=self._green_run)

    def _watch_break(self, bar: RecoveryBar) -> None:
        """Track the bounce high; the buyer fib is born when the low breaks."""
        if bar.high > self.mother.high:
            self._end_now(bar, "mother_broken")
            return
        self.buyer_high = max(self.buyer_high, float(bar.high))
        if bar.close >= self.swing_low:
            return
        if self.buyer_high <= self.swing_low:
            return
        seller = lambda k: self.mother.high - k * (self.mother.high - self.swing_low)  # noqa: E731
        buyer = lambda k: self.buyer_high - k * (self.buyer_high - self.swing_low)  # noqa: E731
        self.zones = [
            FibZone(level=level, upper=round(buyer(level), 2), lower=round(seller(level), 2), lots=lots)
            for level, lots in ((2, 1), (4, 2))
        ]
        self.status = "ZONES"
        self._log(
            bar.timestamp,
            "zones_drawn",
            buyer_high=self.buyer_high,
            swing_low=self.swing_low,
            zones=[{"level": z.level, "upper": z.upper, "lower": z.lower} for z in self.zones],
        )

    # ── zone entries, same mechanism ────────────────────────────────────────

    def _watch_zone_reds(self, bar: RecoveryBar) -> None:
        if self.zone_index >= len(self.zones):
            return
        if self._need_new_low and bar.close >= self.ultimate_low:
            # OVER VALUE. The gap-down bar made the low; buying the bounce off
            # it is buying above a low nothing has closed under. The arming red
            # must itself close below the standing low -- Phil, on the 30 Mar
            # chart: "the candle has to close below 22470 to take the trade".
            return
        zone = self.zones[self.zone_index]
        prev = self._prev
        if prev is None or not (prev.is_red and bar.is_red):
            return
        if bar.close >= prev.low:  # the same qualifier as the ladder
            return
        if bar.close >= zone.upper:  # the zone's first level must be broken
            return
        when = self._bar_close_time(bar)
        if self._pending is None:
            trade = RecoveryTrade(
                trade_no=len(self.trades) + 1,
                armed_at=when,
                trigger=float(bar.high),
                cost_model=self.config.cost_model,
            )
            self.trades.append(trade)
            self._pending = trade
            self._pending_zone = zone
            self._need_new_low = False
            self._log(when, "zone_armed", zone=zone.level, trade=trade.trade_no, trigger=trade.trigger)
        else:
            self._pending.armed_at = when
            self._pending.trigger = float(bar.high)
            self._log(when, "zone_rearmed", zone=zone.level, trigger=self._pending.trigger)

    def _stop_for(self, bar: RecoveryBar) -> float:
        """Where this fill's stop sits. Marked once, never trailed.

        Default is the ENTRY candle's low -- Phil, 2026-08-05: "The SL has to be
        the low of the entry candle." The ultimate low since the mother sat far
        below the fill and let a trade bleed a long way before stopping; the
        previous bar's low is the middle setting. A wick through the level
        survives it; only a CLOSE below stops the trade.
        """
        source = self.config.sl_source
        if source == "ultimate":
            return round(min(self.ultimate_low, float(bar.low)), 2)
        if source == "previous" and self._prev is not None:
            return round(float(self._prev.low), 2)
        return round(float(bar.low), 2)

    def _try_fill(self, bar: RecoveryBar) -> None:
        trade, zone = self._pending, self._pending_zone
        if trade is None or zone is None or bar.high < trade.trigger:
            return
        entry_index = float(max(trade.trigger, bar.open))
        picked = self.contract_for(bar.timestamp, entry_index)
        if picked is None:
            self.trades.remove(trade)
            self._pending = self._pending_zone = None
            return
        trade.strike, trade.expiry = int(picked[0]), picked[1]
        trade.entry_time = bar.timestamp
        trade.entry_index = entry_index
        trade.lots = zone.lots
        trade.quantity = zone.lots * self.lot_size
        trade.sl_level = self._stop_for(bar)
        trade.entry_premium = self._premium(self._bar_close_time(bar), trade)
        self._pending = self._pending_zone = None
        self.zone_index += 1  # the zone is spent by its one entry
        self._log(
            bar.timestamp,
            "zone_filled",
            zone=zone.level,
            trade=trade.trade_no,
            entry_index=entry_index,
            sl=trade.sl_level,
            lots=trade.lots,
            premium=trade.entry_premium,
        )

    # ── managing the basket ─────────────────────────────────────────────────

    def _open_net(self, when: datetime) -> Optional[float]:
        """Net rupees if every open trade left at this minute's premium."""
        total = 0.0
        for trade in self.open_trades:
            premium = self._premium(when, trade)
            if premium is None or trade.entry_premium is None:
                return None
            total += (premium - trade.entry_premium) * trade.quantity - round_costs(
                entry=trade.entry_premium,
                exit_price=premium,
                quantity=trade.quantity,
                lots=trade.lots,
                model=self.config.cost_model,
            )
        return total

    def _close_all(self, bar: RecoveryBar, reason: str, *, at_open_minute: bool = False) -> None:
        when = self._bar_close_time(bar)
        for trade in self.open_trades:
            premium = self._premium(bar.timestamp if at_open_minute else when, trade)
            trade.close_out(when, float(bar.close), premium, reason)

    def _manage_basket(self, bar: RecoveryBar) -> None:
        if not self.open_trades:
            return
        when = self._bar_close_time(bar)
        newest = max(t.entry_time for t in self.open_trades)
        if bar.timestamp <= newest:
            return
        sl = min(t.sl_level for t in self.open_trades if t.sl_level is not None)
        if bar.close < sl:
            self._close_all(bar, "stop")
            self._need_new_low = True
            self._log(when, "basket_stopped", close=bar.close, sl=sl, booked=self.booked_net)
            if self.zone_index >= len(self.zones):
                self._end_now(bar, "zones_spent")
            return
        open_net = self._open_net(when)
        if open_net is not None and self.booked_net + open_net >= self.config.min_profit_inr:
            self._close_all(bar, "target")
            self.status = "RECOVERED"
            self.end_reason = "target"
            self._log(when, "basket_target", booked=self.booked_net)

    # ── the replay ──────────────────────────────────────────────────────────

    def _end_now(self, bar: RecoveryBar, reason: str) -> None:
        self._close_all(bar, "end")
        if self._pending is not None and self._pending.entry_time is None:
            self.trades.remove(self._pending)
            self._pending = self._pending_zone = None
        self.status = "ABANDONED" if self.booked_net < 0 else "ENDED"
        self.end_reason = reason
        self._log(self._bar_close_time(bar), "campaign_over", reason=reason, booked=self.booked_net)

    def on_bar(self, bar: RecoveryBar) -> None:
        if self.status in {"RECOVERED", "ABANDONED", "ENDED"}:
            return
        if bar.timestamp <= self.mother.timestamp:
            return
        self._sessions.add(bar.timestamp.date())
        if len(self._sessions) > self.config.horizon_sessions:
            self._end_now(bar, "horizon")
            return
        if bar.timestamp.date() != self._last_date:
            self._last_date = bar.timestamp.date()
            self._prev = None
            if self._pending is not None and self._pending.entry_time is None:
                self.trades.remove(self._pending)
                self._pending = self._pending_zone = None

        minute = bar.timestamp.hour * 60 + bar.timestamp.minute
        is_eod = minute >= EOD_OPEN_MINUTES[self.config.timeframe]

        if self.status == "AWAIT_LOW":
            if bar.high > self.mother.high:
                self._end_now(bar, "mother_broken")
                return
            self._watch_buyer_involvement(bar)
        elif self.status == "AWAIT_BREAK":
            self._watch_break(bar)
        elif self.status == "ZONES":
            if self._pending is not None and not is_eod:
                self._try_fill(bar)
            if self.open_trades:
                if is_eod:
                    self._close_all(bar, "eod", at_open_minute=True)
                    self._need_new_low = True
                    self._log(self._bar_close_time(bar), "eod_square_off", booked=self.booked_net)
                    if self.zone_index >= len(self.zones):
                        self._end_now(bar, "zones_spent")
                else:
                    self._manage_basket(bar)
            if self.status == "ZONES" and not is_eod:
                self._watch_zone_reds(bar)

        self.ultimate_low = min(self.ultimate_low, float(bar.low))
        self._prev = bar

    def run(self, bars: list[RecoveryBar]) -> "FibZoneEntry":
        for bar in sorted(bars, key=lambda row: row.timestamp):
            self.on_bar(bar)
        if self.status not in {"RECOVERED", "ABANDONED", "ENDED"} and bars:
            self._end_now(bars[-1], "end_of_data")
        return self


# ── the PE side ─────────────────────────────────────────────────────────────
#
# A put campaign is the same strategy upside down: the mother is a swing LOW,
# two GREEN candles where the second closes ABOVE the first's HIGH arm the
# entry, the buy is at the second green's LOW, the stop is the entry candle's
# HIGH, and every later entry must break HIGHER.
#
# Rather than fork the engine and keep two sets of comparisons in step, the
# BARS are mirrored: negate every price and swap high with low.  Then red
# becomes green, "closes below the previous low" becomes "closes above the
# previous high", and the stop flips with them -- the engine is untouched and
# there is exactly one implementation of the rules to get right.
#
# Index-space numbers coming back out (trigger, entry, stop, exit) are in the
# mirrored frame and must be negated again before a human reads them.
# Premiums are NOT mirrored: they are real rupees on a real PE contract.


def mirror_bar(bar: RecoveryBar) -> RecoveryBar:
    """Flip a bar through zero. High and low swap as well as negate."""
    return RecoveryBar(bar.timestamp, -bar.open, -bar.low, -bar.high, -bar.close)


def mirror_bars(bars: Iterable[RecoveryBar]) -> list[RecoveryBar]:
    return [mirror_bar(b) for b in bars]


def unmirror_price(value: Optional[float]) -> Optional[float]:
    """An index-space number from a mirrored run, back in real prices."""
    return None if value is None else -float(value)
