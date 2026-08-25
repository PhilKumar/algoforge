"""Automatic NIFTY and SENSEX journal charts.

This module is deliberately separate from every trading engine.  It performs
read-only historical-data requests, renders PNG files, and writes them only to
the authenticated owner's Journal chart archive.  It has no broker order,
position, strategy, or live-runtime dependency.

Each saved image contains the requested rolling three trading sessions of
5-minute candles, a continuously seeded EMA20, day-scoped previous-session CPR
and floor pivots, and session-open gaps with their eventual fill/completion.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger("philforge.journal_charts")

SYMBOLS = {
    "NIFTY": {"title": "NIFTY 50 Index", "upstox_key": "NSE_INDEX|Nifty 50", "file": "Nifty"},
    "SENSEX": {"title": "SENSEX Index", "upstox_key": "BSE_INDEX|SENSEX", "file": "Sensex"},
}
DEFAULT_BACKFILL_START = date(2023, 1, 1)
SESSION_OPEN_MINUTES = 9 * 60 + 15
SESSION_LAST_OPEN_MINUTES = 15 * 60 + 25
MIN_SESSION_BARS = 60


@dataclass(frozen=True)
class JournalCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def epoch(self) -> int:
        stamp = self.timestamp
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=IST)
        return int(stamp.timestamp())


@dataclass(frozen=True)
class GapEvent:
    day: date
    reference: float
    session_open: float
    start_index: int
    completion_index: int | None

    @property
    def direction(self) -> str:
        return "up" if self.session_open > self.reference else "down"


def _as_ist_naive(value: str | datetime) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(IST).replace(tzinfo=None)
    return stamp.replace(second=0, microsecond=0)


def _rows_from_payload(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        rows = payload.get("candles") or payload.get("data") or []
        return rows if isinstance(rows, list) else []
    return []


def resample_minutes(rows: Iterable[Sequence], size: int = 5) -> list[JournalCandle]:
    """Normalize provider 1-minute rows and resample on the Indian 09:15 grid."""
    buckets: dict[datetime, list[tuple[float, float, float, float]]] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            stamp = _as_ist_naive(row[0])
            values = tuple(float(row[i]) for i in range(1, 5))
        except (TypeError, ValueError, OverflowError):
            continue
        minute = stamp.hour * 60 + stamp.minute
        if minute < SESSION_OPEN_MINUTES or minute > SESSION_LAST_OPEN_MINUTES + 4:
            continue
        offset = minute - SESSION_OPEN_MINUTES
        slot = SESSION_OPEN_MINUTES + (offset // size) * size
        key = stamp.replace(hour=slot // 60, minute=slot % 60)
        buckets.setdefault(key, []).append(values)
    candles: list[JournalCandle] = []
    for stamp in sorted(buckets):
        bars = buckets[stamp]
        candles.append(
            JournalCandle(
                stamp,
                bars[0][0],
                max(row[1] for row in bars),
                min(row[2] for row in bars),
                bars[-1][3],
            )
        )
    return candles


def load_5m_cache(cache_dir: str | Path, symbol: str) -> list[JournalCandle]:
    """Merge the existing ``tools/.nifty_cache`` files without choosing a stale widest file."""
    merged: dict[datetime, JournalCandle] = {}
    for path in sorted(Path(cache_dir).glob(f"{symbol.upper()}_5m_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("Skipping unreadable index cache %s: %s", path, exc)
            continue
        for row in _rows_from_payload(payload):
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                stamp = _as_ist_naive(row[0])
                minute = stamp.hour * 60 + stamp.minute
                if minute < SESSION_OPEN_MINUTES or minute > SESSION_LAST_OPEN_MINUTES:
                    continue
                merged[stamp] = JournalCandle(stamp, *(float(row[i]) for i in range(1, 5)))
            except (TypeError, ValueError, OverflowError):
                continue
    return [merged[key] for key in sorted(merged)]


class UpstoxIndexHistory:
    """Small persistent, read-only index-history source used by the scheduler."""

    def __init__(self, cache_dir: str | Path, token: str | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if token is None:
            from upstox_token_manager import ensure_fresh_token

            token = ensure_fresh_token()
        if not token:
            raise RuntimeError("Upstox historical-data token is unavailable")
        from data.cascade_upstox import UpstoxPremiumSource

        self.source_class = UpstoxPremiumSource
        self.token = token

    @staticmethod
    def _month_end(day: date) -> date:
        return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])

    def _cache_path(self, symbol: str, month: date) -> Path:
        return self.cache_dir / f"{symbol.upper()}_1m_{month:%Y-%m}.json"

    def _month_rows(self, symbol: str, key: str, month: date, requested_end: date) -> list:
        path = self._cache_path(symbol, month)
        month_end = self._month_end(month)
        closed_month = month_end < datetime.now(IST).date().replace(day=1)
        if path.exists() and closed_month:
            try:
                return _rows_from_payload(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

        start = month.replace(day=1)
        end = min(month_end, requested_end)
        import urllib.parse

        quoted = urllib.parse.quote(key, safe="")
        source = self.source_class(token=self.token, cache_dir=self.cache_dir / "upstox", underlying_key=key)
        body = source._get_v3(f"/historical-candle/{quoted}/minutes/1/{end.isoformat()}/{start.isoformat()}")
        rows = (body.get("data") or {}).get("candles") or []
        # The historical endpoint can publish the just-closed session several
        # hours late.  Upstox exposes the current trading day separately, so an
        # after-market run must merge that response before deciding the archive
        # is current.  The timestamp key also prevents the same candle appearing
        # twice when historical publication catches up during a retry.
        if end == datetime.now(IST).date():
            intraday = source._get_v3(f"/historical-candle/intraday/{quoted}/minutes/1")
            intraday_rows = (intraday.get("data") or {}).get("candles") or []
            merged: dict[datetime, Sequence] = {}
            for row in [*rows, *intraday_rows]:
                if not isinstance(row, (list, tuple)) or not row:
                    continue
                try:
                    merged[_as_ist_naive(row[0])] = row
                except (TypeError, ValueError):
                    continue
            rows = [merged[stamp] for stamp in sorted(merged)]
        temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
        return rows

    def candles(self, symbol: str, start: date, end: date) -> list[JournalCandle]:
        spec = SYMBOLS[symbol.upper()]
        rows: dict[datetime, Sequence] = {}
        cursor = start.replace(day=1)
        while cursor <= end:
            for row in self._month_rows(symbol, spec["upstox_key"], cursor, end):
                if not isinstance(row, (list, tuple)) or not row:
                    continue
                try:
                    stamp = _as_ist_naive(row[0])
                except (TypeError, ValueError):
                    continue
                if start <= stamp.date() <= end:
                    rows[stamp] = row
            cursor = (self._month_end(cursor) + timedelta(days=1)).replace(day=1)
        return resample_minutes((rows[key] for key in sorted(rows)), 5)

    def is_trading_session(self, day: date) -> bool:
        """Ask Upstox whether NSE or BSE has a cash-market session that day."""
        if day.weekday() >= 5:
            return False
        source = self.source_class(
            token=self.token,
            cache_dir=self.cache_dir / "upstox",
            underlying_key=SYMBOLS["NIFTY"]["upstox_key"],
        )
        body = source._get(f"/market/timings/{day.isoformat()}")
        for item in body.get("data") or []:
            exchange = str(item.get("exchange") or "").upper()
            if exchange.startswith("NSE") or exchange.startswith("BSE"):
                return True
        return False


def session_map(candles: Iterable[JournalCandle]) -> dict[date, list[JournalCandle]]:
    result: dict[date, list[JournalCandle]] = {}
    for candle in sorted(candles, key=lambda row: row.timestamp):
        result.setdefault(candle.timestamp.date(), []).append(candle)
    return result


def complete_session_days(candles: Iterable[JournalCandle], *, through: date | None = None) -> list[date]:
    sessions = session_map(candles)
    return [
        day
        for day, rows in sorted(sessions.items())
        if (through is None or day <= through)
        and len(rows) >= MIN_SESSION_BARS
        and rows[-1].timestamp.time() >= dt_time(15, 10)
    ]


def ema20(candles: Sequence[JournalCandle]) -> dict[datetime, float]:
    period = 20
    multiplier = 2.0 / (period + 1.0)
    value: float | None = None
    result: dict[datetime, float] = {}
    for index, candle in enumerate(candles):
        value = candle.close if value is None else candle.close * multiplier + value * (1.0 - multiplier)
        if index >= period - 1:
            result[candle.timestamp] = value
    return result


def previous_session_levels(previous: Sequence[JournalCandle]) -> list[tuple[str, float, str, bool]]:
    """Return the same Traditional CPR ladder used by the reference Pine chart.

    Solid lines are the main levels. Half levels and previous-day high/low are
    dotted, matching the visual hierarchy in the supplied TradingView image.
    """
    high = max(row.high for row in previous)
    low = min(row.low for row in previous)
    close = previous[-1].close
    pp = round((high + low + close) / 3.0, 2)
    bc = round((high + low) / 2.0, 2)
    tc = round(2.0 * pp - bc, 2)
    r1, s1 = round(2.0 * pp - low, 2), round(2.0 * pp - high, 2)
    r2, s2 = round(pp + (high - low), 2), round(pp - (high - low), 2)
    r3 = round(high + 2.0 * (pp - low), 2)
    s3 = round(low - 2.0 * (high - pp), 2)
    r4, s4 = round(r3 + (r1 - pp), 2), round(s3 - (pp - s1), 2)
    resistance = "#ef3340"
    support = "#34a853"
    cpr_blue = "#2962ff"
    cpr_pivot = "#d946ef"
    prior = "#5f6368"
    return [
        ("PDH", round(high, 2), prior, True),
        ("R4", r4, resistance, False),
        ("R3.5", round((r3 + r4) / 2.0, 2), resistance, True),
        ("R3", r3, resistance, False),
        ("R2.5", round((r2 + r3) / 2.0, 2), resistance, True),
        ("R2", r2, resistance, False),
        ("R1.5", round((r1 + r2) / 2.0, 2), resistance, True),
        ("R1", r1, resistance, False),
        ("R0.5", round((pp + r1) / 2.0, 2), resistance, True),
        ("TC", tc, cpr_blue, False),
        ("PP", pp, cpr_pivot, False),
        ("BC", bc, cpr_blue, False),
        ("S0.5", round((pp + s1) / 2.0, 2), support, True),
        ("S1", s1, support, False),
        ("S1.5", round((s1 + s2) / 2.0, 2), support, True),
        ("S2", s2, support, False),
        ("S2.5", round((s2 + s3) / 2.0, 2), support, True),
        ("S3", s3, support, False),
        ("S3.5", round((s3 + s4) / 2.0, 2), support, True),
        ("S4", s4, support, False),
        ("PDL", round(low, 2), prior, True),
    ]


def find_gap_events(all_sessions: dict[date, list[JournalCandle]], visible_days: Sequence[date]) -> list[GapEvent]:
    ordered = sorted(all_sessions)
    positions = {day: index for index, day in enumerate(ordered)}
    events: list[GapEvent] = []
    visible_offset = 0
    for day in visible_days:
        rows = all_sessions[day]
        position = positions[day]
        if position == 0:
            visible_offset += len(rows)
            continue
        reference = all_sessions[ordered[position - 1]][-1].close
        opened = rows[0].open
        if math.isclose(opened, reference, rel_tol=0.0, abs_tol=1e-9):
            visible_offset += len(rows)
            continue
        completion = None
        for index, candle in enumerate(rows):
            if (opened > reference and candle.low <= reference) or (opened < reference and candle.high >= reference):
                completion = visible_offset + index
                break
        events.append(GapEvent(day, reference, opened, visible_offset, completion))
        visible_offset += len(rows)
    return events


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _dash(draw: ImageDraw.ImageDraw, xy: tuple[float, float, float, float], fill: str, width: int = 1) -> None:
    x1, y1, x2, _y2 = xy
    x = x1
    while x < x2:
        draw.line((x, y1, min(x + 6, x2), y1), fill=fill, width=width)
        x += 11


def _label(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    color: str,
    font: ImageFont.ImageFont,
    *,
    anchor: str = "lm",
) -> None:
    x, y = xy
    box = draw.textbbox((x, y), text, font=font, anchor=anchor)
    draw.rounded_rectangle((box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2), radius=3, fill="#ffffff")
    draw.text((x, y), text, font=font, fill=color, anchor=anchor)


def render_chart(
    symbol: str, candles: Sequence[JournalCandle], target: date, *, width: int = 2048, height: int = 1000
) -> Image.Image:
    """Render one archival PNG containing ``target`` and its two prior sessions."""
    symbol = symbol.upper()
    sessions = session_map(candles)
    available = [day for day in complete_session_days(candles, through=target) if day <= target]
    if target not in available:
        raise ValueError(f"{symbol} has no complete session for {target.isoformat()}")
    visible_days = available[-3:]
    if len(visible_days) < 3:
        raise ValueError(f"{symbol} needs three complete sessions through {target.isoformat()}")
    visible = [row for day in visible_days for row in sessions[day]]
    prior_days = sorted(sessions)
    first_position = prior_days.index(visible_days[0])
    seeded_days = prior_days[max(0, first_position - 3) : first_position] + visible_days
    seeded = [row for day in seeded_days for row in sessions[day]]
    ema = ema20(seeded)

    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image, "RGBA")
    title_font, meta_font = _font(25, True), _font(15)
    label_font, tiny_font = _font(13, True), _font(12)
    # Keep a TradingView-like right gutter for the current session's exact
    # level labels so they never cover the final candles.
    left, right, top, bottom = 92, width - 190, 108, height - 68
    plot_w, plot_h = right - left, bottom - top

    # Fit price to market action. Off-screen pivot levels remain omitted rather
    # than compressing three days of candles into a narrow ribbon.
    low = min(row.low for row in visible)
    high = max(row.high for row in visible)
    span = max(high - low, max(abs(high) * 0.002, 1.0))
    low -= span * 0.08
    high += span * 0.08
    price_span = high - low

    def x_of(index: float) -> float:
        return left + (index + 0.5) / max(len(visible), 1) * plot_w

    def y_of(price: float) -> float:
        return top + (high - price) / price_span * plot_h

    # Header and legend.
    title = SYMBOLS[symbol]["title"]
    draw.text((left, 28), f"{title} · 5 minute", font=title_font, fill="#0f172a")
    draw.text(
        (left, 67),
        f"{visible_days[0]:%d %b %Y} — {visible_days[-1]:%d %b %Y}  ·  Three-session market journal",
        font=meta_font,
        fill="#64748b",
    )
    legend_x = width - 610
    for text, color in (("EMA 20", "#2962ff"), ("CPR", "#d946ef"), ("Resistance", "#ef3340"), ("Support", "#34a853")):
        draw.line((legend_x, 46, legend_x + 25, 46), fill=color, width=1 if text == "EMA 20" else 2)
        draw.text((legend_x + 32, 46), text, font=tiny_font, fill="#475569", anchor="lm")
        legend_x += 145

    # Price grid and scale.
    for grid in range(7):
        price = low + price_span * grid / 6
        y = y_of(price)
        draw.line((left, y, right, y), fill="#e2e8f0", width=1)
        draw.text((width - 8, y), f"{price:,.2f}", font=tiny_font, fill="#64748b", anchor="rm")
    draw.rectangle((left, top, right, bottom), outline="#cbd5e1", width=1)

    starts: dict[date, int] = {}
    offset = 0
    for day in visible_days:
        starts[day] = offset
        if offset:
            divider_x = x_of(offset - 0.5)
            _dash(draw, (divider_x, top, divider_x, bottom), "#94a3b8", 1)
        day_rows = sessions[day]
        mid = offset + (len(day_rows) - 1) / 2
        draw.text((x_of(mid), bottom + 29), f"{day:%a · %d %b}", font=label_font, fill="#334155", anchor="mm")
        offset += len(day_rows)

    # Session-scoped CPR/pivots: each day uses the immediately prior session.
    ordered = sorted(sessions)
    positions = {day: index for index, day in enumerate(ordered)}
    for day in visible_days:
        pos = positions[day]
        if pos == 0:
            continue
        day_start = starts[day]
        day_end = day_start + len(sessions[day]) - 1
        label_slots: list[float] = []
        for name, price, color, dotted in previous_session_levels(sessions[ordered[pos - 1]]):
            if not low <= price <= high:
                continue
            y = y_of(price)
            is_cpr = name in {"TC", "PP", "BC"}
            if dotted:
                _dash(draw, (x_of(day_start - 0.45), y, x_of(day_end + 0.45), y), color, 1)
            else:
                draw.line(
                    (x_of(day_start - 0.45), y, x_of(day_end + 0.45), y),
                    fill=color,
                    width=2 if is_cpr else 1,
                )
            label_y = y
            for used in label_slots:
                if abs(label_y - used) < 15:
                    label_y = used + 15
            label_slots.append(label_y)
            line_end_x = x_of(day_end + 0.45)
            latest_day = day == visible_days[-1]
            label_x = line_end_x + 7 if latest_day else line_end_x - 5
            if abs(label_y - y) >= 1:
                draw.line((line_end_x, y, label_x, label_y), fill=color, width=1)
            label = f"{name} {price:,.2f}" if latest_day else name
            _label(draw, (label_x, label_y), label, color, tiny_font, anchor="lm" if latest_day else "rm")

    # The gap indicator repaints the REAL 09:15 5-minute candle. It never lays
    # a rectangle over later price action: gap-up is green, gap-down is red,
    # regardless of whether that opening candle itself closed up or down.
    gaps = find_gap_events(sessions, visible_days)
    gap_by_start = {gap.start_index: gap for gap in gaps}

    # Candles.
    candle_step = plot_w / len(visible)
    body_width = max(2, min(6, int(candle_step * 0.58)))
    for index, candle in enumerate(visible):
        x = x_of(index)
        gap = gap_by_start.get(index)
        if gap is not None:
            color = "#089981" if gap.direction == "up" else "#e91e63"
        else:
            color = "#089981" if candle.close >= candle.open else "#e91e63"
        candle_width = body_width + 2 if gap is not None else body_width
        draw.line((x, y_of(candle.high), x, y_of(candle.low)), fill="#000000", width=1)
        body_top, body_bottom = sorted((y_of(candle.open), y_of(candle.close)))
        if body_bottom - body_top < 1:
            body_bottom = body_top + 1
        draw.rectangle(
            (x - candle_width / 2, body_top, x + candle_width / 2, body_bottom),
            fill=color,
            outline="#000000",
            width=1,
        )

    # Plain-text annotations explain the repaint without adding a gap box or
    # obscuring any candle. Completion is only a dot at the prior close plus
    # the exact five-minute stamp.
    for gap in gaps:
        color = "#089981" if gap.direction == "up" else "#e91e63"
        points = gap.session_open - gap.reference
        open_candle = visible[gap.start_index]
        open_y = max(top + 13, y_of(open_candle.high) - 13)
        draw.text((x_of(gap.start_index) + 5, open_y), f"GAP {points:+.2f}", font=tiny_font, fill=color, anchor="lm")
        if gap.completion_index is not None:
            completed = visible[gap.completion_index]
            marker_x, marker_y = x_of(gap.completion_index), y_of(gap.reference)
            draw.ellipse(
                (marker_x - 5, marker_y - 5, marker_x + 5, marker_y + 5), fill=color, outline="#ffffff", width=2
            )
            draw.text(
                (min(marker_x + 8, right - 108), marker_y - 13),
                f"FILLED {completed.timestamp:%H:%M}",
                font=tiny_font,
                fill=color,
                anchor="lm",
            )
        else:
            draw.text(
                (x_of(gap.start_index) + 5, y_of(open_candle.low) + 13),
                "OPEN",
                font=tiny_font,
                fill=color,
                anchor="lm",
            )

    # EMA is calculated with earlier sessions as a seed, then clipped to these
    # three days so the first visible value is not a fresh-start approximation.
    ema_points = [(x_of(index), y_of(ema[row.timestamp])) for index, row in enumerate(visible) if row.timestamp in ema]
    if len(ema_points) >= 2:
        draw.line(ema_points, fill="#2962ff", width=1, joint="curve")
        last_x, last_y = ema_points[-1]
        _label(
            draw,
            (min(last_x + 7, right - 105), last_y),
            f"EMA20 {ema[visible[-1].timestamp]:,.2f}",
            "#2962ff",
            tiny_font,
        )

    # Latest-price guide and compact OHLC readout.
    last = visible[-1]
    last_y = y_of(last.close)
    _dash(draw, (left, last_y, right, last_y), "#0f766e", 1)
    _label(draw, (width - 8, last_y), f"{last.close:,.2f}", "#0f766e", label_font, anchor="rm")
    change = last.close - last.open
    draw.text(
        (left, height - 23),
        f"Last session  O {last.open:,.2f}   H {last.high:,.2f}   L {last.low:,.2f}   C {last.close:,.2f}   ({change:+.2f})",
        font=meta_font,
        fill="#475569",
        anchor="lm",
    )
    draw.text(
        (right, height - 23),
        "PhilForge · Automated after market close · IST",
        font=tiny_font,
        fill="#94a3b8",
        anchor="rm",
    )
    return image


def _file_date(name: str, symbol: str) -> date | None:
    if not name.lower().startswith(SYMBOLS[symbol]["file"].lower()):
        return None
    for pattern, order in (
        (r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})", (0, 1, 2)),
        (r"(\d{1,2})[-_](\d{1,2})[-_](20\d{2})", (2, 1, 0)),
    ):
        match = re.search(pattern, name)
        if not match:
            continue
        values = [int(value) for value in match.groups()]
        try:
            return date(values[order[0]], values[order[1]], values[order[2]])
        except ValueError:
            continue
    return None


def archived_dates(charts_root: str | Path, symbol: str) -> set[date]:
    root = Path(charts_root)
    if not root.exists():
        return set()
    result: set[date] = set()
    for path in root.rglob("*"):
        if path.is_file():
            parsed = _file_date(path.name, symbol.upper())
            if parsed:
                result.add(parsed)
    return result


_MONTH_NUMBERS = {
    name.upper(): number
    for number in range(1, 13)
    for name in (calendar.month_name[number], calendar.month_abbr[number])
}


def _folder_date(path: Path) -> date | None:
    """Read a date from both the original and generated Journal folder styles."""
    try:
        year = int(path.parent.parent.name)
    except (TypeError, ValueError):
        return None
    month_match = re.search(r"[A-Za-z]+", path.parent.name)
    month = _MONTH_NUMBERS.get(month_match.group(0).upper()) if month_match else None
    if month is None:
        return None

    name = path.name
    for pattern, month_is_text in (
        (r"^(\d{1,2})[-_ ](\d{1,2})[-_ ](20\d{2})$", False),
        (r"^(\d{1,2})[-_ ]([A-Za-z]+)[-_ ](20\d{2})$", True),
        (r"^(\d{1,2})[-_ ]([A-Za-z]+)$", True),
        (r"^(\d{1,2})$", False),
    ):
        match = re.fullmatch(pattern, name)
        if not match:
            continue
        values = match.groups()
        day_number = int(values[0])
        parsed_year = int(values[2]) if len(values) == 3 else year
        if len(values) >= 2:
            parsed_month = _MONTH_NUMBERS.get(values[1].upper()) if month_is_text else int(values[1])
        else:
            parsed_month = month
        if parsed_year != year or parsed_month != month:
            return None
        try:
            return date(parsed_year, parsed_month, day_number)
        except ValueError:
            return None
    return None


def _generated_chart_name(name: str, day: date | None = None) -> bool:
    match = re.fullmatch(r"(?:Nifty|Sensex)_(20\d{2}-\d{2}-\d{2})\.png", name, re.IGNORECASE)
    if not match:
        return False
    if day is None:
        return True
    try:
        return date.fromisoformat(match.group(1)) == day
    except ValueError:
        return False


def _existing_day_folders(charts_root: str | Path, day: date) -> list[Path]:
    year_root = Path(charts_root) / str(day.year)
    if not year_root.is_dir():
        return []
    matches: list[Path] = []
    for month_folder in year_root.iterdir():
        if not month_folder.is_dir():
            continue
        for day_folder in month_folder.iterdir():
            if day_folder.is_dir() and _folder_date(day_folder) == day:
                matches.append(day_folder)
    return matches


def _preferred_day_folder(charts_root: str | Path, day: date) -> Path:
    """Use the owner's existing dated folder before creating our canonical one."""
    canonical = Path(charts_root) / str(day.year) / f"{day:%b}-{day:%Y}" / f"{day:%d}-{day:%b}-{day:%Y}"
    existing = _existing_day_folders(charts_root, day)
    if not existing:
        return canonical

    def score(folder: Path) -> tuple[int, int, int, str]:
        files = [item for item in folder.iterdir() if item.is_file()]
        owner_files = sum(not _generated_chart_name(item.name, day) for item in files)
        return (owner_files > 0, owner_files, len(files), str(folder))

    return max(existing, key=score)


