"""Tests for loading real market data.

Weighted toward the defects that do not announce themselves: a timezone assumed
rather than known, an OHLC row that cannot physically exist, prices that do not
sit on the contract's tick grid.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from nautilus_trader.model.data import BarType

from spp.data import check_quality
from spp.data import load_bars
from spp.data import read_ohlcv
from spp.instruments import cme_equity_future


@pytest.fixture(scope="module")
def instrument():
    return cme_equity_future("MES", 2026, 12)


@pytest.fixture(scope="module")
def bar_type(instrument):
    return BarType.from_str(f"{instrument.id}-5-MINUTE-LAST-EXTERNAL")


def write_csv(path, rows, header="ts_event,open,high,low,close,volume"):
    path.write_text(header + "\n" + "\n".join(rows) + "\n")
    return path


GOOD_ROWS = [
    "2026-01-05T14:30:00+00:00,5000.00,5002.50,4999.00,5001.25,500",
    "2026-01-05T14:35:00+00:00,5001.25,5004.00,5000.75,5003.50,610",
    "2026-01-05T14:40:00+00:00,5003.50,5005.00,5001.00,5002.00,480",
]


class TestTimezoneHandling:
    def test_naive_timestamps_without_timezone_are_refused(self, tmp_path):
        """
        The single most damaging silent failure: naive timestamps assumed to be
        UTC when they are exchange local time shift every bar and invalidate the
        session filter.
        """
        path = write_csv(
            tmp_path / "naive.csv",
            ["2026-01-05 08:30:00,5000.00,5002.50,4999.00,5001.25,500"],
        )
        with pytest.raises(ValueError, match="naive timestamps"):
            read_ohlcv(path)

    def test_naive_timestamps_with_explicit_timezone_convert(self, tmp_path):
        path = write_csv(
            tmp_path / "naive.csv",
            ["2026-01-05 08:30:00,5000.00,5002.50,4999.00,5001.25,500"],
        )
        frame = read_ohlcv(path, timezone="America/Chicago")
        # 08:30 Chicago in January (CST, UTC-6) is 14:30 UTC.
        assert frame.index[0] == pd.Timestamp("2026-01-05 14:30:00", tz="UTC")

    def test_aware_timestamps_need_no_timezone(self, tmp_path):
        path = write_csv(tmp_path / "aware.csv", GOOD_ROWS)
        frame = read_ohlcv(path)
        assert frame.index[0] == pd.Timestamp("2026-01-05 14:30:00", tz="UTC")

    def test_offset_timestamps_are_converted_not_stripped(self, tmp_path):
        path = write_csv(
            tmp_path / "offset.csv",
            ["2026-01-05T09:30:00-05:00,5000.00,5002.50,4999.00,5001.25,500"],
        )
        frame = read_ohlcv(path)
        assert frame.index[0] == pd.Timestamp("2026-01-05 14:30:00", tz="UTC")

    def test_epoch_nanoseconds_inferred(self, tmp_path):
        ts = int(dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        path = write_csv(
            tmp_path / "epoch.csv",
            [f"{ts},5000.00,5002.50,4999.00,5001.25,500"],
        )
        frame = read_ohlcv(path)
        assert frame.index[0] == pd.Timestamp("2026-01-05 14:30:00", tz="UTC")

    def test_epoch_seconds_inferred(self, tmp_path):
        ts = int(dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc).timestamp())
        path = write_csv(
            tmp_path / "epoch_s.csv",
            [f"{ts},5000.00,5002.50,4999.00,5001.25,500"],
        )
        frame = read_ohlcv(path)
        assert frame.index[0] == pd.Timestamp("2026-01-05 14:30:00", tz="UTC")


class TestColumnHandling:
    def test_alias_columns_normalised(self, tmp_path):
        path = write_csv(
            tmp_path / "alias.csv",
            ["2026-01-05T14:30:00+00:00,5000.00,5002.50,4999.00,5001.25,500"],
            header="datetime,o,h,l,c,v",
        )
        frame = read_ohlcv(path)
        assert list(frame.columns) == ["open", "high", "low", "close", "volume"]

    def test_missing_price_column_raises(self, tmp_path):
        path = write_csv(
            tmp_path / "bad.csv",
            ["2026-01-05T14:30:00+00:00,5000.00,5002.50,4999.00"],
            header="ts_event,open,high,low",
        )
        with pytest.raises(ValueError, match="Missing required column"):
            read_ohlcv(path)

    def test_missing_timestamp_column_raises(self, tmp_path):
        path = write_csv(
            tmp_path / "bad.csv",
            ["5000.00,5002.50,4999.00,5001.25"],
            header="open,high,low,close",
        )
        with pytest.raises(ValueError, match="No timestamp column"):
            read_ohlcv(path)

    def test_price_scale_applied(self, tmp_path):
        """Databento DBN fixed-point prices are integers scaled by 1e9."""
        path = write_csv(
            tmp_path / "dbn.csv",
            ["2026-01-05T14:30:00+00:00,5000000000000,5002500000000,4999000000000,5001250000000,500"],
        )
        frame = read_ohlcv(path, price_scale=1e-9)
        assert frame["open"].iloc[0] == pytest.approx(5000.0)
        assert frame["high"].iloc[0] == pytest.approx(5002.5)

    def test_rows_are_sorted(self, tmp_path):
        path = write_csv(tmp_path / "unsorted.csv", list(reversed(GOOD_ROWS)))
        frame = read_ohlcv(path)
        assert frame.index.is_monotonic_increasing


class TestQualityChecks:
    def test_clean_data_passes(self, tmp_path):
        frame = read_ohlcv(write_csv(tmp_path / "good.csv", GOOD_ROWS))
        report = check_quality(frame, tick_size=0.25)
        assert report.is_clean
        assert report.rows == 3

    def test_impossible_ohlc_detected(self, tmp_path):
        """A high below the open cannot happen; it means the file is wrong."""
        frame = read_ohlcv(
            write_csv(
                tmp_path / "bad.csv",
                ["2026-01-05T14:30:00+00:00,5000.00,4998.00,4997.00,4997.50,500"],
            ),
        )
        report = check_quality(frame)
        assert report.invalid_ohlc == 1
        assert not report.is_clean

    def test_duplicate_timestamps_detected(self, tmp_path):
        frame = read_ohlcv(write_csv(tmp_path / "dupe.csv", [GOOD_ROWS[0], GOOD_ROWS[0]]))
        report = check_quality(frame)
        assert report.duplicate_timestamps == 1

    def test_off_tick_prices_detected(self, tmp_path):
        """MES trades in quarter points; 5000.10 is not a real price."""
        frame = read_ohlcv(
            write_csv(
                tmp_path / "offtick.csv",
                ["2026-01-05T14:30:00+00:00,5000.10,5002.50,4999.00,5001.25,500"],
            ),
        )
        assert check_quality(frame, tick_size=0.25).off_tick_prices == 1
        # Without a tick size there is nothing to check against.
        assert check_quality(frame).off_tick_prices == 0

    def test_largest_gap_reported(self, tmp_path):
        rows = [
            GOOD_ROWS[0],
            "2026-01-06T14:30:00+00:00,5001.25,5004.00,5000.75,5003.50,610",
        ]
        report = check_quality(read_ohlcv(write_csv(tmp_path / "gap.csv", rows)))
        assert report.largest_gap == pd.Timedelta(days=1)


class TestLoadBars:
    def test_produces_nautilus_bars(self, tmp_path, instrument, bar_type):
        bars, report = load_bars(
            write_csv(tmp_path / "good.csv", GOOD_ROWS), instrument, bar_type,
        )
        assert len(bars) == 3
        assert report.is_clean
        assert float(bars[0].open) == 5000.00
        assert float(bars[0].high) == 5002.50

    def test_strict_mode_rejects_broken_ohlc(self, tmp_path, instrument, bar_type):
        path = write_csv(
            tmp_path / "bad.csv",
            ["2026-01-05T14:30:00+00:00,5000.00,4998.00,4997.00,4997.50,500"],
        )
        with pytest.raises(ValueError, match="failed validation"):
            load_bars(path, instrument, bar_type)

    def test_non_strict_mode_drops_impossible_rows(self, tmp_path, instrument, bar_type):
        """
        Impossible OHLC cannot be loaded either way -- NautilusTrader rejects
        such a bar at construction -- so non-strict drops the row and keeps the
        rest, rather than pretending to accept it.
        """
        path = write_csv(
            tmp_path / "bad.csv",
            [
                "2026-01-05T14:30:00+00:00,5000.00,4998.00,4997.00,4997.50,500",
                *GOOD_ROWS[1:],
            ],
        )
        bars, report = load_bars(path, instrument, bar_type, strict=False)
        assert len(bars) == 2  # the two good rows survive
        assert report.invalid_ohlc == 1  # the report still describes what was found

    def test_non_strict_mode_drops_duplicates(self, tmp_path, instrument, bar_type):
        path = write_csv(tmp_path / "dupe.csv", [GOOD_ROWS[0], GOOD_ROWS[0], GOOD_ROWS[1]])
        bars, report = load_bars(path, instrument, bar_type, strict=False)
        assert len(bars) == 2
        assert report.duplicate_timestamps == 1

    def test_off_tick_warns_but_does_not_block(self, tmp_path, instrument, bar_type):
        """Off-tick prices are reported, not fatal -- some vendors adjust prices."""
        path = write_csv(
            tmp_path / "offtick.csv",
            ["2026-01-05T14:30:00+00:00,5000.10,5002.50,4999.00,5001.25,500"],
        )
        bars, report = load_bars(path, instrument, bar_type)
        assert len(bars) == 1
        assert report.off_tick_prices > 0
