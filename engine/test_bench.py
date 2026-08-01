"""One mother candle, replayed in full — the Test Bench's answer shape.

Phil's brief, in his words: *"if I give the MC on a particular timeframe, it has
to get the candle and give the backtested results with entry time, exit time,
target hit or expiry hit, strike selected, CE/PE at what price bought, how much
spent for each level"* — plus the chart, with the geometry drawn on it.

So this module does two things and nothing else:

* :func:`bench_summary` turns a replay into the handful of facts that answer
  "what happened", flat enough to read without a legend.
* :func:`bench_chart` reshapes the same replay into the payload the ported
  Canvas renderer draws.

Neither recomputes anything.  Every number here was decided by the engine that
took the trades; if the chart and the table ever disagree, this file is the bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class ContractWindow:
    """How far out the contract sits, and how long the mother gets to work.

    Monthly contracts only (Phil, 2026-07-30), so both instruments share the
    measured 15-45 DTE window from the Oct 2024 - Jul 2026 sweep. NIFTY's old
    weekly 10-16 window survived here after the monthly switch and made most
    mothers unbuyable: a monthly expiry only falls inside a 7-day window for
    about one week per month, so the bench alerted "no monthly CE expiry in
    10-16 DTE" on dates the strategy itself trades happily.
    """

    min_dte: int
    max_dte: int
    horizon_days: int


CONTRACT_WINDOWS: dict[str, ContractWindow] = {
    # Horizons match the contract: a monthly bought 15-45 days out needs the
    # BankNifty-style 35-day replay to resolve instead of reading "Still OPEN".
    "NIFTY": ContractWindow(min_dte=15, max_dte=45, horizon_days=35),
    "BANKNIFTY": ContractWindow(min_dte=15, max_dte=45, horizon_days=35),
}

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


def _epoch(value: Optional[str]) -> Optional[int]:
    """ISO timestamp to epoch seconds, which is what the chart projects on."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return None


def outcome_label(exit_reason: Optional[str]) -> str:
    key = str(exit_reason or "").strip().lower()
    return _OUTCOMES.get(key, key.replace("_", " ").capitalize() or "No trade")


def bench_summary(backtest: dict, *, instrument: str, timeframe: str, mother_timestamp: str) -> dict:
    """The plain-language verdict on one mother candle.

    `spend_inr` is what left the account in premium, so it is a sum of the
    entries rather than anything the P&L line implies.  An entry Upstox could
    not price contributes nothing and is counted in `unpriced_entries` instead --
    a spend figure that quietly skipped a leg would be the worst kind of wrong.
    """

    entries = list(backtest.get("entries") or [])
    priced = [entry for entry in entries if entry.get("spend_inr") is not None]
    contract = backtest.get("contract") or {}
    # Nothing bought is a result, not a missing one, and it is the most common
    # one: most mothers simply never fall far enough to reach a buy level.  The
    # engine's own status word for that state ("armed", "awaiting a quote") reads
    # like a fault, so say what happened instead.
    outcome = "No buy — the index never reached a level" if not entries else outcome_label(backtest.get("exit_reason"))
    return {
        "instrument": instrument,
        "timeframe": timeframe,
        "mother_timestamp": mother_timestamp,
        "outcome": outcome,
        "exit_reason": backtest.get("exit_reason"),
        "entry_timestamp": entries[0]["timestamp"] if entries else None,
        "exit_timestamp": backtest.get("exit_timestamp"),
        "entry_count": len(entries),
        "unpriced_entries": len(entries) - len(priced),
        "spend_inr": round(sum(float(entry["spend_inr"]) for entry in priced), 2) if priced else 0.0,
        "net_pnl": backtest.get("net_pnl"),
        "costs_total": backtest.get("costs_total"),
        "fully_priced": bool(backtest.get("fully_priced")),
        "strike": contract.get("strike"),
        "option_type": contract.get("option_type"),
        "expiry": contract.get("expiry"),
        "lot_size": contract.get("lot_size"),
        "underlying": contract.get("underlying"),
        "target_index": backtest.get("target_index"),
        "average_spot": backtest.get("average_spot"),
        "data_gaps": list(backtest.get("data_gaps") or []),
    }


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


