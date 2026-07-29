# spp — harami / inside-bar multi-timeframe strategy for CME futures

A [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) strategy that
trades two-bar compression patterns (harami and inside bars) on a lower timeframe,
filtered by trend on a higher one, on CME equity index futures.

Backtest and live execution run the same strategy code — that is the main reason
this is built on Nautilus rather than a backtest-only framework.

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m spp.backtest --symbol MES --days 90
.venv/bin/python -m pytest
```

## The idea

A harami or inside bar is a volatility compression: the market pausing rather than
choosing a direction. On its own that is not tradeable either way, so the higher
timeframe does the choosing.

- **Context (60-minute)** — a fast/slow EMA relationship sets the trend direction.
- **Signal (5-minute)** — a harami or inside bar marks a compression within it.
- **Entry** — a stop-limit through the pattern's extreme, in the trend direction.
- **Stop** — the far side of the two-bar structure, plus a tick buffer.
- **Target** — a fixed multiple of the stop distance (2R by default).

Position size is derived from the stop distance so every trade risks roughly the
same dollar amount regardless of how wide the pattern is.

The higher timeframe is a *composite* bar type aggregated from the signal bars
(`...-60-MINUTE-LAST-INTERNAL@5-MINUTE-EXTERNAL`), so live trading needs only one
market data subscription and cannot drift out of sync between timeframes.

### Harami vs inside bar

These are different patterns and neither contains the other, which is why
`patterns.py` treats them separately:

| | containment | direction |
|---|---|---|
| **Inside bar** | high/low **range** inside the mother's | none — pure compression |
| **Harami** | real **body** inside the mother's body | closes against the mother |

A harami's wick can poke outside the mother bar's range and still be a harami. An
inside bar can close the same way as its mother and still be an inside bar. So a
harami carries a directional read and an inside bar does not — the strategy
requires a harami's bias to agree with the higher-timeframe trend
(`require_harami_bias_match`), and trades inside bars purely as continuation.

## Layout

| File | What it does |
|---|---|
| `src/spp/patterns.py` | Pattern rules. No Nautilus imports, so it is testable alone. |
| `src/spp/strategy.py` | The strategy: MTF routing, session handling, brackets, sizing. |
| `src/spp/instruments.py` | CME contract definitions with correct multipliers. |
| `src/spp/synthetic.py` | Generated bars, so the backtest runs without paid data. |
| `src/spp/data.py` | Loading and validating real CSV/Parquet bars. |
| `src/spp/backtest.py` | Runnable backtest with a signal funnel and results summary. |

## What the backtests actually showed

**These runs used synthetic data. None of them is evidence of an edge.** They are
worth reading anyway, because each one exposed something that would have silently
distorted a real backtest.

### Bar fill ordering is worth thousands of dollars of fake profit

Nautilus fills from bars, so when a single bar touches both your stop and your
target, something has to decide which came first. The default walks every bar
Open → High → Low → Close, which means a long's target is *always* checked before
its stop. That is a free win on every ambiguous bar:

| seed | adaptive ordering | fixed O-H-L-C |
|---:|---:|---:|
| 1 | $3,757 | $5,380 |
| 2 | $8,553 | $11,807 |
| 3 | $9,952 | $13,117 |
| 7 | $11,820 | $18,396 |
| 11 | $184 | $2,004 |

`bar_adaptive_high_low_ordering=True` is the default here. Neither setting is the
truth — only tick data settles the question — but the adaptive heuristic does not
systematically lie in your favour. If your strategy's results depend on this flag,
you do not have a result.

### A generator with momentum in it will hand momentum profits back

The synthetic series has persistent drift regimes. A trend-following breakout
strategy finds them and prints a profit, which measures the generator rather than
the market. Turning drift off (`--no-drift`) leaves a driftless random walk:

| seed | trending regimes | driftless walk |
|---:|---:|---:|
| 1 | $3,757 (41.0% win) | −$4,462 (31.6% win) |
| 2 | $8,553 (49.7%) | −$3,716 (32.1%) |
| 3 | $9,952 (45.1%) | −$4,422 (30.2%) |
| 7 | $11,820 (47.9%) | $1,766 (38.3%) |
| 11 | $184 (36.4%) | $1,033 (37.4%) |

The driftless win rate lands around 30–32%. A 2:1 target needs better than 33.3%
just to break even before costs, so that is a losing system — which is exactly
what should happen on data with no edge in it. That the mechanics reproduce the
theoretical number is the real result of these runs.

### Contract size decides which setups you can even take

With a $250 risk budget, ES at $50/point affords a 5-point stop; MES at $5/point
affords 50 points. Over 90 days the strategy detected ~750 patterns on ES and had
to reject **428 of them** as unaffordable — it was not trading its strategy, it was
trading whichever setups happened to have tight stops. On MES, zero were rejected.

Run `--symbol ES` and watch `rejected_size_zero` in the funnel. If it is large,
either your risk budget is too small for the contract or you should be on micros.

### A bare EMA cross is not a filter

Two moving averages are never exactly equal, so comparing them only ever assigns
a direction — `rejected_no_trend` was 0 in every early run. The strategy was
happily breaking out into flat, chopping markets and calling it a trend.

`min_trend_separation_atr` (default 0.25) requires the context EMAs to be at
least that multiple of the hourly ATR apart before a trend is called. Set it to
0.0 for the old direction-only behaviour:

| `--trend-sep` | rejected | trades | win rate | trending data | driftless data |
|---:|---:|---:|---:|---:|---:|
| 0.0 | 0 | 165 | 47.9% | $11,820 | $1,766 |
| 0.25 | 32 | 159 | 47.8% | $11,556 | $727 |
| 0.5 | 83 | 145 | 48.3% | $10,701 | −$572 |
| 1.0 | 200 | 124 | 48.4% | $8,691 | $113 |

Worth reading honestly: tightening the filter barely moves the win rate. It cuts
exposure rather than improving trade quality, and total P&L falls roughly in
proportion to the trade count. On this data it is not earning its keep. Whether
it does on real data is exactly the sort of thing you cannot learn from a
generator.

## Running on real data

```bash
python -m spp.backtest --symbol MES --csv bars.csv --csv-tz America/Chicago
```

The loader takes CSV, gzipped CSV, or Parquet, normalises common column spellings
(Databento, IB, TradingView), and prints a data quality report before running:

```
=== Data quality ===
  rows                 25920
  range                2026-01-05 00:05:00+00:00  ->  2026-05-09 00:00:00+00:00
  duplicate timestamps 0
  out of order         0
  invalid OHLC         0
  off-tick prices      0
  largest gap          2 days 00:05:00
