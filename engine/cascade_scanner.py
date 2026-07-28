"""Rank Nifty 200 scrips by how tradeable a Cascade on them would be today.

Phil picks the instrument; everything after that runs by itself. So this list
is the one human decision in the loop, and it has to be honest about why a name
is near the top.

Three things decide the ranking, in this order:

* **Quality gate.** Only names that are actually performing get considered. The
  Cascade buys a dip expecting a recovery toward the mother high, which is a bet
  on the trend still being intact. A falling knife satisfies every geometric
  rule right up until it does not stop falling.
* **Discount.** Among those, the deeper the pullback from the recent high, the
  better the entry -- but only up to a point. Past a certain depth a "pullback"
  is just a broken trend wearing the same shape, so beyond `max_pullback_pct`
  a name is dropped rather than ranked highest.
* **Fundability.** A setup you cannot afford is not a setup. With small capital
  and a three-hundred-rupee share, most Cascade rungs cannot buy even one share,
  and the campaign sits accumulating carry instead of averaging down. This is
  measured, not assumed, and it is reported per name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

# What each fib level is allowed to draw from a leg's pool. Mirrors the cash
# engine's own split, so fundability here means fundability there.
LEVEL_ALLOCATION = (0.20, 0.30, 0.50)

# Sessions the recent high is measured over. Shared with the chart endpoint
# so the line drawn is the number the ranking was computed from.
HIGH_LOOKBACK = 20


@dataclass(frozen=True)
class ScanInput:
    """One scrip's recent history, as the scanner needs it."""

    symbol: str
    name: str
    closes: Sequence[float]  # oldest to newest
    highs: Sequence[float]
    last_price: float
    high_timestamp: Optional[datetime] = None


@dataclass(frozen=True)
class ScanCandidate:
    symbol: str
    name: str
    last_price: float
    strength_pct: float  # trend performance over the lookback
    pullback_pct: float  # how far below the recent high it sits now
    recent_high: float
    affordable_shares: int
    rungs_fundable: int  # of 3 fib levels, how many can buy >= 1 share
    score: float

    @property
    def tradeable(self) -> bool:
        return self.affordable_shares >= 1 and self.rungs_fundable >= 1


@dataclass(frozen=True)
class ScanRejection:
    symbol: str
    reason: str


def _pct(new: float, old: float) -> float:
    return ((new - old) / old * 100.0) if old else 0.0


def rungs_fundable(capital_inr: float, price: float) -> int:
    """How many of the three fib rungs could buy at least one share.

    The Cascade splits a leg's pool 20/30/50 across its levels. If the smallest
    slice cannot reach one share, that rung never fires on its own -- it only
    contributes carry toward a later one.
    """
    if capital_inr <= 0 or price <= 0:
        return 0
    return sum(1 for share in LEVEL_ALLOCATION if capital_inr * share >= price)


def scan(
    rows: Iterable[ScanInput],
    *,
    capital_inr: float,
    min_price: float = 200.0,
    lookback: int = 60,
    high_lookback: int = HIGH_LOOKBACK,
    min_strength_pct: float = 0.0,
    min_pullback_pct: float = 1.0,
    max_pullback_pct: float = 25.0,
    limit: int = 30,
) -> tuple[list[ScanCandidate], list[ScanRejection]]:
    """Return the ranked shortlist plus why everything else was dropped.

    Rejections are returned rather than silently filtered so the screen can
    explain an empty list, which is otherwise indistinguishable from a bug.
    """
    candidates: list[ScanCandidate] = []
    rejected: list[ScanRejection] = []

    for row in rows:
        price = float(row.last_price or 0)
        if price < min_price:
            rejected.append(ScanRejection(row.symbol, f"price Rs {price:,.2f} below Rs {min_price:,.0f}"))
            continue
        if len(row.closes) < lookback or len(row.highs) < high_lookback:
            rejected.append(ScanRejection(row.symbol, "not enough history to judge the trend"))
            continue

        strength = _pct(float(row.closes[-1]), float(row.closes[-lookback]))
        if strength < min_strength_pct:
            rejected.append(ScanRejection(row.symbol, f"trend is {strength:.1f}% over {lookback} sessions"))
            continue

        recent_high = max(float(value) for value in row.highs[-high_lookback:])
        pullback = _pct(recent_high, price)  # positive when price sits below the high
        if pullback < min_pullback_pct:
            rejected.append(ScanRejection(row.symbol, f"only {pullback:.1f}% off its high; no discount yet"))
            continue
        if pullback > max_pullback_pct:
            # Past this depth it is not a pullback in an uptrend any more.
            rejected.append(ScanRejection(row.symbol, f"{pullback:.1f}% off its high; trend likely broken"))
            continue

        affordable = int(capital_inr // price) if price > 0 else 0
        fundable = rungs_fundable(capital_inr, price)
        if affordable < 1:
            rejected.append(
                ScanRejection(row.symbol, f"Rs {capital_inr:,.0f} does not buy one share at Rs {price:,.2f}")
            )
            continue

        # Depth is what makes a Cascade worth taking; strength is the tiebreak
        # that keeps a merely-cheap name below a strong one at the same depth.
        # Fundability scales the whole thing, because a setup that can only ever
        # fire one rung is not the strategy, it is a single buy.
        score = round(pullback * (1 + strength / 100.0) * (fundable / len(LEVEL_ALLOCATION)), 3)

        candidates.append(
            ScanCandidate(
                symbol=row.symbol,
                name=row.name,
                last_price=round(price, 2),
                strength_pct=round(strength, 2),
                pullback_pct=round(pullback, 2),
                recent_high=round(recent_high, 2),
                affordable_shares=affordable,
                rungs_fundable=fundable,
                score=score,
            )
        )

    candidates.sort(key=lambda row: (-row.score, -row.pullback_pct, row.symbol))
    return candidates[:limit], rejected
