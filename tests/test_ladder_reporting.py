"""What Candle Entry's replay reports as -- its table and its chart.

These were the Test Bench's tests. The bench was retired 2026-08-29, but
`ladder_result` and `ladder_chart` were never really its own: Candle Entry
reports through them, so the coverage moved here with the module rather than
being deleted with the feature.
"""

import unittest
from datetime import datetime


def iso(day: int, hour: int, minute: int = 15) -> str:
    return datetime(2026, 7, day, hour, minute).isoformat()


def epoch(day: int, hour: int, minute: int = 15) -> int:
    return int(datetime(2026, 7, day, hour, minute).timestamp())


def a_replay(**overrides) -> dict:
    """A two-entry campaign that reached its target, unless overridden."""
    replay = {
        "exit_reason": "target",
        "exit_timestamp": iso(22, 11),
        "net_pnl": 18400.0,
        "costs_total": 620.0,
        "target_index": 24680.0,
        "average_spot": 24510.0,
        "fully_priced": True,
        "data_gaps": [],
        "contract": {
            "underlying": "NIFTY",
            "strike": 24450,
            "option_type": "CE",
            "expiry": "2026-08-04",
            "lot_size": 65,
        },
        "entries": [
            {
                "timestamp": iso(21, 10),
                "spot": 24520.0,
                "option_price": 180.0,
                "lots": 1,
                "quantity": 65,
                "level": 4,
                "leg_id": 1,
                "spend_inr": 11700.0,
                "strike": 24450,
                "option_type": "CE",
                "expiry": "2026-08-04",
            },
            {
                "timestamp": iso(21, 13),
                "spot": 24500.0,
                "option_price": 150.0,
                "lots": 2,
                "quantity": 130,
                "level": 8,
                "leg_id": 1,
                "spend_inr": 19500.0,
                "strike": 24400,
                "option_type": "CE",
                "expiry": "2026-08-04",
            },
        ],
    }
    replay.update(overrides)
    return replay


def a_geometry() -> dict:
    return {
        "candles": [
            {"t": iso(21, 9), "o": 24600, "h": 24640, "l": 24560, "c": 24570, "is_mother": True},
            {"t": iso(21, 10), "o": 24570, "h": 24580, "l": 24500, "c": 24520, "is_mother": False},
            {"t": iso(21, 13), "o": 24520, "h": 24530, "l": 24480, "c": 24500, "is_mother": False},
        ],
        "mother": {"t": iso(21, 9), "high": 24640.0, "low": 24560.0},
        "trendlines": [
            {"id": 1, "a1t": iso(21, 9), "a1p": 24640.0, "a2t": iso(21, 10), "a2p": 24580.0},
            {"id": 2, "a1t": iso(21, 10), "a1p": 24580.0, "a2t": iso(21, 13), "a2p": 24530.0},
        ],
        "legs": [
            {
                "leg_id": 1,
                "trendline_id": 1,
                "touch_t": iso(21, 10),
                "touch_high": 24580.0,
                "low": 24500.0,
                "levels": {"0": 24580.0, "1": 24500.0, "2": 24420.0, "4": 24260.0, "8": 23940.0},
            }
        ],
        "rounds": [
            {
                "round_id": 1,
                "opened_at": iso(21, 10),
                "closed_at": iso(22, 11),
                "target_index": 24680.0,
                "exit_index_price": 24690.0,
                "exit_reason": "target",
                "net_pnl": 18400.0,
                "fills": [
                    {"timestamp": iso(21, 10), "index_price": 24520.0, "lots": 1},
                    {"timestamp": iso(21, 13), "index_price": 24500.0, "lots": 2},
                ],
            }
        ],
        "open_fills": [],
    }


class LadderReportingTests(unittest.TestCase):
    """An open ladder is reported as OPEN, and the mother candle is drawn."""

    @staticmethod
    def _open_ladder():
        from datetime import datetime, timedelta

        from engine.candle_ladder import LadderCandle, TwoRedLadder

        start = datetime(2026, 7, 28, 9, 15)

        def bar(offset, o, h, low, c):
            return LadderCandle("5m", start + timedelta(minutes=5 * offset), o, h, low, c)

        mother = bar(0, 104, 110, 104, 105)
        ladder = TwoRedLadder(
            mother,
            stages=("5m", "15m"),
            strike_for=lambda _ts, price: (24800, "CE"),
            premium_lookup=lambda _ts, _strike, _side: 50.0,
            lot_size=75,
        )
        replay = [
            bar(1, 105, 106, 104, 106),
            bar(2, 106, 106, 102, 103),  # red 1
            bar(3, 103, 103, 100, 101),  # red 2 -> stop armed at 103
            bar(4, 101, 103.5, 100, 103),  # recovery fills, target above all highs
        ]
        ladder.run(replay)
        assert ladder.fills and ladder.exit_timestamp is None
        return ladder, mother, replay

    def test_an_unfinished_recent_trade_reads_as_open_not_expired(self):
        from engine.ladder_reporting import ladder_result

        ladder, _mother, _replay = self._open_ladder()
        reported = ladder_result(
            ladder, instrument="NIFTY", timeframe="5m", mother_timestamp="2026-07-28T09:15:00", lot_size=75
        )
        self.assertEqual(reported["summary"]["outcome"], "Still OPEN — target not yet reached")
        self.assertTrue(reported["summary"]["still_open"])
        self.assertIsNone(reported["summary"]["exit_timestamp"])

    def test_an_open_trade_labels_its_target_as_watching_not_missed(self):
        from engine.ladder_reporting import ladder_chart

        ladder, mother, replay = self._open_ladder()
        chart = ladder_chart(ladder, [mother, *replay], timeframe="5m")
        self.assertEqual(chart["tp_label"], "TARGET (open — watching)")

    def test_the_mother_bar_is_in_the_chart_and_flagged(self):
        from engine.ladder_reporting import ladder_chart

        ladder, mother, replay = self._open_ladder()
        chart = ladder_chart(ladder, [mother, *replay], timeframe="5m")
        flagged = [row for row in chart["candles"] if row["is_mother"]]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["o"], mother.open)


if __name__ == "__main__":
    unittest.main()
