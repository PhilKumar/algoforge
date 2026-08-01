"""Canonical append-only option-bar archive shared by every replay source.

Provider caches remain useful transport caches. This archive is the stable,
downloadable contract: exact contract identity, raw OHLC minute bars, fetch
metadata and a checksum. Missing minutes remain absent; no zero bars are made.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import config


def _slug(value: Any) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    if not cleaned:
        raise ValueError("Option archive key cannot be blank")
    return cleaned.lower()


def _minute(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        from zoneinfo import ZoneInfo

        parsed = parsed.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0)


def _identity(provider: str, underlying: str, expiry: date | str, strike: int | float, option_type: str) -> dict:
    side = str(option_type).upper()
    if side not in {"CE", "PE"}:
        raise ValueError("option_type must be CE or PE")
    expiry_text = expiry.isoformat() if isinstance(expiry, date) else date.fromisoformat(str(expiry)).isoformat()
    return {
        "provider": _slug(provider),
        "underlying": str(underlying).strip().upper(),
        "expiry": expiry_text,
        "strike": int(round(float(strike))),
        "option_type": side,
    }


class OptionDataArchive:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or config.OPTION_ARCHIVE_ROOT).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, identity: Mapping[str, Any]) -> Path:
        name = f"{int(identity['strike'])}_{identity['option_type']}.json"
        return self.root / _slug(identity["provider"]) / _slug(identity["underlying"]) / str(identity["expiry"]) / name

    @staticmethod
    @contextmanager
    def _locked(path: Path):
        """Serialize cross-thread/process updates for one exact contract."""
        import fcntl

        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _bars_checksum(rows: list[dict[str, Any]]) -> str:
        return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _bar(timestamp: Any, row: Any) -> dict[str, Any]:
        if isinstance(row, Mapping):
            open_price = row.get("open")
            high = row.get("high", open_price)
            low = row.get("low", open_price)
            close = row.get("close", open_price)
        else:
            open_price = getattr(row, "open", row)
            high = getattr(row, "high", open_price)
            low = getattr(row, "low", open_price)
            close = getattr(row, "close", open_price)
        return {
            "timestamp": _minute(timestamp).isoformat(timespec="minutes"),
            "open": float(open_price),
            "high": float(high),
            "low": float(low),
            "close": float(close),
        }

    def store(
        self,
        *,
        provider: str,
        underlying: str,
        expiry: date | str,
        strike: int | float,
        option_type: str,
        bars: Mapping[Any, Any] | Iterable[Mapping[str, Any]],
        instrument_key: str = "",
        source_status: str = "ok",
    ) -> dict:
        identity = _identity(provider, underlying, expiry, strike, option_type)
        path = self._path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(path):
            existing: dict[str, Any] = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    existing = {}
            merged = {str(row["timestamp"]): dict(row) for row in existing.get("bars") or [] if row.get("timestamp")}
            if isinstance(bars, Mapping):
                incoming = [self._bar(timestamp, row) for timestamp, row in bars.items()]
            else:
                incoming = [self._bar(row["timestamp"], row) for row in bars]
            for row in incoming:
                merged[row["timestamp"]] = row
            ordered = [merged[key] for key in sorted(merged)]
            checksum = self._bars_checksum(ordered)
            payload = {
                "version": 1,
                **identity,
                "instrument_key": str(instrument_key or existing.get("instrument_key") or ""),
                "source_status": str(source_status),
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                "first_minute": ordered[0]["timestamp"] if ordered else None,
                "last_minute": ordered[-1]["timestamp"] if ordered else None,
                "bar_count": len(ordered),
                "checksum_sha256": checksum,
                "bars": ordered,
            }
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        return {key: value for key, value in payload.items() if key != "bars"}

    def load(
        self,
        *,
        provider: str,
        underlying: str,
        expiry: date | str,
        strike: int | float,
        option_type: str,
    ) -> dict[datetime, dict[str, Any]]:
        path = self._path(_identity(provider, underlying, expiry, strike, option_type))
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = list(payload.get("bars") or [])
            if payload.get("checksum_sha256") != self._bars_checksum(rows):
                return {}
            return {_minute(row["timestamp"]): dict(row) for row in rows}
        except (OSError, ValueError, TypeError, KeyError):
            return {}

    def inventory(self, *, provider: str = "", underlying: str = "", limit: int = 500) -> list[dict]:
        rows: list[dict] = []
        provider_key = _slug(provider) if provider else ""
        underlying_key = _slug(underlying) if underlying else ""
        for path in self.root.glob("*/*/*/*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if provider_key and _slug(payload.get("provider")) != provider_key:
                continue
            if underlying_key and _slug(payload.get("underlying")) != underlying_key:
                continue
            rows.append({key: value for key, value in payload.items() if key != "bars"})
        rows.sort(key=lambda row: (str(row.get("expiry")), int(row.get("strike") or 0), str(row.get("option_type"))))
        return rows[-max(1, min(int(limit), 2000)) :]

    def export_rows(self, **identity: Any) -> list[dict[str, Any]]:
        series = self.load(**identity)
        return [
            {"timestamp": timestamp.isoformat(timespec="minutes"), **row} for timestamp, row in sorted(series.items())
        ]
