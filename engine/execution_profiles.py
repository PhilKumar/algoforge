"""What an instrument actually costs to trade, resolved on the server.

`execution_profile: "auto"` used to be a LABEL and nothing more. The numbers
behind it lived only in `EXECUTION_PROFILE_DEFAULTS` in philforge-app.js, were
written into the strategy by whatever the browser happened to hold at that
moment, and were then trusted forever -- `restoreExecutionSettings` loads the
stored basis points back over the ones the profile would have applied, so a
single bad moment ratchets in.

It happened. On 2026-08-31 all three running strategies were under-costed:
My_First_Run_PE and _CE said "auto" and carried 12/6/8 -- the Cash Equity
fallback row -- while both are instrument 26000, NIFTY, whose row is 18/10/14.
PE_NoTarget was "custom" with 0/0/0, so its paper book filled every trade at
the exact quoted price.

So "auto" now MEANS auto: the engine derives the numbers from the instrument
and ignores whatever the payload carries. A stored value can only win when the
profile is explicitly "custom". No data migration is needed -- the same
strategies resolve correctly the next time they are configured.

Keep this table and the one in philforge-app.js the same. The JS copy still
drives what the builder DISPLAYS; this copy decides what is charged.
"""

from __future__ import annotations

# instrument code -> the cost of trading it, in basis points of premium
EXECUTION_PROFILE_DEFAULTS: dict[str, dict] = {
    "26000": {
        "label": "NIFTY 50",
        "spread_bps": 18.0,
        "entry_slippage_bps": 10.0,
        "exit_slippage_bps": 14.0,
        "capital_buffer_pct": 5.0,
        "sell_option_margin_per_lot": 100000.0,
        "enforce_capital": True,
    },
    "26009": {
        "label": "BANK NIFTY",
        "spread_bps": 28.0,
        "entry_slippage_bps": 14.0,
        "exit_slippage_bps": 20.0,
        "capital_buffer_pct": 6.0,
        "sell_option_margin_per_lot": 150000.0,
        "enforce_capital": True,
    },
    "26017": {
        "label": "NIFTY FIN SVC",
        "spread_bps": 22.0,
        "entry_slippage_bps": 12.0,
        "exit_slippage_bps": 16.0,
        "capital_buffer_pct": 5.0,
        "sell_option_margin_per_lot": 85000.0,
        "enforce_capital": True,
    },
    "26037": {
        "label": "NIFTY MIDCAP 50",
        "spread_bps": 40.0,
        "entry_slippage_bps": 22.0,
        "exit_slippage_bps": 30.0,
        "capital_buffer_pct": 7.0,
        "sell_option_margin_per_lot": 80000.0,
        "enforce_capital": True,
    },
    "1": {
        "label": "SENSEX",
        "spread_bps": 34.0,
        "entry_slippage_bps": 18.0,
        "exit_slippage_bps": 24.0,
        "capital_buffer_pct": 6.0,
        "sell_option_margin_per_lot": 75000.0,
        "enforce_capital": True,
    },
}

DEFAULT_EXECUTION_PROFILE: dict = {
    "label": "Cash Equity",
    "spread_bps": 12.0,
    "entry_slippage_bps": 6.0,
    "exit_slippage_bps": 8.0,
    "capital_buffer_pct": 4.0,
    "sell_option_margin_per_lot": 0.0,
    "enforce_capital": True,
}

_NUMERIC_KEYS = (
    "spread_bps",
    "entry_slippage_bps",
    "exit_slippage_bps",
    "capital_buffer_pct",
    "sell_option_margin_per_lot",
)


def profile_for_instrument(instrument: object) -> dict:
    """The cost row for one instrument, falling back to cash equity."""
    return EXECUTION_PROFILE_DEFAULTS.get(str(instrument or "").strip(), DEFAULT_EXECUTION_PROFILE)


def _as_float(value: object, fallback: float) -> float:
    try:
        if value is None or value == "":
            return fallback
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return fallback


def resolve_execution_costs(strategy: dict | None) -> dict:
    """What this strategy is charged, and where each number came from.

    On "auto" the instrument decides and the payload is ignored, which is the
    whole point. On "custom" the payload decides, because that is what custom
    means -- including a deliberate zero.
    """
    strategy = strategy if isinstance(strategy, dict) else {}
    mode = str(strategy.get("execution_profile", "") or "").strip().lower() or "custom"
    row = profile_for_instrument(strategy.get("instrument"))

    if mode == "auto":
        resolved = {key: float(row[key]) for key in _NUMERIC_KEYS}
        resolved["enforce_capital"] = bool(row["enforce_capital"])
    else:
        resolved = {key: _as_float(strategy.get(key), 0.0) for key in _NUMERIC_KEYS}
        resolved["enforce_capital"] = bool(strategy.get("enforce_capital", False))

    resolved["execution_profile"] = mode
    resolved["profile_label"] = row["label"]
    return resolved