def chart_path(charts_root: str | Path, symbol: str, day: date) -> Path:
    prefix = SYMBOLS[symbol.upper()]["file"]
    folder = _preferred_day_folder(charts_root, day)
    return folder / f"{prefix}_{day:%Y-%m-%d}.png"


def consolidate_generated_day_folders(charts_root: str | Path) -> list[dict[str, str]]:
    """Merge generated-only duplicate folders into the owner's original date.

    Only our exact ``Nifty_YYYY-MM-DD.png`` and ``Sensex_YYYY-MM-DD.png`` files
    move. Manual charts, notes, spreadsheets, and name conflicts are left
    untouched. Empty generated folders and their empty month are then removed.
    """
    root = Path(charts_root)
    if not root.is_dir():
        return []
    moved: list[dict[str, str]] = []
    for year_root in [path for path in root.iterdir() if path.is_dir() and path.name.isdigit()]:
        for canonical_month in list(year_root.iterdir()):
            if not canonical_month.is_dir() or not re.fullmatch(r"[A-Z][a-z]{2}-\d{4}", canonical_month.name):
                continue
            for generated_folder in list(canonical_month.iterdir()):
                if not generated_folder.is_dir():
                    continue
                day = _folder_date(generated_folder)
                if day is None or generated_folder.name != f"{day:%d}-{day:%b}-{day:%Y}":
                    continue
                alternatives = [folder for folder in _existing_day_folders(root, day) if folder != generated_folder]
                if not alternatives:
                    continue
                destination_folder = _preferred_day_folder(root, day)
                if destination_folder == generated_folder:
                    continue
                for source in list(generated_folder.iterdir()):
                    if not source.is_file() or not _generated_chart_name(source.name, day):
                        continue
                    destination = destination_folder / source.name
                    if destination.exists():
                        if source.read_bytes() == destination.read_bytes():
                            source.unlink()
                            moved.append({"from": str(source), "to": str(destination), "result": "duplicate-removed"})
                        continue
                    destination_folder.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                    moved.append({"from": str(source), "to": str(destination), "result": "moved"})
                try:
                    generated_folder.rmdir()
                except OSError:
                    pass
            try:
                canonical_month.rmdir()
            except OSError:
                pass
    return moved


