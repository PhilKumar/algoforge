"""
engine/fib_space_geometry.py -- Phil's adjudicated trendline+fib geometry, on NIFTY.

This is the port CryptoForge's `_advance_geometry` earned on 2026-07-31 after
two rounds of visual adjudication against Phil's own charts.  PhilForge's older
`NiftyIndexCascadeGeometry` still runs the SUPERSEDED rule (a backward
`find_index_valid_anchor2` search with no memory of the standing line, which is
why its lines got steeper every time); nothing here shares code with it.

The rule, unchanged from the adjudication:

  TRENDLINES.  A low runs down while the market falls and LOCKS when a candle
  closes back above the low candle's close.  Once locked, wicks under it move
  nothing -- only a red candle CLOSING decisively below it, and that close is
  the draw event.  The anchor is the red candle with the highest OPEN after the
  low whose line from the mother high comes out CLEAN (no close crosses it, on
  either side, beyond the slack); a blocked candidate hands over to the next one
  down, and only when every candidate is cut is there no line.  A line then
  STANDS until some close breaks above it -- that, and only that, arms the next.

  FIBS.  A fib lives on a line.  Its touch is a candle whose high reaches the
  standing line and whose close falls back under it, after a low exists;
  fib0 = the touch high (the top graze of the same structure wins), fib1 = the
  ultimate low since the mother as it stood at that touch.  The fib is DRAWN
  only when its own fib1 is decisively closed below -- levels exist to fund the
  way down, so nothing is drawn while the market rises.

TWO NIFTY DEPARTURES, both forced by the instrument rather than by taste:

  * Time is measured in BARS, not seconds.  NSE closes overnight, so a
    wall-clock slope drags a line through 17 hours where nothing traded and it
    lands far below the chart by the next open.  TradingView -- where Phil draws
    the lines this engine has to reproduce -- spaces candles by bar index, so
    bar index is also what matches his eye.
  * The anchor slack is a fraction of a typical CANDLE, not a fraction of
    price.  A crypto-sized 0.045% of 24,000 is ~11 points, wider than a whole
    15m NIFTY bar, and it let the search walk straight past the swing high the
    line belongs on (see GEOMETRY_ANCHOR_CLOSE_TOLERANCE_CANDLES).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

__all__ = [
    "FIB_LEVELS",
    "Bar",
    "DrawnFib",
    "SpaceGeometry",
    "Trendline",
    "run_geometry",
]

# The deep levels a fib funds.  Same three the rest of the stack uses.
FIB_LEVELS: tuple[int, ...] = (2, 4, 8)

# A probe a few points under the dip is the fall resuming, not a completed
# swing being cut.  Carried over from CryptoForge unchanged -- it is a fraction
# of price and at NIFTY scale 0.02% is ~5 points, which is the right order.
DECISIVE_BREAK_PCT = 0.0002
# A swing smaller than this is chop whose levels would be noise.
#
# MEASURED IN CANDLES, NOT PRICE, since 2026-08-17.  The crypto value (0.1% of
# price) is a real swing on BTC and 24 points on NIFTY at 24,400 -- larger
# than the whole 19.65-point swing Phil marked on the 14-Aug-2026 5m chart, so
# the engine drew his trendline, found his touch on the 14:55 swing high, saw
# the low break at 15:10, and then threw the fib away as "chop".  The same
# instrument-scaling trap the anchor slack fell into (see
# GEOMETRY_ANCHOR_CLOSE_TOLERANCE_CANDLES); the same fix: a swing has to be at
# least this many TYPICAL BARS of the mother's own timeframe tall, so a 5m
# ladder and a 1H ladder each judge chop by their own candles.
MIN_FIB_RANGE_CANDLES = 1.0
# The old price fraction, kept only as the floor when a window is too young to
# have a median bar yet (fewer than a handful of candles after the mother).
MIN_FIB_RANGE_PCT = 0.001
# Two fibs whose touch highs sit this close are the same shelf.
MIN_LEG_SEPARATION_PCT = 0.0003
# Every line starts at the mother high, so two lines differing by less than
# this at the candle that drew the second are one line drawn twice.
MIN_TRENDLINE_SEPARATION_PCT = 0.0015
# How far a close may poke above a candidate line before the anchor is
# disqualified -- as a fraction of the window's MEDIAN CANDLE RANGE.  Nobody
# drags a line that honours every close to the paisa, but the crypto rule
# (0.045% of price) is ~11 points on NIFTY, wider than a whole 15m bar, and a
# slack that big swallows the swing high the line is supposed to sit on.
GEOMETRY_ANCHOR_CLOSE_TOLERANCE_CANDLES = 0.2


@dataclass(frozen=True)
class Bar:
    """One closed index candle, carrying its own position in the series.

    ``index`` is what every slope is measured against -- see the module
    docstring on why NIFTY cannot use wall-clock seconds.
    """

    index: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    # THE GAP RULE.  Set only on the FIRST bar of a session, to the previous
    # session's close.  Phil, 2026-08-02: "the previous day close is green and
    # current day's open is gap down, the price has to be calculated from the
    # close of yesterday as the open of the current day."
    #
    # NSE closes overnight, so a session-opening candle's own open is where the
    # gap ENDED, not where the move began.  Judging that candle red or green
    # against it hides the whole overnight fall inside a bar that can look
    # green: BankNifty's 23-Apr-2026 09:15 closed 128 points ABOVE its own open
    # and 378 points BELOW the previous close.  On its own open it is green and
    # the two-candle count resets; measured from yesterday's close it is red,
    # which is what the eye reads on the chart and what the rule means.
    session_prev_close: Optional[float] = None

    @property
    def effective_open(self) -> float:
        """Where this candle's move actually began -- see ``session_prev_close``."""
        return self.open if self.session_prev_close is None else self.session_prev_close

    @property
    def is_red(self) -> bool:
        return self.close < self.effective_open

    @property
    def is_green(self) -> bool:
        return self.close > self.effective_open

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class Trendline:
    trendline_id: int
    anchor1_price: float
    anchor1_index: int
    anchor2_price: float
    anchor2_index: int
    anchor2_timestamp: datetime

    def price_at(self, index: int) -> float:
        span = self.anchor2_index - self.anchor1_index
        if span == 0:
            return self.anchor1_price
        slope = (self.anchor2_price - self.anchor1_price) / span
        return self.anchor1_price + slope * (index - self.anchor1_index)


