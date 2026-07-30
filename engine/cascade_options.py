"""Deterministic 1H NIFTY option-cascade state machine.

This module deliberately has no broker or web dependencies.  It replays closed
index candles and exact option candles supplied by a data adapter.  That keeps
the strategy rules testable before Dhan historical-data access is available.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from cascade_costs import (
    NiftyOptionCostSchedule,
    OptionCostFill,
    OptionRoundCosts,
    calculate_nifty_option_basket_round_costs,
)
from engine.cascade_fib_geometry import boundaries_for_timeframe, boundary_price, normalise_timeframe


class CascadeError(ValueError):
    """Invalid strategy configuration or missing historical data."""


class ContractSelectionError(CascadeError):
    """No contract satisfies the configured DTE and strike rules."""


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def is_red(self) -> bool:
        return self.close < self.open

    @property
    def is_green(self) -> bool:
        return self.close > self.open


@dataclass(frozen=True)
class OptionCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Contract:
    symbol: str
    expiry: date
    strike: float
    option_type: str  # CE or PE
    lot_size: int = 65

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.expiry.isoformat()}|{self.strike:g}|{self.option_type}"


@dataclass(frozen=True)
class CascadeConfig:
    mother_timestamp: datetime
    mother_high: float
    mother_low: float
    option_type: str = "CE"
    timeframe: str = "1h"
    stage_timeframes: tuple[str, ...] = ()
    lot_schedule: tuple[int, ...] = (1, 2, 3)
    lot_size: int = 65
    itm_steps: int = 2
    strike_step: float = 50.0
    min_dte: int = 7
    max_dte: int = 13
    target_fraction: float = 0.25
    strict_option_data: bool = True
    force_exit_on_expiry: bool = True

    # ── Order mechanics ────────────────────────────────────────────
    # A buy-stop placed at the previous candle's close is RESTING in the market
    # for the whole of the next bar.  If that bar's high reaches the trigger it
    # fills, whatever the bar later closes at.  The engine originally suppressed
    # that fill whenever the same bar also closed low enough to walk the stop
    # down, which quietly deleted precisely the whipsaw entries that go against
    # the position.  True reproduces real order mechanics; False reproduces the
    # original behaviour so the difference can be measured rather than argued.
    fill_before_stop_walk: bool = True

    # ── Rule interpretations (swept, not assumed) ──────────────────
    # Rule 4, "the close has to be low compared to the previous candle":
    #   "last_qualifying" -> compare against the previous qualifying red close
    #                        (greens in between are skipped entirely)
    #   "previous_candle" -> compare against the immediately preceding candle's
    #                        close, whatever colour it was
    arm_compare: str = "last_qualifying"
    # Rule 2: must the FIRST of the two reds also close beyond the reference
    # (mother low, then each marked low), or only the second?
    first_leg_beyond_reference: bool = True
    # Rule 6, "mark the latest low before the entry candle":
    #   "lowest" -> the lowest low of the whole stage window
    #   "latest" -> the low of the last candle before the entry candle
    mark_low_mode: str = "lowest"
    # Rule 9: the stop walks down over several bars, so by the time it fills the
    # spot may be far from where the strike was chosen.  A real desk cancelling
    # and re-placing the order would re-pick ATM-N at the new level.
    restrike_on_stop_walk: bool = False

    # ── Execution frictions ────────────────────────────────────────
    # Index points paid through the trigger (stop orders fill at or worse than
    # the trigger, never better).
    slippage_points: float = 0.0
    # Fraction of premium given up to the bid-ask spread on each side.  The
    # strategy deliberately trades the NEXT weekly rather than the current one,
    # which is a thinner book than the front week: do not leave this at zero.
    option_slippage_pct: float = 0.0
    cost_schedule: Optional[NiftyOptionCostSchedule] = None

    def __post_init__(self) -> None:
        side = str(self.option_type).upper()
        if side not in {"CE", "PE"}:
            raise CascadeError("option_type must be CE or PE")
        if not self.lot_schedule or any(int(x) <= 0 for x in self.lot_schedule):
            raise CascadeError("lot_schedule must contain positive lot counts")
        if self.lot_size <= 0 or self.itm_steps <= 0 or self.strike_step <= 0:
            raise CascadeError("lot_size, itm_steps and strike_step must be positive")
        if not 0 < self.target_fraction <= 1:
            raise CascadeError("target_fraction must be between 0 and 1")
        if not 0 <= self.min_dte <= self.max_dte:
            raise CascadeError("invalid DTE range")
        if self.arm_compare not in {"last_qualifying", "previous_candle"}:
            raise CascadeError("arm_compare must be 'last_qualifying' or 'previous_candle'")
        if self.mark_low_mode not in {"lowest", "latest"}:
            raise CascadeError("mark_low_mode must be 'lowest' or 'latest'")
        if self.slippage_points < 0 or not 0 <= self.option_slippage_pct < 1:
            raise CascadeError("slippage_points must be >= 0 and option_slippage_pct in [0, 1)")
        base_timeframe = self.timeframe.lower()
        defaults = {
            "1m": ("1m", "5m", "15m"),
            "5m": ("5m", "15m", "1h"),
            "15m": ("15m", "1h", "4h"),
            "1h": ("1h", "4h", "1d"),
        }
        if base_timeframe not in defaults:
            raise CascadeError("timeframe must be 1m, 5m, 15m, or 1h")
        stages = tuple(str(value).lower() for value in (self.stage_timeframes or defaults[base_timeframe]))
        if len(stages) < len(self.lot_schedule):
            raise CascadeError("stage_timeframes must cover every lot stage")
        if stages[0] != base_timeframe or any(value not in {"1m", "5m", "15m", "1h", "4h", "1d"} for value in stages):
            raise CascadeError("invalid cascade stage timeframe")
        object.__setattr__(self, "timeframe", base_timeframe)
        object.__setattr__(self, "stage_timeframes", stages[: len(self.lot_schedule)])


class NiftyContractResolver:
    """Resolve the fixed contract selected at each cascade stage.

    The resolver intentionally receives historical expiry dates.  It never
    assumes today's expiry calendar or today's lot size, which is required for
    a one-year replay spanning exchange rule changes.
    """

    def __init__(
        self,
        expiries: Iterable[date],
        *,
        strike_step: float = 50.0,
        lot_size: int = 65,
        symbol: str = "NIFTY",
    ) -> None:
        self.expiries = tuple(sorted(expiries))
        self.strike_step = float(strike_step)
        self.lot_size = int(lot_size)
        self.symbol = symbol
        if self.strike_step <= 0 or self.lot_size <= 0:
            raise CascadeError("invalid contract resolver configuration")

    def select(self, timestamp: datetime, spot: float, option_type: str, config: CascadeConfig) -> Contract:
        side = str(option_type).upper()
        if side not in {"CE", "PE"}:
            raise ContractSelectionError("option_type must be CE or PE")
        trade_date = timestamp.date()
        current_week = trade_date.isocalendar()[:2]
        eligible = []
        for expiry in self.expiries:
            dte = (expiry - trade_date).days
            # A Tuesday market holiday shifts weekly expiry to the preceding
            # session (normally Monday). Allow that single 6-DTE exception,
            # while retaining the regular 7-DTE floor for normal Tuesdays.
            minimum_dte = config.min_dte - 1 if expiry.weekday() != 1 else config.min_dte
            if expiry <= trade_date or not minimum_dte <= dte <= config.max_dte:
                continue
            if expiry.isocalendar()[:2] == current_week:
                continue
            eligible.append((dte, expiry))
        if not eligible:
            raise ContractSelectionError(
                f"no {side} expiry in {config.min_dte}-{config.max_dte} DTE outside current expiry week "
                f"for {trade_date}"
            )
        expiry = min(eligible, key=lambda item: (item[0], item[1]))[1]
        atm = math.floor(float(spot) / self.strike_step + 0.5) * self.strike_step
        strike = (
            atm - config.itm_steps * self.strike_step if side == "CE" else atm + config.itm_steps * self.strike_step
        )
        return Contract(self.symbol, expiry, float(strike), side, self.lot_size)


@dataclass(frozen=True)
class Entry:
    timestamp: datetime
    spot: float
    option_price: Optional[float]
    lots: int
    quantity: int
    contract: Contract
    stage: int


@dataclass
class CascadeResult:
    status: str
    entries: list[Entry] = field(default_factory=list)
    exit_timestamp: Optional[datetime] = None
    exit_option_price: Optional[float] = None
    target_index: Optional[float] = None
    average_spot: Optional[float] = None
    realized_pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    data_gap: Optional[str] = None
    events: list[dict] = field(default_factory=list)
    # Every missing option bar, whether or not it aborted the run.  A one-year
    # replay will always have some; the point is that the report shows how many
    # rather than one gap ending the campaign or, worse, going unmentioned.
    data_gaps: list[str] = field(default_factory=list)
    exit_option_prices: list[Optional[float]] = field(default_factory=list)
    costs_total: float = 0.0
    net_pnl: Optional[float] = None
    index_move: Optional[float] = None  # target - average entry, in index points

    @property
    def fully_priced(self) -> bool:
        """True when every leg has a real entry and exit premium."""
        return (
            bool(self.entries)
            and not self.data_gaps
            and all(entry.option_price is not None for entry in self.entries)
            and bool(self.exit_option_prices)
            and all(price is not None for price in self.exit_option_prices)
        )


OptionLookup = Callable[[datetime, Contract], Optional[OptionCandle]]

# Nominal bar length, used only as a fallback when a series has no following
# bar to read the true close time from.  NSE's final intraday bar of the day is
# irregular (the 14:15 hourly bar runs 75 minutes to 15:30), so the real close
# time is always preferred over these.
TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 375}


def _close_times(rows: list[Candle], timeframe: str) -> list[datetime]:
    """Close timestamp of each bar, taken from the next bar's open where possible.

    Dhan stamps a candle at its OPEN.  Replaying merged timeframes in open-time
    order lets a 1H bar stamped 10:15 act an hour before it has finished, ahead
    of the 5m bars that make it up -- the engine would be reading its own
    future.  Ordering by close time is what removes that.
    """
    fallback = timedelta(minutes=TIMEFRAME_MINUTES.get(timeframe, 60))
    closes: list[datetime] = []
    for position, candle in enumerate(rows):
        following = rows[position + 1] if position + 1 < len(rows) else None
        # A following bar on the next trading day says nothing about when this
        # one closed, so only trust it within the same session.
        if following is not None and following.timestamp.date() == candle.timestamp.date():
            closes.append(following.timestamp)
        else:
            closes.append(candle.timestamp + fallback)
    return closes


class OneHourCascade:
    """Mirror-symmetric CE/PE cascade with stage-specific timeframes.

    The historical name is retained for compatibility.  `CascadeConfig` now
    controls the stage route: 5m→15m→1h, 15m→1h→4h, or 1h→4h→1d.
    """

    def __init__(
        self,
        config: CascadeConfig,
        resolver: NiftyContractResolver,
        option_lookup: OptionLookup | Mapping[tuple[datetime, str], OptionCandle],
    ) -> None:
        self.config = config
        self.resolver = resolver
        self.option_lookup = option_lookup
        self.result = CascadeResult(status="waiting")
        self._side = config.option_type.upper()
        self._stage = 0
        self._stage_timeframes = config.stage_timeframes
        self._active_timeframe = self._stage_timeframes[0]
        self._stage_started_at = config.mother_timestamp
        self._reference = config.mother_low if self._side == "CE" else config.mother_high
        self._last_same_colour_close: Optional[float] = None
        self._previous_close: Optional[float] = None
        self._leg_seen = False
        self._pending_trigger: Optional[float] = None
        self._pending_since: Optional[datetime] = None
        self._pending_contract: Optional[Contract] = None
        self._stage_extreme: Optional[float] = self._reference
        self._stage_last_low: Optional[float] = None
        self._last_entry_timestamp: Optional[datetime] = None

    def _lookup_option(self, timestamp: datetime, contract: Contract) -> Optional[OptionCandle]:
        if callable(self.option_lookup):
            return self.option_lookup(timestamp, contract)
        return self.option_lookup.get((timestamp, contract.key)) or self.option_lookup.get((timestamp, contract.symbol))

    def _log(self, timestamp: datetime, event: str, **payload) -> None:
        self.result.events.append({"timestamp": timestamp.isoformat(), "event": event, **payload})

    def _target(self) -> Optional[float]:
        if not self.result.entries:
            return None
        total_qty = sum(entry.quantity for entry in self.result.entries)
        avg = sum(entry.spot * entry.quantity for entry in self.result.entries) / total_qty
        if self._side == "CE":
            return avg + self.config.target_fraction * (self.config.mother_high - avg)
        return avg - self.config.target_fraction * (avg - self.config.mother_low)

    def _average_spot(self) -> float:
        total_qty = sum(entry.quantity for entry in self.result.entries)
        return sum(entry.spot * entry.quantity for entry in self.result.entries) / total_qty

    def _beyond(self, price: float, level: Optional[float]) -> bool:
        """Is `price` past `level` in the direction this side needs?"""
        if level is None:
            return False
        return price < level if self._side == "CE" else price > level

    def _baseline_close(self) -> Optional[float]:
        """The close the current candle must beat, per the rule-4 reading.

        This is also the stop trigger: rule 3 places the order at the close of
        the previous candle, so whichever close is used as the comparison is
        the one the order sits on.
        """
        if self.config.arm_compare == "previous_candle":
            return self._previous_close
        return self._last_same_colour_close

    def _arm_if_qualified(self, candle: Candle) -> None:
        qualifies = candle.is_red if self._side == "CE" else candle.is_green
        if not qualifies:
            return
        beyond_reference = self._beyond(candle.close, self._reference)
        baseline = self._baseline_close()

        if self._leg_seen and beyond_reference and self._beyond(candle.close, baseline):
            self._pending_trigger = baseline
            self._pending_since = candle.timestamp
            self._pending_contract = self.resolver.select(candle.timestamp, candle.close, self._side, self.config)
            self.result.status = "armed"
            self._log(
                candle.timestamp,
                "arm",
                stage=self._stage + 1,
                trigger=self._pending_trigger,
                strike=self._pending_contract.strike,
                expiry=self._pending_contract.expiry.isoformat(),
            )
        elif beyond_reference or not self.config.first_leg_beyond_reference:
            # Not the arming candle, but it counts as the first of the two.
            self._leg_seen = True

        self._last_same_colour_close = candle.close

    def _walk_pending_stop(self, candle: Candle) -> bool:
        """Move an existing stop down after a newly closed qualifying candle.

        Whether this runs before or after the fill test is decided by
        `fill_before_stop_walk`; see that field for why the order matters.
        The revised order is only eligible from the next candle onward, since
        it is placed on this candle's close.
        """
        if self._pending_trigger is None or not (candle.is_red if self._side == "CE" else candle.is_green):
            return False
        baseline = self._baseline_close()
        if not self._beyond(candle.close, self._reference) or not self._beyond(candle.close, baseline):
            return False
        self._pending_trigger = baseline
        self._pending_since = candle.timestamp
        self._last_same_colour_close = candle.close
        if self.config.restrike_on_stop_walk:
            # The order is cancelled and re-placed, so the strike is re-picked
            # at the level the market is at now rather than left at the one
            # chosen when the stop first armed, possibly hundreds of points ago.
            self._pending_contract = self.resolver.select(candle.timestamp, candle.close, self._side, self.config)
        self._log(
            candle.timestamp,
            "move_stop",
            trigger=self._pending_trigger,
            stage=self._stage + 1,
            strike=self._pending_contract.strike if self._pending_contract else None,
        )
        return True

    def _try_fill(self, candle: Candle) -> bool:
        if self._pending_trigger is None or self._pending_contract is None or candle.timestamp <= self._pending_since:
            return False
        crossed = candle.high >= self._pending_trigger if self._side == "CE" else candle.low <= self._pending_trigger
        if not crossed:
            return False
        option_bar = self._lookup_option(candle.timestamp, self._pending_contract)
        if option_bar is None:
            gap = (
                f"missing option candle at {candle.timestamp.isoformat()} for "
                f"{self._pending_contract.symbol} {self._pending_contract.strike}{self._pending_contract.option_type}"
            )
            self.result.data_gaps.append(gap)
            if self.config.strict_option_data:
                self.result.data_gap = gap
                self.result.status = "data_gap"
                return False
        lots = self.config.lot_schedule[self._stage]
        quantity = lots * self._pending_contract.lot_size
        # A stop fills at the trigger or worse, never better.
        trigger_price = float(self._pending_trigger) + (
            self.config.slippage_points if self._side == "CE" else -self.config.slippage_points
        )
        entry_premium = option_bar.open * (1.0 + self.config.option_slippage_pct) if option_bar is not None else None
        entry = Entry(
            candle.timestamp,
            trigger_price,
            entry_premium,
            lots,
            quantity,
            self._pending_contract,
            self._stage + 1,
        )
        self.result.entries.append(entry)
        self._last_entry_timestamp = candle.timestamp
        self._stage += 1
        marked_low = self._stage_last_low if self.config.mark_low_mode == "latest" else self._stage_extreme
        if marked_low is not None:
            self._reference = marked_low
        self._stage_extreme = None
        self._stage_last_low = None
        self._stage_started_at = candle.timestamp
        if self._stage < len(self._stage_timeframes):
            self._active_timeframe = self._stage_timeframes[self._stage]
        self._last_same_colour_close = None
        self._previous_close = None
        self._leg_seen = False
        self._pending_trigger = None
        self._pending_since = None
        self._pending_contract = None
        self.result.status = "open"
        self._log(
            candle.timestamp,
            "fill",
            stage=entry.stage,
            lots=lots,
            quantity=quantity,
            trigger=trigger_price,
            marked_low=marked_low,
            option_price=entry_premium,
            timeframe=self._stage_timeframes[entry.stage - 1],
        )
        return True

    def _try_exit(self, candle: Candle) -> bool:
        if not self.result.entries or candle.timestamp <= (self._last_entry_timestamp or self.config.mother_timestamp):
            return False
        target = self._target()
        if target is None:
            return False
        hit = candle.high >= target if self._side == "CE" else candle.low <= target
        if not hit:
            return False
        option_prices: list[Optional[float]] = []
        for entry in self.result.entries:
            option_bar = self._lookup_option(candle.timestamp, entry.contract)
            if option_bar is None:
                gap = f"missing exit option candle at {candle.timestamp.isoformat()} for {entry.contract.symbol}"
                self.result.data_gaps.append(gap)
                if self.config.strict_option_data:
                    self.result.data_gap = gap
                    self.result.status = "data_gap"
                    return False
                option_prices.append(None)
                continue
            option_prices.append(option_bar.close * (1.0 - self.config.option_slippage_pct))
        self.result.exit_timestamp = candle.timestamp
        self.result.exit_option_prices = list(option_prices)
        known_prices = [price for price in option_prices if price is not None]
        # Legs sit on different strikes, so a single blended premium is only
        # ever a display value. Realized P&L below is summed leg by leg.
        self.result.exit_option_price = sum(known_prices) / len(known_prices) if known_prices else None
        self.result.target_index = target
        self.result.average_spot = self._average_spot()
        self.result.index_move = target - self.result.average_spot
        self._settle(option_prices)
        self.result.status = "closed"
        self.result.exit_reason = "target"
        self._log(
            candle.timestamp,
            "exit",
            target=target,
            realized_pnl=self.result.realized_pnl,
            costs=self.result.costs_total,
            net_pnl=self.result.net_pnl,
            reason="target",
        )
        return True

    def _settle(self, option_prices: list[Optional[float]]) -> None:
        """Book gross P&L and statutory charges for the closing basket.

        Charges are grouped by contract because two stages that land on the
        same strike and expiry are one position at the broker, while stages on
        different strikes are genuinely separate orders. Charging a cascade as
        one synthetic average-price trade would understate brokerage and GST.
        """
        entries = self.result.entries
        if not entries or any(price is None for price in option_prices):
            return
        if any(entry.option_price is None for entry in entries):
            return

        self.result.realized_pnl = sum(
            (float(exit_price) - float(entry.option_price)) * entry.quantity
            for exit_price, entry in zip(option_prices, entries)
        )

        grouped: dict[str, list[tuple[Entry, float]]] = {}
        for entry, exit_price in zip(entries, option_prices):
            grouped.setdefault(entry.contract.key, []).append((entry, float(exit_price)))

        total = 0.0
        for rows in grouped.values():
            quantity = sum(entry.quantity for entry, _ in rows)
            lots = sum(entry.lots for entry, _ in rows)
            # One exit order per contract, at the quantity-weighted exit price.
            sell_price = sum(price * entry.quantity for entry, price in rows) / quantity
            total += calculate_nifty_option_basket_round_costs(
                buys=[
                    OptionCostFill(price=float(entry.option_price), quantity=entry.quantity, lots=entry.lots)
                    for entry, _ in rows
                ],
                sell_price=sell_price,
                sell_quantity=quantity,
                sell_lots=lots,
                schedule=self.config.cost_schedule,
            ).total
        self.result.costs_total = round(total, 2)
        self.result.net_pnl = round(self.result.realized_pnl - total, 2)

    def _try_expiry_exit(self, candle: Candle) -> bool:
        """Force a strategy exit at the last tradable expiry candle.

        A weekly option cannot be carried beyond expiry.  The engine records the
        forced exit as signal-level only unless exact option candles are present.
        """

        if not self.config.force_exit_on_expiry or not self.result.entries:
            return False
        expiry = min(entry.contract.expiry for entry in self.result.entries)
        if candle.timestamp.date() < expiry:
            return False
        if candle.timestamp.date() == expiry and candle.timestamp.time() < dt_time(15, 15):
            return False
        self.result.exit_timestamp = candle.timestamp
        self.result.average_spot = self._average_spot()
        self.result.target_index = self._target()
        self.result.index_move = (
            self.result.target_index - self.result.average_spot if self.result.target_index is not None else None
        )
        # An option at its own expiry is worth intrinsic value and nothing else,
        # which the index alone settles exactly. That matters: expiry is where
        # this strategy takes its losses, and it is the one exit that stays
        # fully priced even with no premium history at all.
        prices: list[Optional[float]] = []
        for entry in self.result.entries:
            option_bar = self._lookup_option(candle.timestamp, entry.contract)
            if option_bar is not None:
                prices.append(option_bar.close * (1.0 - self.config.option_slippage_pct))
            elif entry.contract.expiry <= candle.timestamp.date():
                strike = entry.contract.strike
                intrinsic = (
                    max(0.0, candle.close - strike)
                    if entry.contract.option_type == "CE"
                    else max(0.0, strike - candle.close)
                )
                prices.append(intrinsic)
            else:
                prices.append(None)
        self.result.exit_option_prices = list(prices)
        self._settle(prices)
        self.result.status = "expired"
        self.result.exit_reason = "expiry_square_off"
        self._log(
            candle.timestamp,
            "expiry_exit",
            expiry=expiry.isoformat(),
            realized_pnl=self.result.realized_pnl,
            net_pnl=self.result.net_pnl,
            reason="expiry_square_off",
        )
        return True

    def _update_stage_extreme(self, candle: Candle) -> None:
        self._stage_last_low = candle.low if self._side == "CE" else candle.high
        if self._stage_extreme is None:
            self._stage_extreme = self._stage_last_low
            return
        self._stage_extreme = (
            min(self._stage_extreme, candle.low) if self._side == "CE" else max(self._stage_extreme, candle.high)
        )

    def on_candle(self, candle: Candle, *, timeframe: Optional[str] = None) -> None:
        event_timeframe = (timeframe or self._active_timeframe).lower()
        if (
            self.result.status in {"closed", "expired", "data_gap", "invalid"}
            or candle.timestamp <= self.config.mother_timestamp
        ):
            return
        # Targets and expiry are monitored on the original (finest) timeframe,
        # even while later entries wait for 4H/1D confirmation.
        if self.result.entries and event_timeframe == self._stage_timeframes[0]:
            if self._try_exit(candle) or self._try_expiry_exit(candle):
                return
        if event_timeframe != self._active_timeframe or candle.timestamp <= self._stage_started_at:
            return
        if self.config.fill_before_stop_walk:
            # The stop is resting in the market for the whole of this bar. If
            # the bar's high reached it, it filled -- the bar's eventual close
            # cannot retract an execution that already happened.
            filled = self._try_fill(candle)
            moved_stop = False if filled else self._walk_pending_stop(candle)
        else:
            # Original behaviour, retained only so the difference is measurable:
            # a qualifying close revises the stop and cancels the same bar's fill.
            moved_stop = self._walk_pending_stop(candle) if self._pending_trigger is not None else False
            filled = False if moved_stop else self._try_fill(candle)
        if self.result.status == "data_gap":
            return
        if filled:
            # The entry candle must not also begin the next cascade stage.
            return
        if self._stage >= len(self.config.lot_schedule):
            return
        # The entry candle is excluded from the preceding marked extreme. The
        # next stage starts with it as a baseline and then waits below/above it.
        if self._pending_trigger is None:
            self._arm_if_qualified(candle)
        self._update_stage_extreme(candle)
        self._previous_close = candle.close

    def run(self, candles: Iterable[Candle] | Mapping[str, Iterable[Candle]]) -> CascadeResult:
        if isinstance(candles, Mapping):
            # Bars are stamped at their OPEN but only become knowable at their
            # CLOSE. Merging several timeframes on open time would feed a 1H bar
            # to the engine an hour before it finished, ahead of the 5m bars
            # inside it. Ordering on close time is what keeps the replay honest.
            # Ties break toward the finer timeframe, which closed first in fact.
            priority = {timeframe: index for index, timeframe in enumerate(self._stage_timeframes)}
            rows: list[tuple[datetime, str, Candle]] = []
            for timeframe, series in candles.items():
                name = str(timeframe).lower()
                sorted_rows = sorted(series, key=lambda candle: candle.timestamp)
                for candle, close_time in zip(sorted_rows, _close_times(sorted_rows, name)):
                    rows.append((close_time, name, candle))
            ordered = sorted(rows, key=lambda item: (item[0], priority.get(item[1], len(priority))))
        else:
            ordered = [
                (candle.timestamp, self._stage_timeframes[0], candle)
                for candle in sorted(candles, key=lambda c: c.timestamp)
            ]
        for _close_time, timeframe, candle in ordered:
            self.on_candle(candle, timeframe=timeframe)
            if self.result.status in {"closed", "expired", "data_gap", "invalid"}:
                break
        if self.result.entries:
            self.result.average_spot = self._average_spot()
            self.result.target_index = self._target()
        return self.result


# ---------------------------------------------------------------------------
# Phase 1: NIFTY 5m index geometry port
# ---------------------------------------------------------------------------
#
# The legacy OneHourCascade above is intentionally retained for the existing
# manual replay page.  The types below are separate on purpose: this is the
# CryptoForge Cascade geometry (mother -> trendline -> fib), calculated solely
# from NIFTY index candles.  Phase 2 will attach paper fills and round booking;
# this phase must not submit a broker order.

IST = ZoneInfo("Asia/Kolkata")
GEOMETRY_ANCHOR_CLOSE_TOLERANCE_PCT = 0.00045
GEOMETRY_MIN_FIB_RANGE_PCT = 0.001
GEOMETRY_DECISIVE_BREAK_PCT = 0.0002
GEOMETRY_MIN_LEG_SEPARATION_PCT = 0.0003
GEOMETRY_MIN_TRENDLINE_SEPARATION_PCT = 0.0015
GEOMETRY_MOTHER_RETEST_PCT = 0.0015
GEOMETRY_MOTHER_DEPART_PCT = 0.005
GEOMETRY_FIB_LEVELS = (2, 4, 8)


class PaperOnlyViolation(RuntimeError):
    """Raised if code tries to disable the Phase-1 no-live-order lock."""


class OptionsAdapterError(CascadeError):
    """Dhan data or ScripMaster data was not sufficient for a safe campaign."""


@dataclass(frozen=True)
class IndexCandle:
    """A closed NIFTY index candle used for all Cascade geometry."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def is_red(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class IndexTrendline:
    trendline_id: int
    anchor1_price: float
    anchor1_timestamp: datetime
    anchor2_price: float
    anchor2_timestamp: datetime
    bears_fib: bool = True


