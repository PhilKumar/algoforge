"""One Supertrend campaign, live or replayed, on paper.

The rule itself lives in engine/supertrend_entry.py and is borrowed here, never
rebuilt. What this module adds is the part a running campaign needs and a
stateless rule must not have: a position that survives a restart, a mark that
updates between bars, the roll and the trail applied to a live quote, and a
snapshot that can be written to app_state and read back.

DIFFERENT IN SHAPE FROM GAP CARRY, DELIBERATELY. Gap Carry is one decision a
night and goes terminal when the night is settled. Supertrend is continuous: it
holds through the night, rolls the contract when price runs away from the
strike, and re-enters as long as the trend is alive. So a campaign here does not
end on its own -- it ends when the trend does not come back, at expiry, or when
Phil kills it. ``history`` therefore grows for the life of the campaign and the
status returns to WATCHING rather than CLOSED after each exit.

Every stamp stored is IST-aware. Production has been bitten before by a naive
datetime written on a UTC box and read back a day out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from engine.options_live_executor import ExecutionRefused
from engine.supertrend_entry import (
    CE,
    SignalReading,
    SupertrendConfig,
    SupertrendPosition,
    read_signal,
    strike_for,
    summarise,
)

try:  # pragma: no cover - exercised in production, stubbed in tests
    from cascade_costs import calculate_nifty_option_round_costs
except Exception:  # pragma: no cover
    calculate_nifty_option_round_costs = None  # type: ignore

IST = ZoneInfo("Asia/Kolkata")


def _ist(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=IST)


WATCHING = "WATCHING"
HOLDING = "HOLDING"
CLOSED = "CLOSED"
EXPIRED = "EXPIRED"
KILLED = "KILLED"
TERMINAL = frozenset({CLOSED, EXPIRED, KILLED})


def _as_date(value) -> Optional[date]:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _as_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ist(value)
    try:
        return _ist(datetime.fromisoformat(str(value)))
    except Exception:
        return None


def _as_time(value, fallback: time) -> time:
    if isinstance(value, time):
        return value
    try:
        hour, minute = str(value).split(":")[:2]
        return time(int(hour), int(minute))
    except Exception:
        return fallback


@dataclass
class SupertrendPaper:
    """A running Supertrend campaign."""

    config: SupertrendConfig
    #: (when, strike, side, expiry) -> premium, or None when nothing can answer.
    option_premium_lookup: Optional[Callable] = None
    #: session date -> the expiry this campaign should buy.
    expiry_lookup: Optional[Callable] = None
    #: expiry -> its lot size.
    lot_size_lookup: Optional[Callable] = None

    #: Sends the real orders when this campaign is live. None means paper.
    #: The RULES never consult it: the same flip arms the same contract
    #: either way, and only the last inch differs.
    executor: Optional[object] = None
    #: Set when an order's fate is unknown. Nothing automatic follows.
    frozen_reason: Optional[str] = None

    position: Optional[SupertrendPosition] = None
    history: list = field(default_factory=list)
    _status: str = WATCHING
    last_signal: Optional[SignalReading] = None
    last_index_close: float = 0.0
    last_mark: Optional[dict] = None
    started_at: Optional[datetime] = None
    notes: list = field(default_factory=list)
    _seen_bars: set = field(default_factory=set)
    rolls: int = 0

    # ── identity the runtime and the poll loop read ──────────────────────
    @property
    def status(self) -> str:
        return self._status

    @property
    def stages(self) -> tuple:
        return (self.config.timeframe,)

    @property
    def has_open_position(self) -> bool:
        return self.position is not None and self.position.is_open

    @property
    def expired_by(self) -> Optional[datetime]:
        if self.position is None:
            return None
        return datetime.combine(self.position.expiry, time(15, 30), tzinfo=IST)

    # ── candles in ───────────────────────────────────────────────────────
    def ingest(self, batches: Mapping[str, Iterable]) -> None:
        """Feed CLOSED index candles for this campaign's own chart.

        A bar is acted on exactly once -- ``_seen_bars`` is keyed by the bar's
        own timestamp, so a 20-second poll that keeps handing over the same last
        candle cannot buy the same trend twice.
        """
        rows = list(batches.get(self.config.timeframe) or [])
        if not rows:
            return
        rows.sort(key=lambda c: c.timestamp)
        self.last_index_close = float(rows[-1].close)
        if self._status in TERMINAL:
            return

        signal = read_signal(rows, self.config)
        if signal is None:
            return
        self.last_signal = signal
        stamp = _ist(signal.timestamp)
        key = stamp.isoformat()

        # Exits are decided on the same closed bar the entry would be, and are
        # checked FIRST: a bar that flips the trend must close the position
        # before anything considers opening one.
        if self.has_open_position:
            if not signal.fires:
                self._exit_on_bar(stamp, "flip")
            return
        if key in self._seen_bars:
            return
        self._seen_bars.add(key)
        if not signal.fires:
            return
        # The bar has closed bullish; the fill is the next print, which for a
        # live loop is now. Refuse the session's edges the same way the book did.
        now = datetime.now(IST)
        if not (self.config.entry_after <= now.time() < self.config.square_off):
            self.notes.append(f"{now:%Y-%m-%d %H:%M}: bullish, but outside the entry window")
            return
        self._arm(now, signal)

    def _arm(self, when: datetime, signal: Optional[SignalReading], *, rolled_from: Optional[int] = None) -> None:
        """Choose the contract and try to fill it. A miss is loud, never silent."""
        when = _ist(when)
        session = when.date()
        expiry = self.expiry_lookup(session) if self.expiry_lookup else None
        if expiry is None:
            self.notes.append(f"{when:%Y-%m-%d %H:%M}: no expiry available, nothing bought")
            return
        spot = float(self.last_index_close or 0.0)
        if spot <= 0:
            self.notes.append(f"{when:%Y-%m-%d %H:%M}: no index level, nothing bought")
            return
        strike = strike_for(spot, self.config)
        premium = self._price(when, strike, self.config.side, expiry)
        if premium is None or premium <= 0:
            self.notes.append(f"{when:%Y-%m-%d %H:%M}: no quote for {strike} {self.config.side}, nothing bought")
            return
        lot_size = int(self.lot_size_lookup(expiry)) if self.lot_size_lookup else 75
        quantity = int(lot_size) * int(self.config.lots)
        order_id = bracket_id = None
        traded = float(premium)
        if self.executor is not None:
            # A net, not a rule: Supertrend exits on the TREND FLIPPING, and a
            # premium this far under the entry is only reached in a collapse
            # the engine could not have sat through anyway.
            stop = max(0.05, round(float(premium) * 0.30, 2))
            try:
                receipt = self.executor.buy(
                    when=when,
                    strike=int(strike),
                    expiry=expiry,
                    option_type=self.config.side,
                    quantity=quantity,
                    premium=float(premium),
                    stop_price=stop,
                )
            except ExecutionRefused as exc:
                self.notes.append(f"{when:%Y-%m-%d %H:%M}: not sent -- {exc}")
                return
            except Exception as exc:
                # UNKNOWN. Hold no position and stop deciding: a phantom leg
                # and a missing real one are both wrong.
                self.frozen_reason = f"{when:%Y-%m-%d %H:%M}: entry outcome unknown -- {exc}"
                self.notes.append(self.frozen_reason)
                return
            order_id = str(receipt.get("order_id") or "") or None
            bracket_id = str(receipt.get("bracket_order_id") or "") or None
            if receipt.get("traded_premium"):
                traded = float(receipt["traded_premium"])
            if receipt.get("traded_quantity"):
                lot_size = max(1, int(receipt["traded_quantity"]) // max(1, int(self.config.lots)))
        premium = traded
        self.position = SupertrendPosition(
            side=self.config.side,
            strike=strike,
            expiry=expiry,
            lot_size=lot_size,
            lots=int(self.config.lots),
            entry_timestamp=when,
            entry_spot=spot,
            entry_premium=float(premium),
            signal=signal,
            rolled_from=rolled_from,
            order_id=order_id,
            bracket_order_id=bracket_id,
        )
        self._status = HOLDING
        if self.started_at is None:
            self.started_at = when
        what = "rolled into" if rolled_from else "bought"
        self.notes.append(
            f"{when:%Y-%m-%d %H:%M}: {what} {strike} {self.config.side} {expiry} at Rs {float(premium):.2f}"
        )

    def _price(self, when: datetime, strike: int, side: str, expiry: date) -> Optional[float]:
        if self.option_premium_lookup is None:
            return None
        try:
            value = self.option_premium_lookup(when, int(strike), str(side), expiry)
        except Exception:
            return None
        return None if value is None else float(value)

    # ── between bars ─────────────────────────────────────────────────────
    def mark(self, now: Optional[datetime] = None, premium: Optional[float] = None) -> Optional[dict]:
        """Update the open position against the clock and a live quote.

        Order matters and mirrors the replay: expiry, then the trail, then the
        roll. The trend flip is NOT handled here -- it belongs to a closed bar
        and arrives through ``ingest``.
        """
        now = _ist(now or datetime.now(IST))
        if not self.has_open_position:
            return None
        position = self.position
        assert position is not None

        spot = float(self.last_index_close or position.entry_spot)
        # The high-water mark only ever moves on a level we have actually seen.
        position.mfe = max(position.mfe, spot - float(position.entry_spot))

        if premium is None:
            premium = self._price(now, position.strike, position.side, position.expiry)
        if premium is not None:
            self.last_mark = {
                "timestamp": now.isoformat(),
                "premium": round(float(premium), 2),
                "spot": round(spot, 2),
                "unrealised": round(
                    (float(premium) - float(position.entry_premium)) * position.quantity,
                    2,
                ),
                "mfe": round(position.mfe, 2),
                "trail_level": position.trail_level(self.config),
            }

        # 1. the contract dies today
        if now.date() >= position.expiry and (now.date() > position.expiry or now.time() >= self.config.square_off):
            self._close(position, now, premium, "expiry", priced=premium is not None, spot=spot, status=WATCHING)
            return self.last_mark

        # 2. the trail, armed only once the move has already happened
        level = position.trail_level(self.config)
        if level is not None and spot <= float(level):
            self._close(position, now, premium, "trail", priced=premium is not None, spot=spot, status=WATCHING)
            return self.last_mark

        # 3. the roll -- sell, and buy a fresh at-the-money contract now
        if (
            self.config.rolls_enabled
            and abs(spot - float(position.strike)) >= self.config.roll_strikes * self.config.strike_step
            and now.date() < position.expiry
            and self.config.entry_after <= now.time() < self.config.square_off
        ):
            if premium is not None:
                self._close(position, now, premium, "roll", priced=True, spot=spot, status=WATCHING)
                self.rolls += 1
                self._arm(now, self.last_signal, rolled_from=int(position.strike))
            else:
                self.notes.append(f"{now:%Y-%m-%d %H:%M}: roll due but no quote, holding")
        return self.last_mark

    def _exit_on_bar(self, when: datetime, reason: str) -> None:
        """Close on a closed-bar decision (the trend flip)."""
        position = self.position
        if position is None:
            return
        when = _ist(when)
        spot = float(self.last_index_close or position.entry_spot)
        premium = self._price(when, position.strike, position.side, position.expiry)
        self._close(position, when, premium, reason, priced=premium is not None, spot=spot, status=WATCHING)

    def settle_past_expiry(self, now: datetime) -> bool:
        """A contract whose expiry has passed is worth its intrinsic value and
        nothing more. Flagged, because a floor is not a price."""
        if not self.has_open_position:
            return False
        position = self.position
        assert position is not None
        now = _ist(now)
        if now.date() <= position.expiry:
            return False
        spot = float(self.last_index_close or position.entry_spot)
        self._close(
            position,
            datetime.combine(position.expiry, self.config.square_off, tzinfo=IST),
            position.intrinsic(spot),
            "expiry",
            priced=False,
            spot=spot,
            status=WATCHING,
        )
        return True

    def kill_and_close(self, premium: float) -> bool:
        """Phil's stop button. Requires a real quote: this never floors."""
        if not self.has_open_position:
            self._status = KILLED
            return False
        if premium is None or float(premium) <= 0:
            return False
        position = self.position
        assert position is not None
        now = datetime.now(IST)
        spot = float(self.last_index_close or position.entry_spot)
        self._close(position, now, float(premium), "killed", priced=True, spot=spot, status=KILLED)
        return True

    def stop(self) -> None:
        """End a campaign that holds nothing."""
        if not self.has_open_position:
            self._status = KILLED

    def _close(
        self,
        position: SupertrendPosition,
        when: datetime,
        premium: Optional[float],
        reason: str,
        *,
        priced: bool,
        spot: float,
        status: str,
    ) -> None:
        when = _ist(when)
        if self.executor is not None and position.order_id:
            traded = self._sell_for_real(position, when)
            if traded is None:
                # NOT closed. The position stays held; the next bar tries
                # again, because booking an exit that did not happen is how a
                # ledger stops describing the account.
                return
            premium, priced = traded, True
        if premium is None:
            # A FLOOR, NOT A PRICE. The trade still closes -- the rule fired,
            # and holding on because a quote was missing would be a position
            # decision nobody made -- but intrinsic on a contract with weeks to
            # run is zero for anything out of the money, so this books the WHOLE
            # premium as lost. On 2026-09-01 that turned a 24100 CE worth
            # perhaps Rs 170 into a Rs 14,501 loss on the panel.
            #
            # `summarise()` already separates these into priced_net / floored_net
            # for the tearsheet. What was missing was any of that reaching the
            # screen, so the note below now says plainly what happened.
            premium = position.intrinsic(spot)
            priced = False
        buy = float(position.entry_premium)
        sell = float(premium)
        position.exit_timestamp = when
        position.exit_spot = float(spot)
        position.exit_premium = sell
        position.exit_reason = reason
        position.exit_priced = bool(priced)
        position.charges = self._charges(buy, sell, position.quantity)
        self.history.append(position)
        self.position = None
        self._status = status
        self.last_mark = None
        if priced or reason == "expiry":
            self.notes.append(f"{when:%Y-%m-%d %H:%M}: {reason} at Rs {sell:.2f} · net Rs {position.net:,.0f}")
        else:
            self.notes.append(
                f"{when:%Y-%m-%d %H:%M}: {reason} — NO QUOTE for {position.strike} {position.side} "
                f"{position.expiry}. Closed, but its P&L is unknown and is NOT in the realised total. "
                f"(Intrinsic was Rs {sell:.2f}; the leg cost Rs {buy:.2f}.)"
            )

    def _sell_for_real(self, position: SupertrendPosition, when: datetime) -> Optional[float]:
        """Close the leg at the broker. None means it is NOT closed."""
        if position.bracket_order_id:
            try:
                outcome = self.executor.cancel_bracket(order_id=position.bracket_order_id)
            except Exception as exc:
                self.frozen_reason = f"bracket release failed -- {exc}"
                self.notes.append(self.frozen_reason)
                return None
            position.bracket_order_id = None
            if isinstance(outcome, dict) and outcome.get("traded"):
                position.notes.append("closed by its own bracket leg")
                return float(outcome.get("avg_price") or 0.0) or None
        try:
            receipt = self.executor.sell(
                when=when,
                strike=int(position.strike),
                expiry=position.expiry,
                option_type=position.side,
                quantity=int(position.quantity),
            )
        except Exception as exc:
            self.frozen_reason = f"exit outcome unknown -- {exc}"
            self.notes.append(self.frozen_reason)
            return None
        status = str((receipt or {}).get("status") or "UNKNOWN").upper()
        if status == "FILLED":
            position.exit_order_id = str(receipt.get("order_id") or "")
            return float(receipt.get("avg_price") or 0.0) or None
        if status == "REJECTED":
            self.notes.append("broker rejected the exit; it will be tried again")
            return None
        self.frozen_reason = f"exit outcome unknown at Dhan (order {receipt.get('order_id')})"
        self.notes.append(self.frozen_reason)
        return None

    @staticmethod
    def _charges(buy: float, sell: float, quantity: int) -> float:
        if calculate_nifty_option_round_costs is None:
            return 0.0
        try:
            return float(calculate_nifty_option_round_costs(buy, sell, int(quantity)).total)
        except Exception:
            return 0.0

    # ── what the panel reads ─────────────────────────────────────────────
    def get_status(self) -> dict:
        book = summarise(self.history + ([self.position] if self.position else []))
        return {
            "strategy": "supertrend",
            "status": self._status,
            "timeframe": self.config.timeframe,
            "rule": self.config.as_dict(),
            "signal": self.last_signal.as_dict() if self.last_signal else None,
            "position": self.position.as_dict(self.config) if self.position else None,
            "open": self.has_open_position,
            "mark": self.last_mark,
            "last_index_close": round(float(self.last_index_close or 0.0), 2),
            "closed_trades": book["trades"],
            "realised": book["net"],
            "priced_net": book["priced_net"],
            "floored_exits": book["floored_exits"],
            "floored_net": book["floored_net"],
            "win_rate": book["win_rate"],
            "rolls": self.rolls,
            "started_at": _ist(self.started_at).isoformat() if self.started_at else None,
            "notes": self.notes[-8:],
            "history": [p.as_dict(self.config) for p in self.history[-10:]],
        }

    # ── snapshot round trip ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "strategy": "supertrend",
            "status": self._status,
            "config": self.config.as_dict(),
            "position": _position_to_dict(self.position) if self.position else None,
            "history": [_position_to_dict(p) for p in self.history],
            "last_index_close": float(self.last_index_close or 0.0),
            "last_mark": self.last_mark,
            "last_signal": self.last_signal.as_dict() if self.last_signal else None,
            "started_at": _ist(self.started_at).isoformat() if self.started_at else None,
            "notes": self.notes[-20:],
            "seen_bars": sorted(self._seen_bars)[-40:],
            "rolls": int(self.rolls),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping,
        *,
        option_premium_lookup: Optional[Callable] = None,
        expiry_lookup: Optional[Callable] = None,
        lot_size_lookup: Optional[Callable] = None,
    ) -> "SupertrendPaper":
        raw = dict(data.get("config") or {})
        defaults = SupertrendConfig()
        config = SupertrendConfig(
            timeframe=str(raw.get("timeframe") or defaults.timeframe),
            atr_period=int(raw.get("atr_period") or defaults.atr_period),
            multiplier=float(raw.get("multiplier") or defaults.multiplier),
            side=str(raw.get("side") or defaults.side),
            strike_step=int(raw.get("strike_step") or defaults.strike_step),
            lots=int(raw.get("lots") or defaults.lots),
            expiry_rank=int(raw.get("expiry_rank") or defaults.expiry_rank),
            roll_strikes=int(raw.get("roll_strikes", defaults.roll_strikes)),
            trail_arm_points=float(raw.get("trail_arm_points", defaults.trail_arm_points)),
            trail_give_points=float(raw.get("trail_give_points", defaults.trail_give_points)),
            entry_after=_as_time(raw.get("entry_after"), defaults.entry_after),
            square_off=_as_time(raw.get("square_off"), defaults.square_off),
        )
        engine = cls(
            config=config,
            option_premium_lookup=option_premium_lookup,
            expiry_lookup=expiry_lookup,
            lot_size_lookup=lot_size_lookup,
        )
        engine._status = str(data.get("status") or WATCHING)
        engine.position = _position_from_dict(data.get("position"))
        engine.history = [p for p in (_position_from_dict(row) for row in (data.get("history") or [])) if p]
        engine.last_index_close = float(data.get("last_index_close") or 0.0)
        engine.last_mark = data.get("last_mark") or None
        engine.last_signal = _signal_from_dict(data.get("last_signal"))
        engine.started_at = _as_datetime(data.get("started_at"))
        engine.notes = list(data.get("notes") or [])
        engine._seen_bars = set(data.get("seen_bars") or [])
        engine.rolls = int(data.get("rolls") or 0)
        return engine


