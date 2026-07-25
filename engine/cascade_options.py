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
    def _normalise_index_frame(frame: Any, now: Optional[datetime] = None) -> list[IndexCandle]:
        if frame is None or getattr(frame, "empty", False):
            return []
        now_ist = _as_ist(now or datetime.now(IST))
        closed: list[IndexCandle] = []
        for timestamp, row in frame.iterrows():
            candle_time = timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
            candle_time = _as_ist(candle_time)
            # The Dhan historical endpoint can include the candle currently
            # forming.  Geometry is only ever allowed to see closed candles.
            if candle_time + timedelta(minutes=5) > now_ist:
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
        if str(symbol).upper() != "NIFTY" or str(timeframe).lower() != "5m":
            raise OptionsAdapterError("Phase 1 only supports closed NIFTY 5m index candles")
        current = _as_ist(now or datetime.now(IST))
        start = from_date or current.date()
        end = to_date or current.date()
        start_text = start.isoformat() if isinstance(start, date) else str(start)
        end_text = end.isoformat() if isinstance(end, date) else str(end)
        frame = await asyncio.to_thread(self.dhan.get_nifty_intraday, start_text, end_text, interval="5")
        return self._normalise_index_frame(frame, current)

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
    ) -> FixedCampaignOption:
        if strike_step <= 0:
            raise OptionsAdapterError("strike_step must be positive")
        selected_at = _as_ist(selected_at)
        expiries = [date.fromisoformat(str(value)[:10]) for value in self.scrip_master.get_expiries("NIFTY")]
        expiry = self._next_weekly_expiry(expiries, selected_at.date())
        atm = int(math.floor(float(mother_spot) / strike_step + 0.5) * strike_step)
        strike = atm + int(ce_offset_steps) * strike_step
        lot_size = int(self.scrip_master.get_lot_size("NIFTY", expiry.isoformat()))
        security_id = str(self.scrip_master.lookup("NIFTY", strike, expiry.isoformat(), "CE") or "")
        if not security_id:
            raise OptionsAdapterError(f"ScripMaster has no NIFTY {strike}CE for {expiry.isoformat()}")
        return FixedCampaignOption("NIFTY", strike, expiry, "CE", lot_size, security_id)

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
        if contract.option_type != "CE" or str(side).upper() not in {"BUY", "SELL"}:
            raise OptionsAdapterError("Phase 1 paper adapter only accepts CE BUY/SELL orders")
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
