"""Phil's stop-loss recovery rules, one behavior per test.

The premium tape is a dict of minute -> price, so every trade's rupee outcome
is chosen by the test and the thresholds are computed with the REAL cost
function -- no hand-typed expected rupees that rot when the schedule changes.
"""

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cascade_costs import OptionCostFill, calculate_nifty_option_basket_round_costs  # noqa: E402
from engine.candle_recovery import RecoveryBar, RecoveryConfig, TwoRedRecovery  # noqa: E402

LOT = 65  # NIFTY lot from 2026-01-01
T0 = datetime(2026, 2, 2, 9, 15)
EXPIRY = date(2026, 2, 12)


def bar(i: int, o: float, h: float, l: float, c: float) -> RecoveryBar:  # noqa: E741
    return RecoveryBar(T0 + timedelta(minutes=5 * i), o, h, l, c)


def net_of(entry: float, exit_: float, lots: int = 1) -> float:
    qty = lots * LOT
    costs = calculate_nifty_option_basket_round_costs(
        buys=[OptionCostFill(price=entry, quantity=qty, lots=lots)],
        sell_price=exit_,
        sell_quantity=qty,
        sell_lots=lots,
    )
    return round((exit_ - entry) * qty - costs.total, 2)


class Harness:
    """A campaign wired to a premium tape the test controls."""

    def __init__(self, mother: RecoveryBar, config: RecoveryConfig | None = None):
        self.tape: dict[datetime, float] = {}
        self.engine = TwoRedRecovery(
            mother,
            config or RecoveryConfig(timeframe="5m"),
            contract_for=lambda when, index: (24000, EXPIRY),
            premium_lookup=lambda when, strike, expiry: self.tape.get(when),
            lot_size=LOT,
        )

    def price(self, when: datetime, premium: float, minutes: int = 10) -> None:
        """Premium is `premium` for every minute in [when, when+minutes)."""
        for offset in range(minutes):
            self.tape[when + timedelta(minutes=offset)] = premium


MOTHER = bar(0, 100.0, 110.0, 99.0, 108.0)


class ArmingRules(unittest.TestCase):
    def test_second_red_closing_below_first_low_arms_at_its_high(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))  # red no. 1, low 104
        h.engine.on_bar(bar(2, 105, 106, 102, 103))  # red, but close 103 >= 104? no -- 103 < 104: arms
        self.assertEqual(h.engine.status, "ARMED")
        self.assertEqual(h.engine.trades[0].trigger, 106)

    def test_a_close_below_prior_close_but_not_below_its_low_does_not_arm(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 104.2, 104.5))  # red, lower close, low NOT eaten
        self.assertEqual(h.engine.status, "WATCHING")
        self.assertEqual(h.engine.trades, [])

    def test_a_green_between_reds_resets_the_pair(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))  # red
        h.engine.on_bar(bar(2, 105, 107, 104.5, 106))  # green
        h.engine.on_bar(bar(3, 106, 106.5, 101, 102))  # red, closes below 104 -- but prev is green
        self.assertEqual(h.engine.status, "WATCHING")

    def test_a_newer_pair_rearms_the_trigger_lower(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))  # armed at 106
        h.engine.on_bar(bar(3, 103, 103.5, 100, 100.5))  # red, close 100.5 < prev low 102 -> re-arm at 103.5
        self.assertEqual(len(h.engine.trades), 1)
        self.assertEqual(h.engine.trades[0].trigger, 103.5)


