"""Replays, reshaped for a screen: the tables and charts a ladder reports as.

Written for the Test Bench, which was retired 2026-08-29 once every strategy
could replay its own rule from its own Backtest button. These three outlived it
because they were never really about the bench:

* :func:`ladder_result` and :func:`ladder_chart` are what Candle Entry's replay
  reports through.
* :func:`fib_boundary_chart` draws the Fib geometry over a replay.

Nothing here recomputes anything. Every number was decided by the engine that
took the trades; if the chart and the table ever disagree, this file is the bug.
"""

from __future__ import annotations

from typing import Optional

# Only the deep boundaries are drawn as buy lines.  0 and 1 are the two anchors
# the fib is measured between, and the renderer already draws those separately.
CHART_LEVELS: tuple[int, ...] = (2, 4, 8)

# How the engine's exit reasons read to someone who did not write the engine.
_OUTCOMES: dict[str, str] = {
    "target": "Target hit",
    "target_hit": "Target hit",
    "expiry": "Held to expiry",
    "expiry_squareoff": "Held to expiry",
    "time_stop": "Closed early on the time stop",
    "open": "Still OPEN — target not yet reached",
}


def outcome_label(exit_reason: Optional[str]) -> str:
    key = str(exit_reason or "").strip().lower()
    return _OUTCOMES.get(key, key.replace("_", " ").capitalize() or "No trade")


def ladder_result(ladder, *, instrument: str, timeframe: str, mother_timestamp: str, lot_size: int) -> dict:
    """A two-red ladder, in the same shape the fib strategy reports.

    One screen reads both strategies, so they have to answer the same questions
    in the same words -- "what did it buy, when, at what, and how did it end".
    """

    fills = list(ladder.fills)
    entries = [
        {
            "timestamp": fill.timestamp.isoformat(),
            "spot": fill.index_price,
            "option_price": fill.option_premium,
            "lots": fill.lots,
            "quantity": fill.quantity,
            # The rung's own chart is what a reader needs here, not a fib level.
            "level": fill.rung,
            "timeframe": fill.timeframe,
            "leg_id": fill.rung,
            "spend_inr": (
                round(float(fill.option_premium) * int(fill.quantity), 2) if fill.option_premium is not None else None
            ),
            "strike": fill.strike,
            "option_type": fill.option_type,
        }
        for fill in fills
    ]
    priced = [entry for entry in entries if entry["spend_inr"] is not None]
    outcome = (
        "No buy — the two-red setup never completed"
        if not fills
        else outcome_label(ladder.exit_reason or ("open" if ladder.status not in {"CLOSED", "EXPIRED"} else None))
    )
    return {
        "summary": {
            "instrument": instrument,
            "timeframe": timeframe,
            "mother_timestamp": mother_timestamp,
            "outcome": outcome,
            "exit_reason": ladder.exit_reason,
            "entry_timestamp": entries[0]["timestamp"] if entries else None,
            "exit_timestamp": ladder.exit_timestamp.isoformat() if ladder.exit_timestamp else None,
            "entry_count": len(entries),
            "unpriced_entries": len(entries) - len(priced),
            "spend_inr": round(sum(float(row["spend_inr"]) for row in priced), 2) if priced else 0.0,
            "net_pnl": ladder.net_pnl,
            "costs_total": round(ladder.costs.total, 2) if ladder.costs else 0.0,
            "fully_priced": bool(fills) and len(priced) == len(fills) and ladder.net_pnl is not None,
            "strike": entries[0]["strike"] if entries else None,
            "option_type": entries[0]["option_type"] if entries else None,
            "expiry": None,
            "lot_size": lot_size,
            "underlying": instrument,
            "target_index": ladder.target_index,
            "average_spot": ladder.average_entry,
            # A trade that has bought but not exited is OPEN, and the screen
            # must say so — a recent mother on a live contract ends its replay
            # at "now", not at any exit.
            "still_open": bool(fills) and ladder.exit_timestamp is None,
            "data_gaps": [
                f"missing premium for rung {row['level']} at {row['timestamp']}"
                for row in entries
                if row["spend_inr"] is None
            ],
        },
        "entries": entries,
    }


