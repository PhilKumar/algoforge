"""Dhan data adapter for the 1H option-cascade backtest.

The adapter keeps Dhan-specific response handling out of the strategy engine.
It can fetch the NIFTY index and rolling option series for a feasibility probe,
but refuses to label rolling ATM-relative data as a fixed-strike backtest.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from engine.cascade_options import Candle, CascadeError, OptionCandle


class DhanDataAccessError(CascadeError):
    """Dhan credentials or Data API entitlement prevented a fetch."""


def _friendly_dhan_error(exc: Exception) -> DhanDataAccessError:
    text = str(exc)
    if "DH-902" in text or "451" in text or "not subscribed to Data APIs" in text:
        return DhanDataAccessError(
            "Dhan rejected historical data with DH-902: subscribe/enable Dhan Data APIs "
            "for this client. The access token itself is valid."
        )
    if "DH-901" in text or "Invalid_Authentication" in text or "401" in text:
        return DhanDataAccessError("Dhan rejected the access token (DH-901). Refresh or replace the Dhan token.")
    return DhanDataAccessError(f"Dhan historical data fetch failed: {text[:300]}")


class DhanOneHourSource:
    """Fetch and session-resample closed NIFTY candles for the cascade."""

    INDEX_CHUNK_DAYS = 90
    OPTION_CHUNK_DAYS = 30

    def __init__(self, client, *, nifty_security_id: str = "13") -> None:
        self.client = client
        self.nifty_security_id = str(nifty_security_id)

    @staticmethod
    def _dates(from_date: str, to_date: str, chunk_days: int) -> Iterable[tuple[str, str]]:
        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        while start <= end:
            chunk_end = min(start + timedelta(days=chunk_days - 1), end)
            yield start.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
            start = chunk_end + timedelta(days=1)

    @staticmethod
    def _candles_from_frame(frame) -> list[Candle]:
        return [
            Candle(
                timestamp=timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
            for timestamp, row in frame.iterrows()
        ]

    def _fetch_index_interval(self, from_date: str, to_date: str, *, candle_type: str) -> list[Candle]:
        rows: list[Candle] = []
        try:
            for chunk_start, chunk_end in self._dates(from_date, to_date, self.INDEX_CHUNK_DAYS):
                frame = self.client.get_historical_data(
                    self.nifty_security_id,
                    "IDX_I",
                    "INDEX",
                    from_date=chunk_start,
                    to_date=chunk_end,
                    candle_type=candle_type,
                )
                rows.extend(self._candles_from_frame(frame))
        except Exception as exc:
            raise _friendly_dhan_error(exc) from exc
        return sorted(rows, key=lambda row: row.timestamp)

    @staticmethod
    def _resample_session_four_hour(candles: Iterable[Candle]) -> list[Candle]:
        """Build NSE session-aligned 4H bars from Dhan's 1H bars.

        NSE's 09:15–15:30 session yields a full 09:15 block and a shorter
        13:15 close block.  Keeping that final block avoids crossing sessions.
        """

        groups: dict[datetime, list[Candle]] = defaultdict(list)
        for candle in candles:
            split = candle.timestamp.replace(hour=13, minute=15, second=0, microsecond=0)
            start = candle.timestamp.replace(hour=9, minute=15, second=0, microsecond=0)
            groups[split if candle.timestamp >= split else start].append(candle)
        result: list[Candle] = []
        for timestamp, block in sorted(groups.items()):
            ordered = sorted(block, key=lambda candle: candle.timestamp)
            result.append(
                Candle(
                    timestamp=timestamp,
                    open=ordered[0].open,
                    high=max(candle.high for candle in ordered),
                    low=min(candle.low for candle in ordered),
                    close=ordered[-1].close,
                )
            )
        return result

    def fetch_index(self, from_date: str, to_date: str) -> list[Candle]:
        """Compatibility wrapper for the original 1H backtest source."""
        return self._fetch_index_interval(from_date, to_date, candle_type="60")

    def fetch_index_cascade(self, from_date: str, to_date: str, timeframes: Iterable[str]) -> dict[str, list[Candle]]:
        requested = {str(timeframe).lower() for timeframe in timeframes}
        if not requested.issubset({"1m", "5m", "15m", "1h", "4h", "1d"}):
            raise DhanDataAccessError("Cascade timeframes must be 1m, 5m, 15m, 1h, 4h, or 1d.")
        result: dict[str, list[Candle]] = {}
        interval_map = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "1d": "D"}
        direct = {timeframe for timeframe in requested if timeframe in interval_map}
        if "4h" in requested:
            direct.add("1h")
        for timeframe in direct:
            result[timeframe] = self._fetch_index_interval(from_date, to_date, candle_type=interval_map[timeframe])
        if "4h" in requested:
            result["4h"] = self._resample_session_four_hour(result["1h"])
        return {timeframe: result[timeframe] for timeframe in requested}

    def fetch_rolling_option(
        self,
        from_date: str,
        to_date: str,
        *,
        option_type: str,
        strike_alias: str,
        expiry_code: int = 1,
        interval: str = "60",
    ) -> list[OptionCandle]:
        """Fetch Dhan's rolling ATM-relative option series for feasibility checks."""
        rows: list[OptionCandle] = []
        try:
            for chunk_start, chunk_end in self._dates(from_date, to_date, self.OPTION_CHUNK_DAYS):
                frame = self.client.get_rolling_option_data(
                    self.nifty_security_id,
                    "NSE_FNO",
                    "OPTIDX",
                    "WEEK",
                    expiry_code,
                    strike_alias,
                    option_type,
                    chunk_start,
                    chunk_end,
                    interval=interval,
                    required_data=["open", "high", "low", "close", "volume", "strike", "spot", "iv", "oi"],
                )
                for timestamp, row in frame.iterrows():
                    rows.append(
                        OptionCandle(
                            timestamp=timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp,
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                        )
                    )
        except Exception as exc:
            raise _friendly_dhan_error(exc) from exc
        return sorted(rows, key=lambda row: row.timestamp)

    @property
    def supports_fixed_contracts(self) -> bool:
        # Dhan's documented expired endpoint is rolling ATM-relative data. It
        # cannot prove the exact strike selected at entry stayed constant.
        return False

    def require_fixed_contracts(self) -> None:
        raise DhanDataAccessError(
            "Exact-strike option P&L requires contract-level expired candles. "
            "Dhan rollingoption is suitable for feasibility only, not final fixed-strike validation."
        )
