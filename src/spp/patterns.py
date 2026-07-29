"""Candlestick pattern detection.

Deliberately free of NautilusTrader imports: the pattern rules are the part of
the strategy most worth testing in isolation, and keeping them dependency-free
means they can be exercised without spinning up an engine, and reused against
any other bar source.

Two patterns are recognised, and the distinction between them matters:

``inside bar``
    Range containment -- the child bar's entire high/low range sits within the
    mother bar's range. This is a pure volatility-compression signal and carries
    no directional bias of its own.

``harami``
    Body containment plus opposite direction -- the child's real body sits
    within the mother's real body and closes the other way. This one does carry
    a bias, conventionally read as exhaustion of the move the mother bar made.

Neither is a subset of the other. A harami can poke outside the mother bar's
wicks and still be a harami; an inside bar can close the same direction as its
mother and still be an inside bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Bias(Enum):
    """Directional lean of a detected pattern."""

    BULLISH = 1
    NEUTRAL = 0
    BEARISH = -1


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLC candle, in plain floats."""

    open: float
    high: float
    low: float
    close: float

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def span(self) -> float:
        """High-to-low range. Named ``span`` to avoid shadowing ``range``."""
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True, slots=True)
class PatternSignal:
    """A detected two-bar pattern."""

    kind: str  # "harami" | "inside_bar"
    bias: Bias
    mother: Candle
    child: Candle
    compression: float  # child span as a fraction of mother span

    @property
    def breakout_high(self) -> float:
        """Upside trigger reference: the higher of the two bars' highs."""
        return max(self.mother.high, self.child.high)

    @property
    def breakout_low(self) -> float:
        """Downside trigger reference: the lower of the two bars' lows."""
        return min(self.mother.low, self.child.low)


def is_inside_bar(mother: Candle, child: Candle, *, strict: bool = True) -> bool:
    """
    Return whether ``child`` is an inside bar relative to ``mother``.

    With ``strict`` the child's high and low must be strictly within the
    mother's; otherwise equal highs or lows still qualify. Strict is the
    stricter and less common reading, and is the default because equal highs on
    tick-quantised futures data are common enough to be noise.
    """
    if strict:
        return child.high < mother.high and child.low > mother.low
    return child.high <= mother.high and child.low >= mother.low


def harami_bias(
    mother: Candle,
    child: Candle,
    *,
    max_body_ratio: float = 0.5,
) -> Bias:
    """
    Classify ``child`` as a harami relative to ``mother``.

    Requires the child's real body to sit inside the mother's real body, to be
    no more than ``max_body_ratio`` of the mother's body, and to close opposite
    to the mother. Returns ``Bias.NEUTRAL`` when it is not a harami.

    A mother with no body (a doji) gives nothing to contain, so it never
    produces a signal.
    """
    if mother.body <= 0.0:
        return Bias.NEUTRAL
    if child.body > mother.body * max_body_ratio:
        return Bias.NEUTRAL
    if not (child.body_high <= mother.body_high and child.body_low >= mother.body_low):
        return Bias.NEUTRAL

    if mother.is_bearish and child.is_bullish:
        return Bias.BULLISH
    if mother.is_bullish and child.is_bearish:
        return Bias.BEARISH
    return Bias.NEUTRAL


def detect(
    mother: Candle,
    child: Candle,
    *,
    trade_harami: bool = True,
    trade_inside_bar: bool = True,
    max_body_ratio: float = 0.5,
    max_compression: float = 0.7,
    min_mother_span: float = 0.0,
    strict_inside: bool = True,
) -> PatternSignal | None:
    """
    Detect a tradeable two-bar pattern, or return ``None``.

    Harami is checked first: it is the more specific pattern, so when a bar pair
    qualifies as both, the harami reading and its directional bias win.

    ``min_mother_span`` rejects setups whose mother bar is too small to be worth
    trading -- callers normally pass a multiple of ATR so the threshold adapts
    to prevailing volatility. ``max_compression`` requires the child to actually
    be a compression rather than a near-equal-sized bar that happens to fit.
    """
    if mother.span <= 0.0 or mother.span < min_mother_span:
        return None

    compression = child.span / mother.span

    if trade_harami:
        bias = harami_bias(mother, child, max_body_ratio=max_body_ratio)
        if bias is not Bias.NEUTRAL and compression <= max_compression:
            return PatternSignal("harami", bias, mother, child, compression)

    if trade_inside_bar and is_inside_bar(mother, child, strict=strict_inside):
        if compression <= max_compression:
            return PatternSignal("inside_bar", Bias.NEUTRAL, mother, child, compression)

    return None