def save_chart(image: Image.Image, destination: str | Path) -> bool:
    """Create one chart atomically and never overwrite a manual or prior image."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return False
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        image.save(temp, format="PNG", optimize=True)
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def eligible_through(now: datetime) -> date:
    current = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    return current.date() if current.time() >= dt_time(15, 45) else current.date() - timedelta(days=1)


def backfill_charts(
    charts_root: str | Path,
    data_cache_root: str | Path,
    *,
    now: datetime | None = None,
    start: date = DEFAULT_BACKFILL_START,
    history_factory: Callable[[str | Path], object] = UpstoxIndexHistory,
) -> dict:
    """Generate every missing complete session for both indices, idempotently."""
    current = now or datetime.now(IST)
    through = eligible_through(current)
    result = {
        "status": "ok",
        "through": through.isoformat(),
        "complete_through": False,
        "expected_session": None,
        "pending": [],
        "created": [],
        "skipped": [],
        "consolidated": consolidate_generated_day_folders(charts_root),
        "errors": [],
    }
    histories: dict[str, list[JournalCandle]] = {}
    existing_by_symbol = {symbol: archived_dates(charts_root, symbol) for symbol in SYMBOLS}
    if start > through:
        return result

    history = history_factory(data_cache_root)
    # Always scan from the configured archive boundary.  Starting after the
    # newest image only appends and silently leaves older holes (notably all of
    # 2025) forever. Closed-month market data is cached, so this remains cheap
    # on daily restarts while still repairing any missing day or index image.
    seed_start = start - timedelta(days=14)
    for symbol in SYMBOLS:
        try:
            histories[symbol] = list(history.candles(symbol, seed_start, through))
        except Exception as exc:
            LOG.exception("%s Journal chart history failed", symbol)
            result["errors"].append({"symbol": symbol, "error": str(exc)})

    session_check = getattr(history, "is_trading_session", None)
    if callable(session_check):
        try:
            result["expected_session"] = bool(session_check(through))
        except Exception as exc:
            LOG.exception("Journal chart market-session check failed for %s", through)
            result["errors"].append({"scope": "market_session", "date": through.isoformat(), "error": str(exc)})
    else:
        # Test/offline history providers have no calendar endpoint. A complete
        # target session from either index is sufficient evidence the market ran.
        result["expected_session"] = any(
            through in complete_session_days(rows, through=through) for rows in histories.values()
        )

    if result["expected_session"]:
        for symbol in SYMBOLS:
            has_archive = through in existing_by_symbol[symbol]
            has_complete_data = through in complete_session_days(histories.get(symbol, []), through=through)
            if not has_archive and not has_complete_data:
                result["pending"].append(
                    {"symbol": symbol, "date": through.isoformat(), "reason": "complete session data not ready"}
                )

    for symbol, candles in histories.items():
        for day in complete_session_days(candles, through=through):
            if day < start or day in existing_by_symbol[symbol]:
                continue
            destination = chart_path(charts_root, symbol, day)
            try:
                image = render_chart(symbol, candles, day)
                if save_chart(image, destination):
                    result["created"].append({"symbol": symbol, "date": day.isoformat(), "path": str(destination)})
                else:
                    result["skipped"].append({"symbol": symbol, "date": day.isoformat(), "reason": "exists"})
            except ValueError as exc:
                result["skipped"].append({"symbol": symbol, "date": day.isoformat(), "reason": str(exc)})
            except Exception as exc:
                LOG.exception("%s Journal chart render failed for %s", symbol, day)
                result["errors"].append({"symbol": symbol, "date": day.isoformat(), "error": str(exc)})
    if result["errors"]:
        result["status"] = "partial" if result["created"] else "error"
    elif result["pending"]:
        result["status"] = "pending"
    else:
        result["complete_through"] = True
    return result
