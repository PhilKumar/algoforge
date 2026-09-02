"""A campaign that never bought has no P&L. That is not the same as a missing one.

Phil, 2026-09-02, on a Fib Boundary row: "Why it is unpriced today also?" The
row read:

    09-02 14:35 -> 15:15   contract —   buys 0   deployed Rs 0.00
    exit intraday_close    NET P&L: unpriced

Nothing was bought. There was no contract, no rupee deployed, and therefore
nothing whose price could be missing. The ledger printed "unpriced" anyway,
because it decided on `net_pnl == null` alone -- and a campaign that never
fills never settles, so its net stays null exactly like one whose exit could
not be quoted.

The two need telling apart, because they mean opposite things about the
machinery: one is a session with no signal, the other is a number we owe and
cannot produce. Reading the first as the second is how a quiet day looks like
a broken engine.

The backend already knows: it stamps `exit_reason: "no_buy"` -- but only when
the exit row carries no reason of its own, and this one closed with
"intraday_close". So the count of buys is what decides.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")


class NoTradeIsNotUnpriced(unittest.TestCase):
    def _fn(self):
        i = APP_JS.index("function _paperLedgerMoney(")
        return APP_JS[i : APP_JS.index("\n}", i)]

    def test_the_formatter_is_told_how_many_buys_there_were(self):
        """Without the count it cannot tell the two nulls apart."""
        self.assertIn("function _paperLedgerMoney(value, buys)", APP_JS)
        self.assertIn("_paperLedgerMoney(net, row.buys)", APP_JS)

    def test_zero_buys_reads_as_no_trade_and_never_as_unpriced(self):
        body = self._fn()
        self.assertIn("'no trade'", body)
        # The unpriced branch must be gated on there having been a buy.
        self.assertRegex(body, r"Number\(buys \|\| 0\) > 0 \? 'unpriced' : 'no trade'")

    def test_a_real_number_still_prints_as_money(self):
        self.assertIn("return _candleEntrySigned(Number(value));", self._fn())

    def test_the_row_still_shows_the_buy_count_and_deployed(self):
        """The two columns a reader checks the moment the P&L looks odd."""
        self.assertIn("String(row.buys ?? 0)", APP_JS)
        self.assertIn("row.deployed_inr == null ? '—'", APP_JS)


class TheBackendStillMarksANoBuyCampaign(unittest.TestCase):
    def test_the_no_buy_reason_is_still_emitted(self):
        """It is a second signal, not a replacement: it only lands when the
        exit row has no reason of its own, which is why the UI counts buys."""
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('(None if fills else "no_buy")', app)
        self.assertIn('"buys": len(fills)', app)


if __name__ == "__main__":
    unittest.main()