```

**Naive timestamps are refused rather than guessed.** If a file's timestamps carry
no offset, you must pass `--csv-tz`. This is deliberate: the strategy's session
filter is in US Eastern, so a file written in exchange local time but read as UTC
shifts every bar and silently trades the wrong hours of the day. That produces a
backtest that looks fine and means nothing. Files with offsets or epoch integers
need no flag.

Other checks worth knowing about: rows where the high is below the open cannot
physically exist and are fatal by default (`strict=False` drops them instead —
Nautilus rejects such a bar at construction either way, so the real choice is
stopping versus skipping). Off-tick prices — anything not on MES's quarter-point
grid — are reported but never block, since some vendors publish adjusted prices.
On a futures contract they usually mean the file is not what you think it is.

For Databento DBN fixed-point prices (integers scaled by 1e9), pass
`--price-scale 1e-9`.

## The signal funnel

Every run prints where setups died, because the filters interact and tuning them
blind is guesswork:

```
=== Signal funnel ===
  bars_in_session              6675
  skipped_position_open         232   already in a trade
  patterns_detected             758
  rejected_no_trend               0   EMAs exactly equal (essentially never)
  rejected_bias_mismatch        283   harami disagreed with the trend
  rejected_size_zero            428   stop too wide for the risk budget
  entries_submitted              47
  entries_expired                21   compression never resolved
```

## Before trading this with real money

1. **Get real data.** [Databento](https://databento.com) has CME historical bars;
   export 5-minute OHLCV and run with `--csv`. Everything above is synthetic.
2. **Re-run the fill-ordering comparison on that data.** If the result depends on
   the flag, the result is not real.
3. **Walk it forward.** Fit parameters on one period, test on a later one you have
   not looked at. Tuning the eleven config knobs against a single sample will
   produce a beautiful curve that means nothing.
4. **Check the funnel.** A strategy rejecting more than half its setups on sizing
   is not the strategy you designed.
5. **Paper trade for weeks.** Nautilus runs the same code live via the
   Interactive Brokers adapter, so this step costs you nothing but time.
6. **Then size small.** CME futures are leveraged; one ES point is $50 and the
   contract does not care what your backtest said.

Nothing here is a prediction that this strategy makes money. It is a correct,
tested implementation of a specific idea, plus the instrumentation to find out.