@dataclass(frozen=True)
class IndexFibLadder:
    high_anchor: float
    low_anchor: float

    def level_price(self, level: float) -> float:
        return self.high_anchor - level * (self.high_anchor - self.low_anchor)


@dataclass(frozen=True)
class IndexLeg:
    leg_id: int
    trendline_id: int
    low: float
    touch_high: float
    touch_timestamp: datetime
    fib: IndexFibLadder


@dataclass
class IndexGeometryCampaign:
    mother_timestamp: datetime
    mother_high: float
    mother_low: float
    state: str = "WAITING_FIRST_DEPTH"
    left_mother_range: bool = False
    window_start_timestamp: Optional[datetime] = None
    trendlines: list[IndexTrendline] = field(default_factory=list)
    legs: list[IndexLeg] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def index_trendline_price(trendline: IndexTrendline, at_timestamp: datetime) -> float:
    seconds = (trendline.anchor2_timestamp - trendline.anchor1_timestamp).total_seconds()
    if seconds == 0:
        return trendline.anchor1_price
    slope = (trendline.anchor2_price - trendline.anchor1_price) / seconds
    return trendline.anchor1_price + slope * (at_timestamp - trendline.anchor1_timestamp).total_seconds()


def find_index_valid_anchor2(
    anchor1_price: float,
    anchor1_timestamp: datetime,
    candles_between: Iterable[IndexCandle],
    *,
    epsilon: float = 1e-9,
) -> tuple[Optional[float], Optional[datetime]]:
    """Byte-for-byte rule equivalent to CryptoForge's anchor search.

    It searches backward through red candle opens and accepts the tightest
    descending line that earlier *closes* did not cross (with the same 0.045%
    tolerance).  Wick-only crossings are intentionally allowed.
    """

    candles = list(candles_between)
    for candidate in reversed([c for c in candles if c.is_red]):
        if candidate.timestamp == anchor1_timestamp:
            continue
        elapsed = (candidate.timestamp - anchor1_timestamp).total_seconds()
        if not elapsed:
            continue
        slope = (candidate.open - anchor1_price) / elapsed
        violated = False
        for candle in candles:
            if candle.timestamp < candidate.timestamp:
                line_price = anchor1_price + slope * (candle.timestamp - anchor1_timestamp).total_seconds()
                allowance = abs(line_price) * GEOMETRY_ANCHOR_CLOSE_TOLERANCE_PCT
                if candle.close > line_price + allowance + epsilon:
                    violated = True
                    break
        if not violated:
            return candidate.open, candidate.timestamp
    return None, None


