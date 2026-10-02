"""Tests for the OpenAI model shutdown dates."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from model_retirement import shutdown_date, warning_start

SHUTDOWN = date(2027, 1, 6)


@pytest.mark.parametrize(
    "model",
    [
        "tts-1",
        "tts-1-hd",
        "gpt-4o-mini-tts",
        "gpt-4o-mini-tts-2025-03-20",
        "gpt-4o-mini-tts-2025-12-15",
        "tts-1-1106",
        " GPT-4o-mini-TTS ",
    ],
)
def test_retiring_models_on_openai(model):
    assert shutdown_date(model, on_openai=True) == SHUTDOWN


@pytest.mark.parametrize("model", ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"])
def test_same_names_elsewhere_are_not_retired(model):
    """Kokoro-FastAPI and others accept tts-1 as an alias of their own model."""
    assert shutdown_date(model, on_openai=False) is None


@pytest.mark.parametrize(
    "model",
    ["gpt-realtime-2.1-mini", "tts-10", "gpt-4o-mini-ttsx", "kokoro", "", None],
)
def test_other_models_are_not_retired(model):
    assert shutdown_date(model, on_openai=True) is None


def test_warning_starts_fourteen_days_before_the_shutdown():
    """The user asked for a warning a few days ahead, not from the announcement."""
    assert warning_start("gpt-4o-mini-tts", on_openai=True) == date(2026, 12, 23)


def test_no_warning_where_nothing_retires():
    assert warning_start("tts-1", on_openai=False) is None
    assert warning_start("kokoro", on_openai=True) is None
