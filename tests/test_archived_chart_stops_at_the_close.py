"""A finished campaign's chart stops where the campaign stopped.

Phil, 2026-09-02, on the Fib Boundary closed ledger: "These charts are not at
all getting frozen right at the trade.. they are completely messy giving the
same charts".

They were not frozen at all. `/api/fib-boundary/paper/chart` loaded candles
`from_date=mother.date(), to_date=now.date()` for every request, so a mother
from 26 Aug drew eight days of candles, one from 1 Sep drew two, and every one
of them ended at today's right-hand edge. Six different campaigns rendered as
six versions of the same recent tape, with the campaign itself squeezed into
the left margin.

The mothers were never the problem -- each row carried its own, distinct:

    id 75  mother 09-02 14:35      id 28  mother 08-28 10:15
    id 60  mother 09-01 11:30      id 15  mother 08-27 09:15
    id 50  mother 08-31 09:15      id  7  mother 08-26 09:45

What was missing is where to STOP. The row knows -- it prints the closing time
in its own second column -- and now it sends it.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")


def _chart_route() -> str:
    """The chart route's own source, bounded by the next route.

    This was a fixed 6500/11000-character slice, which had to be widened every
    time the function grew and failed with a wall of source when it wasn't.
    """
    i = APP.index('@app.get("/api/fib-boundary/paper/chart")')
    j = APP.index("\n@app.", i + 1)
    return APP[i:j]


class TheRouteHonoursTheClose(unittest.TestCase):
    def _route(self):
        return _chart_route()

    def test_the_route_accepts_when_the_campaign_ended(self):
        self.assertIn('closed_at: str = ""', self._route())

    def test_candles_stop_at_the_close_not_at_today(self):
        body = self._route()
        self.assertIn("to_date=last_day", body)
        self.assertNotIn("to_date=now.date()", body)

    def test_the_clamp_cannot_invert_the_window(self):
        """A close before the mother would ask for a backwards range."""
        self.assertIn("last_day = max(mother.date(), min(last_day, ended))", self._route())

    def test_an_unreadable_stamp_does_not_break_the_chart(self):
        """A bad timestamp should cost the reader the clamp, not the chart."""
        self.assertIn("except (ValueError, HTTPException):", self._route())

    def test_a_live_panel_still_runs_to_today(self):
        """No closed_at means the campaign is open, and open means now."""
        self.assertIn("last_day = now.date()", self._route())


class TheArchivedChartDrawsItsOwnTrade(unittest.TestCase):
    """A closed campaign's buys and sells, not the running campaign's.

    `_fibBoundaryCanvasPayload` builds its marks from `campaign`, which is
    `_lastFibBoundaryStatus[symbol]` -- the campaign running NOW. So an
    archived chart drew today's live fills onto a trade from last week, and on
    a day the current mother has bought nothing it drew no marks at all. Every
    closed chart came out with no buy and no sell on it.
    """

    def test_the_route_can_return_the_stored_campaigns_marks(self):
        route = _chart_route()
        self.assertIn("campaign_id: int = 0", route)
        self.assertIn('"entries": entries', route)
        self.assertIn('"exits": exits', route)

    def test_marks_are_the_index_level_not_the_premium(self):
        """These candles are NIFTY; a mark at the option's price is off-chart."""
        route = _chart_route()
        self.assertIn('fill["index_price"]', route)
        self.assertIn('rnd["exit_index"]', route)

    def test_a_banked_buy_is_marked_once(self):
        """`payload["fills"]` already holds the rounds' fills; adding them
        again drew campaign 60's eight buys as twelve marks."""
        self.assertNotIn("seen_fills", _chart_route())

    def test_the_stored_marks_win_over_the_running_campaigns(self):
        self.assertIn("const archivedEntries = Array.isArray(payload?.entries)", APP_JS)
        self.assertIn("archivedEntries && archivedEntries.length", APP_JS)
        self.assertIn("archivedExits && archivedExits.length", APP_JS)

    def test_the_row_and_handler_carry_the_campaign_id(self):
        self.assertIn("data-fx-id=", APP_JS)
        self.assertIn("campaignId: node.getAttribute('data-fx-id')", APP_JS)
        self.assertIn("query.set('campaign_id', String(ctx.campaignId))", APP_JS)


class TheFrontEndSendsIt(unittest.TestCase):
    def test_the_row_hands_over_its_closing_time(self):
        self.assertIn("data-fx-closed=", APP_JS)
        self.assertRegex(APP_JS, r"data-fx-closed=.*row\.closed_at")

    def test_the_handler_carries_it_into_the_chart_context(self):
        self.assertIn("closedAt: node.getAttribute('data-fx-closed')", APP_JS)

    def test_the_query_sends_it_only_when_there_is_one(self):
        self.assertIn("if (closedAt) query.set('closed_at', String(closedAt));", APP_JS)


if __name__ == "__main__":
    unittest.main()
