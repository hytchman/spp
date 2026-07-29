"""Loading real OHLCV bars from CSV or Parquet.

Replacing ``spp.synthetic`` with real data is the step that turns this from a
mechanism test into an actual experiment. Most of this module is validation,
because bad market data does not announce itself -- it quietly produces a
plausible equity curve built on nonsense.

The failure worth guarding hardest against is timezones. A CSV of CME bars with
naive timestamps is ambiguous, and if the exporter wrote exchange local time
while this code assumes UTC, every bar lands in the wrong place. The strategy's
session filter is in US Eastern, so a silent shift means it trades the wrong
hours of the day and the backtest is worthless. ``load_bars`` therefore refuses
to guess: naive timestamps require an explicit ``timezone``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.persistence.wranglers import BarDataWrangler

REQUIRED_COLUMNS = ("open", "high", "low", "close")

# Column aliases seen in the wild: Databento, Interactive Brokers exports,
# TradingView, and generic broker CSVs.
COLUMN_ALIASES = {
    "ts_event": "timestamp",
    "ts_recv": "timestamp",
    "date": "timestamp",
    "datetime": "timestamp",
    "time": "timestamp",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "vol": "volume",
}


@dataclass
class DataQualityReport:
    """What was found when checking a loaded bar set."""

    rows: int
    first: pd.Timestamp | None
    last: pd.Timestamp | None
    duplicate_timestamps: int
    out_of_order: int
    invalid_ohlc: int
    off_tick_prices: int
    largest_gap: pd.Timedelta | None

    @property
    def is_clean(self) -> bool:
        return (
            self.duplicate_timestamps == 0
            and self.out_of_order == 0
            and self.invalid_ohlc == 0
            and self.off_tick_prices == 0
        )

    def render(self) -> str:
        lines = [
            f"  rows                 {self.rows}",
            f"  range                {self.first}  ->  {self.last}",
            f"  duplicate timestamps {self.duplicate_timestamps}",
            f"  out of order         {self.out_of_order}",
            f"  invalid OHLC         {self.invalid_ohlc}",
            f"  off-tick prices      {self.off_tick_prices}",
            f"  largest gap          {self.largest_gap}",
        ]
        if not self.is_clean:
            lines.append("  STATUS               PROBLEMS FOUND -- do not trust results")
        return "\n".join(lines)


def read_ohlcv(
    path: str | Path,
    *,
    timezone: str | None = None,
    price_scale: float = 1.0,
) -> pd.DataFrame:
    """
    Read an OHLCV file into a normalised, UTC-indexed frame.

    Parameters
    ----------
    path : str or Path
        A ``.csv``, ``.csv.gz``, or ``.parquet`` file.
    timezone : str, optional
        IANA zone the timestamps are expressed in. Required when the file's
        timestamps carry no offset -- see the module docstring for why this is
        not guessed. Ignored when timestamps are already timezone-aware.
    price_scale : float
        Multiplier applied to price columns. Databento's DBN fixed-point prices
        are integers scaled by 1e9, so pass ``1e-9`` for those; CSV exports are
        usually already decimal and need the default of 1.0.
    """
    path = Path(path)
    if path.suffix == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path)

    frame = frame.rename(columns={c: c.strip().lower() for c in frame.columns})
    frame = frame.rename(columns={k: v for k, v in COLUMN_ALIASES.items() if k in frame.columns})

    if "timestamp" not in frame.columns:
        raise ValueError(
            f"No timestamp column in {path.name}. Columns present: {list(frame.columns)}",
        )
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"Missing required column(s) {missing} in {path.name}")

    stamps = _parse_timestamps(frame["timestamp"], timezone=timezone, source=path.name)
    frame = frame.drop(columns=["timestamp"]).set_index(stamps)
    frame.index.name = "timestamp"

    for column in REQUIRED_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce") * price_scale
    if "volume" in frame.columns:
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)

    keep = [*REQUIRED_COLUMNS] + (["volume"] if "volume" in frame.columns else [])
    frame = frame[keep].dropna(subset=list(REQUIRED_COLUMNS))
    return frame.sort_index()


def _parse_timestamps(values: pd.Series, *, timezone: str | None, source: str) -> pd.DatetimeIndex:
    # Integer epochs are unambiguous; the unit is inferred from magnitude.
    if pd.api.types.is_numeric_dtype(values):
        magnitude = float(values.iloc[0])
        unit = "ns" if magnitude > 1e17 else "us" if magnitude > 1e14 else "ms" if magnitude > 1e11 else "s"
        return pd.DatetimeIndex(pd.to_datetime(values, unit=unit, utc=True))

    parsed = pd.DatetimeIndex(pd.to_datetime(values, format="mixed", utc=False))
    if parsed.tz is not None:
        return parsed.tz_convert("UTC")

    if timezone is None:
        raise ValueError(
            f"{source} has naive timestamps and no timezone was given. Pass the IANA "
            f"zone the file is written in (e.g. 'UTC', or 'America/Chicago' for CME "
            f"local time). Guessing here would silently shift every bar and quietly "
            f"invalidate the session filter.",
        )
    # ``nonexistent``/``ambiguous`` handling matters on DST boundaries: shift
    # forward through the spring gap and take the first pass in the autumn fold.
    localised = parsed.tz_localize(timezone, nonexistent="shift_forward", ambiguous=True)
    return localised.tz_convert("UTC")


def check_quality(frame: pd.DataFrame, *, tick_size: float | None = None) -> DataQualityReport:
    """Inspect a normalised frame for the defects that quietly ruin backtests."""
    index = frame.index
    duplicates = int(index.duplicated().sum())
    out_of_order = int((index.to_series().diff().dropna() < pd.Timedelta(0)).sum())

    highs, lows = frame["high"], frame["low"]
    opens, closes = frame["open"], frame["close"]
    invalid = int(
        (
            (highs < lows)
            | (highs < opens)
            | (highs < closes)
            | (lows > opens)
            | (lows > closes)
        ).sum(),
    )

    off_tick = 0
    if tick_size:
        for column in REQUIRED_COLUMNS:
            remainder = (frame[column] / tick_size).round() * tick_size - frame[column]
            off_tick += int((remainder.abs() > tick_size / 100).sum())

    gaps = index.to_series().diff().dropna()
    return DataQualityReport(
        rows=len(frame),
        first=index[0] if len(frame) else None,
        last=index[-1] if len(frame) else None,
        duplicate_timestamps=duplicates,
        out_of_order=out_of_order,
        invalid_ohlc=invalid,
        off_tick_prices=off_tick,
        largest_gap=gaps.max() if len(gaps) else None,
    )


def load_bars(
    path: str | Path,
    instrument: Instrument,
    bar_type: BarType,
    *,
    timezone: str | None = None,
    price_scale: float = 1.0,
    strict: bool = True,
) -> tuple[list[Bar], DataQualityReport]:
    """
    Load a file into NautilusTrader ``Bar`` objects, with a quality report.

    With ``strict`` (the default), duplicate timestamps and impossible OHLC rows
    raise, so a bad file fails loudly instead of producing a plausible curve.
    Without it those rows are dropped and the run continues -- they cannot be
    loaded either way, because NautilusTrader rejects a bar whose high is below
    its open at construction. The choice is between stopping and skipping, not
    between rejecting and accepting.

    Off-tick prices never block loading: some vendors publish averaged or
    adjusted prices that legitimately sit between ticks. They are still
    reported, because on a futures contract they usually mean the file is not
    what you think it is.

    The returned report always describes the data *as found*, including rows
    that were subsequently dropped.
    """
    frame = read_ohlcv(path, timezone=timezone, price_scale=price_scale)
    report = check_quality(frame, tick_size=float(instrument.price_increment))

    problems = []
    if report.duplicate_timestamps:
        problems.append(f"{report.duplicate_timestamps} duplicate timestamps")
    if report.invalid_ohlc:
        problems.append(f"{report.invalid_ohlc} rows where OHLC is inconsistent")

    if problems and strict:
        raise ValueError(
            f"{Path(path).name} failed validation: {'; '.join(problems)}. "
            f"Fix the data, or pass strict=False to drop the offending rows.",
        )

    if problems:
        frame = _drop_unusable(frame)

    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)
    return wrangler.process(frame), report


def _drop_unusable(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove duplicate timestamps and physically impossible OHLC rows."""
    frame = frame[~frame.index.duplicated(keep="first")]
    highs, lows, opens, closes = (
        frame["high"], frame["low"], frame["open"], frame["close"],
    )
    consistent = (
        (highs >= lows)
        & (highs >= opens)
        & (highs >= closes)
        & (lows <= opens)
        & (lows <= closes)
    )
    return frame[consistent]
