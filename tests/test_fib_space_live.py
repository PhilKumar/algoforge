"""The paper driver: same decisions as the backtest, and no invented prices.

A paper run is only evidence if it reaches the SAME conclusions the backtest
did on the same bars.  The first test here is therefore the important one --
feed the driver a window one bar at a time and assert it ends up holding
exactly what run_space_campaign produces in a single pass.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cascade_options import IndexCandle  # noqa: E402
from engine.fib_space_cascade import SpaceCascadeConfig, run_space_campaign  # noqa: E402
from engine.fib_space_live import (  # noqa: E402
    CampaignHalted,
    FibSpacePaperBook,
    LiveCampaign,
    bars_from_candles,
)

START = datetime(2026, 3, 2, 9, 15)


class _Contract:
    def __init__(self, strike, expiry):
        self.strike, self.expiry = strike, expiry

    def __eq__(self, other):
        return (self.strike, self.expiry) == (other.strike, other.expiry)


def _select(when, index_price):
    return _Contract(strike=round(index_price / 100.0) * 100.0, expiry=date(2026, 3, 26))


def _always(price):
    return lambda when, contract: price


def _candles(rows, *, step_minutes=15):
    """rows: (open, high, low, close) per bar, walking forward from START."""
    out = []
    at = START
    for o, h, low, c in rows:
        out.append(IndexCandle(at, float(o), float(h), float(low), float(c)))
        at += timedelta(minutes=step_minutes)
    return out


def _falling_market(bars=90):
    """A mother high, then a long grind down: geometry, spaces and a recovery."""
    rows = []
    price = 24_000.0
    for i in range(6):  # run up into the pivot
        rows.append((price, price + 30, price - 10, price + 25))
        price += 25
    rows.append((price, price + 120, price - 20, price - 10))  # the mother
    price -= 10
    for i in range(bars):  # the fall, with regular small bounces
        drop = 45 if i % 5 else -35
        rows.append((price, price + 12, price - drop - 12, price - drop))
        price -= drop
    for i in range(30):  # the recovery that gives a target somewhere to land
        rows.append((price, price + 60, price - 8, price + 50))
        price += 50
    return _candles(rows)


class ParityWithTheBacktestTests(unittest.TestCase):
    """Bar-at-a-time must equal one-pass. This is the whole point of the driver."""

    def _book(self, **kwargs):
        return FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(150.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
            **kwargs,
        )

    def test_streaming_reaches_the_same_fills_as_one_pass(self):
        bars = bars_from_candles(_falling_market())
        mother_index = 6
        mother = bars[mother_index]

        one_pass = run_space_campaign(
            mother, bars[mother_index:], SpaceCascadeConfig(lot_size=30), arm_from_index=bars[mother_index + 5].index
        )
        self.assertTrue(one_pass.fills, "fixture must produce fills or it proves nothing")

        book = self._book()
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=mother,
            arm_from_index=None,
            confirmed_at=bars[mother_index + 5].timestamp,
            lot_size=30,
        )
        book.campaigns["c1"] = campaign
        for upto in range(mother_index + 1, len(bars) + 1):
            book.advance(campaign, bars[:upto], bars[:upto], now=bars[upto - 1].timestamp)

        self.assertEqual(
            [(f.timestamp, round(f.index_price, 2), f.lots) for f in campaign.fills],
            [(f.timestamp, round(f.index_price, 2), f.lots) for f in one_pass.fills],
        )

    def test_streaming_reaches_the_same_round_exits(self):
        bars = bars_from_candles(_falling_market())
        mother_index = 6
        mother = bars[mother_index]
        one_pass = run_space_campaign(
            mother, bars[mother_index:], SpaceCascadeConfig(lot_size=30), arm_from_index=bars[mother_index + 5].index
        )

        book = self._book()
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=mother,
            arm_from_index=None,
            confirmed_at=bars[mother_index + 5].timestamp,
            lot_size=30,
        )
        book.campaigns["c1"] = campaign
        for upto in range(mother_index + 1, len(bars) + 1):
            book.advance(campaign, bars[:upto], bars[:upto], now=bars[upto - 1].timestamp)

        closed = [r for r in one_pass.rounds if r.status == "closed"]
        self.assertEqual([e.timestamp for e in campaign.exits], [r.exit_timestamp for r in closed])


class PricingHonestyTests(unittest.TestCase):
    def _run_once(self, lookup):
        bars = bars_from_candles(_falling_market())
        mother = bars[6]
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=lookup,
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=mother,
            arm_from_index=None,
            confirmed_at=bars[11].timestamp,
            lot_size=30,
        )
        book.campaigns["c1"] = campaign
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)
        return campaign

    def test_a_fill_seen_too_late_is_recorded_unpriced_not_guessed(self):
        """A stale quote is not the fill that would have happened."""
        campaign = self._run_once(lambda when, contract: None)
        self.assertTrue(campaign.fills)
        self.assertTrue(all(f.premium is None for f in campaign.fills))
        self.assertGreater(campaign.unpriced, 0)
        self.assertIsNone(campaign.net, "an unpriced leg must poison the P&L, not be treated as zero")

    def test_a_priced_campaign_reports_its_net(self):
        campaign = self._run_once(_always(150.0))
        self.assertEqual(campaign.unpriced, 0)
        self.assertIsNotNone(campaign.net)

    def test_the_quote_is_taken_once_when_the_fill_is_first_seen(self):
        """Re-polling must not re-price a fill at a newer, better quote."""
        quotes = iter([100.0] + [999.0] * 50)
        bars = bars_from_candles(_falling_market())
        mother = bars[6]
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=lambda when, contract: next(quotes, 999.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=mother,
            arm_from_index=None,
            confirmed_at=bars[11].timestamp,
            lot_size=30,
        )
        book.campaigns["c1"] = campaign
        for upto in range(7, len(bars) + 1):
            book.advance(campaign, bars[:upto], bars[:upto], now=bars[upto - 1].timestamp)

        first = campaign.fills[0]
        self.assertEqual(first.premium, 100.0)
        # And it stays 100 no matter how many further polls run.
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)
        self.assertEqual(campaign.fills[0].premium, 100.0)


class HistoryIsImmutableTests(unittest.TestCase):
    def test_a_replay_that_drops_a_recorded_fill_halts_the_campaign(self):
        bars = bars_from_candles(_falling_market())
        mother = bars[6]
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(120.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=mother,
            arm_from_index=None,
            confirmed_at=bars[11].timestamp,
            lot_size=30,
        )
        book.campaigns["c1"] = campaign
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)
        self.assertTrue(campaign.fills)

        # Feed a SHORTER window: the later fills can no longer exist.
        with self.assertRaises(CampaignHalted):
            book.advance(campaign, bars[:20], bars[:20], now=bars[19].timestamp)
        self.assertEqual(campaign.status, "halted")
        self.assertIn("history changed", campaign.halt_reason)

    def test_a_halted_campaign_does_not_advance_again(self):
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(120.0),
            select_contract=_select,
        )
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=bars_from_candles(_falling_market())[6],
            arm_from_index=None,
            confirmed_at=START,
            lot_size=30,
            status="halted",
        )
        book.campaigns["c1"] = campaign
        self.assertEqual(book.advance(campaign, [], [], now=START), ([], []))


class MotherAdoptionTests(unittest.TestCase):
    class _Candidate:
        def __init__(self, index, confirmed_at):
            self.index, self.confirmed_at = index, confirmed_at

    def _book(self, **kwargs):
        return FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(100.0),
            select_contract=_select,
            **kwargs,
        )

    def test_a_mother_is_adopted_once_however_often_it_is_rescanned(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()
        candidate = self._Candidate(6, bars[11].timestamp)
        self.assertEqual(len(book.adopt_mothers(bars, [candidate])), 1)
        self.assertEqual(book.adopt_mothers(bars, [candidate]), [])
        self.assertEqual(len(book.campaigns), 1)

    def test_the_cooldown_refuses_a_start_inside_the_window(self):
        bars = bars_from_candles(_falling_market())
        book = self._book(cooldown_days=3)
        first = self._Candidate(6, datetime(2026, 3, 2, 10, 0))
        soon = self._Candidate(7, datetime(2026, 3, 3, 10, 0))
        later = self._Candidate(8, datetime(2026, 3, 9, 10, 0))
        self.assertEqual(len(book.adopt_mothers(bars, [first])), 1)
        self.assertEqual(book.adopt_mothers(bars, [soon]), [])
        self.assertEqual(len(book.adopt_mothers(bars, [later])), 1)

    def test_banknifty_runs_with_no_cooldown_by_default(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()
        one = self._Candidate(6, datetime(2026, 3, 2, 10, 0))
        two = self._Candidate(7, datetime(2026, 3, 2, 10, 15))
        self.assertEqual(len(book.adopt_mothers(bars, [one])), 1)
        self.assertEqual(len(book.adopt_mothers(bars, [two])), 1)


class GapRuleTests(unittest.TestCase):
    def test_a_session_open_is_measured_from_the_previous_close(self):
        """The live feed must carry the same gap rule as the backtest loader."""
        day1 = IndexCandle(datetime(2026, 3, 2, 15, 15), 24_000, 24_010, 23_990, 24_000)
        day2 = IndexCandle(datetime(2026, 3, 3, 9, 15), 23_800, 23_850, 23_780, 23_820)
        bars = bars_from_candles([day1, day2])

        self.assertEqual(bars[1].session_prev_close, 24_000)
        self.assertEqual(bars[1].effective_open, 24_000)
        # Green against its own open (23,800 -> 23,820), RED against yesterday.
        self.assertTrue(bars[1].is_red)

    def test_a_mid_session_bar_keeps_its_own_open(self):
        first = IndexCandle(datetime(2026, 3, 3, 9, 15), 23_800, 23_850, 23_780, 23_820)
        second = IndexCandle(datetime(2026, 3, 3, 9, 30), 23_820, 23_860, 23_800, 23_840)
        bars = bars_from_candles([first, second])

        self.assertIsNone(bars[1].session_prev_close)
        self.assertEqual(bars[1].effective_open, 23_820)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_counts_what_is_open_and_what_is_unpriceable(self):
        bars = bars_from_candles(_falling_market())
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=lambda when, contract: None,
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=bars[6],
            arm_from_index=None,
            confirmed_at=bars[11].timestamp,
            lot_size=30,
        )
        book.campaigns["c1"] = campaign
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)

        snap = book.snapshot()
        self.assertEqual(snap["symbol"], "banknifty")
        self.assertEqual(snap["campaigns"], 1)
        self.assertGreater(snap["unpriced_legs"], 0)
        self.assertEqual(snap["halted"], [])


if __name__ == "__main__":
    unittest.main()


class ClosedRoundsDistinguishesZeroFromBlankTests(unittest.TestCase):
    """A campaign holding open lots has realised nothing -- not "flat".

    net is 0.0 in both cases, which is arithmetically right and badly
    misleading in a P&L column, so callers need closed_rounds to tell them
    apart. The panel shows an em dash until this is non-zero.
    """

    def _campaign(self):
        bars = bars_from_candles(_falling_market())
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(120.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = LiveCampaign(
            campaign_id="c1",
            symbol="banknifty",
            mother=bars[6],
            arm_from_index=None,
            confirmed_at=bars[11].timestamp,
            lot_size=30,
        )
        book.campaigns["c1"] = campaign
        return book, campaign, bars

    def test_a_fresh_campaign_has_banked_nothing(self):
        _, campaign, _ = self._campaign()
        self.assertEqual(campaign.closed_rounds, 0)

    def test_an_open_position_reports_zero_net_but_no_closed_rounds(self):
        book, campaign, bars = self._campaign()
        # Stop before the recovery so the round is still open.
        book.advance(campaign, bars[:60], bars[:60], now=bars[59].timestamp)

        self.assertTrue(campaign.fills, "fixture must have opened a position")
        self.assertGreater(campaign.open_quantity, 0)
        self.assertEqual(campaign.closed_rounds, 0)
        self.assertEqual(campaign.net, 0.0, "realised is genuinely zero -- callers must not read it as flat")

    def test_a_banked_round_counts(self):
        book, campaign, bars = self._campaign()
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)

        self.assertGreater(campaign.closed_rounds, 0)
        self.assertEqual(campaign.closed_rounds, len(campaign.exits))


class NamedMotherTests(unittest.TestCase):
    """A mother the trader names, not one the scanner guessed at."""

    def _book(self, **kwargs):
        return FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(150.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
            **kwargs,
        )

    def test_a_named_mother_starts_a_campaign_marked_manual(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()
        campaign = book.adopt_manual_mother(bars[6])

        self.assertEqual(campaign.source, "manual")
        self.assertIs(campaign.mother, bars[6])
        self.assertEqual(len(book.campaigns), 1)

    def test_it_arms_at_the_mother_rather_than_waiting_for_a_right_shoulder(self):
        """A typed mother is a decision already made, not a guess to confirm."""
        bars = bars_from_candles(_falling_market())
        campaign = self._book().adopt_manual_mother(bars[6])
        self.assertEqual(campaign.confirmed_at, bars[6].timestamp)

    def test_naming_the_same_mother_twice_is_refused(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()
        book.adopt_manual_mother(bars[6])
        with self.assertRaises(ValueError):
            book.adopt_manual_mother(bars[6])
        self.assertEqual(len(book.campaigns), 1)

    def test_the_cooldown_does_not_apply_to_a_person(self):
        """The throttle stops duplicate AUTO pivots; naming two means two."""
        bars = bars_from_candles(_falling_market())
        book = self._book(cooldown_days=3)
        book.adopt_manual_mother(bars[6])
        book.adopt_manual_mother(bars[7])
        self.assertEqual(len(book.campaigns), 2)

    def test_a_named_mother_trades_exactly_like_a_scanned_one(self):
        """Same engine, same fills -- only the choice of mother differs."""
        bars = bars_from_candles(_falling_market())
        one_pass = run_space_campaign(bars[6], bars[6:], SpaceCascadeConfig(lot_size=30), arm_from_index=bars[6].index)

        book = self._book()
        campaign = book.adopt_manual_mother(bars[6])
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)

        self.assertEqual(
            [(f.timestamp, round(f.index_price, 2), f.lots) for f in campaign.fills],
            [(f.timestamp, round(f.index_price, 2), f.lots) for f in one_pass.fills],
        )

    def test_a_scanned_campaign_is_marked_auto(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()

        class _Candidate:
            index = 6
            confirmed_at = bars[11].timestamp

        started = book.adopt_mothers(bars, [_Candidate()])
        self.assertEqual(started[0].source, "auto")


class AwareCandlesBecomeNaiveBarsTests(unittest.TestCase):
    """The adapter returns TZ-AWARE candles; the backtest reads naive ones.

    That difference shipped and broke naming a mother: an aware datetime never
    equals a naive one, so "03 Aug 15:00" matched no bar and the route reported
    that Dhan had no such candle when Dhan had returned it. Fixtures here used
    naive candles, which is exactly why the tests missed it -- these use aware
    ones, like production does.
    """

    IST = timezone(timedelta(hours=5, minutes=30))

    def _aware(self, *stamps):
        return [IndexCandle(s.replace(tzinfo=self.IST), 57_000.0, 57_100.0, 56_900.0, 57_050.0) for s in stamps]

    def test_an_aware_candle_becomes_a_naive_bar(self):
        bars = bars_from_candles(self._aware(datetime(2026, 8, 3, 15, 0)))
        self.assertIsNone(bars[0].timestamp.tzinfo)
        self.assertEqual(bars[0].timestamp, datetime(2026, 8, 3, 15, 0))

    def test_a_naive_lookup_finds_a_bar_built_from_an_aware_candle(self):
        """The exact failure Phil hit."""
        bars = bars_from_candles(self._aware(datetime(2026, 8, 3, 14, 45), datetime(2026, 8, 3, 15, 0)))
        wanted = datetime(2026, 8, 3, 15, 0)
        self.assertIsNotNone(next((b for b in bars if b.timestamp == wanted), None))

    def test_the_gap_rule_still_reads_across_a_session_boundary(self):
        """Flattening must not break the previous-close carry."""
        bars = bars_from_candles(self._aware(datetime(2026, 8, 3, 15, 15), datetime(2026, 8, 4, 9, 15)))
        self.assertIsNone(bars[0].session_prev_close)
        self.assertEqual(bars[1].session_prev_close, bars[0].close)

    def test_naive_candles_are_left_alone(self):
        bars = bars_from_candles([IndexCandle(datetime(2026, 8, 3, 15, 0), 57_000.0, 57_100.0, 56_900.0, 57_050.0)])
        self.assertEqual(bars[0].timestamp, datetime(2026, 8, 3, 15, 0))
        self.assertIsNone(bars[0].timestamp.tzinfo)


class CampaignDetailTests(unittest.TestCase):
    """The trade sheet: premium paid, capital at stake, what came back."""

    def _traded(self, lookup=None):
        bars = bars_from_candles(_falling_market())
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=lookup or _always(120.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = book.adopt_manual_mother(bars[6])
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)
        return book, campaign

    def test_capital_spent_is_premium_times_quantity(self):
        book, campaign = self._traded()
        detail = book.campaign_detail(campaign, mark_to_market=False)

        expected = sum(f.premium * f.quantity for f in campaign.fills)
        self.assertAlmostEqual(detail["capital_spent"], round(expected, 2))
        self.assertGreater(detail["capital_spent"], 0)

    def test_every_fill_carries_its_premium_strike_and_outlay(self):
        book, campaign = self._traded()
        detail = book.campaign_detail(campaign, mark_to_market=False)

        fills = [f for r in detail["rounds"] for f in r["fills"]]
        self.assertEqual(len(fills), len(campaign.fills))
        for row in fills:
            self.assertIsNotNone(row["premium"])
            self.assertIsNotNone(row["strike"])
            self.assertAlmostEqual(row["outlay"], row["premium"] * row["quantity"])
            self.assertIn("space", row)

    def test_a_banked_round_reports_realised_as_proceeds_minus_cost(self):
        book, campaign = self._traded()
        detail = book.campaign_detail(campaign, mark_to_market=False)

        closed = [r for r in detail["rounds"] if r["status"] == "closed"]
        self.assertTrue(closed, "fixture must close a round")
        for row in closed:
            self.assertAlmostEqual(row["realised"], row["exit"]["proceeds"] - row["capital_spent"], places=2)
        self.assertAlmostEqual(detail["realised"], sum(r["realised"] for r in closed), places=2)

    def test_an_unpriced_leg_leaves_the_money_unknown_not_zero(self):
        book, campaign = self._traded(lookup=lambda when, contract: None)
        detail = book.campaign_detail(campaign, mark_to_market=False)

        self.assertGreater(detail["unpriced_legs"], 0)
        self.assertIsNone(detail["realised"])
        for row in detail["rounds"]:
            self.assertIsNone(row["capital_spent"])

    def test_mark_to_market_is_reported_apart_from_realised(self):
        """What it would fetch now is not a result and must not be added in."""
        bars = bars_from_candles(_falling_market())
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(100.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = book.adopt_manual_mother(bars[6])
        # Stop before the recovery so a round is still open to mark.
        book.advance(campaign, bars[:60], bars[:60], now=bars[59].timestamp)

        detail = book.campaign_detail(campaign, mark_to_market=True)
        self.assertGreater(detail["capital_open"], 0)
        self.assertIsNotNone(detail["open_value"])
        self.assertAlmostEqual(detail["unrealised"], detail["open_value"] - detail["capital_open"], places=2)
        self.assertEqual(detail["realised"], 0.0, "nothing banked yet")

    def test_the_contract_actually_bought_is_named(self):
        book, campaign = self._traded()
        detail = book.campaign_detail(campaign, mark_to_market=False)
        self.assertIsNotNone(detail["contract"])
        self.assertIsNotNone(detail["contract"]["strike"])


class CampaignChartTests(unittest.TestCase):
    def _traded(self):
        bars = bars_from_candles(_falling_market())
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(120.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = book.adopt_manual_mother(bars[6])
        book.advance(campaign, bars, bars, now=bars[-1].timestamp)
        return book, campaign

    def test_a_campaign_with_no_replay_yet_says_so_rather_than_drawing_nothing(self):
        bars = bars_from_candles(_falling_market())
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(120.0),
            select_contract=_select,
        )
        campaign = book.adopt_manual_mother(bars[6])
        self.assertEqual(book.campaign_chart(campaign)["status"], "not_ready")

    def test_every_timestamp_is_epoch_seconds(self):
        """An ISO string silently draws nothing — the renderer does maths on t."""
        book, campaign = self._traded()
        chart = book.campaign_chart(campaign)

        stamps = (
            [c["t"] for c in chart["candles"]]
            + [e["t"] for e in chart["entries"]]
            + [e["t"] for e in chart["exits"]]
            + [leg["touch_timestamp"] for leg in chart["legs"]]
        )
        self.assertTrue(stamps)
        for value in stamps:
            self.assertIsInstance(value, int)
            self.assertGreater(value, 1_000_000_000)

    def test_the_mother_bar_is_flagged_exactly_once(self):
        book, campaign = self._traded()
        chart = book.campaign_chart(campaign)
        self.assertEqual(sum(1 for c in chart["candles"] if c["is_mother"]), 1)

    def test_geometry_and_fills_reach_the_payload(self):
        book, campaign = self._traded()
        chart = book.campaign_chart(campaign)

        self.assertTrue(chart["legs"], "the fibs it drew must be drawable")
        self.assertEqual(len(chart["entries"]), len(campaign.fills))
        self.assertEqual(len(chart["exits"]), len(campaign.exits))
        self.assertEqual(chart["mother"]["high"], campaign.mother.high)

    def test_a_fib_leg_carries_the_ladder_levels(self):
        book, campaign = self._traded()
        leg = book.campaign_chart(campaign)["legs"][0]
        for level in ("0", "1", "2", "4", "8"):
            self.assertIn(level, leg["levels"])
        # Level 8 is eight leg-ranges below level 0, so it must be far lower.
        self.assertLess(leg["levels"]["8"], leg["levels"]["0"])

    def test_the_target_is_never_labelled_hit_when_it_was_not(self):
        bars = bars_from_candles(_falling_market())
        book = FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=_always(120.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )
        campaign = book.adopt_manual_mother(bars[6])
        book.advance(campaign, bars[:60], bars[:60], now=bars[59].timestamp)

        self.assertFalse(campaign.exits)
        self.assertEqual(book.campaign_chart(campaign)["tp_label"], "TARGET")


class PreviewDrawsWithoutTradingTests(unittest.TestCase):
    """A mother named after the close must still be drawable.

    This is the bug Phil hit: the chart only had something to draw once a POLL
    had run, and the poll is session-gated, so naming a mother in the evening
    left the chart permanently empty. Geometry needs closed candles, not an open
    market; only recording a fill needs a live quote.
    """

    def _book(self, lookup=None):
        return FibSpacePaperBook(
            "banknifty",
            config=SpaceCascadeConfig(lot_size=30),
            premium_lookup=lookup or _always(120.0),
            select_contract=_select,
            entry_timeframe="15m",
            geometry_timeframe="15m",
        )

    def test_a_chart_is_available_before_any_poll(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()
        campaign = book.adopt_manual_mother(bars[6])

        self.assertEqual(book.campaign_chart(campaign)["status"], "not_ready")
        book.preview(campaign, bars, bars)
        chart = book.campaign_chart(campaign)

        self.assertEqual(chart["status"], "ok")
        self.assertTrue(chart["candles"])
        self.assertTrue(chart["legs"], "the fibs must be drawn without a poll")

    def test_preview_records_no_fill_and_spends_no_money(self):
        """Looking must never be mistaken for trading."""
        bars = bars_from_candles(_falling_market())
        book = self._book()
        campaign = book.adopt_manual_mother(bars[6])
        book.preview(campaign, bars, bars)

        self.assertEqual(campaign.fills, [])
        self.assertEqual(campaign.exits, [])
        self.assertEqual(campaign.unpriced, 0)
        self.assertEqual(campaign.status, "watching")
        self.assertEqual(book.campaign_detail(campaign, mark_to_market=False)["capital_spent"], 0)

    def test_preview_never_asks_for_a_quote(self):
        """A stale premium must not be fetched just because a chart was opened."""
        asked = []

        def _watching(when, contract):
            asked.append(when)
            return 120.0

        bars = bars_from_candles(_falling_market())
        book = self._book(lookup=_watching)
        campaign = book.adopt_manual_mother(bars[6])
        book.preview(campaign, bars, bars)
        self.assertEqual(asked, [])

    def test_a_later_poll_still_records_normally(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()
        campaign = book.adopt_manual_mother(bars[6])
        book.preview(campaign, bars, bars)

        fills, _ = book.advance(campaign, bars, bars, now=bars[-1].timestamp)
        self.assertTrue(fills, "previewing must not consume the fills")
        self.assertTrue(campaign.fills)

    def test_preview_and_advance_reach_the_same_geometry(self):
        bars = bars_from_candles(_falling_market())
        book = self._book()
        a = book.adopt_manual_mother(bars[6])
        book.preview(a, bars, bars)
        previewed = book.campaign_chart(a)

        book.advance(a, bars, bars, now=bars[-1].timestamp)
        polled = book.campaign_chart(a)

        self.assertEqual(len(previewed["legs"]), len(polled["legs"]))
        self.assertEqual(previewed["candles"], polled["candles"])
