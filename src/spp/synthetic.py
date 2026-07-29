"""Synthetic bar generation for smoke-testing the strategy wiring.

This exists so the backtest runs without a paid market data subscription. It
produces a plausible-looking random walk with trending regimes and an intraday
volatility curve, quantised to the contract's tick grid.

It is emphatically not a substitute for real data. A backtest here validates
that orders are placed, filled, and managed correctly. It cannot tell you
whether the strategy has an edge.

Worse, it can actively mislead: with ``drift_scale`` above zero the generator
creates persistent trending regimes, and a trend-following breakout strategy
duly finds them and prints a profit. That profit is circular -- it measures the
generator, not the market. Running with ``drift_scale=0.0`` removes the
momentum, and the same strategy drops to roughly a 30% win rate against a 2:1
target, which is a loser. Both numbers are facts about this file.

Swap in Databento or your broker's historical bars before drawing any
conclusion about performance.
"""

from __future__ import annotations

import datetime as dt
import random

from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity

_SUBSTEPS = 12  # intra-bar samples used to shape each bar's high/low


def generate_bars(
    instrument: Instrument,
    bar_type: BarType,
    *,
    start: dt.datetime,
    days: int,
    minutes_per_bar: int = 5,
    start_price: float = 5000.0,
    annual_vol: float = 0.16,
    drift_scale: float = 0.15,
    seed: int = 7,
) -> list[Bar]:
    """
    Generate ``days`` weekdays of bars for ``instrument``.

    Bars run continuously through each weekday; the strategy's own session
    filter decides which of them are tradeable.

    ``drift_scale`` controls the persistent-drift regimes. Anything above zero
    puts genuine momentum into the series, which a breakout strategy can then
    find -- so a profitable backtest here is partly circular. Set it to zero for
    a driftless random walk, which is the honest null hypothesis: a strategy
    that still prints a profit on that data is being flattered by fills, costs,
    or sizing rather than by signal.
    """
    rng = random.Random(seed)
    tick = float(instrument.price_increment)
    precision = instrument.price_precision

    bars_per_day = (24 * 60) // minutes_per_bar
    # Per-substep volatility, from an annualised figure.
    steps_per_year = 252 * bars_per_day * _SUBSTEPS
    sigma = annual_vol / (steps_per_year**0.5)

    price = start_price
    drift = 0.0
    bars: list[Bar] = []
    moment = start

    day = 0
    while day < days:
        if moment.weekday() >= 5:  # skip weekends
            moment += dt.timedelta(days=1)
            moment = moment.replace(hour=0, minute=0)
            continue

        for index in range(bars_per_day):
            # Slow-moving drift produces the trending regimes the higher
            # timeframe filter is meant to detect. It decays toward zero so
            # regimes fade rather than compounding the level away over months.
            drift *= 0.995
            if drift_scale > 0.0 and rng.random() < 0.01:
                drift = rng.gauss(0.0, sigma * drift_scale)

            # Volatility is highest around the US cash open and close.
            minute_of_day = index * minutes_per_bar
            vol_scale = _intraday_vol_scale(minute_of_day)

            samples = [price]
            for _ in range(_SUBSTEPS):
                shock = rng.gauss(drift, sigma * vol_scale)
                price *= 1.0 + shock
                samples.append(price)

            bar_open = _round_tick(samples[0], tick, precision)
            bar_close = _round_tick(samples[-1], tick, precision)
            bar_high = _round_tick(max(samples), tick, precision)
            bar_low = _round_tick(min(samples), tick, precision)

            # Guard against rounding collapsing the range inconsistently.
            bar_high = max(bar_high, bar_open, bar_close)
            bar_low = min(bar_low, bar_open, bar_close)

            close_time = moment + dt.timedelta(minutes=minute_of_day + minutes_per_bar)
            ts = int(close_time.timestamp() * 1e9)

            bars.append(
                Bar(
                    bar_type=bar_type,
                    open=instrument.make_price(bar_open),
                    high=instrument.make_price(bar_high),
                    low=instrument.make_price(bar_low),
                    close=instrument.make_price(bar_close),
                    volume=Quantity.from_int(int(400 * vol_scale) + rng.randint(0, 200)),
                    ts_event=ts,
                    ts_init=ts,
                ),
            )

        moment += dt.timedelta(days=1)
        moment = moment.replace(hour=0, minute=0)
        day += 1

    return bars


def _intraday_vol_scale(minute_of_day_utc: int) -> float:
    """Crude U-shape: busiest around the US cash session."""
    hour = (minute_of_day_utc // 60) % 24
    if 13 <= hour < 15:  # 09:00-11:00 ET -- the open
        return 1.8
    if 19 <= hour < 21:  # 15:00-17:00 ET -- the close
        return 1.5
    if 15 <= hour < 19:
        return 1.1
    return 0.5  # overnight


def _round_tick(value: float, tick: float, precision: int) -> float:
    return round(round(value / tick) * tick, precision)
