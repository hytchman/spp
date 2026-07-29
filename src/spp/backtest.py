"""Runnable backtest for the harami / inside-bar multi-timeframe strategy.

    python -m spp.backtest --days 90 --seed 7

By default this runs against generated data (see ``spp.synthetic``), which
exercises the full order lifecycle but contains no edge. Point it at real bars
before believing any number it prints.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.engine import BacktestEngineConfig
from nautilus_trader.backtest.models import FixedFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.objects import Money

from spp.data import load_bars
from spp.instruments import GLBX
from spp.instruments import cme_equity_future
from spp.strategy import HaramiInsideBarMTF
from spp.strategy import HaramiInsideBarMTFConfig
from spp.synthetic import generate_bars


def build_engine(
    *,
    symbol: str,
    days: int,
    seed: int,
    starting_balance: int,
    risk_per_trade: Decimal,
    log_level: str,
    adaptive_bar_ordering: bool = True,
    drift_scale: float = 0.15,
    min_trend_separation_atr: float = 0.25,
    csv_path: str | None = None,
    csv_timezone: str | None = None,
    price_scale: float = 1.0,
) -> BacktestEngine:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="BACKTESTER-001",
            logging=LoggingConfig(log_level=log_level),
        ),
    )

    # Multipliers matter for sizing: ES is $50/point, MES $5/point. With a
    # fixed dollar risk budget that decides how wide a stop you can afford,
    # which in turn decides how many setups are affordable at all.
    instrument = cme_equity_future(symbol, 2026, 12)

    engine.add_venue(
        venue=GLBX,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(starting_balance, USD)],
        # ES notional is ~$250k/contract; roughly 5% initial margin.
        default_leverage=Decimal(20),
        # Round-turn commission, charged per fill.
        fee_model=FixedFeeModel(Money(2.25, USD)),
        # Without this, bars are always walked Open-High-Low-Close, so on any
        # bar that touches both the stop and the target, a long's target is
        # checked first and a short's stop is. That is a free win on every
        # ambiguous bar and it inflates results badly. The adaptive heuristic
        # infers a plausible path from the bar's shape instead. Neither is the
        # truth -- only tick data settles which came first -- but this one does
        # not systematically lie in your favour.
        bar_adaptive_high_low_ordering=adaptive_bar_ordering,
    )
    engine.add_instrument(instrument)

    signal_bar_type = BarType.from_str(f"{instrument.id}-5-MINUTE-LAST-EXTERNAL")
    # Aggregated from the signal bars, so live trading needs one subscription.
    context_bar_type = BarType.from_str(
        f"{instrument.id}-60-MINUTE-LAST-INTERNAL@5-MINUTE-EXTERNAL",
    )

    if csv_path is not None:
        bars, report = load_bars(
            csv_path,
            instrument,
            signal_bar_type,
            timezone=csv_timezone,
            price_scale=price_scale,
        )
        print("=== Data quality ===")
        print(report.render())
    else:
        bars = generate_bars(
            instrument,
            signal_bar_type,
            start=dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc),
            days=days,
            seed=seed,
            drift_scale=drift_scale,
        )
    engine.add_data(bars)

    engine.add_strategy(
        HaramiInsideBarMTF(
            config=HaramiInsideBarMTFConfig(
                instrument_id=instrument.id,
                signal_bar_type=signal_bar_type,
                context_bar_type=context_bar_type,
                risk_per_trade=risk_per_trade,
                min_trend_separation_atr=min_trend_separation_atr,
            ),
        ),
    )
    return engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ES", choices=["ES", "MES", "NQ", "MNQ"])
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--balance", type=int, default=100_000)
    parser.add_argument("--risk", type=Decimal, default=Decimal("250"))
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument(
        "--csv",
        help="Load real 5-minute bars from a CSV/Parquet file instead of generating "
             "them. This is the only mode whose results mean anything.",
    )
    parser.add_argument(
        "--csv-tz",
        help="IANA timezone of the file's timestamps, required when they are naive "
             "(e.g. 'UTC' or 'America/Chicago').",
    )
    parser.add_argument(
        "--price-scale",
        type=float,
        default=1.0,
        help="Multiplier for price columns; pass 1e-9 for Databento DBN fixed-point.",
    )
    parser.add_argument(
        "--optimistic-fills",
        action="store_true",
        help="Walk bars Open-High-Low-Close regardless of shape. Included only to "
             "show how much this assumption flatters results; do not trust it.",
    )
    parser.add_argument(
        "--trend-sep",
        type=float,
        default=0.25,
        help="Context EMA separation required to call a trend, in multiples of the "
             "context ATR. 0.0 disables the filter (direction only).",
    )
    parser.add_argument(
        "--no-drift",
        action="store_true",
        help="Generate a driftless random walk (no trending regimes). This is the "
             "null hypothesis: the strategy should NOT be profitable on it.",
    )
    args = parser.parse_args()

    engine = build_engine(
        symbol=args.symbol,
        days=args.days,
        seed=args.seed,
        starting_balance=args.balance,
        risk_per_trade=args.risk,
        log_level=args.log_level,
        adaptive_bar_ordering=not args.optimistic_fills,
        drift_scale=0.0 if args.no_drift else 0.15,
        min_trend_separation_atr=args.trend_sep,
        csv_path=args.csv,
        csv_timezone=args.csv_tz,
        price_scale=args.price_scale,
    )
    engine.run()

    strategy = engine.trader.strategies()[0]
    positions = engine.trader.generate_positions_report()

    print("\n=== Signal funnel ===")
    for key, value in strategy.stats.items():
        print(f"  {key:<28} {value}")

    print("\n=== Results ===")
    if positions.empty:
        print("  No positions taken.")
    else:
        pnl = positions["realized_pnl"].map(lambda money: float(str(money).split(" ")[0]))
        wins = (pnl > 0).sum()
        print(f"  Trades              {len(pnl)}")
        print(f"  Win rate            {wins / len(pnl):.1%}")
        print(f"  Net P&L             ${pnl.sum():,.2f}")
        print(f"  Average trade       ${pnl.mean():,.2f}")
        print(f"  Best / worst        ${pnl.max():,.2f} / ${pnl.min():,.2f}")

    account = engine.trader.generate_account_report(GLBX)
    if not account.empty:
        print(f"  Ending balance      {account['total'].iloc[-1]} USD")

    if args.csv is None:
        print(
            "\nNOTE: this is synthetic data. Unless --no-drift was passed it contains "
            "trending regimes by construction, which a breakout strategy will find -- "
            "so a profit here measures the generator, not the market. These numbers "
            "confirm the mechanics work, nothing more.",
        )
    engine.dispose()


if __name__ == "__main__":
    main()