def _signal_from_dict(raw) -> Optional[SignalReading]:
    if not raw:
        return None
    stamp = _as_datetime(raw.get("timestamp"))
    if stamp is None:
        return None
    return SignalReading(
        timestamp=stamp,
        close=float(raw.get("close") or 0.0),
        supertrend=float(raw.get("supertrend") or 0.0),
        direction=int(raw.get("direction") or 0),
        flipped=bool(raw.get("flipped")),
        reason=str(raw.get("reason") or ""),
    )


def _position_to_dict(position: Optional[SupertrendPosition]) -> Optional[dict]:
    if position is None:
        return None
    return {
        "side": position.side,
        "strike": int(position.strike),
        "expiry": position.expiry.isoformat() if position.expiry else None,
        "lot_size": int(position.lot_size),
        "lots": int(position.lots),
        "entry_timestamp": _ist(position.entry_timestamp).isoformat(),
        "entry_spot": float(position.entry_spot),
        "entry_premium": float(position.entry_premium),
        "mfe": float(position.mfe),
        "rolled_from": position.rolled_from,
        "exit_timestamp": (_ist(position.exit_timestamp).isoformat() if position.exit_timestamp else None),
        "exit_spot": float(position.exit_spot),
        "exit_premium": float(position.exit_premium),
        "exit_reason": position.exit_reason,
        "exit_priced": bool(position.exit_priced),
        "charges": float(position.charges),
        "notes": list(position.notes),
        "signal": position.signal.as_dict() if position.signal else None,
        # THE REAL ORDERS. A restart that forgot these would come back not
        # knowing what it holds at the broker, which is the whole reason
        # restart reconciliation exists.
        "order_id": position.order_id,
        "bracket_order_id": position.bracket_order_id,
        "exit_order_id": position.exit_order_id,
    }