def _spend_by_rung(backtest: dict) -> dict[tuple[int, int], float]:
    """Premium spent, keyed by the (leg, fib level) the buy fired on."""
    totals: dict[tuple[int, int], float] = {}
    for entry in backtest.get("entries") or []:
        leg_id, level, spend = entry.get("leg_id"), entry.get("level"), entry.get("spend_inr")
        if leg_id is None or level is None or spend is None:
            continue
        key = (int(leg_id), int(level))
        totals[key] = round(totals.get(key, 0.0) + float(spend), 2)
    return totals


def bench_chart(geometry: dict, backtest: dict, *, timeframe: str) -> dict:
    """Reshape a replay into the Canvas renderer's payload.

    The renderer wants epoch seconds and nested anchor points; the engine's own
    journal is written in ISO strings and flat columns.  Translating here keeps
    the journal readable in a log and the chart cheap to draw -- the alternative
    is parsing dates inside a paint loop.
    """

    geometry = geometry or {}
    spend = _spend_by_rung(backtest or {})

    candles = [
        {
            "t": _epoch(row.get("t")),
            "o": row.get("o"),
            "h": row.get("h"),
            "l": row.get("l"),
            "c": row.get("c"),
            "is_mother": bool(row.get("is_mother")),
        }
        for row in geometry.get("candles") or []
    ]
    candles = [row for row in candles if row["t"] is not None]

    trendlines = []
    for index, line in enumerate(geometry.get("trendlines") or []):
        a1t, a2t = _epoch(line.get("a1t")), _epoch(line.get("a2t"))
        if a1t is None or a2t is None:
            continue
        trendlines.append(
            {
                "id": line.get("id"),
                "a1": {"t": a1t, "p": line.get("a1p")},
                "a2": {"t": a2t, "p": line.get("a2p")},
                # The last line drawn is the one the cascade was working when the
                # replay ended, which is what the renderer stars.
                "active": index == len(geometry.get("trendlines") or []) - 1,
            }
        )

    legs = []
    for leg in geometry.get("legs") or []:
        leg_id = leg.get("leg_id")
        levels = leg.get("levels") or {}
        orders = []
        for level in CHART_LEVELS:
            amount = spend.get((int(leg_id), level)) if leg_id is not None else None
            if amount:
                orders.append({"level": level, "inr_notional": amount})
        legs.append(
            {
                "leg_id": leg_id,
                "touch_timestamp": _epoch(leg.get("touch_t")),
                "touch_high": leg.get("touch_high"),
                "low": leg.get("low"),
                "levels": {str(level): levels.get(str(level)) for level in (0, 1, *CHART_LEVELS)},
                "orders": orders,
            }
        )

    entries = []
    exits = []
    for row in geometry.get("rounds") or []:
        for fill in row.get("fills") or []:
            entries.append({"t": _epoch(fill.get("timestamp")), "price": fill.get("index_price")})
        if row.get("closed_at") and row.get("exit_index_price") is not None:
            exits.append(
                {
                    "t": _epoch(row.get("closed_at")),
                    "price": row.get("exit_index_price"),
                    "pnl": row.get("net_pnl"),
                }
            )
    for fill in geometry.get("open_fills") or []:
        entries.append({"t": _epoch(fill.get("timestamp")), "price": fill.get("index_price")})

    mother = geometry.get("mother") or {}
    return {
        "timeframe": timeframe,
        "candles": candles,
        "mother": {"high": mother.get("high"), "low": mother.get("low")},
        "trendlines": trendlines,
        "legs": legs,
        "entries": [row for row in entries if row["t"] is not None],
        "exits": [row for row in exits if row["t"] is not None],
        "avg_entry_price": (backtest or {}).get("average_spot"),
        "tp_price": (backtest or {}).get("target_index"),
        # Name the target line for what actually happened.  A mother that never
        # got there was not "sold at" this price and must not be drawn as if it
        # were -- that is the one line on the chart that decides, at a glance,
        # whether the trade worked.
        "tp_label": (
            "TARGET HIT"
            if str((backtest or {}).get("exit_reason") or "").lower().startswith("target")
            else "TARGET (not reached)"
        ),
    }
