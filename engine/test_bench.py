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

    Phil's rule, per instrument: NIFTY buys the next weekly with **at least 10
    days** left, which in practice lands between 10 and 16 depending on the
    weekday it is bought on.  BankNifty is monthly-only, so "the next expiry" is
    up to a month and a half away and the campaign is given proportionally
    longer to reach its target before expiry ends it.
    """

    min_dte: int
    max_dte: int
    horizon_days: int


CONTRACT_WINDOWS: dict[str, ContractWindow] = {
    "NIFTY": ContractWindow(min_dte=10, max_dte=16, horizon_days=20),
    "BANKNIFTY": ContractWindow(min_dte=5, max_dte=45, horizon_days=35),
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
    "open": "Still open at the end of the window",
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
