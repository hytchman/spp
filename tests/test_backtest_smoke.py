"""End-to-end checks that the strategy actually trades inside an engine.

The pattern tests cover the rules; these cover the wiring -- that bars reach the
strategy, that the higher timeframe aggregates, that brackets get submitted and
resolved, and that risk sizing responds to the contract multiplier.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from spp.backtest import build_engine
from spp.instruments import cme_equity_future
from spp.instruments import third_friday
from spp.strategy import _minus_minutes
from spp.strategy import _parse_hhmm


@pytest.fixture(scope="module")
def finished_engine():
    engine = build_engine(
        symbol="MES",
        days=30,
        seed=3,
        starting_balance=100_000,
        risk_per_trade=Decimal("250"),
        log_level="ERROR",
    )
    engine.run()
    yield engine
    engine.dispose()


class TestEngineRun:
    def test_strategy_sees_bars_and_finds_patterns(self, finished_engine):
        stats = finished_engine.trader.strategies()[0].stats
        assert stats["bars_in_session"] > 0
        assert stats["patterns_detected"] > 0

    def test_trades_are_executed(self, finished_engine):
        positions = finished_engine.trader.generate_positions_report()
        assert not positions.empty

    def test_all_positions_close(self, finished_engine):
        """Nothing should be left open: the strategy flattens on stop."""
        positions = finished_engine.trader.generate_positions_report()
        assert (positions["side"] == "FLAT").all()

    def test_higher_timeframe_aggregated(self, finished_engine):
        """The composite bar type must actually produce context bars."""
        strategy = finished_engine.trader.strategies()[0]
        assert strategy.slow_ema.initialized

    def test_no_trades_survive_outside_session(self, finished_engine):
        """
        Every position opens inside the configured session. The strategy is a
        day strategy; an overnight position would mean the session filter leaks.
        """
        positions = finished_engine.trader.generate_positions_report()
        opened = positions["ts_opened"].dt.tz_convert("America/New_York")
        assert (opened.dt.hour >= 9).all()
        assert (opened.dt.hour < 16).all()


class TestRiskSizing:
    @pytest.mark.parametrize(
        ("symbol", "expected_multiplier"),
        [("ES", 50), ("MES", 5), ("NQ", 20), ("MNQ", 2)],
    )
    def test_contract_multipliers(self, symbol, expected_multiplier):
        instrument = cme_equity_future(symbol, 2026, 12)
        assert int(instrument.multiplier) == expected_multiplier

    def test_micro_allows_more_setups_than_full_size(self):
        """
        A $250 risk budget buys a 5-point stop on ES but a 50-point stop on MES,
        so the full-size contract has to reject far more setups as unaffordable.
        """
        results = {}
        for symbol in ("ES", "MES"):
            engine = build_engine(
                symbol=symbol,
                days=30,
                seed=3,
                starting_balance=100_000,
                risk_per_trade=Decimal("250"),
                log_level="ERROR",
            )
            engine.run()
            results[symbol] = dict(engine.trader.strategies()[0].stats)
            engine.dispose()

        assert results["ES"]["rejected_size_zero"] > results["MES"]["rejected_size_zero"]
        assert results["MES"]["entries_submitted"] > results["ES"]["entries_submitted"]


class TestInstruments:
    def test_third_friday(self):
        assert third_friday(2026, 12) == dt.date(2026, 12, 18)
        assert third_friday(2026, 3) == dt.date(2026, 3, 20)

    def test_symbol_encoding(self):
        assert cme_equity_future("ES", 2026, 12).id.symbol.value == "ESZ6"
        assert cme_equity_future("MES", 2026, 3).id.symbol.value == "MESH6"

    def test_unknown_underlying_rejected(self):
        with pytest.raises(ValueError, match="Unknown underlying"):
            cme_equity_future("ZZ", 2026, 12)

    def test_tick_size(self):
        assert float(cme_equity_future("ES", 2026, 12).price_increment) == 0.25


class TestSessionHelpers:
    def test_parse_hhmm(self):
        assert _parse_hhmm("09:30") == dt.time(9, 30)
        assert _parse_hhmm("16:00") == dt.time(16, 0)

    def test_flatten_time_subtraction(self):
        assert _minus_minutes(dt.time(16, 0), 15) == dt.time(15, 45)
        assert _minus_minutes(dt.time(0, 10), 20) == dt.time(23, 50)
