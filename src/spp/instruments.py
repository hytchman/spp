"""CME futures contract definitions.

NautilusTrader ships ``TestInstrumentProvider.es_future``, but it sets the
contract multiplier to 1, which makes backtest P&L meaningless for position
sizing work. These builders use the real contract specifications so that a
one-point move is worth what it is actually worth.
"""

from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal

import pandas as pd

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

GLBX = Venue("GLBX")

# CME contract month codes.
MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}

# Specifications for the CME equity index futures this strategy targets.
# (tick size, multiplier / dollars per point, price precision)
SPECS = {
    "ES": (Decimal("0.25"), 50, 2),   # E-mini S&P 500,     $12.50 per tick
    "MES": (Decimal("0.25"), 5, 2),   # Micro E-mini S&P,   $1.25 per tick
    "NQ": (Decimal("0.25"), 20, 2),   # E-mini Nasdaq 100,  $5.00 per tick
    "MNQ": (Decimal("0.25"), 2, 2),   # Micro E-mini Nasdaq, $0.50 per tick
}


def third_friday(year: int, month: int) -> dt.date:
    """Return the third Friday of the given month -- CME equity index expiry."""
    fridays = [
        day
        for week in calendar.monthcalendar(year, month)
        if (day := week[calendar.FRIDAY]) != 0
    ]
    return dt.date(year, month, fridays[2])


def cme_equity_future(
    underlying: str,
    expiry_year: int,
    expiry_month: int,
    *,
    venue: Venue = GLBX,
) -> FuturesContract:
    """
    Build a CME equity index futures contract.

    Parameters
    ----------
    underlying : str
        One of the keys in ``SPECS`` (e.g. ``"ES"``, ``"MES"``).
    expiry_year, expiry_month : int
        Contract expiry. Equity index futures are quarterly (Mar/Jun/Sep/Dec)
        but no check is enforced, so off-cycle contracts can be modelled.
    """
    if underlying not in SPECS:
        raise ValueError(f"Unknown underlying {underlying!r}; expected one of {sorted(SPECS)}")

    tick, multiplier, precision = SPECS[underlying]
    raw_symbol = f"{underlying}{MONTH_CODES[expiry_month]}{expiry_year % 10}"

    # Contracts list roughly a year ahead; expiry is 09:30 ET on the third
    # Friday, when the settlement print is taken.
    expiration = pd.Timestamp(third_friday(expiry_year, expiry_month), tz="UTC") + pd.Timedelta(
        hours=13, minutes=30,
    )
    activation = expiration - pd.Timedelta(days=365)

    return FuturesContract(
        instrument_id=InstrumentId(symbol=Symbol(raw_symbol), venue=venue),
        raw_symbol=Symbol(raw_symbol),
        asset_class=AssetClass.INDEX,
        exchange="XCME",
        currency=USD,
        price_precision=precision,
        price_increment=Price(float(tick), precision),
        multiplier=Quantity.from_int(multiplier),
        lot_size=Quantity.from_int(1),
        underlying=underlying,
        activation_ns=activation.value,
        expiration_ns=expiration.value,
        ts_event=activation.value,
        ts_init=activation.value,
    )


def es_future(expiry_year: int, expiry_month: int) -> FuturesContract:
    """The E-mini S&P 500 -- $50 per index point."""
    return cme_equity_future("ES", expiry_year, expiry_month)


def mes_future(expiry_year: int, expiry_month: int) -> FuturesContract:
    """The Micro E-mini S&P 500 -- $5 per index point."""
    return cme_equity_future("MES", expiry_year, expiry_month)
