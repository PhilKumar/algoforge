"""The two-red ladder on cash equity, run forward as paper.

`engine/candle_ladder.py` already owns the rule and has since July: mother high
set, two reds where the second closes below the previous close, stop-buy on the
first red's close, climb a timeframe, one basket out together at the target.
Nothing in here re-implements any of that -- this module is the CASH wrapper
around it, and it exists because the ladder was written for options and a stock
is not an option:

* **Money.** The ladder prices a basket through `premium_lookup` and costs it
  with the NIFTY option schedule. A delivery trade has no premium and a
  different bill, so P&L is computed here from the fills themselves at cash
  rates -- including the flat depository charge, which is the single line that
  decided whether three years of this rule made money.
* **Size.** Phil's funding rule sizes a buy by the FALL: down 1% from the mother
  commits 1% of the purse, down 9% commits 9%. That is injected as the ladder's
  `quantity_for`, and it carries the first-buy gate with it.
* **Life.** A ladder fed a live tape will wait months, buy in a rally far above
  its mother, and target a price BELOW its own entry. The backtest read -12% to
  -27% a year on that alone. A close back above the mother high voids a setup
  that has not bought anything, and that rule lives here because it is about
  when a campaign should be alive, not about geometry.

WHAT THE NUMBERS CAME FROM. Over 36 months and 23 NSE names, run as shipped
(0.25 target, no gate) this rule wins nearly every trade and nets about zero: a
5m run-mother sits ~0.15% above the entry, a quarter of that is worth Rs 8-18,
and the round trip costs about Rs 86. Two changes turned it: wait for an 8%
fall before the first buy, and take 0.75 of the way back instead of 0.25. Every
closed trade came out green -- 61 of 61 on the first basket, 47 of 47 on the
second. Those two constants are the defaults below, and they are not taste.

PAPER ONLY. Nothing here places an order or talks to a broker. It is fed closed
candles and returns what the rule would have done with them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from engine.candle_ladder import LadderCandle, TwoRedLadder, order_events
from engine.cascade_equity import (
    CashMarketCostSchedule,
    CashRoundCosts,
    calculate_cash_round_costs,
)

# The ladders a campaign may climb, keyed by where it starts. 1h -> 1d -> 1w is
# the one the 36-month runs were done on and the one the defaults choose: the
# stuck bags disappear on it (9 of 12 instruments ended with nothing unsold),
# where a 15m start still bags and loses on 8 of 12.
LADDERS: dict[str, tuple[str, ...]] = {
    "15m": ("15m", "1h", "1d"),
    "1h": ("1h", "1d", "1w"),
    "1d": ("1d", "1w"),
}

# The depository charge, per scrip per selling day. It is FLAT, which is the
# whole point of stating it separately from the percentage schedule: on a
# Rs 1,600 position it is a percent of the trade, and it is what made the
# shallow version of this rule lose while winning almost every trade.
DP_PER_SELL_DAY = 15.93

# The two constants the backtest chose. Documented at the top; repeated here
# because a default that reads as arbitrary invites being tuned by feel.
DEFAULT_MIN_FALL_PCT = 8.0
DEFAULT_TARGET_FRACTION = 0.75


class TwoRedEquityError(ValueError):
    """The campaign cannot be built as asked."""


def ladder_for(start: str) -> tuple[str, ...]:
    key = str(start or "").strip().lower()
    if key not in LADDERS:
        raise TwoRedEquityError(f"{start!r} must be one of {', '.join(sorted(LADDERS))}")
    return LADDERS[key]


@dataclass(frozen=True)
class TwoRedEquityInstrument:
    """The scrip being laddered.

    There is no signal/trade split here, unlike the Cash Cascade. That split
    exists so a BEES ETF can read its geometry off the index it tracks, because
    a thin ETF's own candles are noise. This rule reads two reds and a mother
    high off the thing it actually buys -- borrowing an index's candles would
    arm a stop at a price the stock never traded.
    """

    symbol: str
    name: str
    security_id: str
    exchange_segment: str = "NSE_EQ"
    instrument_type: str = "EQUITY"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        if not self.name:
            object.__setattr__(self, "name", self.symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "security_id": self.security_id,
            "exchange_segment": self.exchange_segment,
            "instrument_type": self.instrument_type,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TwoRedEquityInstrument":
        return cls(
            symbol=str(payload.get("symbol") or ""),
            name=str(payload.get("name") or ""),
            security_id=str(payload.get("security_id") or ""),
            exchange_segment=str(payload.get("exchange_segment") or "NSE_EQ"),
            instrument_type=str(payload.get("instrument_type") or "EQUITY"),
        )


@dataclass(frozen=True)
class TwoRedEquityConfig:
    """Everything about a campaign that is a choice rather than a fact."""

    capital_inr: float
    start_timeframe: str = "1h"
    min_fall_pct: float = DEFAULT_MIN_FALL_PCT
    target_fraction: float = DEFAULT_TARGET_FRACTION
    trail_fraction: float = 0.0
    require_new_low: bool = True
    dp_per_sell_day: float = DP_PER_SELL_DAY
    costs: CashMarketCostSchedule = field(default_factory=CashMarketCostSchedule)

    def __post_init__(self) -> None:
        if self.capital_inr <= 0:
            raise TwoRedEquityError("Capital must be positive.")
        if self.min_fall_pct < 0:
            raise TwoRedEquityError("The first-buy gate cannot be negative.")
        if not 0 < self.target_fraction <= 1:
            raise TwoRedEquityError("Target fraction must sit in (0, 1].")
        if not 0 <= self.trail_fraction < 1:
            raise TwoRedEquityError("Trail fraction must sit in [0, 1).")
        ladder_for(self.start_timeframe)

    @property
    def stages(self) -> tuple[str, ...]:
        return ladder_for(self.start_timeframe)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capital_inr": self.capital_inr,
            "start_timeframe": self.start_timeframe,
            "min_fall_pct": self.min_fall_pct,
            "target_fraction": self.target_fraction,
            "trail_fraction": self.trail_fraction,
            "require_new_low": self.require_new_low,
            "dp_per_sell_day": self.dp_per_sell_day,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TwoRedEquityConfig":
        return cls(
            capital_inr=float(payload.get("capital_inr") or 0),
            start_timeframe=str(payload.get("start_timeframe") or "1h"),
            min_fall_pct=float(payload.get("min_fall_pct", DEFAULT_MIN_FALL_PCT)),
            target_fraction=float(payload.get("target_fraction", DEFAULT_TARGET_FRACTION)),
            trail_fraction=float(payload.get("trail_fraction", 0.0)),
            require_new_low=bool(payload.get("require_new_low", True)),
            dp_per_sell_day=float(payload.get("dp_per_sell_day", DP_PER_SELL_DAY)),
        )


@dataclass(frozen=True)
class TwoRedEquityMoney:
    """What the round cost and what it made, with nothing folded together."""

    buy_turnover: float
    sell_turnover: float
    charges: float
    dp_charge: float
    gross_pnl: float
    net_pnl: float

    def to_dict(self) -> dict[str, float]:
        return {
            "buy_turnover": self.buy_turnover,
            "sell_turnover": self.sell_turnover,
            "charges": self.charges,
            "dp_charge": self.dp_charge,
            "gross_pnl": self.gross_pnl,
            "net_pnl": self.net_pnl,
        }


def _round(value: float) -> float:
    return round(float(value), 2)


class TwoRedEquityPaperEngine:
    """One mother, one ladder, on one scrip -- driven forward candle by candle."""

    def __init__(
        self,
        instrument: TwoRedEquityInstrument,
        mother: LadderCandle,
        config: TwoRedEquityConfig,
    ) -> None:
        self.instrument = instrument
        self.config = config
        self.mother = mother
        self.status = "WATCHING"
        # Why the campaign ended, in the engine's own words. Distinct from the
        # ladder's exit reason: a campaign can end without the ladder ever
        # having traded, and "voided" is not an exit.
        self.ended_reason: Optional[str] = None
        self.last_candle_timestamp: Optional[datetime] = None
        self.ladder = TwoRedLadder(
            mother,
            stages=config.stages,
            # A cash trade has no strike and no premium. Passing stubs keeps
            # the ladder's own accounting inert so it cannot quietly produce an
            # option P&L for a stock; every rupee in this module is computed
            # from `ladder.fills` at cash rates instead.
            strike_for=lambda _when, _price: (0, "CE"),
            premium_lookup=lambda _when, _strike, _kind: None,
            lot_size=1,
            quantity_for=self._quantity_for,
            require_new_low=config.require_new_low,
            target_fraction=config.target_fraction,
            trail_fraction=config.trail_fraction,
        )

    # ── sizing ───────────────────────────────────────────────
    def _quantity_for(self, price: float, _lots: int) -> int:
        """Phil's funding rule, with the first-buy gate inside it.

        Returning 0 is not a refusal, it is a wait: the ladder reads it as "not
        worth a share yet", leaves the rung unfilled and keeps watching, so the
        same setup can fill later and lower. That is what makes the 8% gate a
        gate rather than a filter -- the campaign survives it.
        """
        high = float(self.mother.high)
        if high <= 0 or price <= 0:
            return 0
        fallen = (high - price) / high
        if fallen * 100.0 < self.config.min_fall_pct:
            return 0
        return int(self.config.capital_inr * fallen / price)

    # ── the tape ─────────────────────────────────────────────
    def on_candle(self, candle: LadderCandle) -> None:
        if self.status in {"CLOSED", "VOID", "KILLED"}:
            return
        if candle.timestamp <= self.mother.timestamp:
            return
        self.last_candle_timestamp = candle.timestamp

        # THE MOTHER VOIDS AN UNFILLED SETUP. A close back above the mother high
        # says the fall this campaign was waiting for is over. Left alive it
        # would buy into the next rally and then target a price below its own
        # entry, "hitting" instantly at a loss. Once something is bought the
        # basket stays -- its target sits under the mother and is reachable.
        if not self.ladder.fills and candle.close > self.mother.high:
            self.status = "VOID"
            self.ended_reason = "mother_reclaimed"
            return

        self.ladder.on_candle(candle)
        self._sync_status()

    def run(self, candles: Sequence[LadderCandle]) -> "TwoRedEquityPaperEngine":
        for candle in order_events(list(candles)):
            self.on_candle(candle)
        return self

    def kill(self, candle: LadderCandle, price: float) -> None:
        """Stop by hand, selling anything held at this price."""
        if self.status in {"CLOSED", "VOID", "KILLED"}:
            return
        self.ladder.kill(candle, price)
        self.status = "KILLED"
        self.ended_reason = "manual_kill"

    def _sync_status(self) -> None:
        ladder = self.ladder.status
        if ladder in {"CLOSED", "EXPIRED", "KILLED"}:
            self.status = "CLOSED"
            self.ended_reason = self.ladder.exit_reason or "closed"
        elif self.ladder.fills:
            self.status = "HOLDING"
        elif ladder == "ARMED":
            self.status = "ARMED"
        else:
            self.status = "WATCHING"

    # ── money ────────────────────────────────────────────────
    @property
    def quantity(self) -> int:
        return sum(fill.quantity for fill in self.ladder.fills)

    @property
    def invested(self) -> float:
        return _round(sum(fill.index_price * fill.quantity for fill in self.ladder.fills))

    def money_at(self, price: Optional[float]) -> Optional[TwoRedEquityMoney]:
        """What the round is worth if the basket left at `price`.

        Used for both the closed figure (exit price) and the open one (last
        traded price). Returns None with nothing bought, because a campaign
        that has not traded has no P&L -- and a confident 0.00 there reads as a
        flat result rather than an absent one.
        """
        quantity = self.quantity
        if not quantity or price is None or price <= 0:
            return None
        buys = [(float(fill.index_price), int(fill.quantity)) for fill in self.ladder.fills]
        costs: CashRoundCosts = calculate_cash_round_costs(
            buys=buys,
            sell_price=float(price),
            sell_quantity=quantity,
            schedule=self.config.costs,
        )
        gross = _round(sum((float(price) - buy_price) * qty for buy_price, qty in buys))
        dp = _round(self.config.dp_per_sell_day)
        return TwoRedEquityMoney(
            buy_turnover=costs.buy_turnover,
            sell_turnover=costs.sell_turnover,
            charges=costs.total,
            dp_charge=dp,
            gross_pnl=gross,
            net_pnl=_round(gross - costs.total - dp),
        )

    @property
    def realised(self) -> Optional[TwoRedEquityMoney]:
        if self.status != "CLOSED" or self.ladder.exit_index_price is None:
            return None
        return self.money_at(self.ladder.exit_index_price)

    # ── reporting ────────────────────────────────────────────
    def get_status(self, last_price: Optional[float] = None) -> dict[str, Any]:
        realised = self.realised
        open_money = self.money_at(last_price) if self.status == "HOLDING" else None
        target = self.ladder.target_index
        return {
            "symbol": self.instrument.symbol,
            "name": self.instrument.name,
            "status": self.status,
            "ended_reason": self.ended_reason,
            "mother": {
                "timeframe": self.mother.timeframe,
                "timestamp": self.mother.timestamp.isoformat(),
                "high": self.mother.high,
                "low": self.mother.low,
            },
            "stages": list(self.config.stages),
            "rung": len(self.ladder.fills),
            "rungs": len(self.config.stages),
            "quantity": self.quantity,
            "invested": self.invested,
            "average_entry": self.ladder.average_entry,
            "target": target,
            # How far price still has to travel, which is the number that says
            # whether this campaign is nearly done or barely started.
            "target_gap_pct": (
                _round((target - last_price) / last_price * 100.0) if target and last_price and last_price > 0 else None
            ),
            "min_fall_pct": self.config.min_fall_pct,
            "target_fraction": self.config.target_fraction,
            "capital_inr": self.config.capital_inr,
            "fills": [
                {
                    "rung": fill.rung,
                    "timeframe": fill.timeframe,
                    "timestamp": fill.timestamp.isoformat(),
                    "price": fill.index_price,
                    "quantity": fill.quantity,
                    "value": _round(fill.index_price * fill.quantity),
                    "fall_pct": _round((self.mother.high - fill.index_price) / self.mother.high * 100.0),
                }
                for fill in self.ladder.fills
            ],
            "exit": (
                {
                    "timestamp": self.ladder.exit_timestamp.isoformat() if self.ladder.exit_timestamp else None,
                    "timeframe": self.ladder.exit_timeframe,
                    "price": self.ladder.exit_index_price,
                    "reason": self.ladder.exit_reason,
                }
                if self.status == "CLOSED"
                else None
            ),
            "realised": realised.to_dict() if realised else None,
            "open_money": open_money.to_dict() if open_money else None,
            "last_candle_timestamp": (self.last_candle_timestamp.isoformat() if self.last_candle_timestamp else None),
            "running": self.status in {"WATCHING", "ARMED", "HOLDING"},
            "events": list(self.ladder.events),
        }

    # ── persistence ──────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        """Enough to rebuild the campaign exactly, and nothing derived.

        The ladder's own state is NOT stored field by field. It is rebuilt by
        replaying the fills, because a half-restored state machine that looks
        right is worse than one that is reconstructed from what actually
        happened -- see the recalc that ate live rounds.
        """
        return {
            "version": 1,
            "instrument": self.instrument.to_dict(),
            "config": self.config.to_dict(),
            "mother": _candle_to_dict(self.mother),
            "status": self.status,
            "ended_reason": self.ended_reason,
            "last_candle_timestamp": (self.last_candle_timestamp.isoformat() if self.last_candle_timestamp else None),
            "fills": [
                {
                    "rung": fill.rung,
                    "timeframe": fill.timeframe,
                    "timestamp": fill.timestamp.isoformat(),
                    "price": fill.index_price,
                    "quantity": fill.quantity,
                    "lots": fill.lots,
                    "marked_low": fill.marked_low,
                }
                for fill in self.ladder.fills
            ],
            "exit": {
                "timestamp": self.ladder.exit_timestamp.isoformat() if self.ladder.exit_timestamp else None,
                "timeframe": self.ladder.exit_timeframe,
                "price": self.ladder.exit_index_price,
                "reason": self.ladder.exit_reason,
            },
            "lowest": self.ladder.lowest,
            "gate_low": self.ladder.gate_low,
            "active": self.ladder.active,
            "events": list(self.ladder.events),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TwoRedEquityPaperEngine":
        from engine.candle_ladder import LadderFill

        engine = cls(
            TwoRedEquityInstrument.from_dict(payload.get("instrument") or {}),
            _candle_from_dict(payload.get("mother") or {}),
            TwoRedEquityConfig.from_dict(payload.get("config") or {}),
        )
        engine.status = str(payload.get("status") or "WATCHING")
        engine.ended_reason = payload.get("ended_reason")
        last = payload.get("last_candle_timestamp")
        engine.last_candle_timestamp = datetime.fromisoformat(last) if last else None
        engine.ladder.fills = [
            LadderFill(
                rung=int(row.get("rung") or 0),
                timeframe=str(row.get("timeframe") or ""),
                timestamp=datetime.fromisoformat(str(row.get("timestamp"))),
                index_price=float(row.get("price") or 0),
                option_premium=None,
                lots=int(row.get("lots") or 1),
                quantity=int(row.get("quantity") or 0),
                strike=0,
                option_type="CE",
                marked_low=float(row.get("marked_low") or 0),
            )
            for row in (payload.get("fills") or [])
        ]
        exit_row = payload.get("exit") or {}
        if exit_row.get("timestamp"):
            engine.ladder.exit_timestamp = datetime.fromisoformat(str(exit_row["timestamp"]))
        engine.ladder.exit_timeframe = exit_row.get("timeframe")
        engine.ladder.exit_index_price = float(exit_row["price"]) if exit_row.get("price") is not None else None
        engine.ladder.exit_reason = exit_row.get("reason")
        if payload.get("lowest") is not None:
            engine.ladder.lowest = float(payload["lowest"])
        if payload.get("gate_low") is not None:
            engine.ladder.gate_low = float(payload["gate_low"])
        engine.ladder.active = int(payload.get("active") or 0)
        engine.ladder.events = list(payload.get("events") or [])
        # The ladder's own status has to agree with the campaign's, or a
        # restored HOLDING campaign would arm its first rung a second time.
        if engine.status == "CLOSED":
            engine.ladder.status = "CLOSED"
        elif engine.ladder.fills:
            engine.ladder.status = "OPEN" if engine.ladder.active >= len(engine.config.stages) else "OPEN_CLIMBING"
        return engine


def _candle_to_dict(candle: LadderCandle) -> dict[str, Any]:
    return {
        "timeframe": candle.timeframe,
        "timestamp": candle.timestamp.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
    }


def _candle_from_dict(payload: Mapping[str, Any]) -> LadderCandle:
    return LadderCandle(
        timeframe=str(payload.get("timeframe") or "1d"),
        timestamp=datetime.fromisoformat(str(payload.get("timestamp"))),
        open=float(payload.get("open") or 0),
        high=float(payload.get("high") or 0),
        low=float(payload.get("low") or 0),
        close=float(payload.get("close") or 0),
    )


# ── weekly bars ──────────────────────────────────────────────
def regroup_weekly(daily: Sequence[LadderCandle]) -> list[LadderCandle]:
    """Daily bars folded into Monday-to-Friday weeks.

    Grouped by ISO (year, week), never by counting five bars: a holiday would
    make a count-based grouping drift and start slicing weeks in half from then
    on. A short week is still one week, which is what the exchange thinks too.

    This is why the weekly rung needs no new broker call -- Dhan serves daily
    candles, and a week is an arithmetic fact about them.
    """
    weeks: dict[tuple[int, int], list[LadderCandle]] = {}
    for candle in sorted(daily, key=lambda row: row.timestamp):
        key = candle.timestamp.isocalendar()[:2]
        weeks.setdefault((int(key[0]), int(key[1])), []).append(candle)
    folded: list[LadderCandle] = []
    for _key, bars in sorted(weeks.items()):
        folded.append(
            LadderCandle(
                timeframe="1w",
                timestamp=bars[0].timestamp,
                open=bars[0].open,
                high=max(row.high for row in bars),
                low=min(row.low for row in bars),
                close=bars[-1].close,
            )
        )
    return folded


def complete_weeks(weekly: Sequence[LadderCandle], today: date) -> list[LadderCandle]:
    """Only weeks that have actually finished.

    The current week's bar keeps changing until Friday closes, and a ladder
    acting on it would be reading a candle that has not printed. Same reason
    the engine measures a weekly span as 6135 minutes: a slow bar must never be
    acted on early.
    """
    current = tuple(today.isocalendar()[:2])
    return [row for row in weekly if tuple(row.timestamp.isocalendar()[:2]) != current]
