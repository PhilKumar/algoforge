"""Transparent, configurable Indian index-option transaction costs.

Phase 1 does not book rounds yet, but the cost calculation is deliberately a
small pure module so Phase 2 cannot accidentally report gross-only P&L.
Broker pricing and statutory rates change, therefore callers must inject the
current schedule before a paper/live report is marked final.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class NiftyOptionCostSchedule:
    """Rates expressed in INR or a fraction of turnover, as named."""

    brokerage_per_order: float = 20.0
    brokerage_per_lot: float = 0.0
    sell_stt_rate: float = 0.000625
    exchange_transaction_rate: float = 0.00053
    sebi_rate: float = 0.000001
    stamp_buy_rate: float = 0.00003
    gst_rate: float = 0.18


@dataclass(frozen=True)
class OptionRoundCosts:
    buy_turnover: float
    sell_turnover: float
    brokerage: float
    stt: float
    exchange_transaction: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange_transaction + self.sebi + self.stamp + self.gst,
            2,
        )


@dataclass(frozen=True)
class OptionCostFill:
    """One executed option buy used to calculate a basket round's charges."""

    price: float
    quantity: int
    lots: int


def calculate_nifty_option_round_costs(
    *,
    buy_price: float,
    sell_price: float,
    quantity: int,
    lots_bought: int = 1,
    lots_sold: int = 1,
    schedule: NiftyOptionCostSchedule | None = None,
) -> OptionRoundCosts:
    """Return all charges for a completed long-option round.

    `quantity` is the actual number of option units, not lots.  Brokerage is
    modeled separately per executed order and per lot so a broker-specific
    tariff can be represented without hiding assumptions in the engine.
    """

    if buy_price < 0 or sell_price < 0 or quantity <= 0:
        raise ValueError("buy_price/sell_price must be non-negative and quantity must be positive")
    if lots_bought <= 0 or lots_sold <= 0:
        raise ValueError("lots_bought and lots_sold must be positive")
    rates = schedule or NiftyOptionCostSchedule()
    buy_turnover = float(buy_price) * int(quantity)
    sell_turnover = float(sell_price) * int(quantity)
    brokerage = rates.brokerage_per_order * 2 + rates.brokerage_per_lot * (lots_bought + lots_sold)
    exchange_transaction = (buy_turnover + sell_turnover) * rates.exchange_transaction_rate
    sebi = (buy_turnover + sell_turnover) * rates.sebi_rate
    stamp = buy_turnover * rates.stamp_buy_rate
    gst = (brokerage + exchange_transaction + sebi) * rates.gst_rate
    return OptionRoundCosts(
        buy_turnover=round(buy_turnover, 2),
        sell_turnover=round(sell_turnover, 2),
        brokerage=round(brokerage, 2),
        stt=round(sell_turnover * rates.sell_stt_rate, 2),
        exchange_transaction=round(exchange_transaction, 2),
        sebi=round(sebi, 2),
        stamp=round(stamp, 2),
        gst=round(gst, 2),
    )


def calculate_nifty_option_basket_round_costs(
    *,
    buys: Iterable[OptionCostFill],
    sell_price: float,
    sell_quantity: int,
    sell_lots: int,
    schedule: NiftyOptionCostSchedule | None = None,
) -> OptionRoundCosts:
    """Calculate a complete long-option basket with one or more buy orders.

    A Cascade can add to a CE basket more than once before an index target
    exits it.  Charging it as one synthetic average-price buy would understate
    brokerage and GST, so every actual paper fill is kept as its own order.
    """

    fill_rows = tuple(buys)
    if not fill_rows:
        raise ValueError("at least one buy fill is required")
    if sell_price < 0 or sell_quantity <= 0 or sell_lots <= 0:
        raise ValueError("sell_price must be non-negative and sell quantity/lots must be positive")
    if any(row.price < 0 or row.quantity <= 0 or row.lots <= 0 for row in fill_rows):
        raise ValueError("every buy fill needs non-negative price and positive quantity/lots")
    rates = schedule or NiftyOptionCostSchedule()
    buy_turnover = sum(row.price * row.quantity for row in fill_rows)
    sell_turnover = float(sell_price) * int(sell_quantity)
    brokerage = rates.brokerage_per_order * (len(fill_rows) + 1) + rates.brokerage_per_lot * (
        sum(row.lots for row in fill_rows) + sell_lots
    )
    exchange_transaction = (buy_turnover + sell_turnover) * rates.exchange_transaction_rate
    sebi = (buy_turnover + sell_turnover) * rates.sebi_rate
    stamp = buy_turnover * rates.stamp_buy_rate
    gst = (brokerage + exchange_transaction + sebi) * rates.gst_rate
    return OptionRoundCosts(
        buy_turnover=round(buy_turnover, 2),
        sell_turnover=round(sell_turnover, 2),
        brokerage=round(brokerage, 2),
        stt=round(sell_turnover * rates.sell_stt_rate, 2),
        exchange_transaction=round(exchange_transaction, 2),
        sebi=round(sebi, 2),
        stamp=round(stamp, 2),
        gst=round(gst, 2),
    )