class FillAndStop(unittest.TestCase):
    def _armed(self) -> Harness:
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))  # armed at 106, ultimate low 102 after this bar
        return h

    def test_fill_at_trigger_marks_sl_at_the_entry_candle_low(self):
        h = self._armed()
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 101.5, 106))  # rises through 106
        trade = h.engine.trades[0]
        self.assertEqual(h.engine.status, "IN_TRADE")
        self.assertEqual(trade.entry_index, 106)
        self.assertEqual(trade.sl_level, 101.5)  # the ENTRY candle's low
        self.assertEqual(trade.entry_premium, 100.0)

    def test_a_wick_below_the_sl_does_not_stop_but_a_close_does(self):
        h = self._armed()
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 102.5, 106))  # fill; SL = 102 (bar 2's low)
        h.price(bar(4, 0, 0, 0, 0).timestamp, 95.0, minutes=15)
        h.engine.on_bar(bar(4, 106, 106.2, 101, 103))  # wick to 101 < 102, close 103 -- survives
        self.assertEqual(h.engine.status, "IN_TRADE")
        h.price(bar(5, 0, 0, 0, 0).timestamp, 90.0, minutes=15)
        h.engine.on_bar(bar(5, 103, 103.5, 100, 101))  # CLOSE 101 < 102 -- stopped
        trade = h.engine.trades[0]
        self.assertEqual(trade.exit_reason, "stop")
        self.assertEqual(trade.net_pnl, net_of(100.0, 90.0))
        self.assertEqual(h.engine.status, "WATCHING")


class RecoveryTarget(unittest.TestCase):
    def test_second_trade_exits_only_after_paying_back_the_loss_plus_margin(self):
        h = Harness(MOTHER)
        # trade 1: arm, fill at premium 100, stop at premium 90 -> a real booked loss
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 102.5, 106))
        h.price(bar(4, 0, 0, 0, 0).timestamp, 90.0, minutes=15)
        h.engine.on_bar(bar(4, 106, 106.2, 100, 101))  # close below SL 102 -> stopped
        loss = h.engine.trades[0].net_pnl
        self.assertLess(loss, 0)
        required = -loss + 500.0

        # trade 2: the stop bar is red; next red closes below its low -> re-armed
        h.engine.on_bar(bar(5, 101, 101.5, 99, 99.5))  # second red, close 99.5 < 100 -> armed at 101.5
        self.assertEqual(h.engine.status, "ARMED")
        h.price(bar(6, 0, 0, 0, 0).timestamp, 95.0)
        h.engine.on_bar(bar(6, 99.5, 102, 99, 101.8))  # fills at 101.5
        trade2 = h.engine.trades[1]
        self.assertEqual(trade2.entry_premium, 95.0)

        # a premium that nets LESS than the requirement must not exit...
        not_enough = 95.0 + (required / (LOT)) * 0.5
        h.price(bar(7, 0, 0, 0, 0).timestamp, not_enough, minutes=15)
        h.engine.on_bar(bar(7, 101.8, 103, 101.5, 102.5))
        self.assertEqual(h.engine.status, "IN_TRADE")

        # ...and one that clears it exits the campaign RECOVERED, net >= required
        enough = 95.0 + (required + 200.0) / LOT  # +200 headroom for costs
        h.price(bar(8, 0, 0, 0, 0).timestamp, enough, minutes=15)
        h.engine.on_bar(bar(8, 102.5, 104, 102, 103.5))
        self.assertEqual(h.engine.status, "RECOVERED")
        self.assertEqual(trade2.exit_reason, "target")
        self.assertGreaterEqual(trade2.net_pnl, required)
        self.assertGreater(h.engine.booked_net, 0)


class Endings(unittest.TestCase):
    def test_a_campaign_that_never_recovers_ends_abandoned_with_the_ledger_told(self):
        h = Harness(MOTHER, RecoveryConfig(timeframe="5m", horizon_sessions=1))
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 102.5, 106))
        h.price(bar(4, 0, 0, 0, 0).timestamp, 80.0, minutes=15)
        h.engine.on_bar(bar(4, 106, 106.2, 100, 101))  # stopped, loss booked
        # next session -> horizon (1) exceeded on first bar of day 2
        nxt = RecoveryBar(T0 + timedelta(days=1), 101, 102, 100, 101.5)
        h.engine.on_bar(nxt)
        self.assertEqual(h.engine.status, "ABANDONED")
        self.assertLess(h.engine.booked_net, 0)

    def test_missing_premium_defers_the_target_check_instead_of_inventing(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 102.5, 106))
        # no tape for bar 4 at all -> no exit, no crash, still in trade
        h.engine.on_bar(bar(4, 106, 108, 105, 107))
        self.assertEqual(h.engine.status, "IN_TRADE")


