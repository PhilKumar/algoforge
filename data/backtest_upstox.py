"""Exact historical premium-target option selection for generic backtests.

The strategy builder permits ``premium_above``, ``premium_below`` and
``premium_near`` legs. A rolling option series cannot represent those rules,
because every entry may choose a different fixed strike. This adapter resolves
the strike from Upstox expired-contract minute history and returns the selected
fixed contract's OHLC data to the existing backtest engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from data.cascade_upstox import NIFTY_INDEX_KEY, UpstoxPremiumSource
from engine.strike_utils import round_to_nearest_step

PREMIUM_STRIKE_TYPES = {"premium_near", "premium_above", "premium_below"}


@dataclass(frozen=True)
class HistoricalOptionSelection:
    history_key: str
    history: pd.DataFrame
    strike: int
    expiry: date
    entry_price: float


class UpstoxHistoricalPremiumSelector:
    """Resolve premium-target NIFTY option legs with exact historical OHLC."""

    def __init__(self, instrument: str, *, cache_only: bool = False) -> None:
        if str(instrument or "26000") not in {"26000", "NIFTY"}:
            raise ValueError("Upstox premium-target backtests currently support NIFTY 50 only.")
        self.source = UpstoxPremiumSource(
            underlying_key=NIFTY_INDEX_KEY,
            cache_only=cache_only,
            backfill_missing=not cache_only,
        )
        self.expiries = sorted(self.source.available_expiries())
        self._frames: dict[tuple[str, date, int], pd.DataFrame] = {}
        self.last_gap = ""

    @staticmethod
    def _expiry_for(expiries: list[date], on: date, selection: str) -> date | None:
        future = [value for value in expiries if value >= on]
        if not future:
            return None
        selection = str(selection or "current_week").lower()
        if selection == "next_week":
            return future[1] if len(future) > 1 else None
        if selection in {"current_month", "next_month"}:
            months = sorted({(value.year, value.month) for value in future})
            offset = 1 if selection == "next_month" else 0
            if len(months) <= offset:
                return None
            wanted = months[offset]
            return max(value for value in future if (value.year, value.month) == wanted)
        return future[0]

    def _frame(self, instrument_key: str, expiry: date, strike: int, timeframe: int) -> pd.DataFrame:
        cache_key = (instrument_key, expiry, int(timeframe))
        cached = self._frames.get(cache_key)
        if cached is not None:
            return cached
        series = self.source._minute_series(instrument_key, expiry)
        if not series:
            return pd.DataFrame()
        rows = [
            {"timestamp": stamp, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
            for stamp, bar in series.items()
        ]
        frame = pd.DataFrame(rows).set_index("timestamp").sort_index()
        frame = (
            frame.resample(f"{timeframe}min", label="left", closed="left", origin="start_day", offset="15min")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna(subset=["open"])
        )
        self._frames[cache_key] = frame
        return frame

    def select(self, entry_time: datetime, entry_spot: float, leg: dict, timeframe_minutes: int):
        """Return the exact selected fixed contract, or None with ``last_gap``."""
        self.last_gap = ""
        option_type = str(leg.get("option_type") or "CE").upper()
        strike_type = str(leg.get("strike_type") or "").lower()
        target = float(leg.get("strike_value") or 0)
        if option_type not in {"CE", "PE"} or strike_type not in PREMIUM_STRIKE_TYPES or target <= 0:
            self.last_gap = "invalid premium-target leg"
            return None
        expiry = self._expiry_for(self.expiries, entry_time.date(), leg.get("expiry") or "current_week")
        if expiry is None:
            self.last_gap = "no eligible Upstox expiry"
            return None
        contracts = self.source._contract_index(expiry)
        atm = round_to_nearest_step(entry_spot, 50)
        if strike_type == "premium_above":
            offsets = range(0, -16, -1) if option_type == "CE" else range(0, 16)
        elif strike_type == "premium_below":
            offsets = range(0, 16) if option_type == "CE" else range(0, -16, -1)
        else:
            offsets = range(-15, 16)
        candidates = []
        for offset in offsets:
            strike = atm + offset * 50
            instrument_key = contracts.get((strike, option_type))
            if not instrument_key:
                continue
            frame = self._frame(instrument_key, expiry, strike, int(timeframe_minutes))
            if frame.empty or entry_time not in frame.index:
                continue
            price = float(frame.loc[entry_time, "open"])
            candidate = (price, strike, instrument_key, frame)
            candidates.append(candidate)
            if (strike_type == "premium_above" and price >= target) or (
                strike_type == "premium_below" and price <= target
            ):
                return HistoricalOptionSelection(
                    f"upstox|{instrument_key}|{expiry.isoformat()}|{strike}|{option_type}",
                    frame,
                    strike,
                    expiry,
                    price,
                )
        if not candidates:
            self.last_gap = f"no complete Upstox {option_type} candle series at {entry_time:%Y-%m-%d %H:%M}"
            return None
        price, strike, instrument_key, frame = min(candidates, key=lambda item: abs(item[0] - target))
        return HistoricalOptionSelection(
            f"upstox|{instrument_key}|{expiry.isoformat()}|{strike}|{option_type}", frame, strike, expiry, price
        )