def _position_from_dict(raw) -> Optional[SupertrendPosition]:
    if not raw:
        return None
    entry = _as_datetime(raw.get("entry_timestamp"))
    expiry = _as_date(raw.get("expiry"))
    if entry is None or expiry is None:
        return None
    position = SupertrendPosition(
        side=str(raw.get("side") or CE),
        strike=int(raw.get("strike") or 0),
        expiry=expiry,
        lot_size=int(raw.get("lot_size") or 75),
        lots=int(raw.get("lots") or 1),
        entry_timestamp=entry,
        entry_spot=float(raw.get("entry_spot") or 0.0),
        entry_premium=float(raw.get("entry_premium") or 0.0),
        signal=_signal_from_dict(raw.get("signal")),
        mfe=float(raw.get("mfe") or 0.0),
        rolled_from=raw.get("rolled_from"),
    )
    position.exit_timestamp = _as_datetime(raw.get("exit_timestamp"))
    position.exit_spot = float(raw.get("exit_spot") or 0.0)
    position.exit_premium = float(raw.get("exit_premium") or 0.0)
    position.exit_reason = str(raw.get("exit_reason") or "")
    position.exit_priced = bool(raw.get("exit_priced", True))
    position.charges = float(raw.get("charges") or 0.0)
    position.notes = list(raw.get("notes") or [])
    position.order_id = raw.get("order_id") or None
    position.bracket_order_id = raw.get("bracket_order_id") or None
    position.exit_order_id = raw.get("exit_order_id") or None
    return position


def default_expiry_lookup(expiries: Iterable, *, rank: int = 2) -> Callable:
    """session -> the ``rank``-th weekly expiry at or after it.

    rank 1 is the nearest weekly; the measured rule uses 2, because on the
    nearest a third of all entries land within a day of expiry and every one of
    those buckets lost money.
    """
    ordered = sorted({_as_date(value) for value in expiries if _as_date(value) is not None})

    def _lookup(session: date) -> Optional[date]:
        ahead = [value for value in ordered if value >= session]
        index = max(1, int(rank)) - 1
        return ahead[index] if index < len(ahead) else None

    return _lookup