@dataclass(frozen=True)
class DrawnFib:
    """A completed structure: fib0 (touch high) down to fib1 (its low)."""

    fib_id: int
    trendline_id: int
    fib0: float
    fib1: float
    touch_index: int
    touch_timestamp: datetime
    drawn_index: int
    drawn_timestamp: datetime

    @property
    def span(self) -> float:
        return self.fib0 - self.fib1

    def level_price(self, level: float) -> float:
        """Deep level N sits N spans below fib0 -- the ladder funds downward."""
        return self.fib0 - level * self.span


def _ladders_overlap(high_a: float, low_a: float, high_b: float, low_b: float) -> bool:
    """Do two fibs put rungs in the same stretch of price?

    Comparing touch highs alone calls "same high, far deeper low" a duplicate,
    and it is not -- that is the next swing down, funding ground the incumbent
    never reaches.
    """
    deepest, shallowest = max(FIB_LEVELS), min(FIB_LEVELS)
    range_a, range_b = high_a - low_a, high_b - low_b
    if range_a <= 0 or range_b <= 0:
        return True
    floor_a, ceiling_a = high_a - deepest * range_a, high_a - shallowest * range_a
    floor_b, ceiling_b = high_b - deepest * range_b, high_b - shallowest * range_b
    return not (ceiling_a < floor_b or ceiling_b < floor_a)