def index_ladders_overlap(a_high: float, a_low: float, b_high: float, b_low: float) -> bool:
    """The same overlap test used to suppress duplicate Cascade shelves."""

    deepest, shallowest = max(GEOMETRY_FIB_LEVELS), min(GEOMETRY_FIB_LEVELS)
    a_range, b_range = a_high - a_low, b_high - b_low
    if a_range <= 0 or b_range <= 0:
        return True
    a_floor, a_ceiling = a_high - deepest * a_range, a_high - shallowest * a_range
    b_floor, b_ceiling = b_high - deepest * b_range, b_high - shallowest * b_range
    return a_ceiling >= b_floor and b_ceiling >= a_floor


class NiftyIndexCascadeGeometry:
    """Paper-safe port of CryptoForge's 5m trendline/fib state machine.

    No premium, strike, order, or broker state is allowed into this class.  A
    generated leg is therefore an auditable index-space fact, not an execution
    decision.  Existing legs are never rewritten when a later leg is added.
    """

    def __init__(self, mother: IndexCandle) -> None:
        self.campaign = IndexGeometryCampaign(
            mother_timestamp=mother.timestamp,
            mother_high=float(mother.high),
            mother_low=float(mother.low),
            window_start_timestamp=mother.timestamp,
        )
        self._history: list[IndexCandle] = [mother]

    @property
    def history(self) -> tuple[IndexCandle, ...]:
        return tuple(self._history)

    def feed(self, candles: Iterable[IndexCandle]) -> IndexGeometryCampaign:
        for candle in sorted(candles, key=lambda row: row.timestamp):
            self.on_candle(candle)
        return self.campaign

    def on_candle(self, candle: IndexCandle) -> None:
        if candle.timestamp <= self.campaign.mother_timestamp or self.campaign.state in {
            "MOTHER_BROKEN",
            "MOTHER_RETESTED",
        }:
            return
        self._history.append(candle)
        if candle.high > self.campaign.mother_high:
            self.campaign.state = "MOTHER_BROKEN"
            self._log(candle, "mother_broken")
            return
        if candle.low <= self.campaign.mother_high * (1 - GEOMETRY_MOTHER_DEPART_PCT):
            self.campaign.left_mother_range = True
        if self.campaign.left_mother_range and candle.high >= self.campaign.mother_high * (
            1 - GEOMETRY_MOTHER_RETEST_PCT
        ):
            self.campaign.state = "MOTHER_RETESTED"
            self._log(candle, "mother_retested")
            return
        if candle.is_red:
            self._evaluate_cut(candle)

    def _log(self, candle: IndexCandle, event: str, **data: Any) -> None:
        self.campaign.events.append({"timestamp": candle.timestamp.isoformat(), "event": event, **data})

    def _window(self, candle: IndexCandle) -> list[IndexCandle]:
        start = self.campaign.window_start_timestamp or self.campaign.mother_timestamp
        return [
            row
            for row in self._history
            if start <= row.timestamp and self.campaign.mother_timestamp < row.timestamp <= candle.timestamp
        ]

    def _duplicate_trendline(self, candidate: IndexTrendline, at_timestamp: datetime) -> Optional[IndexTrendline]:
        mine = index_trendline_price(candidate, at_timestamp)
        if mine <= 0:
            return None
        for trendline in self.campaign.trendlines:
            existing = index_trendline_price(trendline, at_timestamp)
            if existing > 0 and abs(mine - existing) / existing < GEOMETRY_MIN_TRENDLINE_SEPARATION_PCT:
                return trendline
        return None

    def _evaluate_cut(self, candle: IndexCandle) -> None:
        window = self._window(candle)
        if len(window) < 2:
            return
        between = [row for row in self._history if self.campaign.mother_timestamp < row.timestamp < candle.timestamp]
        anchor_price, anchor_timestamp = find_index_valid_anchor2(
            self.campaign.mother_high, self.campaign.mother_timestamp, between
        )
        if anchor_price is None or anchor_timestamp is None or anchor_price >= self.campaign.mother_high:
            return
        candidate = IndexTrendline(
            trendline_id=len(self.campaign.trendlines) + 1,
            anchor1_price=self.campaign.mother_high,
            anchor1_timestamp=self.campaign.mother_timestamp,
            anchor2_price=anchor_price,
            anchor2_timestamp=anchor_timestamp,
        )
        running_low: Optional[float] = None
        running_low_timestamp: Optional[datetime] = None
        frozen_low: Optional[float] = None
        first_cross_timestamp: Optional[datetime] = None
        touch_high: Optional[float] = None
        touch_timestamp: Optional[datetime] = None
        for row in window:
            line = index_trendline_price(candidate, row.timestamp)
            crossed = row.high >= line and row.close < line and row.high < self.campaign.mother_high
            if crossed and running_low_timestamp is not None and row.timestamp >= running_low_timestamp:
                if first_cross_timestamp is None:
                    first_cross_timestamp = row.timestamp
                    frozen_low = running_low
                if touch_high is None or row.high > touch_high:
                    touch_high, touch_timestamp = row.high, row.timestamp
            if first_cross_timestamp is None and (running_low is None or row.low < running_low):
                running_low, running_low_timestamp = row.low, row.timestamp
        if first_cross_timestamp is None or frozen_low is None or touch_high is None:
            return
        if touch_high - frozen_low < touch_high * GEOMETRY_MIN_FIB_RANGE_PCT:
            return
        if candle.close >= frozen_low or frozen_low - candle.close < candle.close * GEOMETRY_DECISIVE_BREAK_PCT:
            return
        prior_leg = self.campaign.legs[-1] if self.campaign.legs else None
        if prior_leg is not None and candle.close >= prior_leg.low:
            return
        duplicate_shelf: Optional[IndexLeg] = None
        separation = 0.0
        for leg in self.campaign.legs:
            if not index_ladders_overlap(touch_high, frozen_low, leg.touch_high, leg.low):
                continue
            gap = abs(touch_high - leg.touch_high) / leg.touch_high
            if duplicate_shelf is None or gap < separation:
                duplicate_shelf, separation = leg, gap
        if duplicate_shelf is not None and separation < GEOMETRY_MIN_LEG_SEPARATION_PCT:
            display = [
                row for row in self._history if self.campaign.mother_timestamp < row.timestamp <= candle.timestamp
            ]
            display_price, display_timestamp = find_index_valid_anchor2(
                self.campaign.mother_high, self.campaign.mother_timestamp, display
            )
            if display_price is None or display_timestamp is None or display_price >= self.campaign.mother_high:
                display_price, display_timestamp = anchor_price, anchor_timestamp
            ghost = IndexTrendline(
                trendline_id=len(self.campaign.trendlines) + 1,
                anchor1_price=self.campaign.mother_high,
                anchor1_timestamp=self.campaign.mother_timestamp,
                anchor2_price=display_price,
                anchor2_timestamp=display_timestamp,
                bears_fib=False,
            )
            if self._duplicate_trendline(ghost, candle.timestamp) is None:
                self.campaign.trendlines.append(ghost)
            self.campaign.window_start_timestamp = candle.timestamp
            self._log(candle, "same_shelf", existing_leg=duplicate_shelf.leg_id)
            return
        existing_line = self._duplicate_trendline(candidate, candle.timestamp)
        trendline = existing_line or candidate
        if existing_line is None:
            self.campaign.trendlines.append(trendline)
        self.campaign.state = "TRENDLINE_ACTIVE"
        leg = IndexLeg(
            leg_id=len(self.campaign.legs) + 1,
            trendline_id=trendline.trendline_id,
            low=frozen_low,
            touch_high=touch_high,
            touch_timestamp=touch_timestamp or candle.timestamp,
            fib=IndexFibLadder(touch_high, frozen_low),
        )
        self.campaign.legs.append(leg)
        self.campaign.window_start_timestamp = candle.timestamp
        self._log(
            candle,
            "leg",
            leg_id=leg.leg_id,
            trendline_id=leg.trendline_id,
            fib_high=leg.touch_high,
            fib_low=leg.low,
        )


@dataclass(frozen=True)
class FixedCampaignOption:
    """The one CE contract fixed at a campaign's mother candle."""

    underlying: str
    strike: int
    expiry: date
    option_type: str
    lot_size: int
    security_id: str


@dataclass(frozen=True)
class PaperOptionOrder:
    order_id: str
    contract: FixedCampaignOption
    side: str
    quantity: int
    product_type: str
    status: str = "PAPER"


def _as_ist(value: datetime) -> datetime:
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def is_nse_cash_session(timestamp: datetime) -> bool:
    """Normal NSE trading-session guard; exchange holidays are broker-owned."""

    timestamp = _as_ist(timestamp)
    return timestamp.weekday() < 5 and dt_time(9, 15) <= timestamp.time() < dt_time(15, 30)


def option_expiry_squareoff_due(timestamp: datetime, expiry: date) -> bool:
    timestamp = _as_ist(timestamp)
    return timestamp.date() > expiry or (timestamp.date() == expiry and timestamp.time() >= dt_time(15, 0))