if __name__ == "__main__":
    unittest.main()


class IntradayAndLots(unittest.TestCase):
    def test_second_trade_takes_two_lots(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 102.5, 106))
        self.assertEqual(h.engine.trades[0].lots, 1)
        h.price(bar(4, 0, 0, 0, 0).timestamp, 90.0, minutes=15)
        h.engine.on_bar(bar(4, 106, 106.2, 100, 101))  # stopped
        h.engine.on_bar(bar(5, 101, 101.5, 99, 99.5))  # re-armed
        h.price(bar(6, 0, 0, 0, 0).timestamp, 95.0)
        h.engine.on_bar(bar(6, 99.5, 102, 99, 101.8))  # fills
        self.assertEqual(h.engine.trades[1].lots, 2)
        self.assertEqual(h.engine.trades[1].quantity, 2 * LOT)

    def test_the_days_last_bar_squares_off_the_open_trade(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 102.5, 106))
        self.assertEqual(h.engine.status, "IN_TRADE")
        eod = datetime(2026, 2, 2, 15, 25)  # 5m EOD bar
        h.price(eod, 104.0)
        h.engine.on_bar(RecoveryBar(eod, 106, 107, 105.5, 106.5))
        trade = h.engine.trades[0]
        self.assertEqual(trade.exit_reason, "eod")
        self.assertEqual(trade.exit_premium, 104.0)
        self.assertEqual(h.engine.status, "WATCHING")

    def test_no_new_fill_on_the_eod_bar(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))  # armed at 106
        eod = datetime(2026, 2, 2, 15, 25)
        h.price(eod, 100.0)
        h.engine.on_bar(RecoveryBar(eod, 103, 107, 103, 106.5))  # crosses trigger but EOD
        self.assertEqual(h.engine.status, "ARMED")
        self.assertIsNone(h.engine.trades[0].entry_time)

    def test_an_armed_trigger_and_a_half_pair_die_overnight(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))  # armed
        nxt = datetime(2026, 2, 3, 9, 15)
        h.engine.on_bar(RecoveryBar(nxt, 103, 107, 102.5, 106.8))  # crosses 106 next day
        self.assertEqual(h.engine.status, "WATCHING")  # trigger did not survive the night
        self.assertEqual(h.engine.trades, [])


