"""What the cascade needs to know about an index, answered by Dhan.

Every fact here used to be a constant somewhere: NIFTY's 65-unit lot, its
50-point strike ladder, the security id "13" that fetches its candles.  Constants
are wrong the moment an exchange changes a lot size, and wrong silently -- the
backtest still runs, still prints a rupee figure, and the figure is nonsense.

So nothing here is tabulated that Dhan can be asked instead:

* **Lot size** comes from the scrip master, per expiry, because it changes on
  effective dates and the scrip master already carries the dated truth.
* **The strike ladder** is measured from the strikes Dhan actually lists rather
  than assumed from the index name.
* **Weekly or monthly** is derived from the expiry dates themselves.  BankNifty
  being monthly is not a rule written down here; it is what its expiry list
  looks like.

The one thing that cannot be derived is which security id carries an index's
candles.  Those ids are recorded below, taken from the map the live market feed
already runs on -- not invented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional


class InstrumentError(ValueError):
    """The instrument cannot be described from the data available."""


@dataclass(frozen=True)
class IndexSpec:
    """How to reach one index's candles."""

    symbol: str
    security_id: str
    exchange_segment: str
    # Which Dhan F&O segment carries this index's OPTION contracts. The index
    # candles come from IDX_I for every one of them; the options do not --
    # SENSEX is a BSE contract. Asking Dhan for a BSE security id under
    # NSE_FNO does not error: it answers with EMPTY arrays, which read as "no
    # quote" and refused every SENSEX fill in a backtest of a still-listed
    # contract (Phil, 2026-08-19).
    option_segment: str = "NSE_FNO"
    verified: bool = True
    note: str = ""
    # Upstox's own name for the same index, used to pull expired-option premium
    # history.  Empty means nobody has run a priced backtest against it yet --
    # the candles work, the option prices do not, and `premium_key` refuses
    # rather than let a backtest quietly report a trade it never priced.
    premium_key: str = ""


def premium_key(symbol: str) -> str:
    """The Upstox instrument key for an index's option premiums, or a refusal."""
    spec = index_spec(symbol)
    if not spec.premium_key:
        priced = ", ".join(sorted(row.symbol for row in INDEX_SPECS.values() if row.premium_key))
        raise InstrumentError(
            f"{spec.symbol} option premiums are not wired to Upstox yet, so a backtest of it "
            f"would have no prices. Priced today: {priced}."
        )
    return spec.premium_key


# Every id below was confirmed on 2026-07-30 by fetching daily candles from Dhan
# and checking the level against what the index actually trades at.  That check
# mattered: the live feed's map reaches SENSEX through id "1", and asking the
# historical API for "1" returns a perfectly healthy series around 23,000 --
# some other index entirely, not SENSEX at 77,600.  It does not error.  Anything
# priced off it would have looked completely reasonable and been wrong, which is
# why `verified` exists and why nothing goes in here on inference.
#
#   NIFTY 13 -> 24,250   BANKNIFTY 25 -> 57,206   FINNIFTY 27 -> 26,287
#   MIDCPNIFTY 442 -> 14,683          SENSEX 51 -> 77,655
#
# The Upstox keys below carry the same caution.  NIFTY and BankNifty are the two
# that have actually returned expired-option premiums (tools/fib_cascade_sweep.py
# has run full backtests on both).  The other three are left blank on purpose:
# a plausible-looking key that Upstox does not recognise fails as "no data",
# which is indistinguishable from a quiet market and would silently turn every
# trade into an unpriced gap.
INDEX_SPECS: dict[str, IndexSpec] = {
    "NIFTY": IndexSpec("NIFTY", "13", "IDX_I", premium_key="NSE_INDEX|Nifty 50"),
    "BANKNIFTY": IndexSpec("BANKNIFTY", "25", "IDX_I", premium_key="NSE_INDEX|Nifty Bank"),
    "FINNIFTY": IndexSpec("FINNIFTY", "27", "IDX_I"),
    "MIDCPNIFTY": IndexSpec("MIDCPNIFTY", "442", "IDX_I"),
    # BSE index, but its candles come from IDX_I like the rest -- and from id 51,
    # NOT the "1" the live feed uses in its own id space. Its option premiums
    # come from Upstox under BSE_INDEX|SENSEX: 97 weekly expiries Oct-2024 ..
    # Aug-2026 in the expired archive, checked 2026-08-18 (a 77,500 CE on the
    # 13-Aug expiry priced 357.90 at 10:15 that day).
    "SENSEX": IndexSpec("SENSEX", "51", "IDX_I", option_segment="BSE_FNO", premium_key="BSE_INDEX|SENSEX"),
}