@dataclass
class SpaceGeometry:
    """Incremental geometry over one mother candle's window."""

    mother: Bar
    # THE FIRST STRUCTURE NEEDS NO TRENDLINE.  Phil's 07-May-2026 NIFTY chart,
    # marked candle by candle: his fib A is 0 = 24,420.55 (the 13:00 bar's
    # HIGH) over 1 = 24,377.70 (the 12:45 bar's LOW) -- the first pullback off
    # the mother and the bounce out of it, drawn at 13:00.  The engine cannot
    # see that structure at all: a fib here must touch a STANDING line, the
    # first line needs a locked low broken decisively, and that did not happen
    # until 15:00 -- by which time it drew a different fib (0 = 24,385.30,
    # 1 = 24,324.55) whose levels sit ~100 points under his.  That single
    # late reading is why his 08-May 09:25 buy has no counterpart in the
    # backtest.  With this on, the FIRST fib of a campaign is seeded from the
    # first bounce; every later fib still needs its line.
    seed_first_fib: bool = False
    trendlines: list[Trendline] = field(default_factory=list)
    fibs: list[DrawnFib] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    # -- adjudicated state ------------------------------------------------
    active_trendline_id: Optional[int] = None
    armed: bool = True
    low: Optional[float] = None
    low_index: Optional[int] = None
    low_close: Optional[float] = None
    low_locked: bool = False
    ultimate_low: Optional[float] = None
    pending: list[dict] = field(default_factory=list)
    _history: list[Bar] = field(default_factory=list)
    _finished: bool = False

    def __post_init__(self) -> None:
        if not self._history:
            self._history = [self.mother]

    # -- helpers ----------------------------------------------------------

    @property
    def finished(self) -> bool:
        """True once a high back above the mother ended this structure."""
        return self._finished

    @property
    def active_trendline(self) -> Optional[Trendline]:
        if self.active_trendline_id is None:
            return None
        return next((t for t in self.trendlines if t.trendline_id == self.active_trendline_id), None)

    def _log(self, bar: Bar, event: str, **payload) -> None:
        self.events.append({"timestamp": bar.timestamp.isoformat(), "event": event, **payload})

    def _window(self, upto: Bar) -> list[Bar]:
        return [b for b in self._history if self.mother.index < b.index <= upto.index]

    def _typical_bar(self, window: list[Bar]) -> float:
        """The window's median candle range, in points -- the unit both
        instrument-scaled gates are measured in."""
        ranges = sorted(b.range for b in window if b.range > 0)
        return ranges[len(ranges) // 2] if ranges else 0.0

    def _slack(self, window: list[Bar]) -> float:
        """The anchor slack in points: a fraction of the window's median bar."""
        return self._typical_bar(window) * GEOMETRY_ANCHOR_CLOSE_TOLERANCE_CANDLES

    def _min_range(self, at_price: float, window: Optional[list[Bar]] = None) -> float:
        """The smallest swing that is a structure and not chop, in points.

        Candle-scaled (MIN_FIB_RANGE_CANDLES typical bars) once the window has
        enough bars to know its own size; before that, the price-fraction floor
        so a two-bar-old window is not judged against a median of nothing.
        """
        window = self._window(self._history[-1]) if window is None else window
        typical = self._typical_bar(window) if len(window) >= 3 else 0.0
        if typical > 0:
            return typical * MIN_FIB_RANGE_CANDLES
        return at_price * MIN_FIB_RANGE_PCT

    # -- the state machine ------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        if self._finished or bar.index <= self.mother.index:
            return
        # A campaign's geometry belongs to the fall under ONE mother; a high
        # back above it means this structure is over.
        if bar.high > self.mother.high:
            self._finished = True
            self._log(bar, "mother_broken")
            return
        self._history.append(bar)

        # 1. A close above the standing line spends it and arms the next.  The
        #    same candle-sized slack the anchor test uses, so a one-tick poke
        #    does not retire a line the eye still sees standing.
        line = self.active_trendline
        if line is not None and not self.armed:
            level = line.price_at(bar.index)
            if level > 0 and bar.close > level + self._slack(self._window(bar)):
                self.armed = True
                self._log(bar, "trendline_spent", trendline_id=line.trendline_id, level=round(level, 2))

        # 2. Pending fibs whose fib1 this red close breaks are drawn now.
        if bar.is_red:
            self._draw_due_fibs(bar)

        # 3. A decisive red close under the LOCKED low draws the next line.
        if (
            self.low_locked
            and self.low is not None
            and bar.is_red
            and bar.close < self.low - self.low * DECISIVE_BREAK_PCT
        ):
            if self.armed:
                self._draw_trendline(bar)
            self.low = bar.low
            self.low_index = bar.index
            self.low_close = bar.close
            self.low_locked = False
            self.ultimate_low = bar.low if self.ultimate_low is None else min(self.ultimate_low, bar.low)
            return

        # 4. A live touch on the STANDING line files a pending fib.  Touches on
        #    a spent line count for nothing, and a touch needs a low before it:
        #    the V is dip first, rise second.
        if (
            line is not None
            and not self.armed
            and self.low_index is not None
            and bar.index > self.low_index
            and bar.high < self.mother.high
        ):
            level = line.price_at(bar.index)
            if level > 0 and bar.high >= level and bar.close < level:
                fib1 = bar.low if self.ultimate_low is None else min(self.ultimate_low, bar.low)
                self._file_pending(bar.high, bar.index, bar.timestamp, fib1, line.trendline_id)

        # 4b. SEED FIB -- see ``seed_first_fib``.  Runs BEFORE low tracking so
        #     the low still reads the PREVIOUS bar's, which is what makes
        #     fib1 = 24,377.70 (12:45's low) rather than 24,376.90 (13:00's).
        if (
            self.seed_first_fib
            and not self.fibs
            and not self.trendlines
            and self.low_index is not None
            and bar.index > self.low_index
            and bar.high < self.mother.high
        ):
            ultimate = None
            for other in self._history:
                if other.index <= self.mother.index or other.index >= bar.index:
                    continue
                ultimate = other.low if ultimate is None else min(ultimate, other.low)
            if ultimate is not None and (bar.high - ultimate) >= self._min_range(bar.high):
                self._file_pending(bar.high, bar.index, bar.timestamp, ultimate, 0)

        # 5. Low tracking: run down while falling, lock on the rise.  A GREEN
        #    candle that sets the low locks it itself when its recovery is
        #    structure-sized -- the whole V inside one bar.
        self.ultimate_low = bar.low if self.ultimate_low is None else min(self.ultimate_low, bar.low)
        if self.low is None or bar.low < self.low:
            if not self.low_locked:
                self.low = bar.low
                self.low_index = bar.index
                self.low_close = bar.close
                if bar.is_green and (bar.close - bar.low) >= self._min_range(bar.close):
                    self.low_locked = True
        elif not self.low_locked and self.low_close is not None and bar.close > self.low_close:
            self.low_locked = True

    # -- trendlines -------------------------------------------------------

    def _draw_trendline(self, bar: Bar) -> None:
        window = self._window(bar)
        if not window:
            return
        candidates = [
            b
            for b in window
            if self.low_index is not None
            and b.index > self.low_index
            and b.is_red
            and b.effective_open < self.mother.high
        ]
        if not candidates:
            return
        slack = self._slack(window)
        anchor = None
        blocked = None
        for candidate in sorted(candidates, key=lambda b: -b.effective_open):
            span = candidate.index - self.mother.index
            if span <= 0:
                continue
            crossed = None
            for other in window:
                if other.index == candidate.index:
                    continue
                level = self.mother.high + (candidate.effective_open - self.mother.high) * (
                    (other.index - self.mother.index) / span
                )
                if other.close > level + slack:
                    crossed = (other, level)
                    break
            if crossed is None:
                anchor = candidate
                break
            if blocked is None:
                blocked = (candidate, crossed)
        if anchor is None:
            if blocked is not None:
                top, (other, level) = blocked
                self._log(
                    bar,
                    "trendline_refused",
                    highest_open=round(top.effective_open, 2),
                    cut_by_close=round(other.close, 2),
                    against=round(level, 2),
                )
            return

        candidate_line = Trendline(
            trendline_id=len(self.trendlines) + 1,
            anchor1_price=self.mother.high,
            anchor1_index=self.mother.index,
            anchor2_price=anchor.effective_open,
            anchor2_index=anchor.index,
            anchor2_timestamp=anchor.timestamp,
        )
        # Two lines this close at the drawing bar are one line drawn twice.
        mine = candidate_line.price_at(bar.index)
        for existing in self.trendlines:
            theirs = existing.price_at(bar.index)
            if theirs > 0 and abs(mine - theirs) / theirs < MIN_TRENDLINE_SEPARATION_PCT:
                return

        self.trendlines.append(candidate_line)
        self.active_trendline_id = candidate_line.trendline_id
        self.armed = False
        self._log(
            bar,
            "trendline_drawn",
            trendline_id=candidate_line.trendline_id,
            anchor_open=round(anchor.effective_open, 2),
            anchor_at=anchor.timestamp.isoformat(),
        )

        # The touch is usually already behind us -- often the anchor candle
        # itself -- so the new line's history is read back for it, and the very
        # break that drew the line can complete the fib in the same breath.
        touch = self._retro_touch(candidate_line, bar, window)
        if touch is None:
            ultimate = None
            for other in window:
                if other.index >= candidate_line.anchor2_index:
                    break
                ultimate = other.low if ultimate is None else min(ultimate, other.low)
            anchor_bar = next((b for b in window if b.index == candidate_line.anchor2_index), None)
            if ultimate is not None and anchor_bar is not None and anchor_bar.high < self.mother.high:
                fib1 = min(ultimate, anchor_bar.low)
                if (anchor_bar.high - fib1) >= self._min_range(anchor_bar.high, window):
                    touch = (anchor_bar.high, anchor_bar.index, anchor_bar.timestamp, fib1)
        if touch is None:
            return
        high, index, stamp, fib1 = touch
        pending = {
            "fib0": high,
            "index": index,
            "timestamp": stamp,
            "fib1": fib1,
            "trendline_id": candidate_line.trendline_id,
        }
        if bar.index > index and bar.close < fib1 - fib1 * DECISIVE_BREAK_PCT:
            self._draw_fib(bar, pending)
        else:
            self._file_pending(high, index, stamp, fib1, candidate_line.trendline_id)

    def _retro_touch(self, line: Trendline, cut: Bar, window: list[Bar]):
        """The first genuine touch on a just-drawn line, read from behind it."""
        best = None
        ultimate = None
        for bar in window:
            if (
                ultimate is not None
                and bar.index != cut.index
                and bar.high < self.mother.high
                # The anchor candle with no wick above its open never TESTED
                # the line -- the line merely passes through its open.
                and not (bar.index == line.anchor2_index and bar.high <= line.anchor2_price)
            ):
                level = line.price_at(bar.index)
                if level > 0 and bar.high >= level and bar.close < level:
                    fib1 = min(ultimate, bar.low)
                    if (bar.high - fib1) >= self._min_range(bar.high, window):
                        if best is None or fib1 < best[3] - best[3] * 1e-9:
                            best = (bar.high, bar.index, bar.timestamp, fib1)
                        elif bar.high > best[0]:
                            best = (bar.high, bar.index, bar.timestamp, best[3])
            ultimate = bar.low if ultimate is None else min(ultimate, bar.low)
        return best

    # -- fibs -------------------------------------------------------------

    def _file_pending(self, fib0: float, index: int, stamp: datetime, fib1: float, trendline_id: int) -> None:
        if fib1 is None or fib0 <= fib1:
            return
        if (fib0 - fib1) < self._min_range(fib0):
            return
        for row in self.pending:
            if abs(row["fib1"] - fib1) <= fib1 * 1e-9:
                # Same structure grazing again: the TOP graze is fib 0.
                if fib0 > row["fib0"]:
                    row["fib0"], row["index"], row["timestamp"], row["trendline_id"] = (
                        fib0,
                        index,
                        stamp,
                        trendline_id,
                    )
                return
        # A pending the market has already fallen below was not where the swing
        # turned -- the deeper structure supersedes it.
        self.pending = [row for row in self.pending if row["fib1"] <= fib1 + fib1 * 1e-9]
        self.pending.append(
            {"fib0": fib0, "index": index, "timestamp": stamp, "fib1": fib1, "trendline_id": trendline_id}
        )

    def _draw_due_fibs(self, bar: Bar) -> None:
        due = [
            row
            for row in self.pending
            if bar.index > row["index"] and bar.close < row["fib1"] - row["fib1"] * DECISIVE_BREAK_PCT
        ]
        for row in due:
            self.pending.remove(row)
            self._draw_fib(bar, row)

    def _draw_fib(self, bar: Bar, pending: dict) -> None:
        fib0, fib1 = pending["fib0"], pending["fib1"]
        # ONE STRUCTURE, ONE FIB.  Two touches sharing the same low are the same
        # swing grazing the line twice, and Phil's rule is that the TOP graze is
        # fib 0.  _file_pending already merges them, but a fib drawn straight off
        # a freshly-drawn trendline skips that path -- which on his 26-Feb-2026
        # 11:15 mother left both 0=25,468.50 and 0=25,527.15 standing on the same
        # 25,400.95 low, and the pair of them manufactured a convergence space
        # his chart does not have.
        for existing in list(self.fibs):
            if abs(existing.fib1 - fib1) <= fib1 * 1e-9:
                if fib0 <= existing.fib0:
                    return
                self.fibs.remove(existing)
                self._log(bar, "fib_superseded", kept=round(fib0, 2), dropped=round(existing.fib0, 2))
                break
        for existing in self.fibs:
            if not _ladders_overlap(fib0, fib1, existing.fib0, existing.fib1):
                continue
            if abs(fib0 - existing.fib0) / existing.fib0 >= MIN_LEG_SEPARATION_PCT:
                continue
            # Same shelf -- but a structure whose fib1 sits decisively below the
            # incumbent's is the next swing down, funding ground it never reaches.
            if fib1 < existing.fib1 - existing.fib1 * DECISIVE_BREAK_PCT:
                continue
            self._log(bar, "fib_skipped_same_shelf", fib0=round(fib0, 2), against=round(existing.fib0, 2))
            return
        drawn = DrawnFib(
            fib_id=len(self.fibs) + 1,
            trendline_id=pending["trendline_id"],
            fib0=fib0,
            fib1=fib1,
            touch_index=pending["index"],
            touch_timestamp=pending["timestamp"],
            drawn_index=bar.index,
            drawn_timestamp=bar.timestamp,
        )
        self.fibs.append(drawn)
        self._log(
            bar,
            "fib_drawn",
            fib_id=drawn.fib_id,
            fib0=round(fib0, 2),
            fib1=round(fib1, 2),
            levels={str(n): round(drawn.level_price(n), 2) for n in FIB_LEVELS},
        )


def run_geometry(mother: Bar, bars: Iterable[Bar]) -> SpaceGeometry:
    """Replay a whole window in one call."""
    geometry = SpaceGeometry(mother=mother)
    for bar in sorted(bars, key=lambda b: b.index):
        geometry.on_bar(bar)
    return geometry