class FibZoneMode(unittest.TestCase):
    """The 5m two-fib zone variant: seller fib (mother->low), buyer fib
    (bounce high->low, born when the low breaks), entries at the 2-2 and 4-4
    zones with 1 and 2 lots."""

    def _campaign(self):
        from engine.candle_recovery import FibZoneEntry

        mother = RecoveryBar(T0, 100.0, 110.0, 100.0, 108.0)
        h = Harness.__new__(Harness)
        h.tape = {}
        h.engine = FibZoneEntry(
            mother,
            RecoveryConfig(timeframe="5m"),
            contract_for=lambda when, index: (24000, EXPIRY),
            premium_lookup=lambda when, strike, expiry: h.tape.get(when),
            lot_size=LOT,
        )
        h.price = Harness.price.__get__(h)
        return h

    def _to_zones(self, h):
        # fall to a pivot low at 90 (confirmed 3/3), bounce to 102, break 90
        seq = [
            (1, 108, 108, 97, 98),
            (2, 98, 99, 95, 96),
            (3, 96, 97, 93, 94),  # three left bars
            (4, 94, 95, 90, 91),  # the pivot low, 90
            (5, 91, 95, 90.5, 94),
            (6, 94, 99, 93, 98),
            (7, 98, 102, 97, 101),  # confirm + bounce to 102
            (8, 101, 101.5, 92, 93),
            (9, 93, 93.5, 88, 89),  # break: close 89 < 90
        ]
        for row in seq:
            h.engine.on_bar(bar(*row))

    def test_zones_are_bracketed_by_the_two_fibs(self):
        h = self._campaign()
        self._to_zones(h)
        self.assertEqual(h.engine.status, "ZONES")
        z2, z4 = h.engine.zones
        # seller fib: 110 - k*(110-90); buyer fib: 102 - k*(102-90)
        self.assertEqual((z2.lower, z2.upper), (70.0, 78.0))
        self.assertEqual((z4.lower, z4.upper), (30.0, 54.0))
        self.assertEqual((z2.lots, z4.lots), (1, 2))

    def test_zone2_entry_takes_one_lot_and_zone4_two(self):
        h = self._campaign()
        self._to_zones(h)
        # two reds with the second closing below zone-2's upper (78)
        h.engine.on_bar(bar(10, 89, 89.5, 80, 81))
        h.engine.on_bar(bar(11, 81, 82, 76, 77))  # close 77 < prev low 80 and < 78 -> armed at 82
        h.price(bar(12, 0, 0, 0, 0).timestamp, 50.0)
        h.engine.on_bar(bar(12, 77, 83, 76.5, 82.5))  # fills
        t1 = h.engine.trades[0]
        self.assertEqual(t1.lots, 1)
        # deeper: two reds with the second closing below zone-4's upper (54)
        h.engine.on_bar(bar(13, 82, 82.5, 60, 61))
        h.engine.on_bar(bar(14, 61, 62, 52, 53))  # close 53 < prev low 60 and < 54 -> armed
        h.price(bar(15, 0, 0, 0, 0).timestamp, 30.0)
        h.engine.on_bar(bar(15, 53, 63, 52.5, 62))  # fills
        t2 = h.engine.trades[1]
        self.assertEqual(t2.lots, 2)
        # The plunge that reaches zone 4 CLOSES below the zone-2 trade's SL on
        # the way, so trade 1 is stopped and its loss joins the ledger the
        # 2-lot entry must now recover. The zones are sequential by design.
        self.assertEqual(t1.exit_reason, "stop")
        self.assertEqual(h.engine.open_trades, [t2])

    def test_basket_leaves_together_when_ledger_plus_open_clears_margin(self):
        h = self._campaign()
        self._to_zones(h)
        h.engine.on_bar(bar(10, 89, 89.5, 80, 81))
        h.engine.on_bar(bar(11, 81, 82, 76, 77))
        h.price(bar(12, 0, 0, 0, 0).timestamp, 50.0)
        h.engine.on_bar(bar(12, 77, 83, 76.5, 82.5))
        # premium rises enough that one lot's net clears the Rs 500 margin
        h.price(bar(13, 0, 0, 0, 0).timestamp, 65.0, minutes=15)
        h.engine.on_bar(bar(13, 82.5, 84, 81, 83))
        self.assertEqual(h.engine.status, "RECOVERED")
        self.assertEqual(h.engine.trades[0].exit_reason, "target")

    def test_mother_break_before_the_first_low_ends_the_campaign(self):
        h = self._campaign()
        h.engine.on_bar(bar(1, 108, 111, 107, 110.5))  # above mother high 110
        self.assertEqual(h.engine.status, "ENDED")
        self.assertEqual(h.engine.end_reason, "mother_broken")


