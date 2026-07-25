"""Dhan data adapter for the 1H option-cascade backtest.

The adapter keeps Dhan-specific response handling out of the strategy engine.
It can fetch the NIFTY index and rolling option series for a feasibility probe,
but refuses to label rolling ATM-relative data as a fixed-strike backtest.
"""

from __future__ import annotations

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
    """Fetch closed 1H candles in API-sized chunks."""

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

    def fetch_index(self, from_date: str, to_date: str) -> list[Candle]:
        rows: list[Candle] = []
        try:
            for chunk_start, chunk_end in self._dates(from_date, to_date, self.INDEX_CHUNK_DAYS):
                frame = self.client.get_historical_data(
                    self.nifty_security_id,
                    "IDX_I",
                    "INDEX",
                    from_date=chunk_start,
                    to_date=chunk_end,
                    candle_type="60",
                )
                for timestamp, row in frame.iterrows():
                    rows.append(
                        Candle(
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

    def fetch_rolling_option(
        self,
        from_date: str,
        to_date: str,
        *,
        option_type: str,
        strike_alias: str,
        expiry_code: int = 1,
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
                    interval="60",
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
