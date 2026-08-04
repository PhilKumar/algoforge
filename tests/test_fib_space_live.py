"""The paper driver: same decisions as the backtest, and no invented prices.

A paper run is only evidence if it reaches the SAME conclusions the backtest
did on the same bars.  The first test here is therefore the important one --
feed the driver a window one bar at a time and assert it ends up holding
exactly what run_space_campaign produces in a single pass.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta

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