class NewLowGate(unittest.TestCase):
    """After a trade closes, the next may not arm until a candle CLOSES below
    the standing ultimate low.

    The 30 Mar 2026 campaign is the specimen: the index gapped to 22,470.15,
    bounced to 22,714, made two reds INSIDE that bounce, and the engine bought
    22,629.80 -- 160 points over value with nothing closed under the low. It
    then fell back and stopped for -Rs 15,849.
    """

    def _campaign(self):
        from engine.candle_recovery import FibZoneEntry

        mother = RecoveryBar(T0, 100.0, 110.0, 100.0, 108.0)
        h = Harness.__new__(Harness)
        h.tape = {}
        h.engine = FibZoneEntry(
            mother,
            RecoveryConfig(timeframe="5m"),
            contract_for=lambda when, index: (24000, EXPIRY),
            premium_lookup=lambda when, strike, expiry: h.tape.get(when),
            lot_size=LOT,
        )
        h.price = Harness.price.__get__(h)
        return h

    def _into_first_trade(self, h):
        for row in [
            (1, 108, 108, 97, 98),
            (2, 98, 99, 95, 96),
            (3, 96, 97, 93, 94),
            (4, 94, 95, 90, 91),
            (5, 91, 95, 90.5, 94),
            (6, 94, 99, 93, 98),
            (7, 98, 102, 97, 101),
            (8, 101, 101.5, 92, 93),
            (9, 93, 93.5, 88, 89),
        ]:
            h.engine.on_bar(bar(*row))
        h.engine.on_bar(bar(10, 89, 89.5, 80, 81))
        h.engine.on_bar(bar(11, 81, 82, 76, 77))
        h.price(bar(12, 0, 0, 0, 0).timestamp, 50.0)
        h.engine.on_bar(bar(12, 77, 83, 76.5, 82.5))  # T1 fills
        h.price(bar(13, 0, 0, 0, 0).timestamp, 40.0, minutes=20)
        h.engine.on_bar(bar(13, 82.5, 83, 60, 62))  # closes below SL -> stopped
        return h

    def test_a_bounce_after_the_low_is_not_bought(self):
        h = self._into_first_trade(h=self._campaign())
        self.assertEqual(h.engine.trades[0].exit_reason, "stop")
        low = h.engine.ultimate_low
        # bounce well above the low, with two reds INSIDE the bounce
        h.engine.on_bar(bar(14, 62, 75, 61, 74))  # green bounce
        h.engine.on_bar(bar(15, 74, 74.5, 70, 71))  # red
        h.engine.on_bar(bar(16, 71, 71.5, 66, 67))  # red, closes below prev low
        self.assertEqual(len(h.engine.trades), 1)  # NOT armed: over value
        h.price(bar(17, 0, 0, 0, 0).timestamp, 30.0)
        h.engine.on_bar(bar(17, 67, 73, 66, 72))  # rises through 71.5
        self.assertEqual(len(h.engine.trades), 1)  # still no second trade
        self.assertGreater(low, 0)

    def test_a_close_below_the_low_reopens_arming(self):
        h = self._into_first_trade(h=self._campaign())
        low = h.engine.ultimate_low
        h.engine.on_bar(bar(14, 62, 75, 61, 74))
        h.engine.on_bar(RecoveryBar(bar(15, 0, 0, 0, 0).timestamp, 74, 74.5, low - 5, low - 4))  # closes UNDER
        h.engine.on_bar(bar(16, 56, 57, 52, 53))  # red
        h.engine.on_bar(bar(17, 53, 54, 48, 49))  # red, closes below prev low -> may arm now
        self.assertEqual(len(h.engine.trades), 2)


class LadderNewLowGate(unittest.TestCase):
    """Same mother, same invariant: after a trade completes, the ladder's next
    entry must come by breaking lower -- never off a bounce."""

    def _stopped_once(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 102, 103))
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 103, 106.5, 102.5, 106))
        h.price(bar(4, 0, 0, 0, 0).timestamp, 90.0, minutes=15)
        h.engine.on_bar(bar(4, 106, 106.2, 100, 101))  # stopped, ultimate low 100
        self.assertEqual(h.engine.trades[0].exit_reason, "stop")
        return h

    def test_a_pair_inside_a_bounce_does_not_arm(self):
        h = self._stopped_once()
        h.engine.on_bar(bar(5, 101, 112, 101, 111))  # bounce far above the low
        h.engine.on_bar(bar(6, 111, 111.5, 106, 107))  # red
        h.engine.on_bar(bar(7, 107, 107.5, 103, 104))  # red, closes below prev low BUT above 100
        self.assertEqual(len(h.engine.trades), 1)  # refused: over value

    def test_a_pair_that_breaks_the_low_arms(self):
        h = self._stopped_once()
        h.engine.on_bar(bar(5, 101, 101.5, 99.5, 100.5))  # red
        h.engine.on_bar(bar(6, 100.5, 101, 98, 99))  # red, close 99 < 100 -> breaks lower
        self.assertEqual(len(h.engine.trades), 2)
        self.assertEqual(h.engine.status, "ARMED")


