from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from journal_charts import (
    JournalCandle,
    UpstoxIndexHistory,
    archived_dates,
    backfill_charts,
    chart_path,
    complete_session_days,
    consolidate_generated_day_folders,
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


def _minute_rows(day: date, base: float = 24_000.0) -> list[list]:
    rows = []
    for index in range(375):
        stamp = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15) + timedelta(minutes=index)
        opened = base + index * 0.1
        rows.append([stamp.replace(tzinfo=IST).isoformat(), opened, opened + 2, opened - 2, opened + 1, 0, 0])
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

    def test_chart_path_matches_the_existing_year_month_day_tree(self):
        root = Path("charts")
        self.assertEqual(
            chart_path(root, "NIFTY", date(2025, 1, 2)),
            root / "2025" / "Jan-2025" / "02-Jan-2025" / "Nifty_2025-01-02.png",
        )
        self.assertEqual(
            chart_path(root, "SENSEX", date(2026, 8, 24)),
            root / "2026" / "Aug-2026" / "24-Aug-2026" / "Sensex_2026-08-24.png",
        )

    def test_chart_path_reuses_the_owners_existing_date_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "charts"
            original = root / "2023" / "JAN_2023" / "09_01_2023"
            original.mkdir(parents=True)
            (original / "Nifty_09_01_2023.JPG").write_bytes(b"owner chart")

            self.assertEqual(
                chart_path(root, "SENSEX", date(2023, 1, 9)),
                original / "Sensex_2023-01-09.png",
            )

    def test_generated_duplicate_folder_is_merged_without_touching_owner_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "charts"
            original = root / "2023" / "JAN_2023" / "09_01_2023"
            generated = root / "2023" / "Jan-2023" / "09-Jan-2023"
            original.mkdir(parents=True)
            generated.mkdir(parents=True)
            owner = original / "Nifty_09_01_2023.JPG"
            owner.write_bytes(b"owner chart")
            generated_chart = generated / "Sensex_2023-01-09.png"
            generated_chart.write_bytes(b"generated sensex")

            moved = consolidate_generated_day_folders(root)

            self.assertEqual(owner.read_bytes(), b"owner chart")
            self.assertEqual((original / generated_chart.name).read_bytes(), b"generated sensex")
            self.assertFalse(generated.exists())
            self.assertFalse((root / "2023" / "Jan-2023").exists())
            self.assertEqual(len(moved), 1)

    def test_missing_sensex_is_added_to_the_existing_nifty_date_folder(self):
        nifty = _series(self.days)
        sensex = _series(self.days, 78_000.0)

        class FakeHistory:
            def __init__(self, _cache_root):
                pass

            def candles(self, symbol, _start, _end):
                return nifty if symbol == "NIFTY" else sensex

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "charts"
            original = root / "2026" / "FEB_2026" / "26_02_2026"
            original.mkdir(parents=True)
            (original / "Nifty_26_02_2026.JPG").write_bytes(b"owner chart")

            result = backfill_charts(
                root,
                Path(folder) / "cache",
                start=date(2026, 2, 26),
                now=datetime(2026, 2, 26, 16, 0, tzinfo=IST),
                history_factory=FakeHistory,
            )

            self.assertEqual(
                {(item["symbol"], item["date"]) for item in result["created"]},
                {("SENSEX", "2026-02-26")},
            )
            self.assertTrue((original / "Sensex_2026-02-26.png").exists())
            self.assertFalse((root / "2026" / "Feb-2026").exists())

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
        calls = []

        class FakeHistory:
            def __init__(self, _cache_root):
                pass

            def candles(self, symbol, _start, _end):
                calls.append((symbol, _start, _end))
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

            second = backfill_charts(root, Path(folder) / "cache", now=now, history_factory=FakeHistory)
            self.assertEqual(second["created"], [])
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(call[1] == date(2022, 12, 18) for call in calls))

    def test_backfill_repairs_holes_before_the_newest_existing_chart(self):
        nifty = _series(self.days)
        sensex = _series(self.days, 78_000.0)

        class FakeHistory:
            def __init__(self, _cache_root):
                pass

            def candles(self, symbol, _start, _end):
                return nifty if symbol == "NIFTY" else sensex

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "charts"
            for symbol, rows in (("NIFTY", nifty), ("SENSEX", sensex)):
                newest = chart_path(root, symbol, date(2026, 2, 26))
                newest.parent.mkdir(parents=True, exist_ok=True)
                render_chart(symbol, rows, date(2026, 2, 26)).save(newest)

            result = backfill_charts(
                root,
                Path(folder) / "cache",
                now=datetime(2026, 2, 26, 16, 0, tzinfo=IST),
                history_factory=FakeHistory,
            )
            created = {(item["symbol"], item["date"]) for item in result["created"]}
            self.assertIn(("NIFTY", "2026-02-24"), created)
            self.assertIn(("NIFTY", "2026-02-25"), created)
            self.assertIn(("SENSEX", "2026-02-24"), created)
            self.assertIn(("SENSEX", "2026-02-25"), created)
            self.assertNotIn(("NIFTY", "2026-02-26"), created)
            self.assertNotIn(("SENSEX", "2026-02-26"), created)

    def test_current_session_merges_v3_intraday_candles_into_month_cache(self):
        today = datetime.now(IST).date()
        calls = []

        class FakeSource:
            def __init__(self, **_kwargs):
                pass

            def _get_v3(self, path):
                calls.append(path)
                rows = _minute_rows(today) if "/intraday/" in path else []
                return {"status": "success", "data": {"candles": rows}}

        with tempfile.TemporaryDirectory() as folder:
            history = UpstoxIndexHistory(Path(folder) / "cache", token="dummy")
            history.source_class = FakeSource

            candles = history.candles("NIFTY", today, today)

            self.assertEqual(len(candles), 75)
            self.assertEqual(complete_session_days(candles), [today])
            self.assertTrue(any("/historical-candle/intraday/" in path for path in calls))
            self.assertTrue(any("/historical-candle/" in path and "/minutes/1/" in path for path in calls))

    def test_trading_day_stays_pending_until_both_target_charts_have_data(self):
        target = self.days[-1]
        prior_rows = _series(self.days[:-1])

        class DelayedHistory:
            def __init__(self, _cache_root):
                pass

            def candles(self, _symbol, _start, _end):
                return prior_rows

            def is_trading_session(self, _day):
                return True

        with tempfile.TemporaryDirectory() as folder:
            result = backfill_charts(
                Path(folder) / "charts",
                Path(folder) / "cache",
                start=target,
                now=datetime(2026, 2, 26, 16, 0, tzinfo=IST),
                history_factory=DelayedHistory,
            )

            self.assertEqual(result["status"], "pending")
            self.assertFalse(result["complete_through"])
            self.assertEqual({item["symbol"] for item in result["pending"]}, {"NIFTY", "SENSEX"})

    def test_market_holiday_without_candles_is_complete_not_pending(self):
        target = self.days[-1]
        prior_rows = _series(self.days[:-1])

        class HolidayHistory:
            def __init__(self, _cache_root):
                pass

            def candles(self, _symbol, _start, _end):
                return prior_rows

            def is_trading_session(self, _day):
                return False

        with tempfile.TemporaryDirectory() as folder:
            result = backfill_charts(
                Path(folder) / "charts",
                Path(folder) / "cache",
                start=target,
                now=datetime(2026, 2, 26, 16, 0, tzinfo=IST),
                history_factory=HolidayHistory,
            )

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["complete_through"])
            self.assertEqual(result["pending"], [])

    def test_today_becomes_eligible_only_after_market_processing_window(self):
        day = date(2026, 8, 24)
        self.assertEqual(eligible_through(datetime(2026, 8, 24, 15, 44, tzinfo=IST)), day - timedelta(days=1))
        self.assertEqual(eligible_through(datetime(2026, 8, 24, 15, 45, tzinfo=IST)), day)


if __name__ == "__main__":
    unittest.main()