def option_segment(symbol: str) -> str:
    """The Dhan F&O segment an index's option contracts trade in.

    Unknown symbols get NSE_FNO, which is what every index except SENSEX uses;
    a wrong guess here shows up as "no candles", never as a wrong price.
    """
    try:
        return index_spec(symbol).option_segment
    except InstrumentError:
        return "NSE_FNO"


def index_spec(symbol: str) -> IndexSpec:
    """The candle source for an index, or a clear refusal."""
    key = str(symbol or "").strip().upper()
    spec = INDEX_SPECS.get(key)
    if spec is None:
        known = ", ".join(sorted(INDEX_SPECS))
        raise InstrumentError(f"Unknown index {key!r}. Known indices: {known}.")
    if not spec.verified:
        raise InstrumentError(f"{key} candles are not wired yet. {spec.note}")
    return spec


def expiry_rhythm(expiries: list[date] | list[str]) -> str:
    """ "weekly" or "monthly", read off the expiry dates rather than assumed.

    A weekly chain puts several expiries inside one month; a monthly chain puts
    exactly one.  Judged over the run of dates supplied, so a short or ragged
    list says "unknown" instead of guessing from the first two entries.
    """
    days = _as_dates(expiries)
    if len(days) < 3:
        return "unknown"
    months: dict[tuple[int, int], int] = {}
    for day in days:
        months[(day.year, day.month)] = months.get((day.year, day.month), 0) + 1
    # Ignore the first and last month buckets: a window that starts or ends
    # mid-month shows a partial count that means nothing.
    interior = sorted(months)[1:-1]
    if not interior:
        return "unknown"
    per_month = [months[key] for key in interior]
    return "monthly" if max(per_month) <= 1 else "weekly"


@dataclass(frozen=True)
class InstrumentFacts:
    """Everything the cascade needs, all of it sourced from Dhan."""

    symbol: str
    expiry: date
    lot_size: int
    strike_step: float
    rhythm: str
    security_id: str
    exchange_segment: str


def instrument_facts(symbol: str, expiry: date | str, scrip_master: Optional[Any] = None) -> InstrumentFacts:
    """Ask Dhan what this contract is, instead of looking it up in a table."""
    spec = index_spec(symbol)
    if scrip_master is None:
        from broker.dhan import ScripMaster  # lazy: the registry stays testable without Dhan

        scrip_master = ScripMaster

    expiry_day = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    expiry_text = expiry_day.isoformat()

    lot_size = int(scrip_master.get_lot_size(spec.symbol, expiry_text))
    if lot_size <= 0:
        raise InstrumentError(f"Dhan reports a non-positive lot size for {spec.symbol} {expiry_text}")

    try:
        strike_step = float(scrip_master.get_strike_step(spec.symbol, expiry_text))
    except Exception as exc:
        raise InstrumentError(f"Cannot determine the {spec.symbol} strike ladder: {exc}") from exc
    if strike_step <= 0:
        raise InstrumentError(f"Measured a non-positive strike step for {spec.symbol} {expiry_text}")

    return InstrumentFacts(
        symbol=spec.symbol,
        expiry=expiry_day,
        lot_size=lot_size,
        strike_step=strike_step,
        rhythm=expiry_rhythm(list(scrip_master.get_expiries(spec.symbol) or [])),
        security_id=spec.security_id,
        exchange_segment=spec.exchange_segment,
    )


def _as_dates(values: list[date] | list[str]) -> list[date]:
    days: list[date] = []
    for value in values or []:
        if isinstance(value, date):
            days.append(value)
            continue
        try:
            days.append(date.fromisoformat(str(value)[:10]))
        except ValueError:
            continue
    return sorted(set(days))
