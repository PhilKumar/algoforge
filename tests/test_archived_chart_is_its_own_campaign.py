"""A closed campaign's chart is drawn from that campaign, and nothing else.

Phil, 2026-09-03, after three separate fixes to this one chart: "First analyse
the complete chart in every possible way at least 10 diff ways... I cannot tell
one by one all day."

The audit found one cause under most of it. `/api/fib-boundary/paper/chart`
sent GEOMETRY -- candles, fibs, levels -- and the client filled in the TRADE
from `_lastFibBoundaryStatus[symbol]`, which is the campaign running right now.
So on an archived chart:

  * TARGET, AVG ENTRY, BUY STOP and RE-ARM were today's prices
  * rung states and their rupees belonged to a different trade
  * the life-window trim ran against today's clock, matched no bars, and fell
    back to drawing every bar -- the trim was never in effect
  * fib structures were filtered by the running campaign's end
  * convergence zones were suppressed when the LIVE campaign was in zone mode

Two more sat beside it. `loadFibBoundaryChart` rebuilt `_fibxChartCtx` without
`closedAt` or `campaignId`, and the strip's Refresh and timeframe buttons
re-enter through that context -- so the first draw of a closed campaign was
right and every redraw after it ran to today and lost its marks. And the marks
themselves were double-drawn: `payload["fills"]` already holds every round's
fills (`_fib_boundary_campaign_row` folds them in), so concatenating the rounds
turned campaign 60's eight buys into twelve.

The engine snapshot that answers all of it was in the ledger the whole time.
"""

import json
import unittest
from datetime import date, datetime
from pathlib import Path

from engine.fib_touch_ladder import FibTouchConfig, FibTouchLadder, PaperExecutor

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "philforge-app.js").read_text(encoding="utf-8")


def _route() -> str:
    i = APP.index('@app.get("/api/fib-boundary/paper/chart")')
    return APP[i : i + 14000]


class TheRouteSendsTheCampaignNotJustItsGeometry(unittest.TestCase):
    def test_the_archived_ladder_is_returned(self):
        self.assertIn('"campaign": archived', _route())

    def test_it_is_restored_from_the_snapshot_rather_than_re_derived(self):
        """A second hand-rolled copy of target/average is how these bugs start."""
        body = _route()
        self.assertIn("FibTouchLadder.from_dict(", body)
        self.assertIn("ladder.get_status()", body)

    def test_a_read_only_restore_cannot_reach_for_a_quote(self):
        body = _route()
        self.assertIn("premium_lookup=_fib_touch_dead_premium_lookup", body)
        self.assertIn("expiry_source=_fib_touch_dead_expiry_source", body)

    def test_those_lookups_raise_rather_than_return_none(self):
        """Silence here would price an archived chart off today's market."""
        self.assertIn('raise RuntimeError("archived fib chart: premium lookup', APP)
        self.assertIn('raise RuntimeError("archived fib chart: expiry source', APP)

    def test_a_snapshot_this_build_cannot_read_still_gets_a_chart(self):
        body = _route()
        self.assertIn("if archived is None:", body)
        self.assertIn("could not restore snapshot", body)

    def test_a_live_or_idle_chart_sends_no_campaign(self):
        """No campaign_id means the running campaign IS the right answer."""
        self.assertIn("archived: dict | None = None", _route())


class TheBuysAreNotDrawnTwice(unittest.TestCase):
    def test_the_route_reads_the_folded_fill_list_only(self):
        body = _route()
        self.assertIn('for fill in payload.get("fills") or []:', body)
        self.assertNotIn('seen_fills += list(rnd.get("fills") or [])', body)

    def test_the_row_builder_is_what_makes_that_list_complete(self):
        """If this ever stops folding, the route stops seeing banked buys."""
        i = APP.index("def _fib_boundary_campaign_row(")
        builder = APP[i : i + 3000]
        self.assertIn('fills = [dict(f) for row in rounds for f in (row.get("fills") or [])]', builder)
        self.assertIn('fills += [dict(f) for f in (status.get("fills") or [])]', builder)


