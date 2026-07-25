"""Deterministic 1H NIFTY option-cascade state machine.

This module deliberately has no broker or web dependencies.  It replays closed
index candles and exact option candles supplied by a data adapter.  That keeps
the strategy rules testable before Dhan historical-data access is available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
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
    lot_schedule: tuple[int, ...] = (1, 2, 3)
    lot_size: int = 65
    itm_steps: int = 2
    strike_step: float = 50.0
    min_dte: int = 7
    max_dte: int = 13
    target_fraction: float = 0.25
    strict_option_data: bool = True

    def __post_init__(self) -> None:
        side = str(self.option_type).upper()
        if side not in {"CE", "PE"}:
            raise CascadeError("option_type must be CE or PE")
        if self.timeframe.lower() != "1h":
            raise CascadeError("the first release supports only a fixed 1H timeframe")
        if not self.lot_schedule or any(int(x) <= 0 for x in self.lot_schedule):
            raise CascadeError("lot_schedule must contain positive lot counts")
        if self.lot_size <= 0 or self.itm_steps <= 0 or self.strike_step <= 0:
            raise CascadeError("lot_size, itm_steps and strike_step must be positive")
        if not 0 < self.target_fraction <= 1:
            raise CascadeError("target_fraction must be between 0 and 1")
        if not 0 <= self.min_dte <= self.max_dte:
            raise CascadeError("invalid DTE range")


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
            if expiry <= trade_date or not config.min_dte <= dte <= config.max_dte:
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
    option_price: float
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
    realized_pnl: float = 0.0
    data_gap: Optional[str] = None
    events: list[dict] = field(default_factory=list)


OptionLookup = Callable[[datetime, Contract], Optional[OptionCandle]]


class OneHourCascade:
    """Mirror-symmetric CE/PE cascade for a fixed 1H signal timeframe."""

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
        self._reference = config.mother_low if self._side == "CE" else config.mother_high
        self._last_same_colour_close: Optional[float] = None
        self._pending_trigger: Optional[float] = None
        self._pending_since: Optional[datetime] = None
        self._pending_contract: Optional[Contract] = None
        self._stage_extreme = self._reference
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

    def _walk_pending_stop(self, candle: Candle) -> None:
        if self._pending_trigger is None or not (candle.is_red if self._side == "CE" else candle.is_green):
            return
        if self._side == "CE":
            if candle.close >= self._reference or candle.close >= (self._last_same_colour_close or float("inf")):
                return
        else:
            if candle.close <= self._reference or candle.close <= (self._last_same_colour_close or -float("inf")):
                return
        self._pending_trigger = self._last_same_colour_close
        self._pending_since = candle.timestamp
        self._last_same_colour_close = candle.close
        self._log(candle.timestamp, "move_stop", trigger=self._pending_trigger, stage=self._stage + 1)

    def _try_fill(self, candle: Candle) -> None:
        if self._pending_trigger is None or self._pending_contract is None or candle.timestamp <= self._pending_since:
            return
        crossed = candle.high >= self._pending_trigger if self._side == "CE" else candle.low <= self._pending_trigger
        if not crossed:
            return
        option_bar = self._lookup_option(candle.timestamp, self._pending_contract)
        if option_bar is None:
            self.result.data_gap = (
                f"missing option candle at {candle.timestamp.isoformat()} for "
                f"{self._pending_contract.symbol} {self._pending_contract.strike}{self._pending_contract.option_type}"
            )
            if self.config.strict_option_data:
                self.result.status = "data_gap"
            return
        lots = self.config.lot_schedule[self._stage]
        quantity = lots * self._pending_contract.lot_size
        entry = Entry(
            candle.timestamp, candle.close, option_bar.open, lots, quantity, self._pending_contract, self._stage + 1
        )
        self.result.entries.append(entry)
        self._last_entry_timestamp = candle.timestamp
        self._stage += 1
        self._reference = self._stage_extreme
        self._stage_extreme = candle.low if self._side == "CE" else candle.high
        self._last_same_colour_close = None
        self._pending_trigger = None
        self._pending_since = None
        self._pending_contract = None
        self.result.status = "open"
        self._log(
            candle.timestamp, "fill", stage=entry.stage, lots=lots, quantity=quantity, option_price=option_bar.open
        )

    def _try_exit(self, candle: Candle) -> bool:
        if not self.result.entries or candle.timestamp <= (self._last_entry_timestamp or self.config.mother_timestamp):
            return False
        target = self._target()
        if target is None:
            return False
        hit = candle.high >= target if self._side == "CE" else candle.low <= target
        if not hit:
            return False
        option_prices = []
        for entry in self.result.entries:
            option_bar = self._lookup_option(candle.timestamp, entry.contract)
            if option_bar is None:
                self.result.data_gap = (
                    f"missing exit option candle at {candle.timestamp.isoformat()} for {entry.contract.symbol}"
                )
                if self.config.strict_option_data:
                    self.result.status = "data_gap"
                return False
            option_prices.append(option_bar.close)
        self.result.exit_timestamp = candle.timestamp
        self.result.exit_option_price = sum(option_prices) / len(option_prices)
        self.result.target_index = target
        self.result.average_spot = self._average_spot()
        self.result.realized_pnl = sum(
            (exit_price - entry.option_price) * entry.quantity
            for exit_price, entry in zip(option_prices, self.result.entries)
        )
        self.result.status = "closed"
        self._log(candle.timestamp, "exit", target=target, realized_pnl=self.result.realized_pnl)
        return True

    def on_candle(self, candle: Candle) -> None:
        if self.result.status in {"closed", "data_gap", "invalid"} or candle.timestamp <= self.config.mother_timestamp:
            return
        if self._try_exit(candle):
            return
        self._try_fill(candle)
        if self.result.status == "data_gap":
            return
        if self._stage >= len(self.config.lot_schedule):
            return
        # The entry candle is excluded from the preceding marked extreme. The
        # next stage starts with it as a baseline and then waits below/above it.
        if self._pending_trigger is not None:
            self._walk_pending_stop(candle)
        else:
            self._arm_if_qualified(candle)
        self._stage_extreme = (
            min(self._stage_extreme, candle.low) if self._side == "CE" else max(self._stage_extreme, candle.high)
        )

    def run(self, candles: Iterable[Candle]) -> CascadeResult:
        ordered = sorted(candles, key=lambda candle: candle.timestamp)
        for candle in ordered:
            self.on_candle(candle)
            if self.result.status in {"closed", "data_gap", "invalid"}:
                break
        if self.result.entries:
            self.result.average_spot = self._average_spot()
            self.result.target_index = self._target()
        return self.result