class CascadeOptionsAdapter:
    """Read-only Dhan bridge with a hard paper-order boundary for Phase 1.

    The adapter intentionally owns all Dhan knowledge.  Its `place_order`
    method creates an in-memory `PaperOptionOrder`; it never calls
    `DhanClient.place_option_order`.  Passing `paper_only=False` is rejected
    so Phase 1 cannot become live through configuration drift.
    """

    NIFTY_SECURITY_ID = "13"
    POSITIONAL_PRODUCT_TYPE = "CARRYFORWARD"

    def __init__(self, dhan: Any, *, scrip_master: Any = None, paper_only: bool = True) -> None:
        if not paper_only:
            raise PaperOnlyViolation("NIFTY options Cascade Phase 1 is hard-locked to paper-only mode")
        self.dhan = dhan
        if scrip_master is None:
            from broker.dhan import ScripMaster  # lazy: pure geometry remains testable without Dhan

            scrip_master = ScripMaster
        self.scrip_master = scrip_master
        self.paper_only = True
        self._paper_orders: dict[str, PaperOptionOrder] = {}
        self._paper_order_sequence = 0

    @staticmethod
    def _normalise_index_frame(
        frame: Any, now: Optional[datetime] = None, *, timeframe_minutes: int = 5
    ) -> list[IndexCandle]:
        if frame is None or getattr(frame, "empty", False):
            return []
        now_ist = _as_ist(now or datetime.now(IST))
        closed: list[IndexCandle] = []
        for timestamp, row in frame.iterrows():
            candle_time = timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
            candle_time = _as_ist(candle_time)
            # The Dhan historical endpoint can include the candle currently
            # forming.  Geometry is only ever allowed to see closed candles.
            # The final NSE "1H" bar is 15:15–15:30, rather than a full hour.
            bar_minutes = 15 if timeframe_minutes == 60 and candle_time.time() == dt_time(15, 15) else timeframe_minutes
            if candle_time + timedelta(minutes=bar_minutes) > now_ist:
                continue
            closed.append(
                IndexCandle(
                    candle_time,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                )
            )
        return sorted(closed, key=lambda candle: candle.timestamp)

    async def async_get_candles(
        self,
        symbol: str,
        timeframe: str = "5m",
        *,
        from_date: Optional[date | str] = None,
        to_date: Optional[date | str] = None,
        now: Optional[datetime] = None,
    ) -> list[IndexCandle]:
        normalised_tf = str(timeframe).lower()
        # Dhan intraday intervals the fib-boundary cascade can replay.  5m/1h are
        # the originals (the live Cascade tab uses 5m); 1m and 15m were added for
        # the fib-boundary timeframes and map to Dhan's native intervals.
        tf_interval = {"1m": ("1", 1), "5m": ("5", 5), "15m": ("15", 15), "1h": ("60", 60)}
        if str(symbol).upper() != "NIFTY" or normalised_tf not in tf_interval:
            raise OptionsAdapterError("Paper options campaigns support closed NIFTY 1m, 5m, 15m or 1H index candles")
        interval, tf_minutes = tf_interval[normalised_tf]
        current = _as_ist(now or datetime.now(IST))
        start = from_date or current.date()
        end = to_date or current.date()
        start_text = start.isoformat() if isinstance(start, date) else str(start)
        end_text = end.isoformat() if isinstance(end, date) else str(end)
        frame = await asyncio.to_thread(self.dhan.get_nifty_intraday, start_text, end_text, interval=interval)
        return self._normalise_index_frame(frame, current, timeframe_minutes=tf_minutes)

    def get_ticker(self, symbol: str = "NIFTY") -> dict[str, float | str]:
        if str(symbol).upper() != "NIFTY":
            raise OptionsAdapterError("Phase 1 only supports NIFTY")
        payload = self.dhan.get_ohlc_multi({"IDX_I": [self.NIFTY_SECURITY_ID]})
        index_data = (payload or {}).get("IDX_I", {})
        row = index_data.get(str(self.NIFTY_SECURITY_ID), index_data.get(int(self.NIFTY_SECURITY_ID), {}))
        if not isinstance(row, dict):
            raise OptionsAdapterError("Dhan did not return a NIFTY IDX_I quote")
        price = row.get("last_price", row.get("ltp"))
        if price is None:
            raise OptionsAdapterError("Dhan NIFTY quote has no last_price")
        return {"symbol": "NIFTY", "last_price": float(price), "mark_price": float(price)}

    def select_campaign_contract(
        self,
        *,
        mother_spot: float,
        selected_at: datetime,
        ce_offset_steps: int = -2,
        strike_step: int = 50,
        option_type: str = "CE",
    ) -> FixedCampaignOption:
        side = str(option_type).upper()
        if side not in {"CE", "PE"}:
            raise OptionsAdapterError("option_type must be CE or PE")
        if strike_step <= 0:
            raise OptionsAdapterError("strike_step must be positive")
        selected_at = _as_ist(selected_at)
        expiries = [date.fromisoformat(str(value)[:10]) for value in self.scrip_master.get_expiries("NIFTY")]
        expiry = self._next_weekly_expiry(expiries, selected_at.date())
        atm = int(math.floor(float(mother_spot) / strike_step + 0.5) * strike_step)
        # ce_offset_steps is signed toward ITM for the side: a CE goes ITM by
        # dropping strikes (negative), a PE goes ITM by raising them (positive).
        # The caller mirrors the sign per side; the maths here stays one line.
        strike = atm + int(ce_offset_steps) * strike_step
        lot_size = int(self.scrip_master.get_lot_size("NIFTY", expiry.isoformat()))
        security_id = str(self.scrip_master.lookup("NIFTY", strike, expiry.isoformat(), side) or "")
        if not security_id:
            raise OptionsAdapterError(f"ScripMaster has no NIFTY {strike}{side} for {expiry.isoformat()}")
        return FixedCampaignOption("NIFTY", strike, expiry, side, lot_size, security_id)

    @staticmethod
    def _next_weekly_expiry(expiries: Iterable[date], trade_date: date) -> date:
        eligible: list[date] = []
        for expiry in sorted(set(expiries)):
            dte = (expiry - trade_date).days
            # Normal weekly expiry is Tuesday: Monday is 8 DTE and Tuesday is
            # 7 DTE.  A holiday-shifted Monday expiry is permitted at 6 DTE.
            min_dte = 7 if expiry.weekday() == 1 else 6
            if dte >= min_dte:
                eligible.append(expiry)
        if not eligible:
            raise OptionsAdapterError("No next-weekly NIFTY expiry is available in ScripMaster")
        return eligible[0]

    def dte_allows_new_rungs(self, contract: FixedCampaignOption, at: datetime) -> bool:
        return (contract.expiry - _as_ist(at).date()).days > 1

    def expiry_squareoff_due(self, contract: FixedCampaignOption, at: datetime) -> bool:
        return option_expiry_squareoff_due(at, contract.expiry)

    def place_order(
        self,
        contract: FixedCampaignOption,
        *,
        side: str,
        quantity: int,
        product_type: str = POSITIONAL_PRODUCT_TYPE,
    ) -> PaperOptionOrder:
        """Record, never transmit, a paper CE order.

        The positional product is deliberately part of the paper contract so
        Phase 4 can assert CARRYFORWARD/NRML before it wires any live submit.
        """

        if not self.paper_only:
            raise PaperOnlyViolation("Live order submission is not implemented in Phase 1")
        if contract.option_type not in {"CE", "PE"} or str(side).upper() not in {"BUY", "SELL"}:
            raise OptionsAdapterError("Paper adapter accepts only CE/PE BUY/SELL orders")
        if quantity <= 0 or quantity % contract.lot_size:
            raise OptionsAdapterError("Option quantity must be an exact whole number of ScripMaster lots")
        self._paper_order_sequence += 1
        order = PaperOptionOrder(
            order_id=f"paper-nifty-cascade-{self._paper_order_sequence}",
            contract=contract,
            side=str(side).upper(),
            quantity=int(quantity),
            product_type=str(product_type).upper(),
        )
        self._paper_orders[order.order_id] = order
        return order

    def cancel_order(self, order_id: str) -> dict[str, str]:
        if str(order_id) in self._paper_orders:
            return {"orderId": str(order_id), "status": "CANCELLED"}
        return {"orderId": str(order_id), "status": "NOT_FOUND"}

    def get_orders(self) -> list[PaperOptionOrder]:
        return list(self._paper_orders.values())

    def get_order(self, order_id: str) -> Optional[PaperOptionOrder]:
        return self._paper_orders.get(str(order_id))

    def get_wallet(self) -> dict[str, Any]:
        """Read-only funds mapping retained for the eventual engine interface."""

        return self.dhan.get_funds()


# ---------------------------------------------------------------------------
# Phase 2: paper fills, net rounds, and new-low reuse
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperCascadeConfig:
    """Execution settings for a paper-only NIFTY options campaign.

    `rung_inr` is the cash assigned to each crossed index fib marker.  The
    engine converts the collected cash to whole lots at the observed option
    premium.  A one-lot minimum is intentional: a funded rung is an actual
    tradable options decision, never fractional dust.
    """

    rung_inr: float
    target_fraction: float = 0.25
    ce_offset_steps: int = -2
    cost_schedule: NiftyOptionCostSchedule = field(default_factory=NiftyOptionCostSchedule)
    # Fib-boundary sizing: ignore the rupee budget and buy a fixed lot ladder --
    # 1 lot on the first fill, 2 on the second, 3 on the third...  The buy count
    # is the sizing (no percent, no fund allocation).  When False (the default),
    # the classic cascade converts rung_inr cash to whole lots at the premium.
    lot_ladder: bool = False
    # Fib-boundary strike: re-select ATM-N against the index at EACH fill (a
    # deeper CE buys a lower strike as NIFTY falls) instead of one fixed contract
    # for the whole campaign.  Needs a contract_selector on the engine.
    per_entry_strike: bool = False
    # Single-shot: take only the FIRST entry for a mother -- no deeper-level buys
    # and no new round after it closes.  The campaign is one trade: the first
    # deep boundary fills, rides to its target or expiry, and the mother is done.
    # When False (the default), the full cascade arms every deeper level and
    # re-arms a fresh round on each new low.
    single_shot: bool = False
    # One-and-done: cap the number of rounds per mother.  UNLIKE single_shot this
    # keeps the deeper-level averaging WITHIN a round (L2->L4->L8) -- it only stops
    # the new-low re-arm once this many rounds have closed.  max_rounds=1 is the
    # original locked spec ("one target -> close all -> END; a fresh campaign
    # needs a fresh mother").  None (the default) = unlimited re-arms.
    max_rounds: Optional[int] = None
    # Per-round premium ceiling in rupees: stop taking DEEPER legs once the cash
    # actually deployed in option premium this round would breach the cap.  The
    # first entry always fills (the cap limits averaging depth, never the trade
    # itself).  None (the default) = no ceiling.
    max_round_premium_inr: Optional[float] = None

    def __post_init__(self) -> None:
        if self.rung_inr <= 0:
            raise CascadeError("rung_inr must be positive")
        if not 0 < self.target_fraction <= 1:
            raise CascadeError("target_fraction must be between 0 and 1")
        if self.ce_offset_steps > 0:
            raise CascadeError("CE offset must be ATM or below; positive steps are not valid in Phase 2")
        if self.max_rounds is not None and self.max_rounds < 1:
            raise CascadeError("max_rounds must be at least 1 when set")
        if self.max_round_premium_inr is not None and self.max_round_premium_inr <= 0:
            raise CascadeError("max_round_premium_inr must be positive when set")


@dataclass
class PaperCascadeRung:
    leg_id: int
    level: int
    index_price: float
    budget_inr: float
    status: str = "PENDING"  # PENDING | COLLECTED | FILLED | CLOSED | CANCELLED

    @property
    def key(self) -> str:
        return f"{self.leg_id}:{self.level}"


@dataclass(frozen=True)
class PaperCascadeFill:
    timestamp: datetime
    index_price: float
    option_premium: float
    lots: int
    quantity: int
    rung_keys: tuple[str, ...]
    order_id: str
    # Per-entry-strike mode records the exact contract this leg bought; None
    # means the campaign's single fixed contract (the classic cascade).
    contract: Optional["FixedCampaignOption"] = None

    @property
    def turnover(self) -> float:
        return self.option_premium * self.quantity


@dataclass(frozen=True)
class PaperCascadeRound:
    round_id: int
    opened_at: datetime
    closed_at: datetime
    fills: tuple[PaperCascadeFill, ...]
    target_index: float
    exit_index_price: float
    exit_option_premium: float
    exit_quantity: int
    gross_pnl: float
    costs: OptionRoundCosts
    net_pnl: float
    exit_reason: str


OptionPremiumLookup = Callable[[datetime, FixedCampaignOption], Optional[float]]


