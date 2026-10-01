"""Tests for the parent entry title rules.

The presets below copy the fields that ``const.py`` gives the real
presets. ``const.py`` itself imports Home Assistant, so it cannot be
loaded here.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from entry_titles import account_name_from_title, entry_title

OPENAI = {
    "label": "OpenAI", "title_name": "OpenAI",
    "self_hosted": False, "title_shows_host": False,
}
OPENROUTER = {
    "label": "OpenRouter", "title_name": "OpenRouter",
    "self_hosted": False, "title_shows_host": False,
}
GROQ = {
    "label": "Groq (Orpheus TTS)", "title_name": "Groq",
    "self_hosted": False, "title_shows_host": False,
}
KOKORO = {
    "label": "Kokoro-FastAPI (self-hosted Kokoro)", "title_name": "Kokoro",
    "self_hosted": True, "title_shows_host": True,
}
CUSTOM = {
    "label": "Custom / Self-hosted (any OpenAI-compatible endpoint)",
    "title_name": "Custom",
    "self_hosted": False, "title_shows_host": True,
}
PRESETS = [OPENAI, OPENROUTER, GROQ, KOKORO, CUSTOM]


@pytest.mark.parametrize(
    ("preset", "name", "host", "expected"),
    [
        (OPENAI, "", "api.openai.com", "OpenAI"),
        (OPENAI, "Work", "api.openai.com", "OpenAI - Work"),
        (GROQ, None, "api.groq.com", "Groq"),
        (KOKORO, "", "192.168.1.5", "Kokoro (self-hosted, 192.168.1.5)"),
        (KOKORO, "Living room", "192.168.1.5", "Kokoro (self-hosted) - Living room"),
        (CUSTOM, "", "192.168.1.5", "Custom (192.168.1.5)"),
        (CUSTOM, "Living room", "192.168.1.5", "Custom - Living room"),
    ],
)
def test_entry_title(preset, name, host, expected):
    assert entry_title(preset, name, host) == expected


@pytest.mark.parametrize("name", ["OpenRouter", " openrouter ", "OPENROUTER"])
def test_name_that_repeats_the_provider_is_dropped(name):
    """The production entry was titled "OpenRouter - OpenRouter"."""
    assert entry_title(OPENROUTER, name, "openrouter.ai") == "OpenRouter"


def test_name_that_repeats_the_label_is_dropped():
    assert entry_title(GROQ, "Groq (Orpheus TTS)", "api.groq.com") == "Groq"


def test_dropped_name_still_shows_the_host_on_self_hosted():
    assert (
        entry_title(KOKORO, "kokoro", "192.168.1.5")
        == "Kokoro (self-hosted, 192.168.1.5)"
    )


def test_missing_hostname_leaves_no_empty_parentheses():
    assert entry_title(CUSTOM, "", None) == "Custom"
    assert entry_title(KOKORO, "", None) == "Kokoro (self-hosted)"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("OpenAI - Work", "Work"),
        ("Kokoro (self-hosted) - Living room", "Living room"),
        ("Custom - Living room", "Living room"),
        # Titles written before the short names existed.
        ("Groq (Orpheus TTS) - Work", "Work"),
        ("Custom / Self-hosted (any OpenAI-compatible endpoint) - Lab", "Lab"),
        ("OpenRouter - OpenRouter", "OpenRouter"),
        # Titles that carry no account name.
        ("OpenAI", ""),
        ("Kokoro (self-hosted, 192.168.1.5)", ""),
        ("Custom (192.168.1.5)", ""),
        ("Renamed by hand", ""),
    ],
)
def test_account_name_from_title(title, expected):
    assert account_name_from_title(title, PRESETS) == expected


@pytest.mark.parametrize("preset", PRESETS)
def test_title_round_trips_the_account_name(preset):
    title = entry_title(preset, "Upstairs", "10.0.0.2")
    assert account_name_from_title(title, PRESETS) == "Upstairs"