class StopIsTheEntryCandleLow(unittest.TestCase):
    """The SL is the fill bar's own low, NOT the ultimate low since the mother.

    The two differ whenever the entry candle bottoms above the campaign's low,
    which is the normal case for a recovery buy -- the fill happens on a bounce
    that is already off the bottom.
    """

    def test_sl_sits_at_the_entry_bar_low_even_when_the_campaign_low_is_deeper(self):
        h = Harness(MOTHER)
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 95, 96))  # deep second red: low 95, arms at 106
        self.assertEqual(h.engine.ultimate_low, 95)
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        # the fill bar bottoms at 102.5, ten points ABOVE the campaign low of 95
        h.engine.on_bar(bar(3, 96, 107, 102.5, 106.5))
        trade = h.engine.trades[0]
        self.assertEqual(trade.entry_index, 106)
        self.assertEqual(trade.sl_level, 102.5)  # entry candle low, not 95
        # and a close at 100 -- below the SL but above the old ultimate low --
        # now stops the trade, where the old rule would have held it
        h.price(bar(4, 0, 0, 0, 0).timestamp, 92.0, minutes=15)
        h.engine.on_bar(bar(4, 106.5, 107, 99, 100))
        self.assertEqual(trade.exit_reason, "stop")


class StopSourceVariants(unittest.TestCase):
    """entry / previous / ultimate put the stop in three different places on
    the same fill."""

    def _fill(self, sl_source):
        h = Harness(MOTHER, RecoveryConfig(timeframe="5m", sl_source=sl_source))
        h.engine.on_bar(bar(1, 108, 109, 104, 105))
        h.engine.on_bar(bar(2, 105, 106, 95, 96))  # 2nd red, low 95, arms at 106
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        h.engine.on_bar(bar(3, 96, 107, 102.5, 106.5))  # fill bar, low 102.5
        return h.engine.trades[0]

    def test_entry_uses_the_fill_bars_low(self):
        self.assertEqual(self._fill("entry").sl_level, 102.5)

    def test_previous_uses_the_bar_before_the_fill(self):
        self.assertEqual(self._fill("previous").sl_level, 95.0)

    def test_ultimate_uses_the_campaign_low(self):
        self.assertEqual(self._fill("ultimate").sl_level, 95.0)

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            RecoveryConfig(timeframe="5m", sl_source="trailing")


