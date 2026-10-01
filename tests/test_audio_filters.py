"""Tests for the audio filter chain.

Every decision about whether ffmpeg runs at all, on the atomic and the
streaming path alike, comes from ``build_audio_filter``, so a mistake
here either filters audio nobody asked to change or silently drops a
gain the user set.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from audio_filters import (
    GAIN_DB_MAX,
    GAIN_DB_MIN,
    LOUDNESS_FILTER,
    build_audio_filter,
    clamp_gain_db,
)


def test_nothing_to_apply_returns_none():
    """No filter means the provider's bytes are passed through untouched."""
    assert build_audio_filter(False, 0.0) is None
    assert build_audio_filter(False) is None


def test_normalisation_alone_is_unchanged():
    """A profile without a gain must sound exactly as it did before."""
    assert build_audio_filter(True, 0.0) == LOUDNESS_FILTER


def test_gain_comes_after_normalisation():
    """Placed first, dynaudnorm would level the gain away again."""
    chain = build_audio_filter(True, 6.0)
    assert chain is not None
    assert chain.startswith(LOUDNESS_FILTER + ",volume=6dB,")


def test_positive_gain_is_limited():
    chain = build_audio_filter(False, 6.0)
    assert chain is not None
    assert chain.startswith("volume=6dB,")
    assert "alimiter=" in chain
    # Without this alimiter scales its output back up to full scale.
    assert "level=disabled" in chain


def test_negative_gain_needs_no_limiter():
    assert build_audio_filter(False, -3.5) == "volume=-3.5dB"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0.0),
        ("", 0.0),
        ("loud", 0.0),
        (math.nan, 0.0),
        ("4.5", 4.5),
        (99, GAIN_DB_MAX),
        (-99, GAIN_DB_MIN),
    ],
)
def test_clamp_gain_db(value, expected):
    """A missing or unreadable stored value counts as no gain."""
    assert clamp_gain_db(value) == expected


def test_out_of_range_gain_is_clamped_in_the_chain():
    assert build_audio_filter(False, 40).startswith(f"volume={GAIN_DB_MAX:g}dB,")