def fib_boundary_chart(config, result, candles: list, *, timeframe: str) -> dict:
    """Draw a typed-mother fib-boundary replay for the Canvas renderer.

    `bench_chart` cannot draw this one: it renders legs and trendlines the
    auto-geometry engine discovers, and a typed ladder has neither.  Its two
    boundaries are fixed lines measured straight off the mother the user named,
    so they go through `lines` -- solid and carrying what they cost once filled,
    dashed and faint while price never reached them.
    """

    spend_by_level: dict[int, float] = {}
    for entry in result.entries:
        if entry.option_price is None:
            continue
        level = int(entry.stage)
        spend_by_level[level] = round(spend_by_level.get(level, 0.0) + float(entry.option_price) * entry.quantity, 2)
    filled = {int(entry.stage) for entry in result.entries}
    lines = [
        {
            "price": config.boundary_price(level),
            "label": f"L{level}",
            "inr_notional": spend_by_level.get(int(level), 0.0),
            "filled": int(level) in filled,
        }
        for level in config.ordered_boundaries()
    ]
    return {
        "timeframe": timeframe,
        "candles": [
            {
                "t": int(row.timestamp.timestamp()),
                "o": row.open,
                "h": row.high,
                "l": row.low,
                "c": row.close,
                "is_mother": row.timestamp == config.mother_timestamp,
            }
            for row in candles
        ],
        "mother": {"high": config.mother_high, "low": config.mother_low},
        "trendlines": [],
        "legs": [],
        "lines": lines,
        "entries": [{"t": int(entry.timestamp.timestamp()), "price": entry.spot} for entry in result.entries],
        "exits": (
            [
                {
                    "t": int(result.exit_timestamp.timestamp()),
                    "price": result.target_index if result.exit_reason == "target" else result.average_spot,
                    "pnl": result.net_pnl or 0,
                }
            ]
            if result.exit_timestamp and result.entries
            else []
        ),
        "avg_entry_price": result.average_spot,
        "tp_price": result.target_index,
        # A mother squared off at expiry never sold at its target, and the line
        # must not claim it did.
        "tp_label": (
            "TARGET HIT"
            if result.exit_reason == "target"
            else "TARGET (open — watching)"
            if result.entries and result.exit_timestamp is None
            else "TARGET (not reached)"
        ),
    }


def ladder_chart(ladder, candles: list, *, timeframe: str) -> dict:
    """Draw the ladder on the chart Phil chose to read it on.

    Rungs are plain labelled price lines rather than fib levels -- an armed stop
    is dashed and faint, a filled one solid and carrying what it cost, so which
    rungs actually traded is readable without the table.
    """

    rows = [
        {
            "t": int(row.timestamp.timestamp()),
            "o": row.open,
            "h": row.high,
            "l": row.low,
            "c": row.close,
            "is_mother": row.timestamp == ladder.mother.timestamp and row.timeframe == ladder.mother.timeframe,
        }
        for row in candles
        if row.timeframe == timeframe
    ]
    lines = []
    for fill in ladder.fills:
        lines.append(
            {
                "price": fill.index_price,
                "label": f"BUY {fill.rung} · {fill.timeframe}",
                "inr_notional": (
                    round(float(fill.option_premium) * int(fill.quantity), 2) if fill.option_premium is not None else 0
                ),
                "filled": True,
            }
        )
    # A rung that armed but never filled is still worth seeing: it says the
    # setup was there and the recovery never came.
    for stage in ladder.stages:
        if stage.stop is not None:
            lines.append(
                {
                    "price": stage.stop,
                    "label": f"ARMED {stage.rung} · {stage.timeframe}",
                    "inr_notional": 0,
                    "filled": False,
                }
            )
    return {
        "timeframe": timeframe,
        "candles": rows,
        "mother": {"high": ladder.mother.high, "low": ladder.mother.low},
        "trendlines": [],
        "legs": [],
        "lines": lines,
        "entries": [{"t": int(fill.timestamp.timestamp()), "price": fill.index_price} for fill in ladder.fills],
        "exits": (
            [
                {
                    "t": int(ladder.exit_timestamp.timestamp()),
                    "price": ladder.exit_index_price,
                    "pnl": ladder.net_pnl or 0,
                }
            ]
            if ladder.exit_timestamp and ladder.exit_index_price is not None
            else []
        ),
        "avg_entry_price": ladder.average_entry,
        "tp_price": ladder.target_index,
        "tp_label": (
            "TARGET HIT"
            if ladder.exit_reason == "target"
            else "TARGET (open — watching)"
            if ladder.fills and ladder.exit_timestamp is None
            else "TARGET (not reached)"
        ),
    }
