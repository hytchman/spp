# spp

## Position Size Box (`position-size-box.pine`)

TradingView indicator (Pine v6) that shows, in a box pinned to the chart, the
contract size for the next trade — updated live from ATR(14, RMA).

**Install:** TradingView → Pine Editor → paste the file → Add to chart.

### Logic

- `budget = min(target × consistency%, stop)` — the stop wins when it's tighter.
- Point value is read from the symbol (`syminfo.pointvalue`, MNQ = $2/pt).
- `N trades = ceil(budget / (ATR × pointValue × maxSize))` — 1 trade when a full-size
  1 ATR move covers the budget, 2 when it covers at least half, 3 for a third, etc.
- `size = floor((budget / N) / (ATR × pointValue))`, always rounded down, capped at max size.
- **Don't be a dick for a tick** = 4/5 of the per-trade target (e.g. 1500 target,
  1 trade → 1200).

### Examples (MNQ, max 30, target $1500)

| ATR | Full-size 1 ATR | Trades | Size |
|-----|-----------------|--------|------|
| 50  | $3000           | 1      | 15   |
| 24.9| $1494           | 2      | 15   |

### Inputs

Max size, stop ($), target ($), consistency (%), ATR length, ATR timeframe
(chart or fixed), box position, and display toggles.
