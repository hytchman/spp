# Futures Trading Bots — Research Notes

Survey of open-source futures trading bots and frameworks, July 2026.

"Futures" splits into two largely non-overlapping worlds, and the right tool
depends entirely on which one you mean:

- **Crypto perpetual futures** (Binance, Bybit, OKX, Hyperliquid) — retail-accessible,
  API keys in minutes, most of the open-source ecosystem lives here.
- **Traditional listed futures** (CME: ES, NQ, CL, GC) — requires a real futures
  broker, market data subscriptions, and meaningfully more capital.

## Crypto perpetual futures

### Freqtrade — best default choice
- <https://github.com/freqtrade/freqtrade> · ~52.7k stars · Python 3.11+
- Futures/perps on Binance, Bitget, Bybit, Gate, Hyperliquid, Kraken, OKX
- Backtesting, hyperparameter optimization, dry-run paper mode, Telegram and web UI
- Large community, very actively maintained

Most complete package for someone writing their own strategy. The dry-run mode is
the important feature — it lets you run live market data against a fake balance.

### Hummingbot — market making / arbitrage
- <https://github.com/hummingbot/hummingbot> · ~19.3k stars · Apache 2.0
- Python + Cython, 50+ exchange connectors, perpetual connectors included
- Built around liquidity provision rather than directional strategies

Right tool if the goal is capturing spread rather than predicting direction. Wrong
tool if you want trend-following or signal-based entries.

### Passivbot — grid/DCA, high risk
- <https://github.com/enarjord/passivbot> · ~2k stars · Unlicense (public domain)
- Python + Rust, runs on Bybit, Binance, OKX, Bitget, GateIO, KuCoin, Hyperliquid
- Includes an optimizer that fits parameters to historical data

**Risk note:** the strategy is explicitly contrarian martingale — it doubles down on
losing positions to pull the average entry closer to price. On leveraged perpetual
futures this produces long stretches of small gains punctuated by liquidation-scale
losses. Backtests look excellent right up until the trend that does not revert.
This is the single most common way people lose an account with an automated bot.

## Traditional listed futures (CME etc.)

### NautilusTrader — the serious option
- <https://github.com/nautechsystems/nautilus_trader> · ~25.1k stars · LGPL-3.0
- Rust core, Python strategy layer, event-driven
- Interactive Brokers integration (stable), Databento for market data
- Models contract activation/expiry, lot size, multiplier — real futures semantics
- Identical strategy code runs in backtest and live, which eliminates a whole class
  of "worked in backtest, broke in production" bugs

### QuantConnect LEAN
- <https://github.com/QuantConnect/Lean> · ~20.9k stars · Apache 2.0 · C# and Python
- Multi-asset including futures, backtest-to-live parity
- Runs locally, but the ergonomics assume QuantConnect's cloud platform and data

## The honest caveat

None of these ship a profitable strategy. Every one is an *execution framework*: it
handles order routing, position tracking, reconnects, and backtesting. The strategy
is yours to write, and that is the part that determines whether you make money.

Any bot advertised as profitable out of the box is either selling something or
overfit to history. Treat a good backtest as a hypothesis, not a result.

## Suggested path

1. Pick the world — crypto perps or CME. They share almost no tooling.
2. Crypto: start with Freqtrade. CME: start with NautilusTrader.
3. Run in dry-run/paper mode against live data for weeks, not hours.
4. Only then go live, with capital you can afford to lose entirely, and with
   position sizing and a hard stop decided before the first order.
