"""engine/fib_space_host.py -- one poll cycle of the fib-space paper run.

engine/fib_space_live.py decides; this module feeds it.  A cycle fetches the
closed index candles, re-scans for mothers, advances every live campaign, and
reports what changed.  It is deliberately a plain object with an async ``poll``
rather than a loop of its own, so app.py owns the scheduling and the tests can
drive it a tick at a time without a clock.

TWO THINGS HERE EXIST TO STOP KNOWN FAILURES, not hypothetical ones:

**The session gate.**  Dhan's rate budget is account-wide, and a loop polling
through the night once produced a 4 AM 429 storm that starved the live engine.
So a cycle outside NSE cash hours does no I/O at all -- it returns
``{"skipped": "outside session"}`` before touching the broker.

**The lookback window.**  Mothers are found by re-scanning the whole 15m series
each cycle, and a campaign is replayed from its own mother, so the fetch has to
reach back far enough to cover the oldest campaign still open -- not just far
enough to find today's pivot.  Too short a window would silently truncate an old
campaign's replay and trip the driver's history guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from engine.cascade_mothers import find_mother_candles
from engine.cascade_options import IndexCandle, is_nse_cash_session
from engine.fib_space_cascade import SpaceCascadeConfig
from engine.fib_space_live import CampaignHalted, FibSpacePaperBook, bars_from_candles

# The pivot width the design uses -- see tools/fib_space_sweep.MOTHER_PIVOT_BARS.
# Duplicated as an import would drag the whole sweep (and its cache path) into
# the live process; the value is asserted equal in the tests.
MOTHER_PIVOT_BARS = 5

# WHAT EACH SYMBOL TRADES, LIVE.  Every field here is a measured decision, and
# every one of them is asserted equal to tools.fib_space_sweep.SYMBOLS in the
# tests -- a paper run on terms the backtest never measured proves nothing about
# the backtest.  The duplication is deliberate (see MOTHER_PIVOT_BARS); the
# parity test is what makes it safe.
LIVE_SYMBOLS = {
    "banknifty": dict(
        dhan_symbol="BANKNIFTY",
        strike_step=100,
        itm_steps=2,  # ATM-2, toward ITM
        monthly_only=True,
        min_dte=15,
        cooldown_days=0,  # measured: any throttle only drops winners here
    ),
    "nifty": dict(
        dhan_symbol="NIFTY",
        strike_step=50,
        itm_steps=2,
        monthly_only=True,
        min_dte=15,
        cooldown_days=3,  # measured: -Rs 71k unthrottled -> +Rs 1.95L
    ),
}

# THE FETCH WINDOW IS SIZED BY DEMAND, NOT BY THE HORIZON.
#
# This used to ask for a flat 260 days of intraday candles on every poll, which
# was wrong twice over.  Dhan's /charts/intraday endpoint is not built for a
# range that long -- every other caller in this repo asks for 14 or 15 days --
# and even if it answered, pulling nine months of 15m AND 5m bars once a minute
# would spend an account-wide rate budget that the live engine shares.
#
# So the window is now the smallest one that can still answer: far enough back
# to reach the oldest campaign actually being replayed, and never less than
# MIN_LOOKBACK_DAYS so a mother named at the edge of its own window still has
# warm-up bars behind it.  A book with no campaigns fetches a fortnight.
MIN_LOOKBACK_DAYS = 30

# Extra days behind the oldest mother, so ATR and the trendline anchors have
# history to sit on rather than starting flat at the very first bar.
LOOKBACK_WARMUP_DAYS = 10

# One request never spans more than this.  Long ranges are split and stitched
# instead of failing whole -- the exact ceiling Dhan enforces is not documented
# here, so this stays comfortably under any plausible one.
MAX_FETCH_DAYS = 60

# Entries are decided on closed 5m bars, so a minute's cadence is already finer
# than the decisions. Kept slow deliberately: the budget is shared with the live
# engine.
DEFAULT_POLL_SECONDS = 60


@dataclass
class PollReport:
    at: datetime
    skipped: Optional[str] = None
    geometry_bars: int = 0
    entry_bars: int = 0
    mothers_seen: int = 0
    campaigns_started: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    halted: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.campaigns_started or self.fills or self.exits or self.halted)

    def to_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "skipped": self.skipped,
            "geometry_bars": self.geometry_bars,
            "entry_bars": self.entry_bars,
            "mothers_seen": self.mothers_seen,
            "started": [c.campaign_id for c in self.campaigns_started],
            "fills": [
                {
                    "campaign": f.campaign_id,
                    "round": f.round_no,
                    "at": f.timestamp.isoformat(),
                    "index": round(f.index_price, 2),
                    "lots": f.lots,
                    "strike": f.strike,
                    "premium": f.premium,
                }
                for f in self.fills
            ],
            "exits": [
                {
                    "campaign": e.campaign_id,
                    "round": e.round_no,
                    "at": e.timestamp.isoformat(),
                    "index": round(e.exit_index, 2),
                    "reason": e.exit_reason,
                    "premium": e.premium,
                }
                for e in self.exits
            ],
            "halted": list(self.halted),
            "error": self.error,
        }


class FibSpacePaperHost:
    """Fetch, scan, advance -- one underlying's paper run.

    ``adapter`` supplies closed index candles; ``premium_lookup`` a current
    quote or None; ``select_contract`` the contract a fill buys.  All three are
    injected, so the host runs in a test with no broker at all.
    """

    def __init__(
        self,
        symbol: str,
        adapter,
        *,
        premium_lookup,
        select_contract: Callable[[datetime, float], object],
        config: Optional[SpaceCascadeConfig] = None,
        entry_timeframe: str = "5m",
        geometry_timeframe: str = "15m",
        cooldown_days: int = 0,
        min_lookback_days: int = MIN_LOOKBACK_DAYS,
        dhan_symbol: Optional[str] = None,
        auto_scan: bool = False,
    ) -> None:
        self.symbol = symbol
        self.adapter = adapter
        self.dhan_symbol = dhan_symbol or symbol.upper()
        self.min_lookback_days = min_lookback_days
        # MOTHERS ARE NAMED BY DEFAULT, NOT SCANNED.  The pivot scanner exists
        # because a backtest has nobody to ask; running live there is somebody.
        # It stays available (auto_scan=True) because the measured numbers came
        # from it and a like-for-like forward test needs it -- but it is not the
        # default, because the trader picking the mother is the actual strategy.
        self.auto_scan = auto_scan
        self.entry_timeframe = entry_timeframe
        self.geometry_timeframe = geometry_timeframe
        self.book = FibSpacePaperBook(
            symbol,
            config=config or SpaceCascadeConfig(),
            premium_lookup=premium_lookup,
            select_contract=select_contract,
            entry_timeframe=entry_timeframe,
            geometry_timeframe=geometry_timeframe,
            cooldown_days=cooldown_days,
        )
        self.last_poll: Optional[datetime] = None
        self.last_report: Optional[PollReport] = None

    async def poll(self, *, now: datetime) -> PollReport:
        report = PollReport(at=now)

        # THE SESSION GATE.  Before any I/O: the Dhan budget is account-wide and
        # an out-of-hours poll spends it for nothing -- the candles cannot have
        # changed and no decision can be made.
        if not is_nse_cash_session(now):
            report.skipped = "outside NSE cash session"
            self.last_report = report
            return report

        try:
            geometry_candles = await self._fetch(self.geometry_timeframe, now=now)
            if self.entry_timeframe == self.geometry_timeframe:
                entry_candles = geometry_candles
            else:
                entry_candles = await self._fetch(self.entry_timeframe, now=now)
        except Exception as exc:
            report.error = f"candle fetch failed: {exc}"
            self.last_report = report
            return report

        geometry_bars = bars_from_candles(geometry_candles)
        entry_bars = bars_from_candles(entry_candles)
        report.geometry_bars = len(geometry_bars)
        report.entry_bars = len(entry_bars)
        if not geometry_bars:
            report.skipped = "no closed geometry bars yet"
            self.last_report = report
            return report

        if self.auto_scan:
            mothers = find_mother_candles(
                [IndexCandle(b.timestamp, b.open, b.high, b.low, b.close) for b in geometry_bars],
                left_bars=MOTHER_PIVOT_BARS,
                right_bars=MOTHER_PIVOT_BARS,
            )
            report.mothers_seen = len(mothers)
            report.campaigns_started = self.book.adopt_mothers(geometry_bars, mothers)

        for campaign in list(self.book.campaigns.values()):
            try:
                fills, exits = self.book.advance(campaign, geometry_bars, entry_bars, now=now)
            except CampaignHalted:
                # The driver already stamped the campaign; a halt is reported,
                # never retried, and never allowed to stop the other campaigns.
                report.halted.append(campaign.campaign_id)
                continue
            report.fills.extend(fills)
            report.exits.extend(exits)

        self.last_poll = now
        self.last_report = report
        return report

    def lookback_days(self, *, now: datetime) -> int:
        """How far back this book actually needs to see, today.

        Reaching past the oldest campaign being replayed would truncate it and
        trip the driver's history guard; reaching further than that is a request
        nobody reads, paid for out of a shared rate budget.
        """
        oldest = min((c.mother.timestamp for c in self.book.campaigns.values()), default=None)
        if oldest is None:
            return self.min_lookback_days
        span = (now.date() - oldest.date()).days + LOOKBACK_WARMUP_DAYS
        return max(self.min_lookback_days, span)

    async def _fetch(self, timeframe: str, *, now: datetime) -> list:
        """Closed candles over the needed window, in slices the broker accepts.

        A long range is split rather than sent whole. Slices overlap by a day at
        the seam, so bars are de-duplicated by timestamp on the way back in --
        one bar arriving twice must not become two.
        """
        days = self.lookback_days(now=now)
        by_stamp: dict = {}
        end = now.date()
        remaining = days
        while remaining > 0:
            span = min(remaining, MAX_FETCH_DAYS)
            start = end - timedelta(days=span)
            for candle in await self.adapter.async_get_candles(
                self.dhan_symbol, timeframe, from_date=start, to_date=end, now=now
            ):
                by_stamp[candle.timestamp] = candle
            remaining -= span
            end = start
        return [by_stamp[k] for k in sorted(by_stamp)]

    async def geometry_bars(self, *, now: datetime) -> list:
        """Every closed geometry bar in the window this book needs."""
        return bars_from_candles(await self._fetch(self.geometry_timeframe, now=now))

    async def start_named_mother(self, when: datetime, *, now: datetime):
        """Open a campaign on the bar the trader named. Returns the campaign.

        The mother's high and low come from the market bar, never from anything
        typed -- the same discipline the fib-boundary tab and the Test Bench
        already use. A timestamp with no bar behind it is an error, not a shape
        to invent.
        """
        bars = await self.geometry_bars(now=now)
        bar = next((b for b in bars if b.timestamp == when), None)
        if bar is None:
            raise LookupError(
                f"no closed {self.geometry_timeframe} {self.dhan_symbol} candle opens at {when:%d %b %Y %H:%M} IST"
            )
        campaign = self.book.adopt_manual_mother(bar)

        # DRAW IT NOW, don't wait for a poll. The geometry needs only closed
        # candles, so a mother named at 8pm can be checked against your own
        # chart the same evening -- the point of naming it is to confirm the
        # engine sees what you see. preview() records nothing, so no fill gets
        # priced off a stale quote as a side effect of looking.
        entry_bars = bars
        if self.entry_timeframe != self.geometry_timeframe:
            try:
                entry_bars = bars_from_candles(await self._fetch(self.entry_timeframe, now=now))
            except Exception:
                # A missing entry series costs the fill marks, not the geometry;
                # the poll will fill them in. Better a partial chart than none.
                entry_bars = bars
        try:
            self.book.preview(campaign, bars, entry_bars)
        except Exception:
            pass  # the campaign is real either way; the poll redraws it
        return campaign

    async def ensure_drawable(self, campaign, *, now: datetime) -> None:
        """Make sure this campaign has something to draw, fetching if it must.

        A campaign adopted before its first poll -- or before this preview
        existed at all -- has no replay behind it, and asking for its chart got
        a refusal that looked like a bug. Drawing needs only closed candles, so
        it can always be satisfied on demand. Records nothing.
        """
        if getattr(campaign, "last_result", None) is not None:
            return
        bars = await self.geometry_bars(now=now)
        entry_bars = bars
        if self.entry_timeframe != self.geometry_timeframe:
            try:
                entry_bars = bars_from_candles(await self._fetch(self.entry_timeframe, now=now))
            except Exception:
                entry_bars = bars
        self.book.preview(campaign, bars, entry_bars)

    def snapshot(self) -> dict:
        snap = self.book.snapshot()
        snap["last_poll"] = self.last_poll.isoformat() if self.last_poll else None
        snap["entry_timeframe"] = self.entry_timeframe
        snap["geometry_timeframe"] = self.geometry_timeframe
        snap["campaign_rows"] = [
            {
                "id": c.campaign_id,
                "mother": c.mother.timestamp.isoformat(),
                "mother_high": round(c.mother.high, 2),
                "status": c.status,
                "halt_reason": c.halt_reason,
                "fills": len(c.fills),
                "open_quantity": c.open_quantity,
                "unpriced": c.unpriced,
                "closed_rounds": c.closed_rounds,
                "net": c.net,
                "source": c.source,
            }
            for c in sorted(self.book.campaigns.values(), key=lambda c: c.mother.timestamp, reverse=True)
        ]
        snap["auto_scan"] = self.auto_scan
        return snap
