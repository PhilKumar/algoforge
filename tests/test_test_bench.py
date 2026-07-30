import unittest
from datetime import datetime

from engine.test_bench import bench_chart, bench_summary, outcome_label


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


class SummaryTests(unittest.TestCase):
    def test_the_verdict_reads_without_a_legend(self):
        summary = bench_summary(a_replay(), instrument="NIFTY", timeframe="5m", mother_timestamp=iso(21, 9))
        self.assertEqual(summary["outcome"], "Target hit")
        self.assertEqual(summary["entry_timestamp"], iso(21, 10))
        self.assertEqual(summary["exit_timestamp"], iso(22, 11))
        self.assertEqual(summary["strike"], 24450)
        self.assertEqual(summary["lot_size"], 65)

    def test_spend_is_the_premium_that_actually_left_the_account(self):
        summary = bench_summary(a_replay(), instrument="NIFTY", timeframe="5m", mother_timestamp=iso(21, 9))
        self.assertEqual(summary["spend_inr"], 11700.0 + 19500.0)
        self.assertEqual(summary["unpriced_entries"], 0)

    def test_an_unpriced_entry_is_counted_not_treated_as_free(self):
        # Upstox had no bar for the second buy.  Adding 0 to the spend would
        # report a two-leg trade at a one-leg cost, which is worse than useless.
        replay = a_replay()
        replay["entries"][1]["spend_inr"] = None
        replay["entries"][1]["option_price"] = None
        summary = bench_summary(replay, instrument="NIFTY", timeframe="5m", mother_timestamp=iso(21, 9))
        self.assertEqual(summary["spend_inr"], 11700.0)
        self.assertEqual(summary["unpriced_entries"], 1)
        self.assertEqual(summary["entry_count"], 2)

    def test_an_expiry_exit_says_so_in_plain_words(self):
        summary = bench_summary(
            a_replay(exit_reason="expiry"), instrument="NIFTY", timeframe="1h", mother_timestamp=iso(21, 9)
        )
        self.assertEqual(summary["outcome"], "Held to expiry")

    def test_an_unfamiliar_exit_reason_is_still_readable(self):
        self.assertEqual(outcome_label("some_new_reason"), "Some new reason")

    def test_a_mother_that_never_fell_far_enough_says_so(self):
        # The common case. The engine's own word for it is "awaiting a quote",
        # which reads like a fault rather than the ordinary outcome it is.
        replay = a_replay(entries=[], exit_reason="awaiting_option_quote", contract={})
        summary = bench_summary(replay, instrument="NIFTY", timeframe="1h", mother_timestamp=iso(21, 9))
        self.assertEqual(summary["outcome"], "No buy — the index never reached a level")
        self.assertEqual(summary["spend_inr"], 0.0)


class ChartTests(unittest.TestCase):
    def test_timestamps_arrive_as_epoch_seconds_for_the_projection(self):
        chart = bench_chart(a_geometry(), a_replay(), timeframe="5m")
        self.assertEqual(chart["candles"][0]["t"], epoch(21, 9))
        self.assertTrue(chart["candles"][0]["is_mother"])
        self.assertEqual(chart["trendlines"][0]["a1"], {"t": epoch(21, 9), "p": 24640.0})

    def test_the_working_trendline_is_the_one_marked_active(self):
        chart = bench_chart(a_geometry(), a_replay(), timeframe="5m")
        self.assertFalse(chart["trendlines"][0]["active"])
        self.assertTrue(chart["trendlines"][1]["active"])

    def test_each_level_carries_what_that_level_cost(self):
        chart = bench_chart(a_geometry(), a_replay(), timeframe="5m")
        orders = {row["level"]: row["inr_notional"] for row in chart["legs"][0]["orders"]}
        self.assertEqual(orders, {4: 11700.0, 8: 19500.0})

    def test_a_level_that_never_traded_carries_no_figure(self):
        chart = bench_chart(a_geometry(), a_replay(), timeframe="5m")
        self.assertNotIn(2, [row["level"] for row in chart["legs"][0]["orders"]])

    def test_spend_lands_on_the_leg_it_came_from(self):
        # Two legs, same level number.  Keying on the level alone would pile
        # both buys onto whichever leg the chart drew first.
        geometry = a_geometry()
        geometry["legs"].append(
            {
                "leg_id": 2,
                "trendline_id": 2,
                "touch_t": iso(21, 13),
                "touch_high": 24530.0,
                "low": 24480.0,
                "levels": {"0": 24530.0, "1": 24480.0, "2": 24430.0, "4": 24330.0, "8": 24130.0},
            }
        )
        replay = a_replay()
        replay["entries"][1]["leg_id"] = 2
        replay["entries"][1]["level"] = 4
        chart = bench_chart(geometry, replay, timeframe="5m")
        self.assertEqual(chart["legs"][0]["orders"], [{"level": 4, "inr_notional": 11700.0}])
        self.assertEqual(chart["legs"][1]["orders"], [{"level": 4, "inr_notional": 19500.0}])

    def test_entries_and_exits_become_chart_markers(self):
        chart = bench_chart(a_geometry(), a_replay(), timeframe="5m")
        self.assertEqual([row["t"] for row in chart["entries"]], [epoch(21, 10), epoch(21, 13)])
        self.assertEqual(chart["exits"], [{"t": epoch(22, 11), "price": 24690.0, "pnl": 18400.0}])

    def test_a_target_that_was_reached_is_labelled_as_reached(self):
        chart = bench_chart(a_geometry(), a_replay(), timeframe="5m")
        self.assertEqual(chart["tp_label"], "TARGET HIT")

    def test_a_target_that_was_never_reached_is_not_drawn_as_a_sale(self):
        # The one line on this chart that says, at a glance, whether the trade
        # worked.  Upstream labelled it "SOLD AT" for any ended campaign.
        chart = bench_chart(a_geometry(), a_replay(exit_reason="expiry"), timeframe="5m")
        self.assertEqual(chart["tp_label"], "TARGET (not reached)")

    def test_an_empty_replay_does_not_explode(self):
        chart = bench_chart({}, {}, timeframe="5m")
        self.assertEqual(chart["candles"], [])
        self.assertEqual(chart["legs"], [])
        self.assertEqual(chart["entries"], [])


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
        from engine.test_bench import ladder_result

        ladder, _mother, _replay = self._open_ladder()
        reported = ladder_result(
            ladder, instrument="NIFTY", timeframe="5m", mother_timestamp="2026-07-28T09:15:00", lot_size=75
        )
        self.assertEqual(reported["summary"]["outcome"], "Still OPEN — target not yet reached")
        self.assertTrue(reported["summary"]["still_open"])
        self.assertIsNone(reported["summary"]["exit_timestamp"])

    def test_an_open_trade_labels_its_target_as_watching_not_missed(self):
        from engine.test_bench import ladder_chart

        ladder, mother, replay = self._open_ladder()
        chart = ladder_chart(ladder, [mother, *replay], timeframe="5m")
        self.assertEqual(chart["tp_label"], "TARGET (open — watching)")

    def test_the_mother_bar_is_in_the_chart_and_flagged(self):
        from engine.test_bench import ladder_chart

        ladder, mother, replay = self._open_ladder()
        chart = ladder_chart(ladder, [mother, *replay], timeframe="5m")
        flagged = [row for row in chart["candles"] if row["is_mother"]]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["o"], mother.open)


if __name__ == "__main__":
    unittest.main()
