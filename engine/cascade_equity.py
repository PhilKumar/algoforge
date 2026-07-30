"""Cash-market Cascade paper engine for PhilForge Terminal instruments.

The geometry is intentionally delegated to the verified CryptoForge-compatible
state machine that already powers the PhilForge options paper Cascade.  This
module adds the cash-market execution layer: BEES reference mapping, INR
allocation, whole-share sizing, carry-forward cash, own-scrip targets and
delivery-style cost accounting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from engine.cascade_options import (
    GEOMETRY_FIB_LEVELS,
    CascadeError,
    IndexCandle,
    NiftyIndexCascadeGeometry,
    is_nse_cash_session,
)

IST = ZoneInfo("Asia/Kolkata")
LEVEL_ALLOCATION = {2: 0.20, 4: 0.30, 8: 0.50}
REFERENCE_INDEX_BY_TRADED_SYMBOL = {
    "NIFTYBEES": "NIFTY",
    "BANKBEES": "BANKNIFTY",
}


def _as_ist(value: datetime) -> datetime:
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def _candle_from_dict(payload: Mapping[str, Any]) -> IndexCandle:
    return IndexCandle(
        timestamp=_as_ist(datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))),
        open=float(payload["open"]),
        high=float(payload["high"]),
        low=float(payload["low"]),
        close=float(payload["close"]),
    )


def _candle_to_dict(candle: IndexCandle) -> dict[str, Any]:
    return {
        "timestamp": candle.timestamp.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
    }


def _normalise_candle(candle: IndexCandle) -> IndexCandle:
    return IndexCandle(_as_ist(candle.timestamp), candle.open, candle.high, candle.low, candle.close)


def cash_cascade_reference_symbol(symbol: str) -> str:
    """Return the signal symbol for a traded cash-market instrument."""

    normalised = str(symbol or "").strip().upper()
    return REFERENCE_INDEX_BY_TRADED_SYMBOL.get(normalised, normalised)


def cash_budget_to_quantity(budget_inr: float, price: float) -> int:
    """Whole-share sizing with no discarded undersized budget."""

    budget = float(budget_inr or 0)
    unit = float(price or 0)
    if budget <= 0 or unit <= 0:
        return 0
    return max(0, math.floor(budget / unit))


@dataclass(frozen=True)
class CashMarketCostSchedule:
    """Configurable cash-market charges.

    These defaults are deliberately treated as a paper model.  Before live use,
    callers should inject the currently verified broker/statutory schedule.
    """

    brokerage_pct: float = 0.0003
    brokerage_cap_per_order: float = 20.0
    stt_delivery_pct: float = 0.001
    exchange_transaction_pct: float = 0.0000297
    sebi_pct: float = 0.000001
    stamp_buy_pct: float = 0.00015
    gst_pct: float = 0.18


@dataclass(frozen=True)
class CashRoundCosts:
    buy_turnover: float
    sell_turnover: float
    brokerage: float
    stt: float
    exchange_transaction: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange_transaction + self.sebi + self.stamp + self.gst,
            2,
        )


def _brokerage(turnover: float, schedule: CashMarketCostSchedule) -> float:
    if turnover <= 0 or schedule.brokerage_pct <= 0:
        return 0.0
    raw = turnover * schedule.brokerage_pct
    return min(raw, schedule.brokerage_cap_per_order) if schedule.brokerage_cap_per_order > 0 else raw


def calculate_cash_round_costs(
    *,
    buys: Iterable[tuple[float, int]],
    sell_price: float,
    sell_quantity: int,
    schedule: CashMarketCostSchedule,
) -> CashRoundCosts:
    buy_turnover = round(sum(float(price) * int(quantity) for price, quantity in buys), 2)
    sell_turnover = round(float(sell_price or 0) * int(sell_quantity or 0), 2)
    brokerage = round(_brokerage(buy_turnover, schedule) + _brokerage(sell_turnover, schedule), 2)
    total_turnover = buy_turnover + sell_turnover
    stt = round(total_turnover * schedule.stt_delivery_pct, 2)
    exchange = round(total_turnover * schedule.exchange_transaction_pct, 2)
    sebi = round(total_turnover * schedule.sebi_pct, 2)
    stamp = round(buy_turnover * schedule.stamp_buy_pct, 2)
    gst = round((brokerage + exchange + sebi) * schedule.gst_pct, 2)
    return CashRoundCosts(
        buy_turnover=buy_turnover,
        sell_turnover=sell_turnover,
        brokerage=brokerage,
        stt=stt,
        exchange_transaction=exchange,
        sebi=sebi,
        stamp=stamp,
        gst=gst,
    )


@dataclass(frozen=True)
class CashCascadeInstrument:
    """Traded cash instrument plus the signal instrument used for geometry."""

    symbol: str
    name: str
    security_id: str
    exchange_segment: str = "NSE_EQ"
    instrument_type: str = "EQUITY"
    signal_symbol: str = ""
    signal_name: str = ""
    signal_security_id: str = ""
    signal_exchange_segment: str = "NSE_EQ"
    signal_instrument_type: str = "EQUITY"
    reference_mode: str = "own_scrip"

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").upper()
        signal = str(self.signal_symbol or cash_cascade_reference_symbol(symbol)).upper()
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "signal_symbol", signal)
        object.__setattr__(self, "reference_mode", "reference_index" if signal != symbol else "own_scrip")
        if not self.signal_name:
            object.__setattr__(self, "signal_name", signal)
        if not self.name:
            object.__setattr__(self, "name", symbol)

    @property
    def uses_reference_index(self) -> bool:
        return self.signal_symbol != self.symbol

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "security_id": self.security_id,
            "exchange_segment": self.exchange_segment,
            "instrument_type": self.instrument_type,
            "signal_symbol": self.signal_symbol,
            "signal_name": self.signal_name,
            "signal_security_id": self.signal_security_id,
            "signal_exchange_segment": self.signal_exchange_segment,
            "signal_instrument_type": self.signal_instrument_type,
            "reference_mode": self.reference_mode,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CashCascadeInstrument":
        return cls(
            symbol=str(payload.get("symbol") or ""),
            name=str(payload.get("name") or ""),
            security_id=str(payload.get("security_id") or ""),
            exchange_segment=str(payload.get("exchange_segment") or "NSE_EQ"),
            instrument_type=str(payload.get("instrument_type") or "EQUITY"),
            signal_symbol=str(payload.get("signal_symbol") or ""),
            signal_name=str(payload.get("signal_name") or ""),
            signal_security_id=str(payload.get("signal_security_id") or ""),
            signal_exchange_segment=str(payload.get("signal_exchange_segment") or "NSE_EQ"),
            signal_instrument_type=str(payload.get("signal_instrument_type") or "EQUITY"),
            reference_mode=str(payload.get("reference_mode") or "own_scrip"),
        )


@dataclass(frozen=True)
class CashCascadePaperConfig:
    capital_inr: float
    target_fraction: float = 0.25
    timeframe: str = "5m"
    product_type: str = "CNC"
    min_order_inr: float = 0.0
    cost_schedule: CashMarketCostSchedule = field(default_factory=CashMarketCostSchedule)

    def __post_init__(self) -> None:
        if self.capital_inr <= 0:
            raise CascadeError("capital_inr must be positive")
        if not 0 < self.target_fraction <= 1:
            raise CascadeError("target_fraction must be between 0 and 1")
        timeframe = str(self.timeframe or "5m").lower()
        if timeframe not in {"5m", "15m", "1h"}:
            raise CascadeError("cash Cascade timeframe must be 5m, 15m, or 1h")
        product = str(self.product_type or "CNC").upper()
        if product not in {"CNC", "MTF"}:
            raise CascadeError("cash Cascade paper mode supports CNC or MTF")
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "product_type", product)

    @property
    def capital_unit_per_pct(self) -> float:
        return self.capital_inr / 100.0


@dataclass
class CashCascadeRung:
    leg_id: int
    level: int
    signal_price: float
    budget_inr: float
    allocation_pct: float
    pool_inr: float
    status: str = "PENDING"  # PENDING | COLLECTED | FILLED | CLOSED | CANCELLED

    @property
    def key(self) -> str:
        return f"{self.leg_id}:{self.level}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "leg_id": self.leg_id,
            "level": self.level,
            "signal_price": self.signal_price,
            "budget_inr": self.budget_inr,
            "allocation_pct": self.allocation_pct,
            "pool_inr": self.pool_inr,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CashCascadeRung":
        return cls(
            leg_id=int(payload["leg_id"]),
            level=int(payload["level"]),
            signal_price=float(payload["signal_price"]),
            budget_inr=float(payload["budget_inr"]),
            allocation_pct=float(payload.get("allocation_pct") or 0),
            pool_inr=float(payload.get("pool_inr") or 0),
            status=str(payload.get("status") or "PENDING"),
        )


@dataclass(frozen=True)
class CashCascadeFill:
    timestamp: datetime
    signal_price: float
    trade_price: float
    quantity: int
    budget_inr: float
    spent_inr: float
    rung_keys: tuple[str, ...]
    order_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "signal_price": self.signal_price,
            "trade_price": self.trade_price,
            "quantity": self.quantity,
            "budget_inr": self.budget_inr,
            "spent_inr": self.spent_inr,
            "rung_keys": list(self.rung_keys),
            "order_id": self.order_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CashCascadeFill":
        return cls(
            timestamp=_as_ist(datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))),
            signal_price=float(payload["signal_price"]),
            trade_price=float(payload["trade_price"]),
            quantity=int(payload["quantity"]),
            budget_inr=float(payload.get("budget_inr") or 0),
            spent_inr=float(payload.get("spent_inr") or 0),
            rung_keys=tuple(str(key) for key in payload.get("rung_keys") or []),
            order_id=str(payload.get("order_id") or ""),
        )


@dataclass(frozen=True)
class CashCascadeRound:
    round_id: int
    opened_at: datetime
    closed_at: datetime
    fills: tuple[CashCascadeFill, ...]
    target_price: float
    exit_price: float
    exit_quantity: int
    gross_pnl: float
    costs: CashRoundCosts
    net_pnl: float
    exit_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "fills": [fill.to_dict() for fill in self.fills],
            "target_price": self.target_price,
            "exit_price": self.exit_price,
            "exit_quantity": self.exit_quantity,
            "gross_pnl": self.gross_pnl,
            "costs": _costs_to_dict(self.costs),
            "net_pnl": self.net_pnl,
            "exit_reason": self.exit_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CashCascadeRound":
        return cls(
            round_id=int(payload["round_id"]),
            opened_at=_as_ist(datetime.fromisoformat(str(payload["opened_at"]).replace("Z", "+00:00"))),
            closed_at=_as_ist(datetime.fromisoformat(str(payload["closed_at"]).replace("Z", "+00:00"))),
            fills=tuple(CashCascadeFill.from_dict(fill) for fill in payload.get("fills") or []),
            target_price=float(payload["target_price"]),
            exit_price=float(payload["exit_price"]),
            exit_quantity=int(payload["exit_quantity"]),
            gross_pnl=float(payload["gross_pnl"]),
            costs=_costs_from_dict(payload.get("costs") or {}),
            net_pnl=float(payload["net_pnl"]),
            exit_reason=str(payload.get("exit_reason") or "target"),
        )


def _costs_to_dict(costs: CashRoundCosts) -> dict[str, float]:
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


def _costs_from_dict(payload: Mapping[str, Any]) -> CashRoundCosts:
    return CashRoundCosts(
        buy_turnover=float(payload.get("buy_turnover") or 0),
        sell_turnover=float(payload.get("sell_turnover") or 0),
        brokerage=float(payload.get("brokerage") or 0),
        stt=float(payload.get("stt") or 0),
        exchange_transaction=float(payload.get("exchange_transaction") or 0),
        sebi=float(payload.get("sebi") or 0),
        stamp=float(payload.get("stamp") or 0),
        gst=float(payload.get("gst") or 0),
    )


class CashCascadePaperEngine:
    """Paper execution of Cascade for CNC-style cash instruments."""

    def __init__(
        self,
        mother_signal: IndexCandle,
        mother_trade: IndexCandle,
        instrument: CashCascadeInstrument,
        config: CashCascadePaperConfig,
    ) -> None:
        mother_signal = _normalise_candle(mother_signal)
        mother_trade = _normalise_candle(mother_trade)
        if mother_trade.high <= 0:
            raise CascadeError("traded mother candle must have a positive high")
        self.geometry = NiftyIndexCascadeGeometry(mother_signal)
        self.trade_mother = mother_trade
        self.trade_history: list[IndexCandle] = [mother_trade]
        self.instrument = instrument
        self.config = config
        self.rungs: dict[str, CashCascadeRung] = {}
        self.open_fills: list[CashCascadeFill] = []
        self.rounds: list[CashCascadeRound] = []
        self.pending_rung_keys: list[str] = []
        self.pending_inr = 0.0
        self.cash_carry_inr = 0.0
        self.pending_line: Optional[float] = None
        self.pending_last_red: Optional[float] = None
        self.pending_stop: Optional[float] = None
        self.pending_stop_timestamp: Optional[datetime] = None
        self.reuse_below: Optional[float] = None
        self.status = "WAITING"
        self.events: list[dict[str, Any]] = []

    @staticmethod
    def normalise_frame(frame: Any, now: Optional[datetime] = None, *, timeframe_minutes: int = 5) -> list[IndexCandle]:
        if frame is None or getattr(frame, "empty", False):
            return []
        now_ist = _as_ist(now or datetime.now(IST))
        closed: list[IndexCandle] = []
        for timestamp, row in frame.iterrows():
            candle_time = timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
            candle_time = _as_ist(candle_time)
            bar_minutes = 15 if timeframe_minutes == 60 and candle_time.time().hour == 15 else timeframe_minutes
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

    @property
    def open_quantity(self) -> int:
        return sum(fill.quantity for fill in self.open_fills)

    @property
    def open_invested_inr(self) -> float:
        return round(sum(fill.spent_inr for fill in self.open_fills), 2)

    @property
    def average_entry_price(self) -> Optional[float]:
        quantity = self.open_quantity
        if quantity <= 0:
            return None
        return sum(fill.trade_price * fill.quantity for fill in self.open_fills) / quantity

    @property
    def target_price(self) -> Optional[float]:
        average = self.average_entry_price
        if average is None:
            return None
        return average + self.config.target_fraction * (self.trade_mother.high - average)

    def _log(self, candle: IndexCandle, event: str, **payload: Any) -> None:
        self.events.append({"timestamp": candle.timestamp.isoformat(), "event": event, **payload})

    def _allocation_pct_for_leg(self, leg: Any) -> float:
        mother_high = self.geometry.campaign.mother_high
        if mother_high <= 0:
            return 0.0
        if leg.leg_id <= 1:
            return max((mother_high - leg.low) / mother_high * 100.0, 0.0)
        prior = self.geometry.campaign.legs[leg.leg_id - 2]
        return max((prior.low - leg.low) / prior.low * 100.0, 0.0) if prior.low > 0 else 0.0

    def _sync_new_rungs(self, signal_candle: IndexCandle) -> None:
        for leg in self.geometry.campaign.legs:
            allocation_pct = self._allocation_pct_for_leg(leg)
            pool_inr = round(allocation_pct * self.config.capital_unit_per_pct, 2)
            for level in GEOMETRY_FIB_LEVELS:
                key = f"{leg.leg_id}:{level}"
                if key in self.rungs:
                    continue
                budget = round(pool_inr * LEVEL_ALLOCATION[level], 2)
                rung = CashCascadeRung(
                    leg_id=leg.leg_id,
                    level=level,
                    signal_price=leg.fib.level_price(level),
                    budget_inr=budget,
                    allocation_pct=allocation_pct,
                    pool_inr=pool_inr,
                )
                self.rungs[key] = rung
                self._log(
                    signal_candle,
                    "rung_created",
                    rung=key,
                    signal_price=rung.signal_price,
                    budget_inr=rung.budget_inr,
                    allocation_pct=round(allocation_pct, 4),
                )

    def _collect_crossed_rungs(self, signal_candle: IndexCandle, trade_candle: IndexCandle) -> None:
        crossed = [
            rung
            for rung in self.rungs.values()
            if rung.status == "PENDING" and rung.budget_inr > 0 and signal_candle.low <= rung.signal_price
        ]
        if not crossed:
            return
        minimum = max(float(self.config.min_order_inr or 0), float(trade_candle.close or 0))
        for rung in sorted(crossed, key=lambda row: -row.signal_price):
            rung.status = "COLLECTED"
            self.pending_rung_keys.append(rung.key)
            self.pending_inr = round(self.pending_inr + rung.budget_inr, 2)
            total = round(self.pending_inr + self.cash_carry_inr, 2)
            if self.pending_line is None and total + 1e-9 >= minimum:
                self.pending_line = rung.signal_price
                self._log(
                    signal_candle,
                    "cash_placeable",
                    rung=rung.key,
                    signal_price=rung.signal_price,
                    pending_inr=self.pending_inr,
                    carry_inr=self.cash_carry_inr,
                    estimated_trade_price=trade_candle.close,
                )
            else:
                self._log(
                    signal_candle,
                    "rung_collected",
                    rung=rung.key,
                    signal_price=rung.signal_price,
                    pending_inr=self.pending_inr,
                    carry_inr=self.cash_carry_inr,
                    minimum_inr=round(minimum, 2),
                )

    def _advance_stop(self, signal_candle: IndexCandle) -> None:
        if (
            self.pending_line is None
            or self.pending_inr + self.cash_carry_inr <= 0
            or not signal_candle.is_red
            or signal_candle.close >= self.pending_line
        ):
            return
        if self.pending_last_red is None:
            self.pending_last_red = signal_candle.close
            self._log(signal_candle, "await_second_red", line=self.pending_line, close=signal_candle.close)
            return
        if signal_candle.close >= self.pending_last_red:
            return
        first_stop = self.pending_stop is None
        self.pending_stop = self.pending_last_red
        self.pending_stop_timestamp = signal_candle.timestamp
        self.pending_last_red = signal_candle.close
        self.status = "ARMED"
        self._log(signal_candle, "stop_armed" if first_stop else "stop_moved", trigger=self.pending_stop)

    def _fill_pending_stop(self, signal_candle: IndexCandle, trade_candle: IndexCandle) -> None:
        stop_timestamp = _as_ist(self.pending_stop_timestamp) if self.pending_stop_timestamp else None
        if (
            self.pending_stop is None
            or stop_timestamp is None
            or signal_candle.timestamp <= stop_timestamp
            or signal_candle.high < self.pending_stop
        ):
            return
        trade_price = float(trade_candle.close or 0)
        total_budget = round(self.pending_inr + self.cash_carry_inr, 2)
        room = max(self.config.capital_inr - self.open_invested_inr, 0.0)
        usable = min(total_budget, room)
        quantity = cash_budget_to_quantity(usable, trade_price)
        if quantity <= 0:
            self.status = "AWAITING_CASH"
            self._log(
                signal_candle,
                "cash_below_one_share",
                pending_inr=self.pending_inr,
                carry_inr=self.cash_carry_inr,
                trade_price=trade_price,
            )
            return
        spent = round(quantity * trade_price, 2)
        order_id = f"paper-cash-cascade-{len(self.open_fills) + len(self.rounds) + 1}"
        fill = CashCascadeFill(
            timestamp=signal_candle.timestamp,
            signal_price=self.pending_stop,
            trade_price=trade_price,
            quantity=quantity,
            budget_inr=usable,
            spent_inr=spent,
            rung_keys=tuple(self.pending_rung_keys),
            order_id=order_id,
        )
        self.open_fills.append(fill)
        for key in self.pending_rung_keys:
            if key in self.rungs:
                self.rungs[key].status = "FILLED"
        self.cash_carry_inr = round(max(total_budget - spent, 0.0), 2)
        self.pending_rung_keys = []
        self.pending_inr = 0.0
        self.pending_line = None
        self.pending_last_red = None
        self.pending_stop = None
        self.pending_stop_timestamp = None
        self.status = "OPEN"
        self._log(
            signal_candle,
            "paper_cash_fill",
            trade_symbol=self.instrument.symbol,
            quantity=quantity,
            trade_price=trade_price,
            spent_inr=spent,
            carry_inr=self.cash_carry_inr,
            target_price=self.target_price,
        )

    def _release_closed_rungs(self, signal_candle: IndexCandle) -> None:
        if self.reuse_below is None or signal_candle.low >= self.reuse_below:
            return
        released = [rung for rung in self.rungs.values() if rung.status == "CLOSED"]
        self.reuse_below = None
        for rung in released:
            rung.status = "PENDING"
        if released:
            self._log(signal_candle, "new_low_restart", rungs=[rung.key for rung in released], low=signal_candle.low)

    def _close_round(self, signal_candle: IndexCandle, trade_candle: IndexCandle, *, reason: str, price: float) -> None:
        if not self.open_fills:
            return
        quantity = self.open_quantity
        costs = calculate_cash_round_costs(
            buys=[(fill.trade_price, fill.quantity) for fill in self.open_fills],
            sell_price=price,
            sell_quantity=quantity,
            schedule=self.config.cost_schedule,
        )
        gross = round(sum((price - fill.trade_price) * fill.quantity for fill in self.open_fills), 2)
        round_row = CashCascadeRound(
            round_id=len(self.rounds) + 1,
            opened_at=self.open_fills[0].timestamp,
            closed_at=trade_candle.timestamp,
            fills=tuple(self.open_fills),
            target_price=price,
            exit_price=price,
            exit_quantity=quantity,
            gross_pnl=gross,
            costs=costs,
            net_pnl=round(gross - costs.total, 2),
            exit_reason=reason,
        )
        self.rounds.append(round_row)
        for rung in self.rungs.values():
            if rung.status == "FILLED":
                rung.status = "CLOSED"
        self.open_fills = []
        lows = [row.low for row in self.geometry.history]
        self.reuse_below = min(lows) if lows else signal_candle.low
        self.status = "ROUND_CLOSED"
        self._log(
            signal_candle,
            "round_closed",
            reason=reason,
            exit_price=price,
            gross_pnl=round_row.gross_pnl,
            costs=round_row.costs.total,
            net_pnl=round_row.net_pnl,
            reuse_below=self.reuse_below,
        )

    def _check_exit(self, signal_candle: IndexCandle, trade_candle: IndexCandle) -> None:
        if not self.open_fills:
            return
        target = self.target_price
        newest = max(fill.timestamp for fill in self.open_fills)
        if target is not None and trade_candle.timestamp > newest and trade_candle.high >= target:
            self._close_round(signal_candle, trade_candle, reason="target", price=round(target, 2))

    def kill_and_close(self, signal_candle: IndexCandle, trade_candle: IndexCandle) -> dict[str, Any]:
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
            self._close_round(signal_candle, trade_candle, reason="manual_kill", price=float(trade_candle.close))
        self.status = "KILLED"
        self._log(signal_candle, "campaign_killed", cancelled_rungs=cancelled)
        return {"closed": True, "cancelled_rungs": cancelled, "reason": "manual_kill"}

    def on_candle(self, signal_candle: IndexCandle, trade_candle: IndexCandle) -> None:
        signal_candle = _normalise_candle(signal_candle)
        trade_candle = _normalise_candle(trade_candle)
        if not is_nse_cash_session(signal_candle.timestamp):
            return
        self.geometry.on_candle(signal_candle)
        if trade_candle.timestamp > self.trade_history[-1].timestamp:
            self.trade_history.append(trade_candle)
        self._sync_new_rungs(signal_candle)
        self._fill_pending_stop(signal_candle, trade_candle)
        self._check_exit(signal_candle, trade_candle)
        self._release_closed_rungs(signal_candle)
        self._collect_crossed_rungs(signal_candle, trade_candle)
        self._advance_stop(signal_candle)

    def run(self, pairs: Iterable[tuple[IndexCandle, IndexCandle]]) -> "CashCascadePaperEngine":
        for signal_candle, trade_candle in sorted(pairs, key=lambda row: row[0].timestamp):
            self.on_candle(signal_candle, trade_candle)
        return self

    def get_status(self) -> dict[str, Any]:
        return {
            "mode": "paper",
            "running": self.status not in {"STOPPED", "KILLED", "MOTHER_BROKEN", "MOTHER_RETESTED"},
            "status": self.status,
            "instrument": self.instrument.to_dict(),
            "config": {
                "capital_inr": self.config.capital_inr,
                "target_fraction": self.config.target_fraction,
                "timeframe": self.config.timeframe,
                "product_type": self.config.product_type,
                "min_order_inr": self.config.min_order_inr,
            },
            "mother": {
                "signal": _candle_to_dict(self.geometry.history[0]),
                "trade": _candle_to_dict(self.trade_mother),
                "geometry_state": self.geometry.campaign.state,
            },
            "average_entry_price": self.average_entry_price,
            "target_price": self.target_price,
            # The newest traded candle close is the mark the UI values open
            # positions at; the timestamp says how stale that mark is.
            "last_trade_close": float(self.trade_history[-1].close),
            "last_trade_timestamp": self.trade_history[-1].timestamp.isoformat(),
            "open_quantity": self.open_quantity,
            "open_invested_inr": self.open_invested_inr,
            "pending_inr": self.pending_inr,
            "cash_carry_inr": self.cash_carry_inr,
            "pending_stop": self.pending_stop,
            "reuse_below": self.reuse_below,
            "rungs": [rung.to_dict() for rung in sorted(self.rungs.values(), key=lambda row: (row.leg_id, row.level))],
            "open_fills": [fill.to_dict() for fill in self.open_fills],
            "rounds": [row.to_dict() for row in self.rounds],
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
        return {
            "version": 1,
            "instrument": self.instrument.to_dict(),
            "config": {
                "capital_inr": self.config.capital_inr,
                "target_fraction": self.config.target_fraction,
                "timeframe": self.config.timeframe,
                "product_type": self.config.product_type,
                "min_order_inr": self.config.min_order_inr,
                "cost_schedule": dict(self.config.cost_schedule.__dict__),
            },
            "signal_history": [_candle_to_dict(row) for row in self.geometry.history],
            "trade_history": [_candle_to_dict(row) for row in self.trade_history],
            "trade_mother": _candle_to_dict(self.trade_mother),
            "rungs": [rung.to_dict() for rung in self.rungs.values()],
            "open_fills": [fill.to_dict() for fill in self.open_fills],
            "rounds": [row.to_dict() for row in self.rounds],
            "pending_rung_keys": list(self.pending_rung_keys),
            "pending_inr": self.pending_inr,
            "cash_carry_inr": self.cash_carry_inr,
            "pending_line": self.pending_line,
            "pending_last_red": self.pending_last_red,
            "pending_stop": self.pending_stop,
            "pending_stop_timestamp": self.pending_stop_timestamp.isoformat() if self.pending_stop_timestamp else None,
            "reuse_below": self.reuse_below,
            "status": self.status,
            "events": list(self.events[-100:]),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CashCascadePaperEngine":
        signal_history = [_candle_from_dict(row) for row in payload.get("signal_history") or []]
        trade_history = [_candle_from_dict(row) for row in payload.get("trade_history") or []]
        if not signal_history or not trade_history:
            raise CascadeError("Cannot restore cash Cascade without signal and trade history")
        raw_config = payload.get("config") or {}
        raw_schedule = raw_config.get("cost_schedule") or {}
        config = CashCascadePaperConfig(
            capital_inr=float(raw_config.get("capital_inr") or 0),
            target_fraction=float(raw_config.get("target_fraction") or 0.25),
            timeframe=str(raw_config.get("timeframe") or "5m"),
            product_type=str(raw_config.get("product_type") or "CNC"),
            min_order_inr=float(raw_config.get("min_order_inr") or 0),
            cost_schedule=CashMarketCostSchedule(**raw_schedule) if raw_schedule else CashMarketCostSchedule(),
        )
        engine = cls(
            signal_history[0],
            _candle_from_dict(payload.get("trade_mother") or _candle_to_dict(trade_history[0])),
            CashCascadeInstrument.from_dict(payload.get("instrument") or {}),
            config,
        )
        for candle in signal_history[1:]:
            engine.geometry.on_candle(candle)
        engine.trade_history = trade_history
        engine.rungs = {
            rung.key: rung for rung in (CashCascadeRung.from_dict(row) for row in payload.get("rungs") or [])
        }
        engine.open_fills = [CashCascadeFill.from_dict(row) for row in payload.get("open_fills") or []]
        engine.rounds = [CashCascadeRound.from_dict(row) for row in payload.get("rounds") or []]
        engine.pending_rung_keys = [str(key) for key in payload.get("pending_rung_keys") or []]
        engine.pending_inr = float(payload.get("pending_inr") or 0)
        engine.cash_carry_inr = float(payload.get("cash_carry_inr") or 0)
        engine.pending_line = float(payload["pending_line"]) if payload.get("pending_line") not in (None, "") else None
        engine.pending_last_red = (
            float(payload["pending_last_red"]) if payload.get("pending_last_red") not in (None, "") else None
        )
        engine.pending_stop = float(payload["pending_stop"]) if payload.get("pending_stop") not in (None, "") else None
        raw_stop_ts = payload.get("pending_stop_timestamp")
        engine.pending_stop_timestamp = (
            _as_ist(datetime.fromisoformat(str(raw_stop_ts).replace("Z", "+00:00"))) if raw_stop_ts else None
        )
        engine.reuse_below = float(payload["reuse_below"]) if payload.get("reuse_below") not in (None, "") else None
        engine.status = str(payload.get("status") or "WAITING")
        engine.events = list(payload.get("events") or [])
        return engine
