"""Multi-timeframe harami / inside-bar breakout strategy for CME futures.

The idea in one paragraph: a harami or inside bar is a volatility compression --
the market pausing rather than choosing. On its own that is not tradeable in
either direction, which is why the higher timeframe does the choosing. The
context timeframe supplies a trend, the signal timeframe supplies a compressed
two-bar pattern within that trend, and the trade is a stop entry through the
pattern's extreme in the trend's direction, with the stop loss parked on the
other side of the pattern.

For a harami the reading is a little sharper than for an inside bar. A bullish
harami inside a higher-timeframe uptrend is a pullback running out of sellers,
so the pattern's own bias and the trend agree. That agreement is required by
default (``require_harami_bias_match``). An inside bar carries no bias, so it is
traded purely as continuation.

Every trade is a bracket: stop entry, protective stop, and a take profit at a
fixed multiple of the risk. Position size is derived from the stop distance so
that each trade risks approximately the same dollar amount regardless of how
wide the pattern is.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders.list import OrderList
from nautilus_trader.trading.strategy import Strategy

from spp.patterns import Bias
from spp.patterns import Candle
from spp.patterns import PatternSignal
from spp.patterns import detect

CHICAGO_EQUITY_TZ = "America/New_York"  # equity index RTH is quoted in ET


class HaramiInsideBarMTFConfig(StrategyConfig, frozen=True):
    """
    Configuration for :class:`HaramiInsideBarMTF`.

    Parameters
    ----------
    instrument_id : InstrumentId
        The futures contract to trade.
    signal_bar_type : BarType
        Lower timeframe on which patterns are detected (e.g. 5-minute).
    context_bar_type : BarType
        Higher timeframe supplying trend context (e.g. 60-minute). Normally a
        composite bar type aggregated from ``signal_bar_type`` so that live
        trading needs only one market data subscription.
    trade_harami, trade_inside_bar : bool
        Which patterns to act on.
    max_body_ratio : float
        Harami only -- child body as a fraction of mother body.
    max_compression : float
        Child high/low span as a fraction of the mother's. Lower is tighter.
    min_mother_span_atr : float
        Reject setups whose mother bar spans less than this multiple of ATR.
    require_harami_bias_match : bool
        Require a harami's own bias to agree with the higher-timeframe trend.
    fast_ema_period, slow_ema_period : int
        Trend definition on the context timeframe.
    min_trend_separation_atr : float
        Require the context EMAs to be at least this multiple of the context
        timeframe's ATR apart before calling a trend. Two moving averages are
        essentially never exactly equal, so without this the context timeframe
        only assigns a direction -- it never actually filters, and the strategy
        happily trades breakouts into a flat, chopping market. Raise it to trade
        only pronounced trends; set it to 0.0 to restore direction-only
        behaviour.
    atr_period : int
        ATR period on the signal timeframe.
    risk_per_trade : Decimal
        Account currency risked per trade, used to derive position size.
    max_contracts : int
        Hard cap on position size regardless of what risk sizing suggests.
    reward_risk : float
        Take profit distance as a multiple of the stop distance.
    entry_buffer_ticks, stop_buffer_ticks : int
        Ticks beyond the pattern extremes for the entry trigger and stop loss.
    entry_limit_slippage_ticks : int
        The entry is a stop-limit, so it needs a limit price beyond its trigger
        to have room to fill when the breakout is fast. Widen this to miss fewer
        breakouts, tighten it to cap the worst entry price accepted.
    entry_expiry_bars : int
        Cancel an untriggered entry after this many signal bars -- a compression
        that has not resolved within a few bars is no longer the same setup.
    session_start, session_end : str
        Regular trading hours in ``HH:MM`` US Eastern. Patterns outside this
        window are ignored; the strategy is flat overnight.
    flatten_before_close_mins : int
        Close any open position this many minutes before ``session_end``.
    """

    instrument_id: InstrumentId
    signal_bar_type: BarType
    context_bar_type: BarType

    trade_harami: bool = True
    trade_inside_bar: bool = True
    max_body_ratio: float = 0.5
    max_compression: float = 0.7
    min_mother_span_atr: float = 0.6
    require_harami_bias_match: bool = True

    fast_ema_period: int = 10
    slow_ema_period: int = 30
    min_trend_separation_atr: float = 0.25
    atr_period: int = 14

    risk_per_trade: Decimal = Decimal("250")
    max_contracts: int = 5
    reward_risk: float = 2.0
    entry_buffer_ticks: int = 1
    stop_buffer_ticks: int = 2
    entry_limit_slippage_ticks: int = 4
    entry_expiry_bars: int = 3

    session_start: str = "09:30"
    session_end: str = "16:00"
    flatten_before_close_mins: int = 15


class HaramiInsideBarMTF(Strategy):
    """Harami / inside-bar breakout, filtered by higher-timeframe trend."""

    def __init__(self, config: HaramiInsideBarMTFConfig) -> None:
        super().__init__(config)

        self.instrument: Instrument | None = None

        self.atr = AverageTrueRange(config.atr_period)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        # Separate ATR on the context timeframe: EMA separation has to be
        # judged against higher-timeframe volatility, not the signal bars'.
        self.context_atr = AverageTrueRange(config.atr_period)

        self._tz = ZoneInfo(CHICAGO_EQUITY_TZ)
        self._session_start = _parse_hhmm(config.session_start)
        self._session_end = _parse_hhmm(config.session_end)

        # Rolling two-bar window on the signal timeframe.
        self._prev_bar: Bar | None = None
        # Signal bars elapsed since the working entry order was submitted.
        self._bars_since_entry_submitted: int | None = None

        # Why setups did not become trades. The filters interact, so tuning
        # them blind is guesswork -- this shows which one is doing the work.
        self.stats: dict[str, int] = {
            "bars_in_session": 0,
            "skipped_position_open": 0,
            "patterns_detected": 0,
            "rejected_no_trend": 0,
            "rejected_bias_mismatch": 0,
            "rejected_size_zero": 0,
            "entries_submitted": 0,
            "entries_expired": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument {self.config.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(self.config.signal_bar_type, self.atr)
        self.register_indicator_for_bars(self.config.context_bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.context_bar_type, self.slow_ema)
        self.register_indicator_for_bars(self.config.context_bar_type, self.context_atr)

        # Subscribe to the context timeframe first. When it is a composite bar
        # type its aggregator consumes the signal bars, so it must exist before
        # those bars start flowing.
        self.subscribe_bars(self.config.context_bar_type)
        self.subscribe_bars(self.config.signal_bar_type)

        self.log.info(
            f"Started on {self.config.instrument_id}: "
            f"signal={self.config.signal_bar_type.spec}, "
            f"context={self.config.context_bar_type.spec}",
            color=LogColor.BLUE,
        )

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)
        summary = ", ".join(f"{k}={v}" for k, v in self.stats.items())
        self.log.info(f"Stopped. {summary}", color=LogColor.BLUE)

    def on_reset(self) -> None:
        self.atr.reset()
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.context_atr.reset()
        self._prev_bar = None
        self._bars_since_entry_submitted = None
        for key in self.stats:
            self.stats[key] = 0

    # ------------------------------------------------------------------
    # Data handling
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        # Composite bar types deliver bars stamped with the composite type, so
        # compare on the standard form to route reliably either way.
        standard = bar.bar_type.standard()
        if standard == self.config.context_bar_type.standard():
            return  # trend indicators are updated by the registration above
        if standard == self.config.signal_bar_type.standard():
            self._on_signal_bar(bar)

    def _on_signal_bar(self, bar: Bar) -> None:
        prev, self._prev_bar = self._prev_bar, bar

        self._expire_stale_entry()

        bar_time = _to_tz(bar.ts_event, self._tz)
        if self._past_flatten_time(bar_time):
            self._flatten("session close")
            return

        if not self._in_session(bar_time):
            return
        if not (self.atr.initialized and self.slow_ema.initialized):
            return
        if prev is None:
            return

        self.stats["bars_in_session"] += 1

        # One setup at a time: skip while a position or working order exists.
        if not self.portfolio.is_flat(self.config.instrument_id) or self.cache.orders_open(
            instrument_id=self.config.instrument_id,
        ):
            self.stats["skipped_position_open"] += 1
            return

        signal = detect(
            _to_candle(prev),
            _to_candle(bar),
            trade_harami=self.config.trade_harami,
            trade_inside_bar=self.config.trade_inside_bar,
            max_body_ratio=self.config.max_body_ratio,
            max_compression=self.config.max_compression,
            min_mother_span=self.config.min_mother_span_atr * self.atr.value,
        )
        if signal is None:
            return

        self.stats["patterns_detected"] += 1

        trend = self._context_trend()
        if trend is Bias.NEUTRAL:
            self.stats["rejected_no_trend"] += 1
            return
        if (
            signal.kind == "harami"
            and self.config.require_harami_bias_match
            and signal.bias is not trend
        ):
            self.stats["rejected_bias_mismatch"] += 1
            return

        self._enter(signal, trend)

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def _context_trend(self) -> Bias:
        """
        Higher-timeframe direction, or ``NEUTRAL`` when there is no clear trend.

        The separation threshold is what makes this a filter rather than just a
        coin flip: two EMAs are always on one side or the other of each other,
        so a bare comparison never returns NEUTRAL and never blocks anything.
        """
        if not (self.fast_ema.initialized and self.slow_ema.initialized):
            return Bias.NEUTRAL

        separation = self.fast_ema.value - self.slow_ema.value

        if self.config.min_trend_separation_atr > 0.0:
            if not self.context_atr.initialized:
                return Bias.NEUTRAL
            threshold = self.config.min_trend_separation_atr * self.context_atr.value
            if abs(separation) < threshold:
                return Bias.NEUTRAL

        if separation > 0:
            return Bias.BULLISH
        if separation < 0:
            return Bias.BEARISH
        return Bias.NEUTRAL

    def _enter(self, signal: PatternSignal, trend: Bias) -> None:
        assert self.instrument is not None
        tick = float(self.instrument.price_increment)
        long = trend is Bias.BULLISH

        if long:
            entry = signal.breakout_high + self.config.entry_buffer_ticks * tick
            stop = signal.breakout_low - self.config.stop_buffer_ticks * tick
            risk_points = entry - stop
            target = entry + self.config.reward_risk * risk_points
        else:
            entry = signal.breakout_low - self.config.entry_buffer_ticks * tick
            stop = signal.breakout_high + self.config.stop_buffer_ticks * tick
            risk_points = stop - entry
            target = entry - self.config.reward_risk * risk_points

        if risk_points <= 0:
            return

        quantity = self._size_position(risk_points)
        if quantity <= 0:
            self.stats["rejected_size_zero"] += 1
            self.log.debug(
                f"Skipping {signal.kind}: stop distance {risk_points:.2f} pts too wide "
                f"for {self.config.risk_per_trade} risk budget",
            )
            return

        # A breakout entry is stop semantics: trigger once price trades through
        # the pattern's extreme. The limit sits a few ticks the far side of the
        # trigger so a fast break still fills, while capping how bad a price we
        # will accept.
        slip = self.config.entry_limit_slippage_ticks * tick
        entry_limit = entry + slip if long else entry - slip

        order_list: OrderList = self.order_factory.bracket(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY if long else OrderSide.SELL,
            quantity=self.instrument.make_qty(quantity),
            entry_order_type=OrderType.STOP_LIMIT,
            entry_trigger_price=self.instrument.make_price(entry),
            entry_price=self.instrument.make_price(entry_limit),
            sl_trigger_price=self.instrument.make_price(stop),
            tp_price=self.instrument.make_price(target),
        )
        self.submit_order_list(order_list)

        self._bars_since_entry_submitted = 0
        self.stats["entries_submitted"] += 1
        self.log.info(
            f"{signal.kind} {'LONG' if long else 'SHORT'} x{quantity} "
            f"entry={entry:.2f} stop={stop:.2f} target={target:.2f} "
            f"(risk {risk_points:.2f} pts, compression {signal.compression:.2f})",
            color=LogColor.GREEN if long else LogColor.RED,
        )

    def _size_position(self, risk_points: float) -> int:
        """Contracts such that stop-out costs about ``risk_per_trade``."""
        assert self.instrument is not None
        dollars_at_risk_per_contract = risk_points * float(self.instrument.multiplier)
        if dollars_at_risk_per_contract <= 0:
            return 0
        raw = float(self.config.risk_per_trade) / dollars_at_risk_per_contract
        return max(0, min(int(raw), self.config.max_contracts))

    def _expire_stale_entry(self) -> None:
        """Cancel an entry that has not triggered within the allowed bars."""
        if self._bars_since_entry_submitted is None:
            return

        # Once filled, the bracket's stop and target manage the trade.
        if not self.portfolio.is_flat(self.config.instrument_id):
            self._bars_since_entry_submitted = None
            return

        self._bars_since_entry_submitted += 1
        if self._bars_since_entry_submitted > self.config.entry_expiry_bars:
            self.cancel_all_orders(self.config.instrument_id)
            self._bars_since_entry_submitted = None
            self.stats["entries_expired"] += 1
            self.log.debug("Entry expired: compression did not resolve")

    def _flatten(self, reason: str) -> None:
        has_orders = bool(self.cache.orders_open(instrument_id=self.config.instrument_id))
        is_flat = self.portfolio.is_flat(self.config.instrument_id)
        if is_flat and not has_orders:
            return
        self.log.info(f"Flattening: {reason}", color=LogColor.YELLOW)
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)
        self._bars_since_entry_submitted = None

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _in_session(self, moment: dt.datetime) -> bool:
        return self._session_start <= moment.time() < self._session_end

    def _past_flatten_time(self, moment: dt.datetime) -> bool:
        flatten_at = _minus_minutes(self._session_end, self.config.flatten_before_close_mins)
        return moment.time() >= flatten_at


def _parse_hhmm(value: str) -> dt.time:
    hours, minutes = value.split(":")
    return dt.time(int(hours), int(minutes))


def _minus_minutes(moment: dt.time, minutes: int) -> dt.time:
    anchor = dt.datetime.combine(dt.date(2000, 1, 1), moment) - dt.timedelta(minutes=minutes)
    return anchor.time()


def _to_tz(ts_event: int, tz: ZoneInfo) -> dt.datetime:
    """Convert a NautilusTrader UNIX nanosecond timestamp to a zoned datetime."""
    return dt.datetime.fromtimestamp(ts_event / 1e9, tz=dt.timezone.utc).astimezone(tz)


def _to_candle(bar: Bar) -> Candle:
    return Candle(
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
    )
