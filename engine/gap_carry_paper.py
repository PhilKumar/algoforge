"""Gap Carry as a paper campaign: the measured rule, wearing a campaign's clothes.

:mod:`engine.gap_carry` owns the rule and nothing else — it is stateless, it was
measured before it was built, and fifteen tests pin it. This module is the part
a live page needs around that rule: a status a badge can render, a position that
survives a restart, a mark-to-market between the two clock times, and a kill.

It reimplements NONE of the geometry. Every decision still comes from
``read_signal`` and ``strike_for``; if this file ever computes a side or a strike
of its own, the monitor and the backtest have quietly become two strategies.

THE SHAPE OF A CAMPAIGN
-----------------------
Unlike its siblings on this page there is no ladder and no mother. A campaign is
one contract, bought at the entry clock and sold at the exit clock the next
session::

    WAITING   the session's entry time has not arrived, or it came and the
              candle asked for nothing
    HOLDING   one leg is open, carried overnight
    CLOSED    sold at the exit clock
    EXPIRED   the contract settled before the exit could be taken
    KILLED    closed by hand from the console

``ARMED`` exists for the moment between reading a qualifying candle and getting
a fill, so a start that cannot be priced does not silently read as WAITING.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from engine.gap_carry import (
    CE,
    GapCarryConfig,
    GapCarryPosition,
    SignalReading,
    read_signal,
    strike_for,
)

try:  # pragma: no cover - the costs module is always present in the app
    from cascade_costs import calculate_nifty_option_round_costs
except Exception:  # pragma: no cover
    calculate_nifty_option_round_costs = None  # type: ignore[assignment]

# THE STAMPS THIS ENGINE HANDS OUT ARE IST-AWARE. `datetime.combine` makes a
# NAIVE datetime, and prod runs on a UTC box: `.timestamp()` then reads a naive
# 15:10 as 15:10 UTC, five and a half hours late. The chart plots markers on an
# epoch axis beside timezone-AWARE candles, so the buy arrow landed 66 bars
# past the last candle, in empty space (Phil, 2026-08-25: "The buy arrow is
# somewhere where nothing is on the screen"). Local naive values are still fine
# for reading a bar's date/time; anything STORED on a position is aware.
IST = ZoneInfo("Asia/Kolkata")


def _ist(value: datetime) -> datetime:
    """Stamp a naive wall-clock datetime as IST; leave an aware one alone."""
    return value if value.tzinfo is not None else value.replace(tzinfo=IST)


WAITING, ARMED, HOLDING, CLOSED, EXPIRED, KILLED = (
    "WAITING",
    "ARMED",
    "HOLDING",
    "CLOSED",
    "EXPIRED",
    "KILLED",
)
#: A campaign is finished only when the ENGINE says so. Reading a runtime's
#: ``running`` flag instead once stamped a live basket as freed.
TERMINAL = frozenset({CLOSED, EXPIRED, KILLED})


def _as_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_datetime(value: Any) -> Optional[datetime]:
    # Positions saved before the IST fix carry NAIVE stamps on disk, and an open
    # one is restored on every boot. Re-stamp at the load boundary so the running
    # position is repaired in place instead of needing the file rewritten.
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ist(value)
    return _ist(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _as_time(value: Any, fallback: time) -> time:
    if isinstance(value, time):
        return value
    if value in (None, ""):
        return fallback
    hh, _, mm = str(value).partition(":")
    return time(int(hh), int(mm or 0))


@dataclass
class GapCarryPaper:
    """One Gap Carry campaign, live or replayed, on paper."""

    config: GapCarryConfig
    #: Resolves (when, strike, side, expiry) -> premium, or None when the
    #: archive cannot answer. Assignable so a replay can hand in recorded
    #: history where the live loop hands in a broker quote.
    option_premium_lookup: Optional[Callable] = None
    #: Resolves a session date -> the expiry this campaign should buy.
    expiry_lookup: Optional[Callable] = None
    #: Resolves an expiry -> its lot size.
    lot_size_lookup: Optional[Callable] = None

    position: Optional[GapCarryPosition] = None
    history: list = field(default_factory=list)
    _status: str = WAITING
    last_signal: Optional[SignalReading] = None
    last_index_close: float = 0.0
    last_mark: Optional[dict] = None
    started_at: Optional[datetime] = None
    notes: list = field(default_factory=list)
    _seen_sessions: set = field(default_factory=set)

    # ── identity the runtime and the poll loop read ──────────────────────
    @property
    def status(self) -> str:
        return self._status

    @property
    def stages(self) -> tuple:
        """The charts the poll loop must fetch. One rule, one chart."""
        return (self.config.timeframe,)

    @property
    def has_open_position(self) -> bool:
        return self.position is not None and self.position.is_open

    @property
    def expired_by(self) -> Optional[datetime]:
        if self.position is None:
            return None
        return datetime.combine(self.position.expiry, time(15, 30))

    # ── the rule, borrowed rather than rebuilt ───────────────────────────
    def ingest(self, batches: Mapping[str, Iterable]) -> None:
        """Feed closed index candles; act only at the entry clock.

        Candles arrive as ``{timeframe: [IndexCandle, ...]}`` exactly as the
        Cascade adapter returns them. Anything that is not this campaign's own
        chart is ignored rather than blended -- two charts would mean two
        different EMA readings for one decision.
        """
        rows = list(batches.get(self.config.timeframe) or [])
        if not rows:
            return
        rows.sort(key=lambda c: c.timestamp)
        self.last_index_close = float(rows[-1].close)
        if self.has_open_position or self._status in TERMINAL:
            return
        session = rows[-1].timestamp.date()
        if session in self._seen_sessions:
            return
        # The decision is only ever taken once a session, on the last candle
        # closed at or before the entry clock. A poll at 15:12 and a replay of
        # the whole day must reach the same bar.
        if rows[-1].timestamp.time() < self.config.entry_time:
            return
        signal = read_signal(rows, self.config, at=datetime.combine(session, self.config.entry_time))
        if signal is None:
            return
        self._seen_sessions.add(session)
        self.last_signal = signal
        if not signal.fires:
            self.notes.append(f"{session}: {signal.reason}")
            return
        self._status = ARMED
        self._arm(session, signal)

    def _arm(self, session: date, signal: SignalReading) -> None:
        """Choose the contract and try to fill it. A miss stays ARMED, loudly."""
        expiry = self.expiry_lookup(session) if self.expiry_lookup else None
        if expiry is None:
            self.notes.append(f"{session}: no expiry that survives to the exit")
            self._status = WAITING
            return
        spot = float(self.last_index_close or signal.close)
        strike = strike_for(spot, signal.side, self.config)
        lot = int(self.lot_size_lookup(expiry)) if self.lot_size_lookup else 75
        entry_ts = datetime.combine(session, self.config.entry_time)
        premium = self._price(entry_ts, strike, signal.side, expiry)
        if premium is None or premium <= 0:
            self.notes.append(f"{session}: no premium for {strike}{signal.side}; nothing bought")
            self._status = WAITING
            return
        self.position = GapCarryPosition(
            session=session,
            side=signal.side,
            strike=strike,
            expiry=expiry,
            lot_size=lot,
            lots=int(self.config.lots),
            signal=signal,
            entry_timestamp=_ist(entry_ts),
            entry_spot=spot,
            entry_premium=float(premium),
        )
        self._status = HOLDING
        self.started_at = self.started_at or entry_ts

    def _price(self, when: datetime, strike: int, side: str, expiry: date) -> Optional[float]:
        if self.option_premium_lookup is None:
            return None
        try:
            value = self.option_premium_lookup(when, strike, side, expiry)
        except Exception:
            return None
        return None if value is None else float(value)

    # ── between the two clocks ───────────────────────────────────────────
    def mark(self, now: Optional[datetime] = None, premium: Optional[float] = None) -> Optional[dict]:
        """Mark the open leg, and take the exit once its clock has passed.

        The exit comparison is ``>=`` and not ``==`` on purpose: a process that
        was down at 09:20 must still take the exit it missed when it comes back
        at 09:35, rather than carry an unplanned second night.
        """
        if not self.has_open_position or now is None:
            return None
        pos = self.position
        assert pos is not None
        if premium is None:
            premium = self._price(now, pos.strike, pos.side, pos.expiry)
        if premium is not None and premium > 0:
            self.last_mark = {
                "at": now.isoformat(),
                "premium": round(float(premium), 2),
                "unrealised": round((float(premium) - float(pos.entry_premium or 0.0)) * pos.quantity, 2),
            }
        due = now.date() > pos.session and now.time() >= self.config.exit_time
        if due and premium is not None and premium > 0:
            self._close(pos, now, float(premium), "MORNING_EXIT", priced=True)
        return self.last_mark

    def settle_past_expiry(self, now: datetime) -> bool:
        """A contract that settled before its exit ends where the contract did.

        Valued at intrinsic and FLAGGED. A floor is not a price, and the whole
        book keeps the two apart everywhere they appear.
        """
        if not self.has_open_position:
            return False
        pos = self.position
        assert pos is not None
        if now.date() <= pos.expiry:
            return False
        floor = pos.intrinsic(float(self.last_index_close or pos.entry_spot))
        self._close(
            pos,
            datetime.combine(pos.expiry, time(15, 30)),
            floor,
            "EXPIRED_AT_INTRINSIC",
            priced=False,
            status=EXPIRED,
        )
        return True

    def kill_and_close(self, premium: float) -> bool:
        """Close by hand, at a real quote.

        The caller must supply a traded premium. A live kill is never floored at
        intrinsic -- that concession belongs to a replay reading an archive, not
        to a position someone is standing in front of.
        """
        if not self.has_open_position:
            if self._status not in TERMINAL:
                self._status = KILLED
            return False
        if premium is None or float(premium) <= 0:
            return False
        pos = self.position
        assert pos is not None
        self._close(pos, datetime.now(), float(premium), "KILLED", priced=True, status=KILLED)
        return True

    def _close(
        self,
        pos: GapCarryPosition,
        when: datetime,
        premium: float,
        reason: str,
        *,
        priced: bool,
        status: str = CLOSED,
    ) -> None:
        pos.exit_timestamp = _ist(when)
        pos.exit_spot = float(self.last_index_close or pos.entry_spot)
        pos.exit_premium = float(premium)
        pos.exit_reason = reason
        pos.exit_priced = bool(priced)
        pos.charges = self._charges(float(pos.entry_premium or 0.0), float(premium), pos.quantity)
        self.history.append(pos)
        self.position = None
        self._status = status

    @staticmethod
    def _charges(buy: float, sell: float, quantity: int) -> float:
        if calculate_nifty_option_round_costs is None or quantity <= 0:
            return 0.0
        try:
            return float(calculate_nifty_option_round_costs(buy_price=buy, sell_price=sell, quantity=quantity).total)
        except Exception:
            return 0.0

    # ── what the page reads ──────────────────────────────────────────────
    def get_status(self) -> dict:
        pos = self.position or (self.history[-1] if self.history else None)
        realised = sum(float(p.net or 0.0) for p in self.history)
        floored = [p for p in self.history if not p.exit_priced]
        return {
            "strategy": "gap_carry",
            "status": self._status,
            "timeframe": self.config.timeframe,
            "rule": {
                "rsi_threshold": self.config.rsi_threshold,
                "rsi_for_call": self.config.rsi_floor_for_call,
                "rsi_for_put": self.config.rsi_ceiling_for_put,
                "strike_offset_steps": self.config.strike_offset_steps,
                "lots": self.config.lots,
                "entry_time": self.config.entry_time.strftime("%H:%M"),
                "exit_time": self.config.exit_time.strftime("%H:%M"),
                "ema_period": self.config.ema_period,
            },
            "signal": self.last_signal.as_dict() if self.last_signal else None,
            "position": pos.as_dict() if pos else None,
            "open": self.has_open_position,
            "mark": self.last_mark,
            "last_index_close": round(float(self.last_index_close or 0.0), 2),
            "closed_trades": len(self.history),
            "realised": round(realised, 2),
            # Kept visible on purpose: an exit the archive could not quote is a
            # floor, and a page that hides which ones they were is not a book.
            "floored_exits": len(floored),
            "floored_net": round(sum(float(p.net or 0.0) for p in floored), 2),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "notes": list(self.notes[-8:]),
            # The nights already settled, so the panel can show the book so far
            # rather than a single row. Capped: this rides a 3s poll.
            "history": [p.as_dict() for p in self.history[-10:]],
        }

    # ── persistence ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "strategy": "gap_carry",
            "status": self._status,
            "config": {
                "timeframe": self.config.timeframe,
                "ema_period": self.config.ema_period,
                "rsi_period": self.config.rsi_period,
                "rsi_threshold": self.config.rsi_threshold,
                "strike_offset_steps": self.config.strike_offset_steps,
                "strike_step": self.config.strike_step,
                "lots": self.config.lots,
                "entry_time": self.config.entry_time.strftime("%H:%M"),
                "exit_time": self.config.exit_time.strftime("%H:%M"),
                "min_days_to_expiry": self.config.min_days_to_expiry,
            },
            "position": _position_to_dict(self.position),
            "history": [_position_to_dict(p) for p in self.history],
            "last_index_close": self.last_index_close,
            "last_mark": self.last_mark,
            "last_signal": self.last_signal.as_dict() if self.last_signal else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "notes": list(self.notes[-20:]),
            "seen_sessions": sorted(d.isoformat() for d in self._seen_sessions),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        option_premium_lookup: Optional[Callable] = None,
        expiry_lookup: Optional[Callable] = None,
        lot_size_lookup: Optional[Callable] = None,
    ) -> "GapCarryPaper":
        raw = dict(data.get("config") or {})
        config = GapCarryConfig(
            timeframe=str(raw.get("timeframe") or "5m"),
            ema_period=int(raw.get("ema_period") or 20),
            rsi_period=int(raw.get("rsi_period") or 14),
            rsi_threshold=float(raw.get("rsi_threshold") or 70.0),
            strike_offset_steps=int(raw.get("strike_offset_steps") or 4),
            strike_step=int(raw.get("strike_step") or 50),
            lots=int(raw.get("lots") or 1),
            entry_time=_as_time(raw.get("entry_time"), time(15, 10)),
            exit_time=_as_time(raw.get("exit_time"), time(9, 20)),
            min_days_to_expiry=int(raw.get("min_days_to_expiry") or 1),
        )
        engine = cls(
            config=config,
            option_premium_lookup=option_premium_lookup,
            expiry_lookup=expiry_lookup,
            lot_size_lookup=lot_size_lookup,
        )
        engine._status = str(data.get("status") or WAITING)
        engine.position = _position_from_dict(data.get("position"))
        engine.history = [p for p in (_position_from_dict(row) for row in data.get("history") or []) if p]
        engine.last_index_close = float(data.get("last_index_close") or 0.0)
        engine.last_mark = data.get("last_mark") or None
        engine.started_at = _as_datetime(data.get("started_at"))
        engine.notes = list(data.get("notes") or [])
        engine._seen_sessions = {d for d in (_as_date(x) for x in data.get("seen_sessions") or []) if d}
        return engine


def _signal_from_dict(row: Any) -> Optional[SignalReading]:
    if not isinstance(row, Mapping):
        return None
    stamp = _as_datetime(row.get("timestamp"))
    if stamp is None:
        return None
    return SignalReading(
        timestamp=stamp,
        close=float(row.get("close") or 0.0),
        ema=float(row.get("ema") or 0.0),
        rsi=float(row.get("rsi") or 0.0),
        side=row.get("side") or None,
        reason=str(row.get("reason") or ""),
    )


def _position_to_dict(pos: Optional[GapCarryPosition]) -> Optional[dict]:
    if pos is None:
        return None
    return {
        "session": pos.session.isoformat(),
        "side": pos.side,
        "strike": pos.strike,
        "expiry": pos.expiry.isoformat(),
        "lot_size": pos.lot_size,
        "lots": pos.lots,
        "signal": pos.signal.as_dict() if pos.signal else None,
        "entry_timestamp": pos.entry_timestamp.isoformat() if pos.entry_timestamp else None,
        "entry_spot": pos.entry_spot,
        "entry_premium": pos.entry_premium,
        "exit_timestamp": pos.exit_timestamp.isoformat() if pos.exit_timestamp else None,
        "exit_spot": pos.exit_spot,
        "exit_premium": pos.exit_premium,
        "exit_reason": pos.exit_reason,
        "exit_priced": pos.exit_priced,
        "charges": pos.charges,
        "notes": list(pos.notes),
    }


def _position_from_dict(row: Any) -> Optional[GapCarryPosition]:
    if not isinstance(row, Mapping):
        return None
    session = _as_date(row.get("session"))
    expiry = _as_date(row.get("expiry"))
    if session is None or expiry is None:
        return None
    signal = _signal_from_dict(row.get("signal"))
    if signal is None:
        signal = SignalReading(
            timestamp=datetime.combine(session, time(15, 10)),
            close=float(row.get("entry_spot") or 0.0),
            ema=0.0,
            rsi=0.0,
            side=row.get("side") or None,
            reason="restored",
        )
    pos = GapCarryPosition(
        session=session,
        side=str(row.get("side") or CE),
        strike=int(row.get("strike") or 0),
        expiry=expiry,
        lot_size=int(row.get("lot_size") or 75),
        lots=int(row.get("lots") or 1),
        signal=signal,
        entry_timestamp=_as_datetime(row.get("entry_timestamp")),
        entry_spot=float(row.get("entry_spot") or 0.0),
        entry_premium=None if row.get("entry_premium") is None else float(row["entry_premium"]),
        exit_timestamp=_as_datetime(row.get("exit_timestamp")),
        exit_spot=float(row.get("exit_spot") or 0.0),
        exit_premium=None if row.get("exit_premium") is None else float(row["exit_premium"]),
        exit_reason=str(row.get("exit_reason") or ""),
        exit_priced=bool(row.get("exit_priced", True)),
        charges=float(row.get("charges") or 0.0),
    )
    pos.notes = list(row.get("notes") or [])
    return pos


def next_session_after(day: date, sessions: Iterable[date]) -> Optional[date]:
    """The first trading session strictly after `day`, or None."""
    later = sorted(d for d in sessions if d > day)
    return later[0] if later else None


def default_expiry_lookup(expiries: Iterable[date], *, min_days: int = 1) -> Callable:
    """Nearest expiry that still exists when the position is sold.

    A weekly settling tonight cannot carry an overnight position, so it is
    refused rather than settled at intrinsic -- the engine makes the same
    refusal in ``replay`` and the two must not disagree.
    """
    ordered = sorted(set(expiries))

    def _lookup(session: date) -> Optional[date]:
        for expiry in ordered:
            if expiry >= session + timedelta(days=int(min_days)):
                return expiry
        return None

    return _lookup