class PutSideMirror(unittest.TestCase):
    """A PE campaign is the CE rules upside down, run on mirrored bars.

    The mirror is what lets one implementation serve both sides: negate every
    price and swap high with low, and "two reds, the second closing below the
    first's LOW, buy the recovery at its HIGH, stop below the entry candle's
    LOW" becomes "two greens, the second closing above the first's HIGH, buy at
    its LOW, stop above the entry candle's HIGH".
    """

    def test_a_green_bar_mirrors_to_red_with_high_and_low_swapped(self):
        from engine.candle_recovery import mirror_bar

        b = bar(1, 100, 110, 95, 105)  # green
        m = mirror_bar(b)
        self.assertTrue(m.is_red)
        self.assertEqual(m.high, -b.low)
        self.assertEqual(m.low, -b.high)
        self.assertEqual(m.timestamp, b.timestamp)

    def test_the_mirror_is_its_own_inverse(self):
        from engine.candle_recovery import mirror_bar

        b = bar(1, 100, 110, 95, 105)
        again = mirror_bar(mirror_bar(b))
        self.assertEqual((again.open, again.high, again.low, again.close), (b.open, b.high, b.low, b.close))

    def test_a_rising_market_arms_and_fills_a_put_campaign(self):
        from engine.candle_recovery import mirror_bar, unmirror_price

        # A swing LOW mother, then a rally: two greens where the second closes
        # ABOVE the first's high, then a dip back through that green's LOW.
        raw_mother = bar(0, 108, 109, 100, 102)  # the swing low
        rows = [
            bar(1, 102, 106, 101, 105),  # green
            bar(2, 105, 110, 104, 109),  # green, closes above 106 -> arms at its LOW 104
            bar(3, 109, 110, 103, 104),  # dips through 104 -> fills
        ]
        h = Harness.__new__(Harness)
        h.tape = {}
        h.engine = TwoRedRecovery(
            mirror_bar(raw_mother),
            RecoveryConfig(timeframe="5m"),
            contract_for=lambda when, index: (24000, EXPIRY),
            # the engine hands back a MIRRORED index price; a real caller
            # negates it before asking the chain for a strike
            premium_lookup=lambda when, strike, expiry: h.tape.get(when),
            lot_size=LOT,
        )
        h.price = Harness.price.__get__(h)
        h.price(bar(3, 0, 0, 0, 0).timestamp, 100.0)
        for row in rows:
            h.engine.on_bar(mirror_bar(row))

        trade = h.engine.trades[0]
        self.assertIsNotNone(trade.entry_time)
        # armed at the second GREEN's low, stopped above the entry candle's HIGH
        self.assertEqual(unmirror_price(trade.trigger), 104.0)
        self.assertEqual(unmirror_price(trade.sl_level), 110.0)
        self.assertEqual(unmirror_price(trade.entry_index), 104.0)


class RunMothers(unittest.TestCase):
    """Five consecutive higher highs, the LAST of them being the mother.

    Distinct from a pivot: a pivot is a V needing future bars to confirm, so a
    live system learns of it late. A run's last bar IS the mother, known the
    instant it closes.
    """

    def _series(self, highs):
        from types import SimpleNamespace

        base = datetime(2026, 2, 2, 9, 15)
        return [
            SimpleNamespace(timestamp=base + timedelta(minutes=5 * i), open=h - 5, high=h, low=h - 10, close=h - 2)
            for i, h in enumerate(highs)
        ]

    def test_a_staircase_of_five_makes_its_last_bar_the_mother(self):
        from engine.cascade_mothers import find_run_mothers

        rows = self._series([100, 101, 102, 103, 104, 103, 102])
        found = find_run_mothers(rows, run=5, atr_period=2)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].index, 4)  # the 5th bar, the highest
        self.assertEqual(found[0].high, 104)
        # confirmed by its OWN close -- no future bar consulted
        self.assertEqual(found[0].confirmed_at, rows[4].timestamp)

    def test_a_break_in_the_run_disqualifies_it(self):
        from engine.cascade_mothers import find_run_mothers

        rows = self._series([100, 101, 100.5, 102, 103, 104])
        self.assertEqual(find_run_mothers(rows, run=5, atr_period=2), [])

    def test_mirrored_bars_turn_it_into_five_consecutive_LOWER_LOWS(self):
        from engine.candle_recovery import mirror_bar
        from engine.cascade_mothers import find_run_mothers

        base = datetime(2026, 2, 2, 9, 15)
        lows = [100, 99, 98, 97, 96, 97]  # a falling staircase
        real = [RecoveryBar(base + timedelta(minutes=5 * i), lo + 8, lo + 10, lo, lo + 2) for i, lo in enumerate(lows)]
        found = find_run_mothers([mirror_bar(b) for b in real], run=5, atr_period=2)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].index, 4)
        self.assertEqual(-found[0].high, 96)  # the lowest low, back in real prices
