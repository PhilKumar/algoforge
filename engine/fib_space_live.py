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

  * **Never price a fill from a quote taken at the wrong moment.**  A live LTP
    fetched now is not the fill that would have happened an hour ago, and a
    paper P&L built from late quotes flatters itself exactly where the market
    moved fastest.  A RECORDED one-minute bar from the fill's own minute is a
    different thing entirely -- a real traded price, which is what the backtest
    prices every fill from -- so the lookup may answer with one, and says so:
    each leg carries ``pricing`` of ``"live"`` (quoted as it happened) or
    ``"history"`` (read back from the contract's own candle).  A leg neither
    source can price stays UNPRICED and the campaign reports no P&L at all.
    The distinction is kept end to end because it is the honest caveat: a
    recorded bar proves the price existed, not that this fill would have been
    got at it.
  * **Never rewrite a decision it already recorded.**  The replay is causal, so
    a fill once seen must reappear identically on every later poll.  If one
    changes or vanishes, that is a real defect -- the campaign halts for a human
    rather than quietly restating its own history.
  * **Never confirm a mother early.**  A swing high is not a swing high until
    its right shoulder has printed; the scanner's own confirmed_at governs, and
    campaigns arm from there.

The driver holds no broker.  Premium comes from an injected lookup with the same
signature the options cascade already uses -- ``(timestamp, contract) -> float |
None`` -- so paper and live differ only in what that callable does.  A lookup
that can tell live quotes from recorded bars may answer ``(price, source)``
instead; a bare price still means "live", so every existing caller and every
test fake keeps working untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    premium: Optional[float] = None  # None => neither source could price it
    # "live"    quoted at the moment the decision was seen
    # "history" read back from the contract's own recorded minute
    # None      unpriced
    pricing: Optional[str] = None
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
    pricing: Optional[str] = None  # see PaperFill.pricing
    quantity: int = 0
    recorded_at: Optional[datetime] = None

    @property
    def key(self) -> tuple:
        return (self.campaign_id, self.round_no, self.timestamp)

    @property
    def proceeds(self) -> Optional[float]:
        return None if self.premium is None else self.premium * self.quantity


def _unpack_premium(quoted) -> tuple:
    """Normalise a lookup's answer to ``(price, source)``.

    A lookup that only has live quotes answers a bare float, which means it was
    quoted as the decision happened. One that can also read recorded candles
    answers ``(price, "history")`` so the record can keep the two apart. Both
    shapes are supported so no existing caller or test fake has to change.
    """
    value, source = quoted if isinstance(quoted, tuple) else (quoted, "live")
    if value is None:
        return None, None
    return float(value), str(source or "live")


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
    # The most recent replay, kept so the chart and the trade detail are drawn
    # from the SAME run that made the decisions rather than a second one.
    last_result: object = None
    last_bars: list = field(default_factory=list)

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


def _epoch(when: Optional[datetime]) -> Optional[int]:
    """Naive IST datetime -> epoch SECONDS, which is all the renderer accepts.

    It does arithmetic on these, so an ISO string silently draws nothing. The
    bars are naive IST (see bars_from_candles), so IST is subtracted here rather
    than trusting the platform's local zone.
    """
    if when is None:
        return None
    return int((when - datetime(1970, 1, 1) - timedelta(hours=5, minutes=30)).total_seconds())


def _contract_dict(contract) -> Optional[dict]:
    """The contract as the panel needs to name it, or None before one is picked."""
    if contract is None:
        return None
    expiry = getattr(contract, "expiry", None)
    return {
        "underlying": getattr(contract, "underlying", None),
        "strike": getattr(contract, "strike", None),
        "option_type": getattr(contract, "option_type", "CE"),
        "expiry": str(expiry) if expiry else None,
        "lot_size": getattr(contract, "lot_size", None),
    }


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

    def _replay(self, campaign: LiveCampaign, geometry_bars: Sequence[Bar], entry_bars: Sequence[Bar]):
        """Run the campaign over everything closed so far and stash the result.

        Pure: it reads bars and writes nothing but ``last_result``/``last_bars``.
        Splitting it out is what lets a mother be DRAWN the moment it is named --
        geometry needs only closed candles, while recording a fill needs a live
        quote, so the two must not be welded together.
        """
        window = [b for b in geometry_bars if b.timestamp >= campaign.mother.timestamp]
        span = horizon_bars(self.geometry_timeframe, self.horizon_sessions)
        window = window[: span + 1]
        if not window:
            return None

        if self.entry_timeframe == self.geometry_timeframe:
            replay, geometry = window, None
        else:
            last = window[-1].timestamp
            replay = [b for b in entry_bars if campaign.mother.timestamp <= b.timestamp <= last]
            geometry = window
        if not replay:
            return None
        armed = next((i for i, b in enumerate(replay) if b.timestamp >= campaign.confirmed_at), None)

        result = run_space_campaign(
            campaign.mother,
            replay,
            self.config,
            arm_from_index=replay[armed].index if armed is not None else None,
            geometry_bars=geometry,
        )
        campaign.last_result = result
        campaign.last_bars = list(geometry if geometry is not None else replay)
        return result

    def preview(self, campaign: LiveCampaign, geometry_bars: Sequence[Bar], entry_bars: Sequence[Bar]):
        """Draw the campaign without trading it.

        Named a mother at 8pm and want to check the fibs match your own chart?
        That needs no market and no quote. Nothing is recorded here, so no fill
        is ever priced off a stale premium as a side effect of looking.
        """
        return self._replay(campaign, geometry_bars, entry_bars)

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

        result = self._replay(campaign, geometry_bars, entry_bars)
        if result is None:
            return [], []

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
                premium, pricing = _unpack_premium(self.premium_lookup(fill.timestamp, contract))
                recorded = self._as_paper_fill(
                    campaign, round_no, fill, premium=premium, pricing=pricing, recorded_at=now, contract=contract
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
            premium, pricing = _unpack_premium(self.premium_lookup(rnd.exit_timestamp, contract))
            quantity = sum(f.quantity for f in campaign.fills if f.round_no == round_no)
            recorded = PaperExit(
                campaign_id=campaign.campaign_id,
                round_no=round_no,
                timestamp=rnd.exit_timestamp,
                exit_index=float(rnd.exit_index),
                exit_reason=str(rnd.exit_reason or "target"),
                premium=premium,
                pricing=pricing,
                quantity=quantity,
                recorded_at=now,
            )
            campaign.exits.append(recorded)
            if premium is None:
                campaign.unpriced += 1
            fresh.append(recorded)
        return fresh

    def _as_paper_fill(
        self,
        campaign: LiveCampaign,
        round_no: int,
        fill: SpaceFill,
        *,
        premium,
        recorded_at,
        contract=None,
        pricing=None,
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
            pricing=pricing,
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

    def campaign_detail(self, campaign: LiveCampaign, *, mark_to_market: bool = True, now: datetime = None) -> dict:
        """Every rupee of one campaign: what was paid, what came back, what is still on.

        ``capital_spent`` is premium actually paid out, which for a bought
        option IS the money at stake -- there is no margin beyond it and no
        further call, so it is both the cost and the worst case.

        ``mark_to_market`` re-quotes the OPEN legs at the current premium. That
        number moves every tick and is not a result; it is what the position
        would fetch if closed now.  It is reported separately from ``realised``
        and never added into it.

        It asks for the price AT ``now``, never at the fill's own timestamp.
        That distinction did not matter while the lookup could only answer with
        a live quote -- asking about an old minute simply returned None.  It
        matters absolutely once the lookup can read recorded candles: asking
        about the fill's minute would hand back the entry premium and report it
        as "worth this now", so every open position would show exactly zero
        unrealised P&L forever.  Without a ``now`` there is nothing honest to
        mark against, so the marking is skipped rather than guessed.
        """
        closed_rounds = {e.round_no for e in campaign.exits}
        by_round: dict = {}
        for fill in campaign.fills:
            by_round.setdefault(fill.round_no, {"fills": [], "exit": None})["fills"].append(fill)
        for exit_ in campaign.exits:
            by_round.setdefault(exit_.round_no, {"fills": [], "exit": None})["exit"] = exit_

        rounds = []
        spent_total = open_cost = realised_total = 0.0
        open_value = 0.0
        marked = False
        for number in sorted(by_round):
            entry = by_round[number]
            fills, exit_ = entry["fills"], entry["exit"]
            spent = sum(f.outlay or 0.0 for f in fills)
            spent_total += spent
            unpriced = any(f.premium is None for f in fills)

            row = {
                "round": number,
                "fills": [
                    {
                        "at": f.timestamp.isoformat(),
                        "index_price": round(f.index_price, 2),
                        "lots": f.lots,
                        "quantity": f.quantity,
                        "strike": f.strike,
                        "expiry": str(f.expiry) if f.expiry else None,
                        "premium": f.premium,
                        "pricing": f.pricing,
                        "outlay": None if f.premium is None else round(f.outlay, 2),
                        "space": f.space_label,
                    }
                    for f in fills
                ],
                "quantity": sum(f.quantity for f in fills),
                "lots": sum(f.lots for f in fills),
                "capital_spent": None if unpriced else round(spent, 2),
                "average_premium": (
                    None if unpriced or not fills else round(spent / max(sum(f.quantity for f in fills), 1), 2)
                ),
                "status": "closed" if exit_ is not None else "open",
            }
            if exit_ is not None:
                proceeds = exit_.proceeds
                row["exit"] = {
                    "at": exit_.timestamp.isoformat(),
                    "index_price": round(exit_.exit_index, 2),
                    "reason": exit_.exit_reason,
                    "premium": exit_.premium,
                    "pricing": exit_.pricing,
                    "proceeds": None if proceeds is None else round(proceeds, 2),
                }
                if proceeds is not None and not unpriced:
                    realised_total += proceeds - spent
                    row["realised"] = round(proceeds - spent, 2)
                else:
                    row["realised"] = None
            else:
                row["exit"] = None
                row["realised"] = None
                open_cost += spent
                if mark_to_market and not unpriced and fills and now is not None:
                    quote, _ = _unpack_premium(self.premium_lookup(now, campaign.contract))
                    if quote is not None:
                        value = quote * row["quantity"]
                        row["mark_premium"] = quote
                        row["mark_value"] = round(value, 2)
                        row["unrealised"] = round(value - spent, 2)
                        open_value += value
                        marked = True
            rounds.append(row)

        return {
            "campaign_id": campaign.campaign_id,
            "symbol": campaign.symbol,
            "source": campaign.source,
            "status": campaign.status,
            "halt_reason": campaign.halt_reason,
            "lot_size": campaign.lot_size,
            "mother": {
                "at": campaign.mother.timestamp.isoformat(),
                "high": round(campaign.mother.high, 2),
                "low": round(campaign.mother.low, 2),
            },
            "contract": _contract_dict(campaign.contract),
            "rounds": rounds,
            "closed_rounds": len(closed_rounds),
            "unpriced_legs": campaign.unpriced,
            # Legs priced from a recorded candle rather than quoted as they
            # happened. The rupees are real traded prices, but they are not
            # proof this fill would have been got — so the caveat travels with
            # the number instead of being lost in the total.
            "history_priced_legs": sum(
                1 for leg in list(campaign.fills) + list(campaign.exits) if leg.pricing == "history"
            ),
            # Totals. capital_spent is every rupee ever paid; capital_open is
            # what is still tied up in positions that have not closed.
            "capital_spent": round(spent_total, 2),
            "capital_open": round(open_cost, 2),
            "realised": None if campaign.unpriced else round(realised_total, 2),
            "open_value": round(open_value, 2) if marked else None,
            "unrealised": round(open_value - open_cost, 2) if marked else None,
        }

    def campaign_chart(self, campaign: LiveCampaign) -> dict:
        """The payload static/philforge-bench-chart.js draws.

        Built from ``campaign.last_result`` -- the same replay the decisions came
        out of -- so the picture cannot disagree with the trade beside it.  See
        that file for the contract; the rule that bites is that every ``t`` is
        epoch SECONDS, and OHLC must be native, because the renderer draws its
        own session-gap blocks.
        """
        result = campaign.last_result
        bars = campaign.last_bars or []
        if result is None or not bars:
            return {"status": "not_ready", "reason": "no replay yet — the first poll has not run"}

        mother_at = campaign.mother.timestamp
        candles = [
            {
                "t": _epoch(b.timestamp),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "is_mother": b.timestamp == mother_at,
            }
            for b in bars
        ]

        geometry = getattr(result, "geometry", None)
        # A trendline stores its anchors by BAR INDEX, not by time, so the
        # timestamps have to come back through the series the geometry ran on.
        stamp_of = {b.index: b.timestamp for b in bars}
        trendlines = []
        for line in getattr(geometry, "trendlines", []) or []:
            a1, a2 = stamp_of.get(line.anchor1_index), stamp_of.get(line.anchor2_index)
            if a1 is None or a2 is None:
                continue  # an anchor older than the window is not drawable
            trendlines.append(
                {
                    "id": line.trendline_id,
                    "a1": {"t": _epoch(a1), "p": line.anchor1_price},
                    "a2": {"t": _epoch(a2), "p": line.anchor2_price},
                    "active": line.trendline_id == getattr(geometry, "active_trendline_id", None),
                }
            )

        # Each drawn fib becomes a leg, with its rungs as levels. The ladder
        # this design buys is 1-2-4-8, so those are the levels worth drawing.
        legs = [
            {
                "leg_id": fib.fib_id,
                "touch_timestamp": _epoch(fib.drawn_timestamp),
                "touch_high": fib.fib0,
                "low": fib.fib1,
                "levels": {str(level): fib.fib0 - level * fib.span for level in (0, 1, 2, 4, 8)},
                "orders": [],
            }
            for fib in getattr(geometry, "fibs", []) or []
        ]

        entries = [{"t": _epoch(f.timestamp), "price": f.index_price} for f in campaign.fills]
        exits = [
            {
                "t": _epoch(e.timestamp),
                "price": e.exit_index,
                "pnl": next(
                    (
                        r.get("realised")
                        for r in self.campaign_detail(campaign, mark_to_market=False)["rounds"]
                        if r["round"] == e.round_no
                    ),
                    None,
                ),
            }
            for e in campaign.exits
        ]

        open_round = next((r for r in reversed(result.rounds) if r.status != "closed"), None)
        last_round = result.rounds[-1] if result.rounds else None
        target = (open_round or last_round).target_index if (open_round or last_round) else None
        hit = bool(campaign.exits)
        return {
            "status": "ok",
            "timeframe": self.geometry_timeframe,
            "candles": candles,
            "mother": {"high": campaign.mother.high, "low": campaign.mother.low},
            "trendlines": trendlines,
            "legs": legs,
            "entries": entries,
            "exits": exits,
            "avg_entry_price": (open_round or last_round).average_entry if (open_round or last_round) else None,
            "tp_price": target,
            # Never draw the target as if it were a sale that happened. Whether
            # price got there is the one thing the chart must not blur.
            "tp_label": "TARGET HIT" if hit else "TARGET",
        }

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
            "history_priced_legs": sum(
                1 for c in self.campaigns.values() for leg in list(c.fills) + list(c.exits) if leg.pricing == "history"
            ),
            "realised": round(sum(priced), 2),
        }


def bars_from_candles(candles: Sequence[IndexCandle]) -> list[Bar]:
    """IndexCandles -> geometry Bars, carrying THE GAP RULE.

    A session's first bar is measured from the previous session's close, so
    red/green and the trendline anchor read the move rather than the gap.  The
    backtest's loader does this in tools/fib_space_sweep.load_bars; a live feed
    needs the identical treatment or the geometry silently differs.

    TIMESTAMPS ARE FLATTENED TO NAIVE IST HERE, and that is not cosmetic.  The
    adapter hands back tz-AWARE candles; the backtest reads naive ones out of
    its JSON cache.  Leaving the difference in place meant the live driver and
    the replay it is supposed to be identical to were carrying different types
    in the one field every comparison keys on -- and an aware datetime never
    equals a naive one, so a mother named as "03 Aug 15:00" silently matched no
    bar at all.  Everything downstream (campaign ids, the history guard, the
    premium lookup) is naive IST, so the conversion belongs at this boundary.
    """
    bars: list[Bar] = []
    previous = None
    for i, candle in enumerate(sorted(candles, key=lambda c: c.timestamp)):
        stamp = candle.timestamp.replace(tzinfo=None) if candle.timestamp.tzinfo else candle.timestamp
        prev_close = previous.close if previous is not None and previous.timestamp.date() != stamp.date() else None
        bar = Bar(
            index=i,
            timestamp=stamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            session_prev_close=prev_close,
        )
        bars.append(bar)
        previous = bar
    return bars
