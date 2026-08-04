"""engine/fib_space_live.py -- drive the fib-space design forward, in paper.

The design was measured by replaying history (tools/fib_space_book.py).  This
module runs the SAME replay on bars that arrive one at a time, so a paper run
proves the live path rather than a second implementation of it.

WHY IT REPLAYS INSTEAD OF STEPPING.  run_space_campaign is a pure function of
(mother, bars): given the same bars it returns the same rounds, always.  So on
every poll this driver hands it every bar since the mother and takes the result
whole, rather than keeping a live state machine in step with it.  That costs a
few milliseconds per campaign and buys three things worth far more:

  * the paper run and the backtest cannot disagree -- they are one function
  * a restart needs no saved engine state, only the fills already recorded
  * a bug found in paper is a bug found in the backtest, and vice versa

WHAT IT REFUSES TO DO, each because the alternative fabricates a result:

  * **Never price a fill it did not see happen.**  A premium is a live quote.
    If the driver was asleep when the bar closed, the quote it can fetch now is
    not the fill that would have happened then, so the fill is recorded UNPRICED
    and the campaign is flagged.  A paper P&L built from late quotes flatters
    itself exactly where the market moved fastest.
  * **Never rewrite a decision it already recorded.**  The replay is causal, so
    a fill once seen must reappear identically on every later poll.  If one
    changes or vanishes, that is a real defect -- the campaign halts for a human
    rather than quietly restating its own history.
  * **Never confirm a mother early.**  A swing high is not a swing high until
    its right shoulder has printed; the scanner's own confirmed_at governs, and
    campaigns arm from there.

The driver holds no broker.  Premium comes from an injected lookup with the same
signature the options cascade already uses -- ``(timestamp, contract) -> float |
None`` -- so paper and live differ only in what that callable does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Protocol, Sequence

from engine.cascade_options import IndexCandle
from engine.fib_space_cascade import SpaceCascadeConfig, SpaceFill, run_space_campaign
from engine.fib_space_geometry import Bar

# A mother is only worth watching for so long.  The contract terms cap the real
# risk (a monthly bought at 45 DTE is gone within two months), but a campaign
# whose rounds never close would otherwise be replayed forever, growing its bar
# window on every poll.  120 sessions matches the backtest's horizon.
DEFAULT_HORIZON_SESSIONS = 120
BARS_PER_SESSION = {"5m": 75, "15m": 25, "1h": 7}


class PremiumLookup(Protocol):
    def __call__(self, when: datetime, contract: object) -> Optional[float]: ...


@dataclass(frozen=True)
class PaperFill:
    """One lot actually recorded, at the premium quoted when it was seen."""

    campaign_id: str
    round_no: int
    timestamp: datetime
    index_price: float
    lots: int
    quantity: int
    space_label: str
    strike: Optional[float] = None
    expiry: Optional[object] = None
    premium: Optional[float] = None  # None => seen too late to quote honestly
    recorded_at: Optional[datetime] = None

    @property
    def key(self) -> tuple:
        """Identity of the underlying DECISION, independent of its pricing."""
        return (self.campaign_id, self.round_no, self.timestamp, round(self.index_price, 4), self.lots)

    @property
    def outlay(self) -> Optional[float]:
        return None if self.premium is None else self.premium * self.quantity


@dataclass(frozen=True)
class PaperExit:
    campaign_id: str
    round_no: int
    timestamp: datetime
    exit_index: float
    exit_reason: str
    premium: Optional[float] = None
    quantity: int = 0
    recorded_at: Optional[datetime] = None

    @property
    def key(self) -> tuple:
        return (self.campaign_id, self.round_no, self.timestamp)

    @property
    def proceeds(self) -> Optional[float]:
        return None if self.premium is None else self.premium * self.quantity


class CampaignHalted(RuntimeError):
    """The replay contradicted something already recorded."""


@dataclass
class LiveCampaign:
    """One mother, watched forward."""

    campaign_id: str
    symbol: str
    mother: Bar
    arm_from_index: Optional[int]
    confirmed_at: datetime
    lot_size: int
    contract: object = None
    fills: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    status: str = "watching"  # watching | trading | done | halted
    halt_reason: Optional[str] = None
    unpriced: int = 0
    source: str = "auto"  # auto (pivot scanner) | manual (named by the trader)

    @property
    def open_quantity(self) -> int:
        closed = {e.round_no for e in self.exits}
        return sum(f.quantity for f in self.fills if f.round_no not in closed)

    @property
    def closed_rounds(self) -> int:
        """Rounds that have actually banked. Distinguishes a zero from a blank.

        A campaign holding three open lots has realised exactly nothing, so its
        net is 0.0 -- arithmetically right and badly misleading, because it reads
        as "flat" next to a campaign that banked and broke even. Callers should
        show no number at all until this is non-zero.
        """
        return len(self.exits)

    @property
    def net(self) -> Optional[float]:
        """Realised P&L, or None while any leg is unpriced."""
        if self.unpriced:
            return None
        closed = {e.round_no for e in self.exits}
        spent = sum(f.outlay or 0.0 for f in self.fills if f.round_no in closed)
        made = sum(e.proceeds or 0.0 for e in self.exits)
        return made - spent


def _mother_key(symbol: str, mother: Bar) -> str:
    return f"{symbol.lower()}:{mother.timestamp:%Y%m%dT%H%M}"


def horizon_bars(timeframe: str, sessions: int = DEFAULT_HORIZON_SESSIONS) -> int:
    return BARS_PER_SESSION[timeframe] * sessions


class FibSpacePaperBook:
    """Every live campaign for one underlying, advanced together.

    ``premium_lookup`` returns a CURRENT quote or None; ``select_contract`` maps
    a fill to the contract the design would buy.  Both are injected so the whole
    driver is testable without a broker, and so the same object can later be
    pointed at a real order path without touching any decision logic.
    """

    def __init__(
        self,
        symbol: str,
        *,
        config: SpaceCascadeConfig,
        premium_lookup: PremiumLookup,
        select_contract: Callable[[datetime, float], object],
        entry_timeframe: str = "5m",
        geometry_timeframe: str = "15m",
        horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
        cooldown_days: int = 0,
    ) -> None:
        self.symbol = symbol
        self.config = config
        self.premium_lookup = premium_lookup
        self.select_contract = select_contract
        self.entry_timeframe = entry_timeframe
        self.geometry_timeframe = geometry_timeframe
        self.horizon_sessions = horizon_sessions
        self.cooldown_days = cooldown_days
        self.campaigns: dict[str, LiveCampaign] = {}
        self._last_accepted_start: Optional[datetime] = None

    # -- mothers ------------------------------------------------------------

    def adopt_mothers(self, geometry_bars: Sequence[Bar], mothers: Sequence) -> list[LiveCampaign]:
        """Open a campaign for each newly CONFIRMED mother.

        The scanner already refuses to confirm a pivot whose right shoulder has
        not printed, so simply re-scanning the growing series each poll is safe:
        a mother appears here on the poll after it became knowable, never before.
        """
        by_index = {b.index: b for b in geometry_bars}
        started: list[LiveCampaign] = []
        for candidate in mothers:
            bar = by_index.get(candidate.index)
            if bar is None:
                continue
            key = _mother_key(self.symbol, bar)
            if key in self.campaigns:
                continue
            # The portfolio throttle, applied to STARTS, exactly as the book
            # runner applies it -- and against the last ACCEPTED start, so a
            # cluster of near-duplicate mothers cannot walk its own tail
            # through one day at a time.
            if self.cooldown_days and self._last_accepted_start is not None:
                if (candidate.confirmed_at - self._last_accepted_start).days < self.cooldown_days:
                    continue
            campaign = LiveCampaign(
                campaign_id=key,
                symbol=self.symbol,
                mother=bar,
                arm_from_index=None,
                confirmed_at=candidate.confirmed_at,
                lot_size=self.config.lot_size,
            )
            self.campaigns[key] = campaign
            self._last_accepted_start = candidate.confirmed_at
            started.append(campaign)
        return started

    def adopt_manual_mother(self, bar: Bar, *, confirmed_at: Optional[datetime] = None) -> LiveCampaign:
        """Open a campaign on a mother the trader named.

        The scanner exists because a backtest cannot ask anyone; a person can
        look at the chart. And the record says a person should: every rule tried
        so far reproduces trades from Phil's own charts faithfully and then loses
        money applied mechanically, because he is SELECTING among the mothers the
        geometry offers and the scanner takes every one.

        A typed mother needs no right shoulder -- it is not a guess being
        confirmed, it is a decision already made -- so it arms as soon as the
        mother's own bar has closed. No cooldown either: the throttle exists to
        stop near-duplicate AUTO pivots stacking the same fall, and a person
        naming two mothers days apart meant to.

        Raises if this mother is already running, so a double submit cannot
        create two campaigns racing on one fall.
        """
        key = _mother_key(self.symbol, bar)
        if key in self.campaigns:
            raise ValueError(f"a campaign is already running on the {bar.timestamp:%d %b %Y %H:%M} mother")
        campaign = LiveCampaign(
            campaign_id=key,
            symbol=self.symbol,
            mother=bar,
            arm_from_index=None,
            confirmed_at=confirmed_at or bar.timestamp,
            lot_size=self.config.lot_size,
            source="manual",
        )
        self.campaigns[key] = campaign
        return campaign

    # -- the poll -----------------------------------------------------------

    def advance(
        self,
        campaign: LiveCampaign,
        geometry_bars: Sequence[Bar],
        entry_bars: Sequence[Bar],
        *,
        now: datetime,
    ) -> tuple[list[PaperFill], list[PaperExit]]:
        """Replay this campaign on everything closed so far; record what is new."""
        if campaign.status in ("done", "halted"):
            return [], []

        window = [b for b in geometry_bars if b.timestamp >= campaign.mother.timestamp]
        span = horizon_bars(self.geometry_timeframe, self.horizon_sessions)
        window = window[: span + 1]
        if not window:
            return [], []

        if self.entry_timeframe == self.geometry_timeframe:
            replay = window
            geometry = None
            armed = next((i for i, b in enumerate(replay) if b.timestamp >= campaign.confirmed_at), None)
        else:
            last = window[-1].timestamp
            replay = [b for b in entry_bars if campaign.mother.timestamp <= b.timestamp <= last]
            geometry = window
            armed = next((i for i, b in enumerate(replay) if b.timestamp >= campaign.confirmed_at), None)
        if not replay:
            return [], []

        result = run_space_campaign(
            campaign.mother,
            replay,
            self.config,
            arm_from_index=replay[armed].index if armed is not None else None,
            geometry_bars=geometry,
        )

        new_fills = self._record_fills(campaign, result, now=now)
        new_exits = self._record_exits(campaign, result, now=now)
        if campaign.fills and campaign.status == "watching":
            campaign.status = "trading"
        return new_fills, new_exits

    # -- recording ----------------------------------------------------------

    def _record_fills(self, campaign: LiveCampaign, result, *, now: datetime) -> list[PaperFill]:
        seen = {f.key for f in campaign.fills}
        fresh: list[PaperFill] = []
        for round_no, rnd in enumerate(result.rounds, start=1):
            for fill in rnd.fills:
                probe = self._as_paper_fill(campaign, round_no, fill, premium=None, recorded_at=None)
                if probe.key in seen:
                    continue
                contract = self.select_contract(fill.timestamp, fill.index_price)
                premium = self.premium_lookup(fill.timestamp, contract)
                recorded = self._as_paper_fill(
                    campaign, round_no, fill, premium=premium, recorded_at=now, contract=contract
                )
                campaign.fills.append(recorded)
                if premium is None:
                    campaign.unpriced += 1
                if campaign.contract is None:
                    campaign.contract = contract
                fresh.append(recorded)
        self._assert_history_intact(campaign, result)
        return fresh

    def _record_exits(self, campaign: LiveCampaign, result, *, now: datetime) -> list[PaperExit]:
        seen = {e.key for e in campaign.exits}
        fresh: list[PaperExit] = []
        for round_no, rnd in enumerate(result.rounds, start=1):
            if rnd.status != "closed" or rnd.exit_timestamp is None:
                continue
            key = (campaign.campaign_id, round_no, rnd.exit_timestamp)
            if key in seen:
                continue
            contract = campaign.contract or self.select_contract(rnd.exit_timestamp, rnd.exit_index)
            premium = self.premium_lookup(rnd.exit_timestamp, contract)
            quantity = sum(f.quantity for f in campaign.fills if f.round_no == round_no)
            recorded = PaperExit(
                campaign_id=campaign.campaign_id,
                round_no=round_no,
                timestamp=rnd.exit_timestamp,
                exit_index=float(rnd.exit_index),
                exit_reason=str(rnd.exit_reason or "target"),
                premium=premium,
                quantity=quantity,
                recorded_at=now,
            )
            campaign.exits.append(recorded)
            if premium is None:
                campaign.unpriced += 1
            fresh.append(recorded)
        return fresh

    def _as_paper_fill(
        self, campaign: LiveCampaign, round_no: int, fill: SpaceFill, *, premium, recorded_at, contract=None
    ) -> PaperFill:
        return PaperFill(
            campaign_id=campaign.campaign_id,
            round_no=round_no,
            timestamp=fill.timestamp,
            index_price=fill.index_price,
            lots=fill.lots,
            quantity=fill.quantity,
            space_label=fill.space_label,
            strike=getattr(contract, "strike", None),
            expiry=getattr(contract, "expiry", None),
            premium=premium,
            recorded_at=recorded_at,
        )

    def _assert_history_intact(self, campaign: LiveCampaign, result) -> None:
        """A causal replay must never restate a decision it already made.

        If it does, something is wrong with the engine or the bar feed, and the
        honest response is to stop this campaign -- not to overwrite a fill that
        a real broker would already have executed.
        """
        replayed = {
            (campaign.campaign_id, round_no, f.timestamp, round(f.index_price, 4), f.lots)
            for round_no, rnd in enumerate(result.rounds, start=1)
            for f in rnd.fills
        }
        missing = [f for f in campaign.fills if f.key not in replayed]
        if missing:
            campaign.status = "halted"
            campaign.halt_reason = (
                f"replay dropped {len(missing)} recorded fill(s), first at "
                f"{missing[0].timestamp:%Y-%m-%d %H:%M} -- history changed under us"
            )
            raise CampaignHalted(campaign.halt_reason)

    # -- reporting ----------------------------------------------------------

    def snapshot(self) -> dict:
        live = [c for c in self.campaigns.values() if c.status == "trading"]
        priced = [c.net for c in self.campaigns.values() if c.net is not None]
        return {
            "symbol": self.symbol,
            "campaigns": len(self.campaigns),
            "trading": len(live),
            "halted": [c.campaign_id for c in self.campaigns.values() if c.status == "halted"],
            "open_quantity": sum(c.open_quantity for c in self.campaigns.values()),
            "unpriced_legs": sum(c.unpriced for c in self.campaigns.values()),
            "realised": round(sum(priced), 2),
        }


def bars_from_candles(candles: Sequence[IndexCandle]) -> list[Bar]:
    """IndexCandles -> geometry Bars, carrying THE GAP RULE.

    A session's first bar is measured from the previous session's close, so
    red/green and the trendline anchor read the move rather than the gap.  The
    backtest's loader does this in tools/fib_space_sweep.load_bars; a live feed
    needs the identical treatment or the geometry silently differs.
    """
    bars: list[Bar] = []
    previous = None
    for i, candle in enumerate(sorted(candles, key=lambda c: c.timestamp)):
        prev_close = (
            previous.close if previous is not None and previous.timestamp.date() != candle.timestamp.date() else None
        )
        bar = Bar(
            index=i,
            timestamp=candle.timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            session_prev_close=prev_close,
        )
        bars.append(bar)
        previous = bar
    return bars