class AnUnpricedExitSaysSo(unittest.TestCase):
    """The renderer prints "unpriced" for a null P&L and "+Rs 0" for a real
    zero. The payload builder coerced with `Number(x) || 0` first, so that
    handling was dead code and every unquotable exit looked free."""

    def test_no_branch_coerces_a_null_pnl_to_zero(self):
        i = APP_JS.index("function _fibBoundaryCanvasPayload(")
        body = APP_JS[i : APP_JS.index("\n}\n", i)]
        self.assertNotIn("Number(mark.pnl) || 0", body)
        self.assertNotIn("Number(round.net_pnl) || 0", body)
        self.assertNotIn("Number(campaign.net_pnl) || 0", body)

    def test_null_is_carried_through_as_null(self):
        self.assertIn("mark.pnl == null ? null : Number(mark.pnl)", APP_JS)
        self.assertIn("round.net_pnl == null ? null : Number(round.net_pnl)", APP_JS)


class TheChartContextSurvivesARedraw(unittest.TestCase):
    def test_the_rebuilt_context_keeps_what_the_row_handed_over(self):
        i = APP_JS.index("  _fibxChartCtx = {\n    symbol, side, timestamp, baseTf,")
        block = APP_JS[i : i + 400]
        self.assertIn("closedAt: ctx?.closedAt || ''", block)
        self.assertIn("campaignId: ctx?.campaignId || ''", block)
        self.assertIn("buyMode: ctx?.buyMode || ''", block)

    def test_the_archived_campaign_overrides_the_running_one(self):
        self.assertIn(
            "_fibBoundaryCanvasPayload(data, _fibxChartCtx?.isCampaign ? symbol : '', data.campaign || null)", APP_JS
        )

    def test_a_new_chart_does_not_inherit_the_last_ones_timeframe(self):
        self.assertIn("let _fibxChartKey = '';", APP_JS)
        self.assertIn("if (_fibxChartKey !== `${symbol}|${side}|${timestamp}`) { _fibxChartTf = '';", APP_JS)

    def test_the_timeframe_strip_re_syncs_when_the_chart_changes(self):
        """Built once per page, it kept the first campaign's active button."""
        i = APP_JS.index("const stripHost = el('oc-fib-chart-strip');")
        block = APP_JS[i : i + 900]
        self.assertIn("stripHost.dataset.stripKey !== stripKey", block)
        self.assertIn("stripHost.replaceChildren();", block)

    def test_a_closed_chart_is_titled_in_the_loader_not_after_the_await(self):
        """Titling it after the await let every redraw reclaim the live title."""
        self.assertIn("const frozen = !!_fibxChartCtx?.closedAt;", APP_JS)
        self.assertIn("frozen ? 'Closed campaign · frozen · ' : ''", APP_JS)
        i = APP_JS.index("async function openArchivedFibChart(")
        self.assertNotIn("'Closed campaign · frozen'", APP_JS[i : i + 1200])


class ARestoredLadderReportsItsOwnTrade(unittest.TestCase):
    """The round trip the route depends on, exercised rather than asserted.

    Every closed Fib campaign on prod (ids 7, 15, 28, 60, 75) restores through
    this path and reports its own mother, exit, target and average -- including
    the two whose target never armed.
    """

    def _ladder(self) -> FibTouchLadder:
        return FibTouchLadder(
            FibTouchConfig(
                symbol="NIFTY",
                side="CE",
                mother_timestamp=datetime(2026, 8, 28, 10, 15),
                lot_size=65,
                strike_step=50.0,
            ),
            premium_lookup=lambda *a: 200.0,
            expiry_source=lambda on: [date(2026, 9, 1)],
            executor=PaperExecutor(),
        )

    def _dead(self, *_a, **_k):
        raise RuntimeError("must not be called")

    def test_a_snapshot_restores_without_a_broker_and_keeps_its_mother(self):
        snapshot = json.loads(json.dumps(self._ladder().to_dict()))
        back = FibTouchLadder.from_dict(
            snapshot, premium_lookup=self._dead, expiry_source=self._dead, executor=PaperExecutor()
        )
        status = back.get_status()
        self.assertEqual(status["mother_timestamp"], datetime(2026, 8, 28, 10, 15).isoformat())
        self.assertEqual(status["symbol"], "NIFTY")

    def test_the_status_carries_every_overlay_the_chart_reads(self):
        """If one of these disappears, an archived chart silently loses a line."""
        status = self._ladder().get_status()
        for field in (
            "levels",
            "fills",
            "rounds",
            "buy_stop",
            "rearm_below",
            "resting_exits",
            "average_index_entry",
            "target_index",
            "exit_timestamp",
            "mother_timestamp",
        ):
            self.assertIn(field, status, f"the chart draws {field} and the status stopped reporting it")


if __name__ == "__main__":
    unittest.main()
