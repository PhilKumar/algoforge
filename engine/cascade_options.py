"""Deterministic 1H NIFTY option-cascade state machine.

This module deliberately has no broker or web dependencies.  It replays closed
index candles and exact option candles supplied by a data adapter.  That keeps
the strategy rules testable before Dhan historical-data access is available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from typing import Callable, Iterable, Mapping, Optional


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
        base_timeframe = self.timeframe.lower()
        defaults = {
            "5m": ("5m", "15m", "1h"),
            "15m": ("15m", "1h", "4h"),
            "1h": ("1h", "4h", "1d"),
        }
        if base_timeframe not in defaults:
            raise CascadeError("timeframe must be 5m, 15m, or 1h")
        stages = tuple(str(value).lower() for value in (self.stage_timeframes or defaults[base_timeframe]))
        if len(stages) < len(self.lot_schedule):
            raise CascadeError("stage_timeframes must cover every lot stage")
        if stages[0] != base_timeframe or any(value not in {"5m", "15m", "1h", "4h", "1d"} for value in stages):
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


OptionLookup = Callable[[datetime, Contract], Optional[OptionCandle]]


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
        self._pending_trigger: Optional[float] = None
        self._pending_since: Optional[datetime] = None
        self._pending_contract: Optional[Contract] = None
        self._stage_extreme: Optional[float] = self._reference
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

    def _arm_if_qualified(self, candle: Candle) -> None:
        qualifies = candle.is_red if self._side == "CE" else candle.is_green
        beyond_reference = candle.close < self._reference if self._side == "CE" else candle.close > self._reference
        if not qualifies or not beyond_reference:
            return
        current_close = candle.close
        if self._last_same_colour_close is None:
            self._last_same_colour_close = current_close
            return
        if self._side == "CE" and current_close >= self._last_same_colour_close:
            return
        if self._side == "PE" and current_close <= self._last_same_colour_close:
            return
        # Previous same-colour close is the stop trigger. Greens/reds between
        # qualifying candles are ignored by design.
        self._pending_trigger = self._last_same_colour_close
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
        self._last_same_colour_close = current_close

    def _walk_pending_stop(self, candle: Candle) -> bool:
        """Move an existing stop after a newly closed qualifying candle.

        A red CE candle (or green PE candle) that makes a new qualifying close
        has priority over a possible intrabar trigger in the same OHLC bar.
        The revised order is only eligible from the next candle onward; this is
        the unambiguous, conservative interpretation of a close-based rule.
        """
        if self._pending_trigger is None or not (candle.is_red if self._side == "CE" else candle.is_green):
            return False
        if self._side == "CE":
            if candle.close >= self._reference or candle.close >= (self._last_same_colour_close or float("inf")):
                return False
        else:
            if candle.close <= self._reference or candle.close <= (self._last_same_colour_close or -float("inf")):
                return False
        self._pending_trigger = self._last_same_colour_close
        self._pending_since = candle.timestamp
        self._last_same_colour_close = candle.close
        self._log(candle.timestamp, "move_stop", trigger=self._pending_trigger, stage=self._stage + 1)
        return True

    def _try_fill(self, candle: Candle) -> bool:
        if self._pending_trigger is None or self._pending_contract is None or candle.timestamp <= self._pending_since:
            return False
        crossed = candle.high >= self._pending_trigger if self._side == "CE" else candle.low <= self._pending_trigger
        if not crossed:
            return False
        option_bar = self._lookup_option(candle.timestamp, self._pending_contract)
        if option_bar is None and self.config.strict_option_data:
            self.result.data_gap = (
                f"missing option candle at {candle.timestamp.isoformat()} for "
                f"{self._pending_contract.symbol} {self._pending_contract.strike}{self._pending_contract.option_type}"
            )
            self.result.status = "data_gap"
            return False
        lots = self.config.lot_schedule[self._stage]
        quantity = lots * self._pending_contract.lot_size
        trigger_price = float(self._pending_trigger)
        entry = Entry(
            candle.timestamp,
            trigger_price,
            option_bar.open if option_bar is not None else None,
            lots,
            quantity,
            self._pending_contract,
            self._stage + 1,
        )
        self.result.entries.append(entry)
        self._last_entry_timestamp = candle.timestamp
        self._stage += 1
        if self._stage_extreme is not None:
            self._reference = self._stage_extreme
        self._stage_extreme = None
        self._stage_started_at = candle.timestamp
        if self._stage < len(self._stage_timeframes):
            self._active_timeframe = self._stage_timeframes[self._stage]
        self._last_same_colour_close = None
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
            option_price=option_bar.open if option_bar is not None else None,
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
                if self.config.strict_option_data:
                    self.result.data_gap = (
                        f"missing exit option candle at {candle.timestamp.isoformat()} for {entry.contract.symbol}"
                    )
                    self.result.status = "data_gap"
                    return False
                option_prices.append(None)
                continue
            option_prices.append(option_bar.close)
        self.result.exit_timestamp = candle.timestamp
        known_prices = [price for price in option_prices if price is not None]
        self.result.exit_option_price = sum(known_prices) / len(known_prices) if known_prices else None
        self.result.target_index = target
        self.result.average_spot = self._average_spot()
        if all(price is not None for price in option_prices) and all(
            entry.option_price is not None for entry in self.result.entries
        ):
            self.result.realized_pnl = sum(
                (float(exit_price) - float(entry.option_price)) * entry.quantity
                for exit_price, entry in zip(option_prices, self.result.entries)
            )
        self.result.status = "closed"
        self.result.exit_reason = "target"
        self._log(candle.timestamp, "exit", target=target, realized_pnl=self.result.realized_pnl, reason="target")
        return True

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
        self.result.status = "expired"
        self.result.exit_reason = "expiry_square_off"
        self._log(candle.timestamp, "expiry_exit", expiry=expiry.isoformat(), reason="expiry_square_off")
        return True

    def _update_stage_extreme(self, candle: Candle) -> None:
        if self._stage_extreme is None:
            self._stage_extreme = candle.low if self._side == "CE" else candle.high
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
        # A new qualifying close revises the stop before any fill test.  This
        # prevents a falling red candle from filling against its *old* trigger
        # merely because its high traded there earlier in the same 1H bar.
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

    def run(self, candles: Iterable[Candle] | Mapping[str, Iterable[Candle]]) -> CascadeResult:
        if isinstance(candles, Mapping):
            priority = {timeframe: index for index, timeframe in enumerate(self._stage_timeframes)}
            ordered = sorted(
                (
                    (candle.timestamp, str(timeframe).lower(), candle)
                    for timeframe, rows in candles.items()
                    for candle in rows
                ),
                key=lambda item: (item[0], priority.get(item[1], len(priority))),
            )
        else:
            ordered = [
                (candle.timestamp, self._stage_timeframes[0], candle)
                for candle in sorted(candles, key=lambda c: c.timestamp)
            ]
        for _timestamp, timeframe, candle in ordered:
            self.on_candle(candle, timeframe=timeframe)
            if self.result.status in {"closed", "expired", "data_gap", "invalid"}:
                break
        if self.result.entries:
            self.result.average_spot = self._average_spot()
            self.result.target_index = self._target()
        return self.result
