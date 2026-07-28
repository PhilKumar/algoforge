"""Effective-dated NIFTY contract rules for historical replay.

A one-year backtest crosses exchange rule changes.  NIFTY's weekly expiry
weekday and its lot size have both moved inside recent twelve-month windows.
A replay that assumes *today's* rules will select the wrong expiry and size
every contract wrongly for the whole stretch before the change, and it will do
so silently.

Two things are kept apart here on purpose:

* The **rules** (which weekday carries weekly expiry, what a lot is, what the
  strike ladder step is) are a dated table.  They are exchange policy and no
  amount of price data can derive them.
* The **holiday calendar** is not tabulated at all.  It is read back from the
  trading sessions actually present in the index candles, which is the only
  source that cannot drift out of date.

`app.py` previously generated the expiry calendar by hardcoding Tuesday, which
is correct only for the most recent rule period.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence


class CalendarError(ValueError):
    """The contract-rule table cannot answer the question asked of it."""


@dataclass(frozen=True)
class ContractRule:
    """Exchange contract rules in force from `effective_from` onward."""

    effective_from: date
    expiry_weekday: int  # Monday=0 .. Sunday=6
    lot_size: int
    strike_step: float = 50.0
    note: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.expiry_weekday <= 6:
            raise CalendarError("expiry_weekday must be 0 (Mon) through 6 (Sun)")
        if self.lot_size <= 0 or self.strike_step <= 0:
            raise CalendarError("lot_size and strike_step must be positive")


# The rule period this repository already assumes everywhere else (Tuesday
# weekly expiry, 65-unit lots).  It is deliberately the ONLY seeded record.
#
# Earlier periods are not invented here.  If a backtest starts before this
# date, `ContractCalendar` refuses to guess and tells the caller to supply the
# earlier rules with --calendar, because a wrong expiry weekday silently
# reprices every trade rather than raising.
DEFAULT_RULES: tuple[ContractRule, ...] = (
    ContractRule(
        effective_from=date(2025, 9, 1),
        expiry_weekday=1,
        lot_size=65,
        strike_step=50.0,
        note="Repository default: Tuesday weekly expiry, 65-unit lot. VERIFY against NSE circulars.",
    ),
)


class ContractCalendar:
    """Resolve contract rules and the weekly expiry ladder for a date range."""

    def __init__(self, rules: Iterable[ContractRule] = DEFAULT_RULES) -> None:
        ordered = sorted(rules, key=lambda rule: rule.effective_from)
        if not ordered:
            raise CalendarError("at least one ContractRule is required")
        starts = [rule.effective_from for rule in ordered]
        if len(set(starts)) != len(starts):
            raise CalendarError("two ContractRules share an effective_from date")
        self.rules: tuple[ContractRule, ...] = tuple(ordered)
        self._starts = starts

    @classmethod
    def from_json(cls, path: str) -> "ContractCalendar":
        """Load an override table so rule history is data, not a code change."""
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload["rules"] if isinstance(payload, dict) else payload
        return cls(
            ContractRule(
                effective_from=date.fromisoformat(row["effective_from"]),
                expiry_weekday=int(row["expiry_weekday"]),
                lot_size=int(row["lot_size"]),
                strike_step=float(row.get("strike_step", 50.0)),
                note=str(row.get("note", "")),
            )
            for row in rows
        )

    @property
    def earliest(self) -> date:
        return self.rules[0].effective_from

    def rule_for(self, day: date) -> ContractRule:
        """The rule in force on `day`.

        Raises rather than extrapolating backwards.  An unnoticed wrong expiry
        weekday is far more expensive than a failed run.
        """
        if day < self.earliest:
            raise CalendarError(
                f"no contract rule covers {day.isoformat()}; the earliest known rule starts "
                f"{self.earliest.isoformat()}. Supply the earlier NSE rule period "
                f"(expiry weekday, lot size) via --calendar instead of backdating today's rules."
            )
        return self.rules[bisect_right(self._starts, day) - 1]

    def covers(self, day: date) -> bool:
        return day >= self.earliest

    def weekly_expiries(self, from_day: date, to_day: date, sessions: Sequence[date] | set[date]) -> list[date]:
        """Weekly expiry dates between the bounds, holiday-shifted by sessions.

        `sessions` are the trading days observed in the index candles.  When the
        rule's expiry weekday is not a session, NSE moves expiry to the
        preceding trading day, which is exactly what rolling back to the latest
        session at or before the target reproduces.

        Beyond the last observed session the target weekday is emitted as-is:
        it cannot be holiday-validated yet, and a contract selected there is
        flagged by the caller rather than silently trusted.
        """
        session_days = sorted(set(sessions))
        if to_day < from_day:
            raise CalendarError("to_day cannot precede from_day")
        # Refuse the whole range rather than quietly applying today's expiry
        # weekday to the uncovered part of it.
        self.rule_for(from_day)
        # Look two weeks past the end so a campaign opened on the last day can
        # still find the next weekly expiry it would actually have traded.
        horizon = to_day + timedelta(days=14)
        last_known = session_days[-1] if session_days else None

        expiries: list[date] = []
        cursor = from_day
        while cursor <= horizon:
            rule = self.rule_for(cursor)
            # Step to this week's expiry weekday under the rule in force.
            target = cursor + timedelta(days=(rule.expiry_weekday - cursor.weekday()) % 7)
            if target > horizon:
                break
            if last_known is not None and target <= last_known:
                candidates = [day for day in session_days if day <= target]
                if candidates:
                    expiries.append(max(candidates))
            else:
                expiries.append(target)
            cursor = target + timedelta(days=1)
        return sorted(set(expiries))

    def describe(self) -> str:
        """Human-readable table, printed by the harness so a wrong rule shows."""
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        lines = ["contract rules in force:"]
        for rule in self.rules:
            lines.append(
                f"  from {rule.effective_from.isoformat()}  expiry={names[rule.expiry_weekday]}  "
                f"lot={rule.lot_size}  step={rule.strike_step:g}" + (f"  # {rule.note}" if rule.note else "")
            )
        return "\n".join(lines)


def sessions_from_candles(candles: Iterable) -> set[date]:
    """Trading dates present in a candle series (the trustworthy holiday map)."""
    return {candle.timestamp.date() for candle in candles}


def optional_calendar(path: Optional[str]) -> ContractCalendar:
    return ContractCalendar.from_json(path) if path else ContractCalendar()
