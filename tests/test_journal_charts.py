from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from journal_charts import (
    JournalCandle,
    archived_dates,
    backfill_charts,
    chart_path,
    eligible_through,
    find_gap_events,
    previous_session_levels,
    render_chart,
    session_map,
)

IST = ZoneInfo("Asia/Kolkata")


def _series(days: list[date], base: float = 24_000.0) -> list[JournalCandle]:
    rows: list[JournalCandle] = []
    previous_close = base
    for day_index, day in enumerate(days):
        # Alternate a filled gap-up and an unfilled gap-down so both archive
        # annotations are exercised by a deterministic fixture.
        session_open = previous_close + (40 if day_index % 2 == 0 else -35)
        for index in range(75):
            stamp = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15) + timedelta(minutes=5 * index)
            drift = index * (0.8 if day_index % 2 == 0 else -0.35)
            opened = session_open + drift
            # Make each opening candle oppose its gap colour: gap-up opens are
            # bearish and gap-down opens are bullish. This proves the renderer
            # is repainting the real candle from the gap direction.
            if index == 0:
                closed = opened + (2 if day_index % 2 else -2)
            else:
                closed = opened + (3 if index % 3 else -2)
            low = min(opened, closed) - 4
            high = max(opened, closed) + 4
            if day_index % 2 == 0 and index == 8:
                low = min(low, previous_close - 1)
            rows.append(JournalCandle(stamp, opened, high, low, closed))
        previous_close = rows[-1].close
    return rows


class JournalChartsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.days = [date(2026, 2, 20), date(2026, 2, 23), date(2026, 2, 24), date(2026, 2, 25), date(2026, 2, 26)]

    def test_gap_completion_records_the_fill_candle(self):
        rows = _series(self.days)
        sessions = session_map(rows)
        visible = self.days[-3:]
        events = find_gap_events(sessions, visible)
        self.assertEqual([event.day for event in events], visible)
        self.assertEqual(events[0].completion_index, 8)
        self.assertIsNone(events[1].completion_index)

    def test_cpr_uses_full_traditional_ladder_and_midpoints(self):
        stamp = datetime(2026, 2, 23, 15, 25)
        levels = dict(
            (name, value)
            for name, value, _color, _dotted in previous_session_levels(
                [JournalCandle(stamp, 100.0, 110.0, 90.0, 100.0)]
            )
        )
        self.assertEqual(levels["TC"], 100.0)
        self.assertEqual(levels["PP"], 100.0)
        self.assertEqual(levels["BC"], 100.0)
        self.assertEqual(levels["R0.5"], 105.0)
        self.assertEqual(levels["R4"], 140.0)
        self.assertEqual(levels["S0.5"], 95.0)
        self.assertEqual(levels["S4"], 60.0)

    def test_render_contains_three_complete_sessions(self):
        image = render_chart("NIFTY", _series(self.days), self.days[-1])
        self.assertEqual(image.size, (2048, 1000))
        self.assertEqual(image.mode, "RGB")
        # A blank image would have a single colour; the chart has grid, candles,
        # EMA, pivots, and gap annotations.
        self.assertGreater(len(image.resize((128, 64)).getcolors(maxcolors=100_000) or []), 20)

    def test_gap_direction_repaints_the_real_opening_candle(self):
        rows = _series(self.days)
        image = render_chart("NIFTY", rows, self.days[-1])
        sessions = session_map(rows)
        visible_days = self.days[-3:]
        visible = [row for day in visible_days for row in sessions[day]]
        low, high = min(row.low for row in visible), max(row.high for row in visible)
        span = max(high - low, max(abs(high) * 0.002, 1.0))
        low, high = low - span * 0.08, high + span * 0.08
        left, right, top, bottom = 92, image.width - 190, 108, image.height - 68

        def body_contains(day_offset: int, expected: tuple[int, int, int]) -> bool:
            index = day_offset * 75
            candle = visible[index]
            x = round(left + (index + 0.5) / len(visible) * (right - left))
            y1 = round(top + (high - candle.open) / (high - low) * (bottom - top))
            y2 = round(top + (high - candle.close) / (high - low) * (bottom - top))
            return any(
                image.getpixel((px, py)) == expected
                for px in range(x - 5, x + 6)
                for py in range(min(y1, y2) - 2, max(y1, y2) + 3)
            )

        self.assertTrue(body_contains(0, (8, 153, 129)))  # bearish gap-up candle repainted green
        self.assertTrue(body_contains(1, (233, 30, 99)))  # bullish gap-down candle repainted red

    def test_backfill_writes_both_indices_and_is_idempotent(self):
        nifty = _series(self.days)
        sensex = _series(self.days, 78_000.0)

        class FakeHistory:
            def __init__(self, _cache_root):
                pass

            def candles(self, symbol, _start, _end):
                return nifty if symbol == "NIFTY" else sensex

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "charts"
            now = datetime(2026, 2, 26, 16, 0, tzinfo=IST)
            first = backfill_charts(root, Path(folder) / "cache", now=now, history_factory=FakeHistory)
            self.assertEqual(first["status"], "ok")
            self.assertEqual(len(first["created"]), 6)
            self.assertTrue(chart_path(root, "NIFTY", date(2026, 2, 26)).is_file())
            self.assertTrue(chart_path(root, "SENSEX", date(2026, 2, 26)).is_file())
            self.assertEqual(max(archived_dates(root, "NIFTY")), date(2026, 2, 26))
            self.assertEqual(max(archived_dates(root, "SENSEX")), date(2026, 2, 26))

            class MustNotFetch:
                def __init__(self, _cache_root):
                    raise AssertionError("an up-to-date archive must not fetch history")

            second = backfill_charts(root, Path(folder) / "cache", now=now, history_factory=MustNotFetch)
            self.assertEqual(second["created"], [])

    def test_today_becomes_eligible_only_after_market_processing_window(self):
        day = date(2026, 8, 24)
        self.assertEqual(eligible_through(datetime(2026, 8, 24, 15, 44, tzinfo=IST)), day - timedelta(days=1))
        self.assertEqual(eligible_through(datetime(2026, 8, 24, 15, 45, tzinfo=IST)), day)


if __name__ == "__main__":
    unittest.main()
