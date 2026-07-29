"""Tests for the two-bar pattern rules.

These are the rules the whole strategy rests on, and they are easy to get
subtly wrong -- particularly the distinction between range containment (inside
bar) and body containment (harami).
"""

from __future__ import annotations

import pytest

from spp.patterns import Bias
from spp.patterns import Candle
from spp.patterns import detect
from spp.patterns import harami_bias
from spp.patterns import is_inside_bar


def candle(open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(open=open_, high=high, low=low, close=close)


# A large bearish mother bar: opens 5020, closes 4980, spans 4975-5025.
BEARISH_MOTHER = candle(5020, 5025, 4975, 4980)
# A large bullish mother bar: the mirror image.
BULLISH_MOTHER = candle(4980, 5025, 4975, 5020)


class TestInsideBar:
    def test_contained_bar_is_inside(self):
        child = candle(5000, 5010, 4990, 5005)
        assert is_inside_bar(BEARISH_MOTHER, child)

    def test_equal_high_fails_strict_but_passes_loose(self):
        child = candle(5000, 5025, 4990, 5005)
        assert not is_inside_bar(BEARISH_MOTHER, child, strict=True)
        assert is_inside_bar(BEARISH_MOTHER, child, strict=False)

    def test_breaking_the_low_is_not_inside(self):
        child = candle(5000, 5010, 4970, 5005)
        assert not is_inside_bar(BEARISH_MOTHER, child)

    def test_engulfing_bar_is_not_inside(self):
        child = candle(4970, 5030, 4970, 5030)
        assert not is_inside_bar(BEARISH_MOTHER, child)

    def test_inside_bar_ignores_direction(self):
        """Range containment says nothing about which way the child closed."""
        up = candle(4990, 5010, 4985, 5005)
        down = candle(5005, 5010, 4985, 4990)
        assert is_inside_bar(BEARISH_MOTHER, up)
        assert is_inside_bar(BEARISH_MOTHER, down)


class TestHarami:
    def test_bullish_harami_after_bearish_mother(self):
        # Body 4990-5005 sits inside the mother's 4980-5020 body, closes up.
        child = candle(4990, 5008, 4988, 5005)
        assert harami_bias(BEARISH_MOTHER, child) is Bias.BULLISH

    def test_bearish_harami_after_bullish_mother(self):
        child = candle(5005, 5008, 4988, 4990)
        assert harami_bias(BULLISH_MOTHER, child) is Bias.BEARISH

    def test_same_direction_child_is_not_a_harami(self):
        """A harami must close against the mother bar."""
        child = candle(5005, 5008, 4988, 4990)  # bearish, like its mother
        assert harami_bias(BEARISH_MOTHER, child) is Bias.NEUTRAL

    def test_oversized_child_body_rejected(self):
        # Mother body is 40 points; this child's body is 30, over the 50% cap.
        child = candle(4985, 5018, 4983, 5015)
        assert harami_bias(BEARISH_MOTHER, child) is Bias.NEUTRAL
        # Relaxing the cap admits it.
        assert harami_bias(BEARISH_MOTHER, child, max_body_ratio=0.9) is Bias.BULLISH

    def test_body_outside_mother_body_rejected(self):
        """Closing above the mother's open breaks body containment."""
        child = candle(5015, 5030, 5010, 5025)
        assert harami_bias(BEARISH_MOTHER, child) is Bias.NEUTRAL

    def test_doji_mother_gives_no_signal(self):
        doji = candle(5000, 5025, 4975, 5000)
        child = candle(4998, 5005, 4995, 5002)
        assert harami_bias(doji, child) is Bias.NEUTRAL

    def test_harami_may_have_wicks_outside_the_mother_range(self):
        """
        Body containment is the rule, not range containment -- so a harami is
        not necessarily an inside bar.
        """
        child = candle(4990, 5030, 4970, 5005)
        assert harami_bias(BEARISH_MOTHER, child) is Bias.BULLISH
        assert not is_inside_bar(BEARISH_MOTHER, child)


class TestDetect:
    def test_harami_takes_priority_over_inside_bar(self):
        """When a pair qualifies as both, the directional reading wins."""
        child = candle(4990, 5008, 4988, 5005)
        signal = detect(BEARISH_MOTHER, child)
        assert signal is not None
        assert signal.kind == "harami"
        assert signal.bias is Bias.BULLISH

    def test_inside_bar_when_not_a_harami(self):
        child = candle(4990, 5010, 4985, 4988)  # bearish child, bearish mother
        signal = detect(BEARISH_MOTHER, child)
        assert signal is not None
        assert signal.kind == "inside_bar"
        assert signal.bias is Bias.NEUTRAL

    def test_disabling_harami_falls_through_to_inside_bar(self):
        child = candle(4990, 5008, 4988, 5005)
        signal = detect(BEARISH_MOTHER, child, trade_harami=False)
        assert signal is not None
        assert signal.kind == "inside_bar"

    def test_both_disabled_yields_nothing(self):
        child = candle(4990, 5008, 4988, 5005)
        assert detect(
            BEARISH_MOTHER, child, trade_harami=False, trade_inside_bar=False,
        ) is None

    def test_small_mother_rejected(self):
        """Mother span of 50 points is below a 60-point floor."""
        child = candle(4990, 5008, 4988, 5005)
        assert detect(BEARISH_MOTHER, child, min_mother_span=60.0) is None
        assert detect(BEARISH_MOTHER, child, min_mother_span=10.0) is not None

    def test_insufficient_compression_rejected(self):
        # Child spans 45 of the mother's 50: a compression ratio of 0.9.
        child = candle(4995, 5020, 4975, 5000)
        assert detect(BEARISH_MOTHER, child, max_compression=0.7) is None
        assert detect(BEARISH_MOTHER, child, max_compression=0.95) is not None

    def test_zero_span_mother_is_safe(self):
        """A flat mother bar must not divide by zero."""
        flat = candle(5000, 5000, 5000, 5000)
        assert detect(flat, candle(5000, 5000, 5000, 5000)) is None

    def test_breakout_levels_span_both_bars(self):
        """
        Triggers use the extremes of the pair, so a harami whose wick pokes past
        the mother bar still gets a stop outside the whole structure.
        """
        child = candle(4990, 5030, 4970, 5005)
        signal = detect(BEARISH_MOTHER, child, max_compression=2.0)
        assert signal is not None
        assert signal.breakout_high == 5030  # the child's wick, not the mother's
        assert signal.breakout_low == 4970

    def test_compression_ratio_reported(self):
        child = candle(5000, 5010, 4990, 5005)
        signal = detect(BEARISH_MOTHER, child)
        assert signal is not None
        assert signal.compression == pytest.approx(20 / 50)