class NiftyOptionsPaperCascade:
    """Paper execution of the fixed-CE Cascade against closed NIFTY candles.

    Geometry is delegated unchanged to :class:`NiftyIndexCascadeGeometry`.
    This class merely translates its fib markers into a paper CE basket, so it
    cannot alter a trendline or calculate a signal from option premium data.
    """

    def __init__(
        self,
        mother: IndexCandle,
        contract: FixedCampaignOption,
        adapter: CascadeOptionsAdapter,
        option_premium_lookup: OptionPremiumLookup,
        config: PaperCascadeConfig,
        contract_selector: Optional[Callable[[datetime, float], FixedCampaignOption]] = None,
    ) -> None:
        if not adapter.paper_only:
            raise PaperOnlyViolation("Phase 2 paper cascade requires a paper-only adapter")
        if contract.option_type != "CE":
            raise CascadeError("Phase 2 is locked to CE-only campaigns")
        if config.per_entry_strike and contract_selector is None:
            raise CascadeError("per_entry_strike needs a contract_selector")
        self.geometry = NiftyIndexCascadeGeometry(mother)
        self.contract = contract
        self.adapter = adapter
        self.option_premium_lookup = option_premium_lookup
        self.config = config
        # Called (timestamp, index_price) -> contract when per_entry_strike is on.
        self.contract_selector = contract_selector
        self.rungs: dict[str, PaperCascadeRung] = {}
        self.open_fills: list[PaperCascadeFill] = []
        self.rounds: list[PaperCascadeRound] = []
        self.pending_rung_keys: list[str] = []
        self.pending_inr = 0.0
        self.pending_line: Optional[float] = None
        self.pending_last_red: Optional[float] = None
        self.pending_stop: Optional[float] = None
        self.pending_stop_timestamp: Optional[datetime] = None
        self.reuse_below: Optional[float] = None
        self.events: list[dict[str, Any]] = []
        self.status = "WAITING"

    @property
    def average_index_entry(self) -> Optional[float]:
        quantity = sum(fill.quantity for fill in self.open_fills)
        if quantity <= 0:
            return None
        return sum(fill.index_price * fill.quantity for fill in self.open_fills) / quantity

    @property
    def target_index(self) -> Optional[float]:
        average = self.average_index_entry
        if average is None:
            return None
        return average + self.config.target_fraction * (self.geometry.campaign.mother_high - average)

    @property
    def open_quantity(self) -> int:
        return sum(fill.quantity for fill in self.open_fills)

    def _deployed_premium(self) -> float:
        """Rupees of option premium spent on the open legs of the current round."""
        return sum(fill.option_premium * fill.quantity for fill in self.open_fills if fill.option_premium is not None)

    def _log(self, candle: IndexCandle, event: str, **payload: Any) -> None:
        self.events.append({"timestamp": candle.timestamp.isoformat(), "event": event, **payload})

    def _premium(self, candle: IndexCandle, contract: Optional[FixedCampaignOption] = None) -> Optional[float]:
        value = self.option_premium_lookup(candle.timestamp, contract or self.contract)
        if value is None:
            return None
        try:
            premium = float(value)
        except (TypeError, ValueError):
            return None
        return premium if premium > 0 else None

    def _sync_new_rungs(self, candle: IndexCandle) -> None:
        for leg in self.geometry.campaign.legs:
            for level in GEOMETRY_FIB_LEVELS:
                rung = PaperCascadeRung(
                    leg_id=leg.leg_id,
                    level=level,
                    index_price=leg.fib.level_price(level),
                    budget_inr=self.config.rung_inr,
                )
                self.rungs.setdefault(rung.key, rung)
                if rung.key in self.rungs and self.rungs[rung.key] is rung:
                    self._log(candle, "rung_created", rung=rung.key, index_price=rung.index_price)

    def _release_closed_rungs(self, candle: IndexCandle) -> None:
        # Single-shot: the mother is done after its one round -- never re-arm.
        if self.config.single_shot:
            return
        # One-and-done / capped rounds: stop re-arming once max_rounds have closed.
        # The deeper-level averaging within a round is untouched -- only the
        # new-low restart is suppressed.
        if self.config.max_rounds is not None and len(self.rounds) >= self.config.max_rounds:
            return
        if self.reuse_below is None or candle.low >= self.reuse_below:
            return
        released = [rung for rung in self.rungs.values() if rung.status == "CLOSED"]
        self.reuse_below = None
        for rung in released:
            rung.status = "PENDING"
        if released:
            self._log(candle, "new_low_restart", rungs=[rung.key for rung in released], low=candle.low)

    def _collect_crossed_rungs(self, candle: IndexCandle) -> None:
        # Single-shot: the mother buys once, at the first boundary only.  Once a
        # rung is collected (pending), filled (open), or booked (rounds), take no
        # deeper boundary -- so a single fill can never bundle L2+L4.
        if self.config.single_shot and (self.pending_rung_keys or self.open_fills or self.rounds):
            return
        # Capped rounds: once max_rounds have BOOKED, take no further boundary --
        # otherwise a deep rung that round 1 never reached would collect after it
        # closes and open a fresh round, bypassing the re-arm gate.  While the
        # capped round is still open (not yet booked) deeper averaging is allowed.
        if self.config.max_rounds is not None and len(self.rounds) >= self.config.max_rounds:
            return
        if not self.adapter.dte_allows_new_rungs(self.contract, candle.timestamp):
            return
        crossed = [rung for rung in self.rungs.values() if rung.status == "PENDING" and candle.low <= rung.index_price]
        for rung in sorted(crossed, key=lambda row: -row.index_price):
            rung.status = "COLLECTED"
            self.pending_rung_keys.append(rung.key)
            self.pending_inr = round(self.pending_inr + rung.budget_inr, 2)
            if self.pending_line is None:
                self.pending_line = rung.index_price
            self._log(
                candle,
                "rung_collected",
                rung=rung.key,
                index_price=rung.index_price,
                pending_inr=self.pending_inr,
            )

    def _advance_stop(self, candle: IndexCandle) -> None:
        if self.pending_line is None or self.pending_inr <= 0 or not candle.is_red or candle.close >= self.pending_line:
            return
        if self.pending_last_red is None:
            self.pending_last_red = candle.close
            self._log(candle, "await_second_red", line=self.pending_line, close=candle.close)
            return
        if candle.close >= self.pending_last_red:
            return
        first_stop = self.pending_stop is None
        self.pending_stop = self.pending_last_red
        self.pending_stop_timestamp = candle.timestamp
        self.pending_last_red = candle.close
        self.status = "ARMED"
        self._log(candle, "stop_armed" if first_stop else "stop_moved", trigger=self.pending_stop)

    def _fill_pending_stop(self, candle: IndexCandle) -> None:
        if (
            self.pending_stop is None
            or self.pending_stop_timestamp is None
            or candle.timestamp <= self.pending_stop_timestamp
            or candle.high < self.pending_stop
        ):
            return
        # Capped rounds: after max_rounds have booked the campaign is done -- a
        # stop armed during the capped round must not fill into a fresh round.
        # (While the capped round is still open, len(rounds) < max_rounds, so
        # in-round averaging fills normally.)
        if self.config.max_rounds is not None and len(self.rounds) >= self.config.max_rounds:
            return
        # Per-entry strike: re-select ATM-N against the index at this fill, so a
        # deeper CE buys a lower strike.  Otherwise the single campaign contract.
        fill_contract = self.contract
        if self.config.per_entry_strike and self.contract_selector is not None:
            fill_contract = self.contract_selector(candle.timestamp, self.pending_stop)
        premium = self._premium(candle, fill_contract)
        if premium is None:
            self.status = "AWAITING_OPTION_QUOTE"
            self._log(candle, "option_quote_missing", action="buy")
            return
        if self.config.lot_ladder:
            # 1 lot on the first fill of this basket, 2 on the second, 3 next...
            lots = len(self.open_fills) + 1
        else:
            lots = max(1, math.floor(self.pending_inr / (premium * fill_contract.lot_size)))
        quantity = lots * fill_contract.lot_size

        # Per-round premium ceiling: a DEEPER leg (open_fills already non-empty)
        # is skipped if the premium it would deploy pushes the round past the cap.
        # The rung is retired (CANCELLED) so the round does not retry it, and the
        # existing open position simply rides to its target/expiry.
        if (
            self.config.max_round_premium_inr is not None
            and self.open_fills
            and self._deployed_premium() + premium * quantity > self.config.max_round_premium_inr
        ):
            for key in self.pending_rung_keys:
                self.rungs[key].status = "CANCELLED"
            self._log(
                candle,
                "premium_cap_reached",
                deployed=round(self._deployed_premium(), 2),
                cap=self.config.max_round_premium_inr,
                skipped_leg_cost=round(premium * quantity, 2),
            )
            self.pending_rung_keys = []
            self.pending_inr = 0.0
            self.pending_line = None
            self.pending_last_red = None
            self.pending_stop = None
            self.pending_stop_timestamp = None
            self.status = "OPEN"
            return

        paper_order = self.adapter.place_order(fill_contract, side="BUY", quantity=quantity)
        fill = PaperCascadeFill(
            timestamp=candle.timestamp,
            index_price=self.pending_stop,
            option_premium=premium,
            lots=lots,
            quantity=quantity,
            rung_keys=tuple(self.pending_rung_keys),
            order_id=paper_order.order_id,
            contract=fill_contract if self.config.per_entry_strike else None,
        )
        self.open_fills.append(fill)
        for key in self.pending_rung_keys:
            self.rungs[key].status = "FILLED"
        self._log(
            candle,
            "paper_fill",
            index_price=fill.index_price,
            option_premium=fill.option_premium,
            lots=fill.lots,
            quantity=fill.quantity,
            target_index=self.target_index,
        )
        self.pending_rung_keys = []
        self.pending_inr = 0.0
        self.pending_line = None
        self.pending_last_red = None
        self.pending_stop = None
        self.pending_stop_timestamp = None
        self.status = "OPEN"

    def _close_round(self, candle: IndexCandle, *, reason: str, target: float) -> None:
        if not self.open_fills:
            return
        if self.config.per_entry_strike:
            self._close_round_multi_strike(candle, reason=reason, target=target)
            return
        premium = self._premium(candle)
        if premium is None:
            self.status = "AWAITING_OPTION_QUOTE"
            self._log(candle, "option_quote_missing", action="sell", reason=reason)
            return
        quantity = self.open_quantity
        lots = quantity // self.contract.lot_size
        paper_order = self.adapter.place_order(self.contract, side="SELL", quantity=quantity)
        costs = calculate_nifty_option_basket_round_costs(
            buys=[OptionCostFill(fill.option_premium, fill.quantity, fill.lots) for fill in self.open_fills],
            sell_price=premium,
            sell_quantity=quantity,
            sell_lots=lots,
            schedule=self.config.cost_schedule,
        )
        gross_pnl = round(sum((premium - fill.option_premium) * fill.quantity for fill in self.open_fills), 2)
        round_row = PaperCascadeRound(
            round_id=len(self.rounds) + 1,
            opened_at=self.open_fills[0].timestamp,
            closed_at=candle.timestamp,
            fills=tuple(self.open_fills),
            target_index=target,
            exit_index_price=target if reason == "target" else candle.close,
            exit_option_premium=premium,
            exit_quantity=quantity,
            gross_pnl=gross_pnl,
            costs=costs,
            net_pnl=round(gross_pnl - costs.total, 2),
            exit_reason=reason,
        )
        self.rounds.append(round_row)
        for rung in self.rungs.values():
            if rung.status == "FILLED":
                rung.status = "CLOSED"
        self.open_fills = []
        self.reuse_below = min(row.low for row in self.geometry.history)
        self.status = "ROUND_CLOSED"
        self._log(
            candle,
            "round_closed",
            order_id=paper_order.order_id,
            reason=reason,
            gross_pnl=round_row.gross_pnl,
            costs=round_row.costs.total,
            net_pnl=round_row.net_pnl,
            reuse_below=self.reuse_below,
        )

    def _close_round_multi_strike(self, candle: IndexCandle, *, reason: str, target: float) -> None:
        """Close a per-entry-strike basket: each strike sells at its OWN exit
        premium, and costs sum per sell order (brokerage is per order)."""
        groups: dict[str, tuple[FixedCampaignOption, list[PaperCascadeFill]]] = {}
        for fill in self.open_fills:
            contract = fill.contract or self.contract
            group_key = f"{contract.strike}:{contract.expiry.isoformat()}:{contract.option_type}"
            groups.setdefault(group_key, (contract, []))[1].append(fill)
        exit_premiums: dict[str, float] = {}
        for key, (contract, _fills) in groups.items():
            exit_premium = self._premium(candle, contract)
            if exit_premium is None:
                self.status = "AWAITING_OPTION_QUOTE"
                self._log(candle, "option_quote_missing", action="sell", reason=reason)
                return
            exit_premiums[key] = exit_premium
        group_costs: list[OptionRoundCosts] = []
        gross_pnl = 0.0
        total_quantity = 0
        last_order_id = ""
        for key, (contract, fills) in groups.items():
            exit_premium = exit_premiums[key]
            group_quantity = sum(fill.quantity for fill in fills)
            group_lots = group_quantity // contract.lot_size
            order = self.adapter.place_order(contract, side="SELL", quantity=group_quantity)
            last_order_id = order.order_id
            group_costs.append(
                calculate_nifty_option_basket_round_costs(
                    buys=[OptionCostFill(fill.option_premium, fill.quantity, fill.lots) for fill in fills],
                    sell_price=exit_premium,
                    sell_quantity=group_quantity,
                    sell_lots=group_lots,
                    schedule=self.config.cost_schedule,
                )
            )
            gross_pnl += sum((exit_premium - fill.option_premium) * fill.quantity for fill in fills)
            total_quantity += group_quantity
        gross_pnl = round(gross_pnl, 2)
        costs = OptionRoundCosts(
            buy_turnover=round(sum(c.buy_turnover for c in group_costs), 2),
            sell_turnover=round(sum(c.sell_turnover for c in group_costs), 2),
            brokerage=round(sum(c.brokerage for c in group_costs), 2),
            stt=round(sum(c.stt for c in group_costs), 2),
            exchange_transaction=round(sum(c.exchange_transaction for c in group_costs), 2),
            sebi=round(sum(c.sebi for c in group_costs), 2),
            stamp=round(sum(c.stamp for c in group_costs), 2),
            gst=round(sum(c.gst for c in group_costs), 2),
        )
        # A single representative exit premium for the round row (qty-weighted).
        exit_premium = round(
            sum(exit_premiums[key] * sum(f.quantity for f in fills) for key, (_c, fills) in groups.items())
            / total_quantity,
            2,
        )
        round_row = PaperCascadeRound(
            round_id=len(self.rounds) + 1,
            opened_at=self.open_fills[0].timestamp,
            closed_at=candle.timestamp,
            fills=tuple(self.open_fills),
            target_index=target,
            exit_index_price=target if reason == "target" else candle.close,
            exit_option_premium=exit_premium,
            exit_quantity=total_quantity,
            gross_pnl=gross_pnl,
            costs=costs,
            net_pnl=round(gross_pnl - costs.total, 2),
            exit_reason=reason,
        )
        self.rounds.append(round_row)
        for rung in self.rungs.values():
            if rung.status == "FILLED":
                rung.status = "CLOSED"
        self.open_fills = []
        self.reuse_below = min(row.low for row in self.geometry.history)
        self.status = "ROUND_CLOSED"
        self._log(
            candle,
            "round_closed",
            order_id=last_order_id,
            reason=reason,
            gross_pnl=round_row.gross_pnl,
            costs=round_row.costs.total,
            net_pnl=round_row.net_pnl,
            reuse_below=self.reuse_below,
        )

    def _check_exit(self, candle: IndexCandle) -> None:
        if not self.open_fills:
            return
        target = self.target_index
        newest_fill = max(fill.timestamp for fill in self.open_fills)
        if target is not None and candle.timestamp > newest_fill and candle.high >= target:
            self._close_round(candle, reason="target", target=target)
            return
        if self.adapter.expiry_squareoff_due(self.contract, candle.timestamp):
            self._close_round(candle, reason="expiry_square_off", target=target or candle.close)

    def kill_and_close(self, candle: IndexCandle) -> dict[str, Any]:
        """Cancel unfunded paper rungs and close an open paper basket now.

        There are no broker orders in this paper-only engine.  A kill therefore
        cancels its pending/collected paper rungs and records a paper sell at a
        current option quote.  If the quote is unavailable, the open basket is
        deliberately left intact so the caller can report that no exit was
        confirmed rather than silently discarding a position.
        """

        cancelled = []
        for rung in self.rungs.values():
            if rung.status in {"PENDING", "COLLECTED"}:
                rung.status = "CANCELLED"
                cancelled.append(rung.key)
        self.pending_rung_keys = []
        self.pending_inr = 0.0
        self.pending_line = None
        self.pending_last_red = None
        self.pending_stop = None
        self.pending_stop_timestamp = None
        if self.open_fills:
            self._close_round(candle, reason="manual_kill", target=candle.close)
            if self.open_fills:
                self._log(candle, "kill_exit_unconfirmed", cancelled_rungs=cancelled)
                return {"closed": False, "cancelled_rungs": cancelled, "reason": "option_quote_missing"}
        self.status = "KILLED"
        self._log(candle, "campaign_killed", cancelled_rungs=cancelled)
        return {"closed": True, "cancelled_rungs": cancelled, "reason": "manual_kill"}

    def on_candle(self, candle: IndexCandle) -> None:
        """Advance one closed NIFTY 5m candle in the same safe order as CryptoForge."""

        if not is_nse_cash_session(candle.timestamp):
            return
        self.geometry.on_candle(candle)
        self._sync_new_rungs(candle)
        self._fill_pending_stop(candle)
        self._check_exit(candle)
        self._release_closed_rungs(candle)
        self._collect_crossed_rungs(candle)
        self._advance_stop(candle)

    def run(self, candles: Iterable[IndexCandle]) -> "NiftyOptionsPaperCascade":
        for candle in sorted(candles, key=lambda row: row.timestamp):
            self.on_candle(candle)
        return self

    @staticmethod
    def _candle_from_dict(payload: Mapping[str, Any]) -> IndexCandle:
        return IndexCandle(
            timestamp=_as_ist(datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))),
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
        )

    @staticmethod
    def _candle_to_dict(candle: IndexCandle) -> dict[str, Any]:
        return {
            "timestamp": candle.timestamp.isoformat(),
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
        }

    @staticmethod
    def _fill_to_dict(fill: PaperCascadeFill) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": fill.timestamp.isoformat(),
            "index_price": fill.index_price,
            "option_premium": fill.option_premium,
            "lots": fill.lots,
            "quantity": fill.quantity,
            "rung_keys": list(fill.rung_keys),
            "order_id": fill.order_id,
        }
        if fill.contract is not None:
            payload["contract"] = {
                "underlying": fill.contract.underlying,
                "strike": fill.contract.strike,
                "expiry": fill.contract.expiry.isoformat(),
                "option_type": fill.contract.option_type,
                "lot_size": fill.contract.lot_size,
                "security_id": fill.contract.security_id,
            }
        return payload

    @classmethod
    def _fill_from_dict(cls, payload: Mapping[str, Any]) -> PaperCascadeFill:
        contract_payload = payload.get("contract")
        contract = None
        if contract_payload:
            contract = FixedCampaignOption(
                underlying=str(contract_payload["underlying"]),
                strike=int(contract_payload["strike"]),
                expiry=date.fromisoformat(str(contract_payload["expiry"])),
                option_type=str(contract_payload["option_type"]),
                lot_size=int(contract_payload["lot_size"]),
                security_id=str(contract_payload["security_id"]),
            )
        return PaperCascadeFill(
            timestamp=_as_ist(datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))),
            index_price=float(payload["index_price"]),
            option_premium=float(payload["option_premium"]),
            lots=int(payload["lots"]),
            quantity=int(payload["quantity"]),
            rung_keys=tuple(str(key) for key in payload.get("rung_keys") or []),
            order_id=str(payload.get("order_id") or ""),
            contract=contract,
        )

    @staticmethod
    def _costs_to_dict(costs: OptionRoundCosts) -> dict[str, float]:
        return {
            "buy_turnover": costs.buy_turnover,
            "sell_turnover": costs.sell_turnover,
            "brokerage": costs.brokerage,
            "stt": costs.stt,
            "exchange_transaction": costs.exchange_transaction,
            "sebi": costs.sebi,
            "stamp": costs.stamp,
            "gst": costs.gst,
            "total": costs.total,
        }

    @staticmethod
    def _costs_from_dict(payload: Mapping[str, Any]) -> OptionRoundCosts:
        return OptionRoundCosts(
            buy_turnover=float(payload.get("buy_turnover") or 0),
            sell_turnover=float(payload.get("sell_turnover") or 0),
            brokerage=float(payload.get("brokerage") or 0),
            stt=float(payload.get("stt") or 0),
            exchange_transaction=float(payload.get("exchange_transaction") or 0),
            sebi=float(payload.get("sebi") or 0),
            stamp=float(payload.get("stamp") or 0),
            gst=float(payload.get("gst") or 0),
        )

    def get_status(self) -> dict[str, Any]:
        """A stable, UI-ready snapshot.  It contains no broker-order state."""

        contract = self.contract
        return {
            "mode": "paper",
            "running": self.status not in {"STOPPED", "KILLED", "MOTHER_BROKEN", "MOTHER_RETESTED"},
            "status": self.status,
            "contract": {
                "underlying": contract.underlying,
                "strike": contract.strike,
                "expiry": contract.expiry.isoformat(),
                "option_type": contract.option_type,
                "lot_size": contract.lot_size,
                "security_id": contract.security_id,
            },
            "mother": {
                "timestamp": self.geometry.campaign.mother_timestamp.isoformat(),
                "high": self.geometry.campaign.mother_high,
                "low": self.geometry.campaign.mother_low,
                "state": self.geometry.campaign.state,
            },
            "target_index": self.target_index,
            "average_index_entry": self.average_index_entry,
            "open_quantity": self.open_quantity,
            "pending_inr": self.pending_inr,
            "pending_stop": self.pending_stop,
            "reuse_below": self.reuse_below,
            "rungs": [
                {
                    "key": rung.key,
                    "leg_id": rung.leg_id,
                    "level": rung.level,
                    "index_price": rung.index_price,
                    "budget_inr": rung.budget_inr,
                    "status": rung.status,
                }
                for rung in sorted(self.rungs.values(), key=lambda row: (row.leg_id, row.level))
            ],
            "open_fills": [self._fill_to_dict(fill) for fill in self.open_fills],
            "rounds": [
                {
                    "round_id": row.round_id,
                    "opened_at": row.opened_at.isoformat(),
                    "closed_at": row.closed_at.isoformat(),
                    "fills": [self._fill_to_dict(fill) for fill in row.fills],
                    "target_index": row.target_index,
                    "exit_index_price": row.exit_index_price,
                    "exit_option_premium": row.exit_option_premium,
                    "exit_quantity": row.exit_quantity,
                    "gross_pnl": row.gross_pnl,
                    "costs": self._costs_to_dict(row.costs),
                    "net_pnl": row.net_pnl,
                    "exit_reason": row.exit_reason,
                }
                for row in self.rounds
            ],
            "events": self.events[-100:],
            "geometry": {
                "trendlines": [
                    {
                        "id": row.trendline_id,
                        "anchor1_price": row.anchor1_price,
                        "anchor1_timestamp": row.anchor1_timestamp.isoformat(),
                        "anchor2_price": row.anchor2_price,
                        "anchor2_timestamp": row.anchor2_timestamp.isoformat(),
                        "bears_fib": row.bears_fib,
                    }
                    for row in self.geometry.campaign.trendlines
                ],
                "legs": [
                    {
                        "leg_id": row.leg_id,
                        "trendline_id": row.trendline_id,
                        "fib_high": row.touch_high,
                        "fib_low": row.low,
                        "touch_timestamp": row.touch_timestamp.isoformat(),
                    }
                    for row in self.geometry.campaign.legs
                ],
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Persist a paper campaign; live execution is never serialised here."""

        status = self.get_status()
        return {
            "version": 1,
            "config": {
                "rung_inr": self.config.rung_inr,
                "target_fraction": self.config.target_fraction,
                "ce_offset_steps": self.config.ce_offset_steps,
                "cost_schedule": dict(self.config.cost_schedule.__dict__),
            },
            "contract": status["contract"],
            "history": [self._candle_to_dict(row) for row in self.geometry.history],
            "rungs": status["rungs"],
            "open_fills": status["open_fills"],
            "rounds": status["rounds"],
            "pending_rung_keys": list(self.pending_rung_keys),
            "pending_inr": self.pending_inr,
            "pending_line": self.pending_line,
            "pending_last_red": self.pending_last_red,
            "pending_stop": self.pending_stop,
            "pending_stop_timestamp": self.pending_stop_timestamp.isoformat() if self.pending_stop_timestamp else None,
            "reuse_below": self.reuse_below,
            "status": self.status,
            "events": list(self.events[-100:]),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        adapter: CascadeOptionsAdapter,
        option_premium_lookup: OptionPremiumLookup,
    ) -> "NiftyOptionsPaperCascade":
        history = [cls._candle_from_dict(row) for row in payload.get("history") or []]
        if not history:
            raise CascadeError("Cannot restore a Cascade campaign without its mother candle")
        raw_contract = payload.get("contract") or {}
        contract = FixedCampaignOption(
            underlying=str(raw_contract["underlying"]),
            strike=int(raw_contract["strike"]),
            expiry=date.fromisoformat(str(raw_contract["expiry"])),
            option_type=str(raw_contract["option_type"]),
            lot_size=int(raw_contract["lot_size"]),
            security_id=str(raw_contract["security_id"]),
        )
        raw_config = payload.get("config") or {}
        config = PaperCascadeConfig(
            rung_inr=float(raw_config["rung_inr"]),
            target_fraction=float(raw_config.get("target_fraction") or 0.25),
            ce_offset_steps=int(raw_config.get("ce_offset_steps") or -2),
            cost_schedule=NiftyOptionCostSchedule(**dict(raw_config.get("cost_schedule") or {})),
        )
        engine = cls(history[0], contract, adapter, option_premium_lookup, config)
        engine.geometry.feed(history[1:])
        engine.rungs = {
            str(row["key"]): PaperCascadeRung(
                leg_id=int(row["leg_id"]),
                level=int(row["level"]),
                index_price=float(row["index_price"]),
                budget_inr=float(row["budget_inr"]),
                status=str(row.get("status") or "PENDING"),
            )
            for row in payload.get("rungs") or []
        }
        engine.open_fills = [cls._fill_from_dict(row) for row in payload.get("open_fills") or []]
        engine.rounds = [
            PaperCascadeRound(
                round_id=int(row["round_id"]),
                opened_at=_as_ist(datetime.fromisoformat(str(row["opened_at"]).replace("Z", "+00:00"))),
                closed_at=_as_ist(datetime.fromisoformat(str(row["closed_at"]).replace("Z", "+00:00"))),
                fills=tuple(cls._fill_from_dict(fill) for fill in row.get("fills") or []),
                target_index=float(row["target_index"]),
                exit_index_price=float(row["exit_index_price"]),
                exit_option_premium=float(row["exit_option_premium"]),
                exit_quantity=int(row["exit_quantity"]),
                gross_pnl=float(row["gross_pnl"]),
                costs=cls._costs_from_dict(row.get("costs") or {}),
                net_pnl=float(row["net_pnl"]),
                exit_reason=str(row["exit_reason"]),
            )
            for row in payload.get("rounds") or []
        ]
        engine.pending_rung_keys = [str(key) for key in payload.get("pending_rung_keys") or []]
        engine.pending_inr = float(payload.get("pending_inr") or 0)
        engine.pending_line = payload.get("pending_line")
        engine.pending_last_red = payload.get("pending_last_red")
        engine.pending_stop = payload.get("pending_stop")
        raw_stop_timestamp = payload.get("pending_stop_timestamp")
        if raw_stop_timestamp:
            engine.pending_stop_timestamp = _as_ist(
                datetime.fromisoformat(str(raw_stop_timestamp).replace("Z", "+00:00"))
            )
        engine.reuse_below = payload.get("reuse_below")
        engine.status = str(payload.get("status") or "WAITING")
        engine.events = list(payload.get("events") or [])[-100:]
        return engine


class OneHourCandleEntryPaper:
    """One-hour, no-escalation CE paper campaign.

    Two qualifying red candles may be separated by green candles.  A red
    candle qualifies only when it closes below the immediately preceding 1H
    candle; the recovery buy-stop follows the latest qualifying red close.
    """

    def __init__(
        self,
        mother: IndexCandle,
        contract: FixedCampaignOption,
        adapter: CascadeOptionsAdapter,
        option_premium_lookup: OptionPremiumLookup,
        *,
        target_fraction: float = 0.25,
        signal_only: bool = False,
    ) -> None:
        if not adapter.paper_only or contract.option_type != "CE":
            raise PaperOnlyViolation("The 1H Candle Entry campaign is CE-only and paper-only")
        self.mother = mother
        self.contract = contract
        self.adapter = adapter
        self.option_premium_lookup = option_premium_lookup
        self.target_fraction = float(target_fraction)
        # Historical Dhan index candles can prove the entry/target geometry,
        # but must never be paired with a current option LTP and presented as
        # a historical fixed-strike fill.  This mode records index signals
        # only; normal current-session paper campaigns remain quote-backed.
        self.signal_only = bool(signal_only)
        self.replay_complete = False
        self.history = [mother]
        self.qualifying_reds: list[IndexCandle] = []
        self.pending_stop: Optional[float] = None
        self.pending_stop_timestamp: Optional[datetime] = None
        self.fill: Optional[PaperCascadeFill] = None
        self.signal_entry_timestamp: Optional[datetime] = None
        self.signal_entry_index_price: Optional[float] = None
        self.signal_exit_timestamp: Optional[datetime] = None
        self.rounds: list[PaperCascadeRound] = []
        self.events: list[dict[str, Any]] = []
        self.status = "WAITING_TWO_RED"

    def _log(self, candle: IndexCandle, event: str, **payload: Any) -> None:
        self.events.append({"timestamp": candle.timestamp.isoformat(), "event": event, **payload})

    @property
    def target_index(self) -> Optional[float]:
        entry_price = self.fill.index_price if self.fill is not None else self.signal_entry_index_price
        if entry_price is None:
            return None
        return entry_price + self.target_fraction * (self.mother.high - entry_price)

    def complete_historical_replay(self, candle: Optional[IndexCandle] = None) -> None:
        """Mark a finite signal-only replay complete without inventing a trade."""

        if not self.signal_only:
            return
        self.replay_complete = True
        if candle is not None:
            self._log(candle, "historical_replay_complete", status=self.status)

    def _premium(self, candle: IndexCandle) -> Optional[float]:
        value = self.option_premium_lookup(candle.timestamp, self.contract)
        try:
            return float(value) if value is not None and float(value) > 0 else None
        except (TypeError, ValueError):
            return None

    def _close(self, candle: IndexCandle, reason: str) -> bool:
        if self.signal_only:
            if self.signal_entry_index_price is None:
                return True
            self.signal_exit_timestamp = candle.timestamp
            self.status = "CLOSED" if reason != "manual_kill" else "KILLED"
            self._log(candle, "signal_target_reached", reason=reason, target_index=self.target_index)
            return True
        if self.fill is None:
            return True
        premium = self._premium(candle)
        if premium is None:
            self.status = "AWAITING_OPTION_QUOTE"
            self._log(candle, "option_quote_missing", action="sell", reason=reason)
            return False
        quantity = self.fill.quantity
        self.adapter.place_order(self.contract, side="SELL", quantity=quantity)
        costs = calculate_nifty_option_basket_round_costs(
            buys=[OptionCostFill(self.fill.option_premium, quantity, self.fill.lots)],
            sell_price=premium,
            sell_quantity=quantity,
            sell_lots=self.fill.lots,
        )
        gross = round((premium - self.fill.option_premium) * quantity, 2)
        target = self.target_index or candle.close
        self.rounds.append(
            PaperCascadeRound(
                round_id=len(self.rounds) + 1,
                opened_at=self.fill.timestamp,
                closed_at=candle.timestamp,
                fills=(self.fill,),
                target_index=target,
                exit_index_price=candle.close,
                exit_option_premium=premium,
                exit_quantity=quantity,
                gross_pnl=gross,
                costs=costs,
                net_pnl=round(gross - costs.total, 2),
                exit_reason=reason,
            )
        )
        self.fill = None
        self.status = "CLOSED" if reason != "manual_kill" else "KILLED"
        self._log(candle, "round_closed", reason=reason, net_pnl=self.rounds[-1].net_pnl)
        return True

    def on_candle(self, candle: IndexCandle) -> None:
        if candle.timestamp <= self.mother.timestamp or self.status in {"CLOSED", "KILLED"}:
            return
        prior = self.history[-1]
        self.history.append(candle)
        if self.signal_only and self.signal_entry_index_price is not None:
            target = self.target_index
            if target is not None and candle.high >= target:
                self._close(candle, "target")
            return
        if self.fill is not None:
            target = self.target_index
            if target is not None and candle.high >= target:
                self._close(candle, "target")
            return
        if (
            self.pending_stop is not None
            and self.pending_stop_timestamp
            and candle.timestamp > self.pending_stop_timestamp
        ):
            if candle.high >= self.pending_stop:
                if self.signal_only:
                    self.signal_entry_timestamp = candle.timestamp
                    self.signal_entry_index_price = self.pending_stop
                    self.pending_stop = None
                    self.pending_stop_timestamp = None
                    self.status = "OPEN_SIGNAL_ONLY"
                    self._log(
                        candle,
                        "signal_entry",
                        index_price=self.signal_entry_index_price,
                        target_index=self.target_index,
                    )
                    return
                premium = self._premium(candle)
                if premium is None:
                    self.status = "AWAITING_OPTION_QUOTE"
                    self._log(candle, "option_quote_missing", action="buy")
                    return
                quantity = self.contract.lot_size
                order = self.adapter.place_order(self.contract, side="BUY", quantity=quantity)
                self.fill = PaperCascadeFill(
                    candle.timestamp, self.pending_stop, premium, 1, quantity, (), order.order_id
                )
                self.pending_stop = None
                self.pending_stop_timestamp = None
                self.status = "OPEN"
                self._log(
                    candle,
                    "paper_fill",
                    index_price=self.fill.index_price,
                    option_premium=premium,
                    quantity=quantity,
                    target_index=self.target_index,
                )
                return
        if candle.is_red and candle.close < prior.close:
            self.qualifying_reds.append(candle)
            if len(self.qualifying_reds) >= 2:
                self.pending_stop = candle.close
                self.pending_stop_timestamp = candle.timestamp
                self.status = "ARMED"
                self._log(
                    candle, "entry_stop_armed", trigger=self.pending_stop, qualifying_count=len(self.qualifying_reds)
                )

    def kill_and_close(self, candle: IndexCandle) -> bool:
        self.pending_stop = None
        self.pending_stop_timestamp = None
        if self.signal_only and self.signal_entry_index_price is not None:
            return self._close(candle, "manual_kill")
        if self.fill is not None:
            return self._close(candle, "manual_kill")
        self.status = "KILLED"
        self._log(candle, "campaign_killed")
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "mode": "paper",
            "strategy": "candle_entry_1h",
            "running": self.status not in {"CLOSED", "KILLED"},
            "status": self.status,
            "pricing_mode": "signal_only_dhan" if self.signal_only else "current_quote_paper",
            "pricing_warning": (
                "Historical replay verifies NIFTY 1H entry and target geometry only. "
                "Fixed-strike option premium and P&L are intentionally withheld."
                if self.signal_only
                else None
            ),
            "replay_complete": self.replay_complete,
            "mother": {
                "timestamp": self.mother.timestamp.isoformat(),
                "high": self.mother.high,
                "low": self.mother.low,
            },
            "contract": {
                "underlying": self.contract.underlying,
                "strike": self.contract.strike,
                "expiry": self.contract.expiry.isoformat(),
                "option_type": self.contract.option_type,
                "lot_size": self.contract.lot_size,
            },
            "entry_stop": self.pending_stop,
            "target_index": self.target_index,
            "qualifying_reds": [row.timestamp.isoformat() for row in self.qualifying_reds],
            "open_fill": NiftyOptionsPaperCascade._fill_to_dict(self.fill) if self.fill else None,
            "signal_entry": (
                {
                    "timestamp": self.signal_entry_timestamp.isoformat() if self.signal_entry_timestamp else None,
                    "index_price": self.signal_entry_index_price,
                    "exit_timestamp": self.signal_exit_timestamp.isoformat() if self.signal_exit_timestamp else None,
                }
                if self.signal_entry_index_price is not None
                else None
            ),
            "rounds": [
                {"round_id": row.round_id, "net_pnl": row.net_pnl, "exit_reason": row.exit_reason}
                for row in self.rounds
            ],
            "events": self.events[-100:],
        }


@dataclass
class _FibRung:
    """One deep fib line the campaign may buy on, and how far it has got."""

    level: int
    index_price: float
    status: str = "PENDING"  # PENDING -> ARMED -> FILLED

    @property
    def key(self) -> str:
        return f"L{self.level}"


class FibBoundaryPaper:
    """Manual-mother fib-boundary CE/PE paper campaign, one target then done.

    The sibling of :class:`OneHourCandleEntryPaper`.  Both share the recovery
    buy-stop mechanic (two same-colour closes arm a stop at that close; it fills
    when price trades back through it) and the same paper infrastructure --
    ``IndexCandle`` in, ``FixedCampaignOption`` out through the paper-only
    adapter, current-quote premium only.  What differs is *where* a buy is
    allowed: never at an arbitrary swing, only on the deep fib lines measured
    straight off the typed mother candle -- levels (4, 8) on 1m/5m and
    (2, 4, 8) on 15m/1h.  CE arms below support and targets 0.25 up toward the
    mother high; PE mirrors above resistance and targets 0.25 down toward the
    mother low.  When the averaged basket reaches its target the whole thing
    closes and the campaign ENDS -- there is no new-low re-arm.

    ``signal_only`` proves the index geometry over historical Dhan candles
    without ever pairing a current LTP with a past fill; it withholds premium
    and P&L exactly like the 1H Candle Entry replay.
    """

    def __init__(
        self,
        mother: IndexCandle,
        contract: FixedCampaignOption,
        adapter: CascadeOptionsAdapter,
        option_premium_lookup: OptionPremiumLookup,
        *,
        timeframe: str = "5m",
        rung_inr: float = 75_000.0,
        target_fraction: float = 0.25,
        signal_only: bool = False,
    ) -> None:
        side = str(contract.option_type).upper()
        if not adapter.paper_only:
            raise PaperOnlyViolation("The fib-boundary campaign requires a paper-only adapter")
        if side not in {"CE", "PE"}:
            raise CascadeError("fib-boundary option_type must be CE or PE")
        if mother.high <= mother.low:
            raise CascadeError("mother high must exceed mother low")
        if rung_inr <= 0:
            raise CascadeError("rung_inr must be positive")
        if not 0 < float(target_fraction) <= 1:
            raise CascadeError("target_fraction must be between 0 and 1")
        self.mother = mother
        self.contract = contract
        self.adapter = adapter
        self.option_premium_lookup = option_premium_lookup
        self.side = side
        self.timeframe = normalise_timeframe(timeframe)
        self.rung_inr = float(rung_inr)
        self.target_fraction = float(target_fraction)
        self.signal_only = bool(signal_only)
        self.replay_complete = False

        self.history: list[IndexCandle] = [mother]
        self.rungs: list[_FibRung] = [
            _FibRung(level=level, index_price=boundary_price(side, mother.high, mother.low, level))
            for level in boundaries_for_timeframe(self.timeframe)
        ]
        # 2-candle trigger + a single armed buy-stop waiting to fill.
        self.streak = 0
        self.pending_rung: Optional[_FibRung] = None
        self.pending_stop: Optional[float] = None
        self.pending_stop_timestamp: Optional[datetime] = None
        # Real quote-backed fills (current session).
        self.open_fills: list[PaperCascadeFill] = []
        # Index-only fills for signal_only historical proof: (timestamp, price, level).
        self.signal_fills: list[dict[str, Any]] = []
        self.rounds: list[PaperCascadeRound] = []
        self.events: list[dict[str, Any]] = []
        self.status = "WAITING"

    # ── geometry helpers ──────────────────────────────────────────

    @staticmethod
    def _is_green(candle: IndexCandle) -> bool:
        return candle.close > candle.open

    def _qualifies(self, candle: IndexCandle) -> bool:
        return candle.is_red if self.side == "CE" else self._is_green(candle)

    def _beyond(self, price: float, level_price: float) -> bool:
        return price <= level_price if self.side == "CE" else price >= level_price

    def _next_rung(self) -> Optional[_FibRung]:
        for rung in self.rungs:
            if rung.status == "PENDING":
                return rung
        return None

    @property
    def average_index_entry(self) -> Optional[float]:
        if self.signal_only:
            if not self.signal_fills:
                return None
            return sum(row["index_price"] for row in self.signal_fills) / len(self.signal_fills)
        quantity = sum(fill.quantity for fill in self.open_fills)
        if quantity <= 0:
            return None
        return sum(fill.index_price * fill.quantity for fill in self.open_fills) / quantity

    @property
    def open_quantity(self) -> int:
        return sum(fill.quantity for fill in self.open_fills)

    @property
    def target_index(self) -> Optional[float]:
        average = self.average_index_entry
        if average is None:
            return None
        if self.side == "CE":
            return average + self.target_fraction * (self.mother.high - average)
        return average - self.target_fraction * (average - self.mother.low)

    def _has_open(self) -> bool:
        return bool(self.signal_fills) if self.signal_only else bool(self.open_fills)

    # ── plumbing ──────────────────────────────────────────────────

    def _log(self, candle: IndexCandle, event: str, **payload: Any) -> None:
        self.events.append({"timestamp": candle.timestamp.isoformat(), "event": event, **payload})

    def _premium(self, candle: IndexCandle) -> Optional[float]:
        value = self.option_premium_lookup(candle.timestamp, self.contract)
        try:
            return float(value) if value is not None and float(value) > 0 else None
        except (TypeError, ValueError):
            return None

    def complete_historical_replay(self, candle: Optional[IndexCandle] = None) -> None:
        """Mark a finite signal-only replay complete without inventing a trade."""
        if not self.signal_only:
            return
        self.replay_complete = True
        if candle is not None:
            self._log(candle, "historical_replay_complete", status=self.status)

    # ── state machine ─────────────────────────────────────────────

    def _fill_pending_stop(self, candle: IndexCandle) -> None:
        if (
            self.pending_rung is None
            or self.pending_stop is None
            or self.pending_stop_timestamp is None
            or candle.timestamp <= self.pending_stop_timestamp
        ):
            return
        crossed = candle.high >= self.pending_stop if self.side == "CE" else candle.low <= self.pending_stop
        if not crossed:
            return
        rung = self.pending_rung
        if self.signal_only:
            self.signal_fills.append(
                {"timestamp": candle.timestamp.isoformat(), "index_price": self.pending_stop, "level": rung.level}
            )
            rung.status = "FILLED"
            self.pending_rung = None
            self.pending_stop = None
            self.pending_stop_timestamp = None
            self.streak = 0
            self.status = "OPEN_SIGNAL_ONLY"
            self._log(candle, "signal_entry", level=rung.level, index_price=self.signal_fills[-1]["index_price"])
            return
        premium = self._premium(candle)
        if premium is None:
            self.status = "AWAITING_OPTION_QUOTE"
            self._log(candle, "option_quote_missing", action="buy", level=rung.level)
            return
        lots = max(1, math.floor(self.rung_inr / (premium * self.contract.lot_size)))
        quantity = lots * self.contract.lot_size
        order = self.adapter.place_order(self.contract, side="BUY", quantity=quantity)
        fill = PaperCascadeFill(
            timestamp=candle.timestamp,
            index_price=self.pending_stop,
            option_premium=premium,
            lots=lots,
            quantity=quantity,
            rung_keys=(rung.key,),
            order_id=order.order_id,
        )
        self.open_fills.append(fill)
        rung.status = "FILLED"
        self.pending_rung = None
        self.pending_stop = None
        self.pending_stop_timestamp = None
        self.streak = 0
        self.status = "OPEN"
        self._log(
            candle,
            "paper_fill",
            level=rung.level,
            index_price=fill.index_price,
            option_premium=premium,
            lots=lots,
            quantity=quantity,
            target_index=self.target_index,
        )

    def _arm_if_qualified(self, candle: IndexCandle) -> None:
        if not self._qualifies(candle):
            self.streak = 0
            return
        self.streak += 1
        rung = self._next_rung()
        if rung is None or self.streak < 2 or not self._beyond(candle.close, rung.index_price):
            return
        self.pending_rung = rung
        self.pending_stop = candle.close
        self.pending_stop_timestamp = candle.timestamp
        rung.status = "ARMED"
        self.status = "ARMED"
        self._log(
            candle,
            "stop_armed",
            level=rung.level,
            boundary=round(rung.index_price, 2),
            trigger=round(candle.close, 2),
        )

    def _close(self, candle: IndexCandle, reason: str) -> bool:
        target = self.target_index
        if self.signal_only:
            if not self.signal_fills:
                self.status = "KILLED" if reason == "manual_kill" else "CLOSED"
                return True
            self.status = "KILLED" if reason == "manual_kill" else "CLOSED"
            self._log(candle, "signal_target_reached", reason=reason, target_index=target)
            return True
        if not self.open_fills:
            self.status = "KILLED" if reason == "manual_kill" else "CLOSED"
            return True
        premium = self._premium(candle)
        if premium is None:
            self.status = "AWAITING_OPTION_QUOTE"
            self._log(candle, "option_quote_missing", action="sell", reason=reason)
            return False
        quantity = self.open_quantity
        lots = quantity // self.contract.lot_size
        self.adapter.place_order(self.contract, side="SELL", quantity=quantity)
        costs = calculate_nifty_option_basket_round_costs(
            buys=[OptionCostFill(fill.option_premium, fill.quantity, fill.lots) for fill in self.open_fills],
            sell_price=premium,
            sell_quantity=quantity,
            sell_lots=lots,
        )
        gross = round(sum((premium - fill.option_premium) * fill.quantity for fill in self.open_fills), 2)
        self.rounds.append(
            PaperCascadeRound(
                round_id=len(self.rounds) + 1,
                opened_at=self.open_fills[0].timestamp,
                closed_at=candle.timestamp,
                fills=tuple(self.open_fills),
                target_index=target or candle.close,
                exit_index_price=target if reason == "target" and target is not None else candle.close,
                exit_option_premium=premium,
                exit_quantity=quantity,
                gross_pnl=gross,
                costs=costs,
                net_pnl=round(gross - costs.total, 2),
                exit_reason=reason,
            )
        )
        for rung in self.rungs:
            if rung.status == "FILLED":
                rung.status = "CLOSED"
        self.open_fills = []
        self.status = "KILLED" if reason == "manual_kill" else "CLOSED"
        self._log(candle, "round_closed", reason=reason, net_pnl=self.rounds[-1].net_pnl)
        return True

    def _check_exit(self, candle: IndexCandle) -> bool:
        if not self._has_open():
            return False
        target = self.target_index
        if target is not None:
            hit = candle.high >= target if self.side == "CE" else candle.low <= target
            if hit:
                return self._close(candle, "target")
        if not self.signal_only and self.adapter.expiry_squareoff_due(self.contract, candle.timestamp):
            return self._close(candle, "expiry_square_off")
        return False

    def on_candle(self, candle: IndexCandle) -> None:
        if candle.timestamp <= self.mother.timestamp or self.status in {"CLOSED", "KILLED"}:
            return
        self.history.append(candle)
        # Same safe order as the sibling engines: settle a resting stop, take a
        # target if one is now reachable, then look for the next arm.  One
        # target ends the campaign, so nothing re-arms after a close.
        self._fill_pending_stop(candle)
        if self._check_exit(candle):
            return
        self._arm_if_qualified(candle)

    def run(self, candles: Iterable[IndexCandle]) -> "FibBoundaryPaper":
        for candle in sorted(candles, key=lambda row: row.timestamp):
            self.on_candle(candle)
        return self

    def kill_and_close(self, candle: IndexCandle) -> bool:
        self.pending_rung = None
        self.pending_stop = None
        self.pending_stop_timestamp = None
        for rung in self.rungs:
            if rung.status in {"PENDING", "ARMED"}:
                rung.status = "CANCELLED"
        if self._has_open():
            return self._close(candle, "manual_kill")
        self.status = "KILLED"
        self._log(candle, "campaign_killed")
        return True

    # ── snapshot + persistence ────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        contract = self.contract
        return {
            "mode": "paper",
            "strategy": "fib_boundary",
            "running": self.status not in {"CLOSED", "KILLED"},
            "status": self.status,
            "side": self.side,
            "timeframe": self.timeframe,
            "pricing_mode": "signal_only_dhan" if self.signal_only else "current_quote_paper",
            "pricing_warning": (
                "Historical replay verifies NIFTY index entry and target geometry only. "
                "Fixed-strike option premium and P&L are intentionally withheld."
                if self.signal_only
                else None
            ),
            "replay_complete": self.replay_complete,
            "mother": {
                "timestamp": self.mother.timestamp.isoformat(),
                "high": self.mother.high,
                "low": self.mother.low,
            },
            "contract": {
                "underlying": contract.underlying,
                "strike": contract.strike,
                "expiry": contract.expiry.isoformat(),
                "option_type": contract.option_type,
                "lot_size": contract.lot_size,
                "security_id": contract.security_id,
            },
            "rung_inr": self.rung_inr,
            "target_index": self.target_index,
            "average_index_entry": self.average_index_entry,
            "open_quantity": self.open_quantity,
            "entry_stop": self.pending_stop,
            "boundaries": [
                {"key": rung.key, "level": rung.level, "index_price": rung.index_price, "status": rung.status}
                for rung in self.rungs
            ],
            "open_fills": [NiftyOptionsPaperCascade._fill_to_dict(fill) for fill in self.open_fills],
            "signal_fills": list(self.signal_fills),
            "rounds": [
                {
                    "round_id": row.round_id,
                    "opened_at": row.opened_at.isoformat(),
                    "closed_at": row.closed_at.isoformat(),
                    "fills": [NiftyOptionsPaperCascade._fill_to_dict(fill) for fill in row.fills],
                    "target_index": row.target_index,
                    "exit_index_price": row.exit_index_price,
                    "exit_option_premium": row.exit_option_premium,
                    "exit_quantity": row.exit_quantity,
                    "gross_pnl": row.gross_pnl,
                    "costs": NiftyOptionsPaperCascade._costs_to_dict(row.costs),
                    "net_pnl": row.net_pnl,
                    "exit_reason": row.exit_reason,
                }
                for row in self.rounds
            ],
            "events": self.events[-100:],
        }

    def to_dict(self) -> dict[str, Any]:
        status = self.get_status()
        return {
            "version": 1,
            "strategy": "fib_boundary",
            "config": {
                "timeframe": self.timeframe,
                "rung_inr": self.rung_inr,
                "target_fraction": self.target_fraction,
                "signal_only": self.signal_only,
            },
            "contract": status["contract"],
            "history": [NiftyOptionsPaperCascade._candle_to_dict(row) for row in self.history],
            "boundaries": status["boundaries"],
            "open_fills": status["open_fills"],
            "signal_fills": list(self.signal_fills),
            "rounds": status["rounds"],
            "streak": self.streak,
            "pending_level": self.pending_rung.level if self.pending_rung else None,
            "pending_stop": self.pending_stop,
            "pending_stop_timestamp": self.pending_stop_timestamp.isoformat() if self.pending_stop_timestamp else None,
            "replay_complete": self.replay_complete,
            "status": self.status,
            "events": list(self.events[-100:]),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        adapter: CascadeOptionsAdapter,
        option_premium_lookup: OptionPremiumLookup,
    ) -> "FibBoundaryPaper":
        history = [NiftyOptionsPaperCascade._candle_from_dict(row) for row in payload.get("history") or []]
        if not history:
            raise CascadeError("Cannot restore a fib-boundary campaign without its mother candle")
        raw_contract = payload.get("contract") or {}
        contract = FixedCampaignOption(
            underlying=str(raw_contract["underlying"]),
            strike=int(raw_contract["strike"]),
            expiry=date.fromisoformat(str(raw_contract["expiry"])),
            option_type=str(raw_contract["option_type"]),
            lot_size=int(raw_contract["lot_size"]),
            security_id=str(raw_contract["security_id"]),
        )
        raw_config = payload.get("config") or {}
        engine = cls(
            history[0],
            contract,
            adapter,
            option_premium_lookup,
            timeframe=str(raw_config.get("timeframe") or "5m"),
            rung_inr=float(raw_config.get("rung_inr") or 75_000.0),
            target_fraction=float(raw_config.get("target_fraction") or 0.25),
            signal_only=bool(raw_config.get("signal_only")),
        )
        engine.history = history
        by_level = {int(row["level"]): str(row.get("status") or "PENDING") for row in payload.get("boundaries") or []}
        for rung in engine.rungs:
            rung.status = by_level.get(rung.level, rung.status)
        engine.open_fills = [NiftyOptionsPaperCascade._fill_from_dict(row) for row in payload.get("open_fills") or []]
        engine.signal_fills = [dict(row) for row in payload.get("signal_fills") or []]
        engine.rounds = [
            PaperCascadeRound(
                round_id=int(row["round_id"]),
                opened_at=_as_ist(datetime.fromisoformat(str(row["opened_at"]).replace("Z", "+00:00"))),
                closed_at=_as_ist(datetime.fromisoformat(str(row["closed_at"]).replace("Z", "+00:00"))),
                fills=tuple(NiftyOptionsPaperCascade._fill_from_dict(fill) for fill in row.get("fills") or []),
                target_index=float(row["target_index"]),
                exit_index_price=float(row["exit_index_price"]),
                exit_option_premium=float(row["exit_option_premium"]),
                exit_quantity=int(row["exit_quantity"]),
                gross_pnl=float(row["gross_pnl"]),
                costs=NiftyOptionsPaperCascade._costs_from_dict(row.get("costs") or {}),
                net_pnl=float(row["net_pnl"]),
                exit_reason=str(row["exit_reason"]),
            )
            for row in payload.get("rounds") or []
        ]
        engine.streak = int(payload.get("streak") or 0)
        pending_level = payload.get("pending_level")
        if pending_level is not None:
            engine.pending_rung = next((rung for rung in engine.rungs if rung.level == int(pending_level)), None)
        engine.pending_stop = payload.get("pending_stop")
        raw_stop_ts = payload.get("pending_stop_timestamp")
        if raw_stop_ts:
            engine.pending_stop_timestamp = _as_ist(datetime.fromisoformat(str(raw_stop_ts).replace("Z", "+00:00")))
        engine.replay_complete = bool(payload.get("replay_complete"))
        engine.status = str(payload.get("status") or "WAITING")
        engine.events = list(payload.get("events") or [])[-100:]
        return engine
