"""
engine/cascade_fib_boundary.py -- the manual-mother fib-boundary Cascade.

A sibling of :class:`OneHourCascade` (the candle-entry strategy).  Both fill on
the same mechanic -- two same-colour closes arm a stop, the stop fills when
price recovers back through it -- but they decide *where* a buy may happen very
differently:

  * OneHourCascade arms at every marked low, laddering as deep as price walks.
  * This engine arms ONLY at two fixed, deep fib boundaries derived straight
    from the mother candle you type in: level 4 and level 8, where
        CE:  price = mother_high - level * (mother_high - mother_low)
        PE:  price = mother_low  + level * (mother_high - mother_low)
    Buys begin once price trades beyond the level-4 line and ladder to level 8.
    Deep levels are cheaper premium and snap back to target faster.

Lifecycle is one-and-done: when the averaged position reaches 0.25 of the way
back toward the mother high (CE) or low (PE), the whole basket closes and the
campaign ENDS.  There is no new-low re-arm.

The mother candle is given by hand (high + low + the timeframe it was read on),
so this engine never detects a swing or draws a trendline.  It only prices the
decisions those two typed numbers imply, exactly like the rest of the cascade
stack: geometry from the index, premium only moves P&L.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from cascade_costs import (
    NiftyOptionCostSchedule,
    OptionCostFill,
    calculate_nifty_option_basket_round_costs,
)
from engine.cascade_fib_geometry import (
    DEEP_FIB_BOUNDARIES,
    STRUCTURAL_FIB_BOUNDARIES,
    boundaries_for_timeframe,
    boundary_price,
)
from engine.cascade_options import (
    Candle,
    CascadeError,
    CascadeResult,
    Contract,
    Entry,
    NiftyContractResolver,
    OptionLookup,
)

# Re-exported so existing importers keep working. The deep boundaries Phil chose
# now live (with the timeframe rule) in engine.cascade_fib_geometry, shared with
# the paper engine so both price the same lines.
DEFAULT_FIB_BOUNDARIES: tuple[int, ...] = DEEP_FIB_BOUNDARIES
__all__ = [
    "FibBoundaryCascade",
    "FibBoundaryConfig",
    "DEFAULT_FIB_BOUNDARIES",
    "DEEP_FIB_BOUNDARIES",
    "STRUCTURAL_FIB_BOUNDARIES",
    "boundaries_for_timeframe",
]


@dataclass(frozen=True)
class FibBoundaryConfig:
    """Everything the fib-boundary campaign needs, all from the typed mother."""

    mother_timestamp: datetime
    mother_high: float
    mother_low: float
    option_type: str = "CE"  # CE or PE
    timeframe: str = "5m"  # 1m, 5m, 15m or 1h
    # None -> derive from the timeframe: (8,) on 1m, (4, 8) everywhere else.
    # An explicit tuple overrides that (tests and future retuning).
    boundaries: Optional[tuple[int, ...]] = None
    rung_inr: float = 75_000.0
    itm_steps: int = 2  # ATM-2
    strike_step: float = 50.0
    lot_size: int = 65
    target_fraction: float = 0.25
    # Monthly contracts only, 15-45 DTE -- see `CascadeConfig.monthly_only` and
    # `min_dte` for the measurements behind both numbers.  No stop loss means
    # expiry is the only thing that can end a losing position, so the floor is
    # what keeps a campaign from being born with too little road.
    monthly_only: bool = True
    min_dte: int = 15
    max_dte: int = 45
    strict_option_data: bool = True
    force_exit_on_expiry: bool = True
    slippage_points: float = 0.0
    option_slippage_pct: float = 0.0
    cost_schedule: Optional[NiftyOptionCostSchedule] = None

    def __post_init__(self) -> None:
        side = str(self.option_type).upper()
        if side not in {"CE", "PE"}:
            raise CascadeError("option_type must be CE or PE")
        if self.mother_high <= self.mother_low:
            raise CascadeError("mother_high must be greater than mother_low")
        if self.timeframe.lower() not in {"1m", "5m", "15m", "1h"}:
            raise CascadeError("timeframe must be 1m, 5m, 15m or 1h")
        # A None boundaries means "use the timeframe's ladder"; resolve it now so
        # everything downstream sees a concrete, frozen tuple.
        if self.boundaries is None:
            object.__setattr__(self, "boundaries", boundaries_for_timeframe(self.timeframe))
        if not self.boundaries or any(int(level) <= 0 for level in self.boundaries):
            raise CascadeError("boundaries must be positive fib multipliers")
        if self.rung_inr <= 0:
            raise CascadeError("rung_inr must be positive")
        if self.lot_size <= 0 or self.itm_steps < 0 or self.strike_step <= 0:
            raise CascadeError("lot_size/itm_steps/strike_step must be valid")
        if not 0 < self.target_fraction <= 1:
            raise CascadeError("target_fraction must be between 0 and 1")

    @property
    def range(self) -> float:
        return self.mother_high - self.mother_low

    def boundary_price(self, level: int) -> float:
        """Index price of a fib boundary, on the side the option trades."""
        return boundary_price(self.option_type, self.mother_high, self.mother_low, level)

    def lots_for_entry(self, entry_index: int) -> int:
        """Lots for the Nth fill (0-based): a fixed 1, 2, 3... ladder.

        The buy count is the sizing -- no rupee budget, no premium division.
        First buy takes 1 lot, the second 2, the third 3, and so on.
        """
        return int(entry_index) + 1

    def ordered_boundaries(self) -> list[int]:
        """Boundaries in the order price reaches them (shallow-first)."""
        # CE prices fall (level 4 sits above level 8); PE prices rise (level 4
        # below level 8). Sorting by the multiplier gives shallow-first for both.
        return sorted(self.boundaries)


@dataclass
class _RungState:
    level: int
    index_price: float
    status: str = "PENDING"  # PENDING -> ARMED -> FILLED


class FibBoundaryCascade:
    """Replays one manual-mother fib-boundary campaign over closed candles.

    Geometry (the two boundaries and the target) is fixed by the typed mother.
    ``option_lookup`` supplies a real premium bar for the fixed contract at a
    given minute; a missing bar is a recorded gap, never a fabricated price.
    """

    def __init__(
        self,
        config: FibBoundaryConfig,
        resolver: NiftyContractResolver,
        option_lookup: OptionLookup,
    ) -> None:
        self.config = config
        self.resolver = resolver
        self.option_lookup = option_lookup
        self._side = str(config.option_type).upper()

        self.result = CascadeResult(status="waiting")
        self.rungs: list[_RungState] = [
            _RungState(level=level, index_price=config.boundary_price(level)) for level in config.ordered_boundaries()
        ]

        # The contract for the currently-armed rung.  Each entry re-selects its
        # own strike against the index at that depth (ATM-N slides down as NIFTY
        # falls), so a campaign holds several strikes -- not one fixed contract.
        self._pending_contract: Optional[Contract] = None
        # 2-candle trigger bookkeeping.
        self._streak = 0
        self._prev_close: Optional[float] = None
        # An armed buy-stop waiting to fill.
        self._pending_rung: Optional[_RungState] = None
        self._pending_trigger: Optional[float] = None
        self._pending_since: Optional[datetime] = None
        self._last_entry_timestamp: Optional[datetime] = None

    # ── geometry helpers ──────────────────────────────────────────

    def _beyond(self, price: float, level_price: float) -> bool:
        """Is price past a boundary in the working direction?"""
        return price <= level_price if self._side == "CE" else price >= level_price

    def _qualifies(self, candle: Candle) -> bool:
        return candle.is_red if self._side == "CE" else candle.is_green

    @property
    def _open_entries(self) -> list[Entry]:
        return self.result.entries

    def _average_spot(self) -> Optional[float]:
        entries = self._open_entries
        quantity = sum(entry.quantity for entry in entries)
        if quantity <= 0:
            return None
        return sum(entry.spot * entry.quantity for entry in entries) / quantity

    def _target(self) -> Optional[float]:
        average = self._average_spot()
        if average is None:
            return None
        if self._side == "CE":
            return average + self.config.target_fraction * (self.config.mother_high - average)
        return average - self.config.target_fraction * (average - self.config.mother_low)

    def _next_rung(self) -> Optional[_RungState]:
        for rung in self.rungs:
            if rung.status == "PENDING":
                return rung
        return None

    # ── candle processing ─────────────────────────────────────────

    def _log(self, timestamp: datetime, event: str, **payload) -> None:
        self.result.events.append({"timestamp": timestamp.isoformat(), "event": event, **payload})

    def _lookup_option(self, timestamp: datetime, contract: Contract):
        return self.option_lookup(timestamp, contract)

    def _arm_if_qualified(self, candle: Candle) -> None:
        """Count the two-candle streak and arm a stop at the next boundary."""
        if not self._qualifies(candle):
            self._streak = 0
            self._prev_close = candle.close
            return
        self._streak += 1
        rung = self._next_rung()
        # Two same-colour closes AND price has reached beyond the shallowest
        # unfilled boundary -> place a buy-stop at this close, exactly the
        # OneHourCascade mechanic, but only inside the deep fib zone.
        if rung is not None and self._streak >= 2 and self._beyond(candle.close, rung.index_price):
            self._pending_rung = rung
            self._pending_trigger = candle.close
            self._pending_since = candle.timestamp
            rung.status = "ARMED"
            # Re-select the strike for THIS rung against the current index, so a
            # deeper entry buys a lower CE strike (higher PE) as the index moves.
            self._pending_contract = self.resolver.select(
                candle.timestamp, candle.close, self._side, self._contract_config()
            )
            self._log(
                candle.timestamp,
                "arm",
                level=rung.level,
                boundary=round(rung.index_price, 2),
                trigger=round(candle.close, 2),
                strike=self._pending_contract.strike,
                expiry=self._pending_contract.expiry.isoformat(),
            )
        self._prev_close = candle.close

    def _try_fill(self, candle: Candle) -> bool:
        if (
            self._pending_rung is None
            or self._pending_trigger is None
            or self._pending_contract is None
            or candle.timestamp <= (self._pending_since or candle.timestamp)
        ):
            return False
        # Buy-stop fills when price recovers back through the trigger.
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

        premium = option_bar.open * (1.0 + self.config.option_slippage_pct) if option_bar is not None else None
        # Fixed lot ladder: 1 lot on the first buy, 2 on the second, 3 on the
        # third...  No rupee budget -- the count IS the sizing.
        lots = self.config.lots_for_entry(len(self.result.entries))
        quantity = lots * self.config.lot_size
        fill_index = float(self._pending_trigger) + (
            self.config.slippage_points if self._side == "CE" else -self.config.slippage_points
        )
        entry = Entry(
            candle.timestamp,
            fill_index,
            premium,
            lots,
            quantity,
            self._pending_contract,
            self._pending_rung.level,
        )
        self.result.entries.append(entry)
        self._pending_rung.status = "FILLED"
        self._last_entry_timestamp = candle.timestamp
        self.result.status = "open"
        self._log(
            candle.timestamp,
            "fill",
            level=self._pending_rung.level,
            lots=lots,
            quantity=quantity,
            index=round(fill_index, 2),
            premium=premium,
        )
        self._pending_rung = None
        self._pending_trigger = None
        self._pending_since = None
        self._pending_contract = None
        self._streak = 0
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
        prices = [self._exit_premium(candle, entry) for entry in self.result.entries]
        self._settle(prices)
        self.result.status = "closed"
        self.result.exit_reason = "target"
        self.result.exit_timestamp = candle.timestamp
        self.result.target_index = target
        self.result.average_spot = self._average_spot()
        if self.result.average_spot is not None:
            self.result.index_move = target - self.result.average_spot
        self._log(candle.timestamp, "target", target=round(target, 2), reason="target")
        return True

    def _try_expiry_exit(self, candle: Candle) -> bool:
        if not self.config.force_exit_on_expiry or not self.result.entries:
            return False
        expiry = min(entry.contract.expiry for entry in self.result.entries)
        if candle.timestamp.date() < expiry:
            return False
        from datetime import time as dt_time

        if candle.timestamp.date() == expiry and candle.timestamp.time() < dt_time(15, 15):
            return False
        # An option at expiry is worth intrinsic value, which the index settles
        # exactly -- no premium history needed for the loss to be real.
        prices: list[Optional[float]] = []
        for entry in self.result.entries:
            if entry.contract.option_type == "CE":
                prices.append(max(candle.close - entry.contract.strike, 0.0))
            else:
                prices.append(max(entry.contract.strike - candle.close, 0.0))
        self._settle(prices)
        self.result.status = "expired"
        self.result.exit_reason = "expiry_square_off"
        self.result.exit_timestamp = candle.timestamp
        self.result.average_spot = self._average_spot()
        self._log(candle.timestamp, "expiry_exit", expiry=expiry.isoformat(), reason="expiry_square_off")
        return True

    def _exit_premium(self, candle: Candle, entry: Entry) -> Optional[float]:
        bar = self._lookup_option(candle.timestamp, entry.contract)
        if bar is None:
            gap = f"missing exit option candle at {candle.timestamp.isoformat()} for {entry.contract.key}"
            self.result.data_gaps.append(gap)
            return None
        return bar.open

    def _settle(self, option_prices: list[Optional[float]]) -> None:
        entries = self.result.entries
        self.result.exit_option_prices = list(option_prices)
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

    def _contract_config(self):
        """Adapt this engine's config to the shape NiftyContractResolver.select
        reads (it only needs itm_steps, strike_step, DTE window and side)."""
        return _ResolverView(self.config)

    # ── entry point ───────────────────────────────────────────────

    def run(self, candles: list[Candle]) -> CascadeResult:
        ordered = sorted(candles, key=lambda c: c.timestamp)
        for candle in ordered:
            if candle.timestamp <= self.config.mother_timestamp:
                self._prev_close = candle.close
                continue
            if self.result.status in {"closed", "expired", "data_gap"}:
                break
            # Fill any armed stop first, then look for a new arm, then exits.
            self._try_fill(candle)
            self._arm_if_qualified(candle)
            if self._try_exit(candle) or self._try_expiry_exit(candle):
                break
        if self.result.status in {"waiting", "armed"} and not self.result.entries:
            self.result.status = "no_entry"
        if self.result.entries and self.result.status == "open":
            self.result.target_index = self._target()
            self.result.average_spot = self._average_spot()
        return self.result


@dataclass(frozen=True)
class _ResolverView:
    """Read-only adapter exposing the handful of fields the resolver reads,
    so FibBoundaryConfig doesn't have to inherit CascadeConfig's whole surface."""

    _cfg: FibBoundaryConfig

    @property
    def itm_steps(self) -> int:
        return self._cfg.itm_steps

    @property
    def strike_step(self) -> float:
        return self._cfg.strike_step

    @property
    def min_dte(self) -> int:
        return self._cfg.min_dte

    @property
    def max_dte(self) -> int:
        return self._cfg.max_dte

    @property
    def monthly_only(self) -> bool:
        return self._cfg.monthly_only
