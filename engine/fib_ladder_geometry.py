"""engine/fib_ladder_geometry.py -- the ladder's view of the drawn structures.

Phil merged Fib Boundary and Fib Space into one strategy on 2026-08-15. Both
now read the SAME geometry -- the adjudicated trendline+fib rule already ported
to NIFTY in `fib_space_geometry` -- and differ only in what they buy:

    levels       every level of every fib          (the old Fib Boundary)
    convergence  only where two fibs' levels meet  (the old Fib Space)

This module is the seam. It owns the two things the ladder cannot get from
`SpaceGeometry` by itself:

  * THE BAR CONVERSION. `SpaceGeometry` measures slopes in BAR INDEX, not
    wall-clock seconds -- NSE closes overnight, and a clock-based slope drags a
    line through 17 hours where nothing traded. It also needs
    `session_prev_close` on the first bar of each session, or a gap-down candle
    that closed above its own open reads GREEN when the eye and the rule both
    say red. The ladder is fed plain candles, so something has to number them
    and carry yesterday's close across; that is done here, once, rather than at
    every call site.

  * THE STACK. Phil, 2026-08-15: a new fib ADDS its levels and the old ones
    keep resting. So a rung is identified by (fib, level), never by level
    alone -- two fibs both have an L4 and they are different prices and
    different money.

Nothing here decides anything. It draws, and it lists what is drawable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Optional, Sequence

from engine.fib_space_geometry import Bar as GeoBar
from engine.fib_space_geometry import DrawnFib, SpaceGeometry


@dataclass(frozen=True)
class LadderLevel:
    """One drawn level: which fib it belongs to, and what it is worth."""

    fib_id: int
    level: int
    price: float
    # The bar the parent fib was DRAWN on. Nothing may trade a level before
    # this: the fib did not exist yet, and buying on it would be reading the
    # future the same way the old swing anchor did before `confirmed_at`.
    drawn_at: datetime

    @property
    def key(self) -> str:
        """Stable across redraws, and unique per (fib, level)."""
        return f"F{self.fib_id}L{self.level}"


class LadderGeometry:
    """Draws the structures and lists their levels, in bar order."""

    def __init__(self, levels: Sequence[int], *, seed_first_fib: bool = False, side: str = "CE") -> None:
        if not levels:
            raise ValueError("levels must not be empty")
        side = str(side).upper()
        if side not in {"CE", "PE"}:
            raise ValueError("side must be CE or PE")
        self.levels = tuple(int(level) for level in levels)
        self.seed_first_fib = bool(seed_first_fib)
        # THE PE MIRROR. `SpaceGeometry` knows one shape: a FALL under a
        # mother -- locked lows, a decisive close BELOW, fib1 at the low, rungs
        # stepping DOWN. A PE is the same structure upside down: the mother
        # marks a low, the setup is a rise that fails, the break is a close
        # ABOVE a locked high, and the rungs step UP. Rather than teach the
        # geometry a second shape (Phil, 2026-08-17: one geometry under
        # everything, entries differ only), a PE feeds it the world negated --
        # every price times -1, high and low swapped -- and every price read
        # back out is negated again. The engine never learns PE exists.
        #
        # Until 2026-08-17 there was no mirror at all: a PE mother was handed
        # CE geometry, waited for a LOW to break on a rising day, and sat at
        # "waiting for swing" forever (Phil's 17-Aug 11:35 mother).
        self.side = side
        self._sign = -1.0 if side == "PE" else 1.0
        self._geometry: Optional[SpaceGeometry] = None
        self._bars: list[GeoBar] = []
        self._prev: Optional[GeoBar] = None

    def _in(self, price: float) -> float:
        """A market price as the fall-shaped geometry must see it."""
        return float(price) * self._sign

    def _out(self, price: float) -> float:
        """A geometry price back in market terms."""
        return float(price) * self._sign

    # -- feeding ---------------------------------------------------------

    def _to_geo_bar(self, bar: Any) -> GeoBar:
        """Number the bar and carry the session gap across.

        `session_prev_close` is set ONLY on the first bar of a session, to the
        previous session's close -- exactly as tools/fib_space_sweep.py builds
        it, so the engine and every sweep that has been measured against Phil's
        charts read the same candles as red or green.
        """
        stamp = bar.timestamp
        prev_close = None
        if self._prev is not None and self._prev.timestamp.date() != stamp.date():
            prev_close = self._prev.close  # already in geometry terms
        # Under negation the bar's high becomes its low and vice versa; open
        # and close keep their roles, so red/green flip -- which is exactly the
        # PE reading (a rising red day is a falling green day, mirrored).
        hi, lo = self._in(bar.high), self._in(bar.low)
        return GeoBar(
            index=len(self._bars),
            timestamp=stamp,
            open=self._in(bar.open),
            high=max(hi, lo),
            low=min(hi, lo),
            close=self._in(bar.close),
            session_prev_close=prev_close,
        )

    def on_bar(self, bar: Any, *, is_mother: bool = False) -> None:
        """Feed one CLOSED candle of the timeframe the geometry is drawn on."""
        geo = self._to_geo_bar(bar)
        self._bars.append(geo)
        self._prev = geo
        if self._geometry is None:
            if not is_mother:
                return  # nothing is drawn before the mother is seen
            self._geometry = SpaceGeometry(mother=geo, seed_first_fib=self.seed_first_fib)
            return
        self._geometry.on_bar(geo)

    # -- reading ---------------------------------------------------------

    @property
    def fibs(self) -> list[DrawnFib]:
        """Every drawn fib in MARKET terms.

        For a PE the geometry's fib0/fib1 are negated back. Because
        `DrawnFib.level_price` is fib0 - n*span and span = fib0 - fib1, a
        negated fib has a NEGATIVE span and its levels step UP -- which is
        exactly a put ladder. So one arithmetic serves both sides, and every
        consumer of these fibs (rungs, the convergence spaces, the chart)
        is correct without knowing which side it is on.
        """
        if self._geometry is None:
            return []
        if self._sign > 0:
            return list(self._geometry.fibs)
        return [replace(fib, fib0=self._out(fib.fib0), fib1=self._out(fib.fib1)) for fib in self._geometry.fibs]

    @property
    def raw_fibs(self) -> list[DrawnFib]:
        """The geometry's own fibs, un-mirrored. Diagnostics only."""
        return list(self._geometry.fibs) if self._geometry is not None else []

    @property
    def trendlines(self) -> list:
        return list(self._geometry.trendlines) if self._geometry is not None else []

    @property
    def mother_high(self) -> Optional[float]:
        if self._geometry is None:
            return None
        m = self._geometry.mother
        return max(self._out(m.high), self._out(m.low))

    @property
    def mother_low(self) -> Optional[float]:
        if self._geometry is None:
            return None
        m = self._geometry.mother
        return min(self._out(m.high), self._out(m.low))

    def level_price(self, fib: DrawnFib, level: int) -> float:
        """A fib level in MARKET terms. `fib` must come from `self.fibs`
        (already market terms), where a PE fib's negative span steps UP."""
        return float(fib.level_price(level))

    def market_fib(self, fib: DrawnFib) -> tuple[float, float]:
        """(fib0, fib1) of a `self.fibs` entry. For a PE fib0 is the LOWER price."""
        return float(fib.fib0), float(fib.fib1)

    def structures(self) -> dict[str, list[dict[str, Any]]]:
        """Everything a chart has to draw, in the renderer's own vocabulary.

        The chart used to redraw the geometry for itself out of
        `find_swing_anchor` -- one swing, one trendline -- which since the merge
        is not what the ladder trades at all. A stacked ladder drawn as a single
        fib is a chart that quietly lies: the levels on screen are not the
        levels holding money.

        Timestamps come back ISO; the route serializes and the client converts.
        A trendline's first anchor has only an INDEX on `Trendline`, so it is
        resolved against the bars this object numbered -- the same numbering the
        slope was measured in, which is why the resolution belongs here.
        """
        out: dict[str, list[dict[str, Any]]] = {"fibs": [], "trendlines": []}
        if self._geometry is None:
            return out
        for fib in self.fibs:
            out["fibs"].append(
                {
                    "fib_id": fib.fib_id,
                    "trendline_id": fib.trendline_id,
                    "fib0": round(float(fib.fib0), 2),
                    "fib1": round(float(fib.fib1), 2),
                    "span": round(abs(float(fib.span)), 2),
                    "touch_timestamp": fib.touch_timestamp.isoformat(),
                    "drawn_timestamp": fib.drawn_timestamp.isoformat(),
                    "levels": [
                        {"level": int(level), "price": round(self.level_price(fib, level), 2)} for level in self.levels
                    ],
                }
            )
        active = getattr(self._geometry, "active_trendline_id", None)
        for line in self._geometry.trendlines:
            first = self._bar_at(line.anchor1_index)
            if first is None:
                continue
            out["trendlines"].append(
                {
                    "id": line.trendline_id,
                    "a1": {"t": first.timestamp.isoformat(), "p": round(self._out(line.anchor1_price), 2)},
                    "a2": {
                        "t": line.anchor2_timestamp.isoformat(),
                        "p": round(self._out(line.anchor2_price), 2),
                    },
                    "active": line.trendline_id == active,
                }
            )
        return out

    def _bar_at(self, index: int) -> Optional[GeoBar]:
        return self._bars[index] if 0 <= index < len(self._bars) else None

    def all_levels(self) -> list[LadderLevel]:
        """Every level of every drawn fib, deepest LAST.

        Sorted by price descending so a walk down the list is a walk down the
        chart -- the order money is committed in, and the order the old
        single-fib ladder happened to have for free.
        """
        out: list[LadderLevel] = []
        for fib in self.fibs:
            for level in self.levels:
                out.append(
                    LadderLevel(
                        fib_id=fib.fib_id,
                        level=int(level),
                        price=self.level_price(fib, level),
                        drawn_at=fib.drawn_timestamp,
                    )
                )
        # Deepest LAST in the direction the ladder runs: down the chart for a
        # CE, UP the chart for a PE -- the order money is committed in either way.
        out.sort(key=lambda row: -row.price * self._sign)
        return out
