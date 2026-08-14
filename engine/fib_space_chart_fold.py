"""Folding a fib-space campaign chart onto a coarser timeframe.

The campaign chart is drawn from the campaign's OWN replay bars, so it cannot
re-fetch a different timeframe the way the swing-ladder chart does -- the whole
point of that payload is that the picture cannot disagree with the trade beside
it.  What it CAN do is fold those same bars into bigger ones, which is not a
second opinion about the market: a 1H bar built from four 15M bars of the same
replay is the same data, read further back.

Only the candles fold.  Trendlines, fib levels, entries and exits are already
expressed in epoch seconds and prices, and the renderer draws them by time and
price -- so they land in exactly the right place over a folded series without
being touched.  That is what makes this safe.
"""

from __future__ import annotations

from datetime import datetime

# Minutes per bar, for the timeframes a campaign chart may be folded onto.
_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": None}


def foldable_timeframes(base: str) -> list[str]:
    """The timeframes this base may be folded onto, coarsest last.

    A chart can only be folded UP.  Asking for 5m bars from a 15m replay would
    mean inventing prices, so those timeframes are simply not offered.
    """
    base = str(base or "").lower()
    if base not in _MINUTES:
        return [base] if base else []
    base_minutes = _MINUTES[base]
    if base_minutes is None:
        return [base]
    out = [base]
    for tf, minutes in _MINUTES.items():
        if tf == base:
            continue
        if minutes is None or (minutes > base_minutes and minutes % base_minutes == 0):
            out.append(tf)
    return sorted(out, key=lambda tf: (_MINUTES[tf] is None, _MINUTES[tf] or 0))


def _day_of(epoch_seconds: int) -> tuple:
    when = datetime.fromtimestamp(epoch_seconds)
    return (when.year, when.month, when.day)


def fold_campaign_chart(payload: dict, timeframe: str) -> dict:
    """`payload` redrawn on `timeframe`. The input is not mutated."""
    base = str(payload.get("timeframe") or "").lower()
    timeframe = str(timeframe or "").lower()
    if not timeframe or timeframe == base:
        return payload
    if timeframe not in foldable_timeframes(base) or timeframe == base:
        raise ValueError(f"a {base} campaign chart cannot be drawn on {timeframe}")

    # Bars are counted from each DAY'S OWN first bar, never off the wall clock.
    # NSE opens at 09:15, so clock-anchored hours would cut a 45-minute stub at
    # 10:00 and slide the boundary through the session -- the same reason
    # engine/two_red_equity.regroup_hours counts from the open. A short group at
    # the close is left short rather than borrowed from tomorrow.
    per_group = None if timeframe == "1d" else _MINUTES[timeframe] // _MINUTES[base]

    by_day: dict[tuple, list[dict]] = {}
    day_order: list[tuple] = []
    for candle in payload.get("candles") or []:
        day = _day_of(int(candle["t"]))
        if day not in by_day:
            by_day[day] = []
            day_order.append(day)
        by_day[day].append(candle)

    groups: list[list[dict]] = []
    for day in day_order:
        day_bars = sorted(by_day[day], key=lambda b: int(b["t"]))
        if per_group is None:
            groups.append(day_bars)
            continue
        for start in range(0, len(day_bars), per_group):
            groups.append(day_bars[start : start + per_group])

    folded = []
    for bars in groups:
        folded.append(
            {
                "t": bars[0]["t"],
                "o": bars[0]["o"],
                "h": max(b["h"] for b in bars),
                "l": min(b["l"] for b in bars),
                "c": bars[-1]["c"],
                # The mother is a bar of the ORIGINAL series. Whichever folded
                # bar swallowed it inherits the flag, so the chart still marks
                # where the campaign started instead of losing it in the fold.
                "is_mother": any(b.get("is_mother") for b in bars),
            }
        )

    out = dict(payload)
    out["candles"] = folded
    out["timeframe"] = timeframe
    out["base_timeframe"] = base
    return out
