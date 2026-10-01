"""Tests for the catalogue parsers.

A parser that raises breaks the config flow for everyone on that
provider, and one that returns an empty list instead of ``None`` gives
the user an empty dropdown instead of a text field. Both outcomes are
covered here.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from catalogue_parsers import (
    openrouter_catalogue_from_payload,
    voice_options_for_model,
    voice_options_from_payload,
    voice_picker_allows_typing,
)

# Trimmed from a real ``GET /api/v1/models?output_modalities=speech``
# answer on 2026-09-30.
OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "hexgrad/kokoro-82m",
            "architecture": {"output_modalities": ["speech"]},
            "supported_voices": ["af_alloy", "af_bella"],
        },
        {
            "id": "fish-audio/s1",
            "architecture": {"output_modalities": ["speech"]},
            "supported_voices": None,
        },
        {
            "id": "openai/gpt-5",
            "architecture": {"output_modalities": ["text"]},
            "supported_voices": None,
        },
        {"id": "", "supported_voices": ["x"]},
        "not a model",
    ]
}


def test_openrouter_catalogue_maps_models_to_voices():
    catalogue = openrouter_catalogue_from_payload(OPENROUTER_PAYLOAD)
    assert catalogue == {
        "hexgrad/kokoro-82m": ["af_alloy", "af_bella"],
        # Kept, so it stays selectable and falls back to a typed voice.
        "fish-audio/s1": [],
    }


@pytest.mark.parametrize("payload", [None, [], "x", {}, {"data": "x"}, {"data": []}])
def test_openrouter_catalogue_unusable_payload_is_none(payload):
    assert openrouter_catalogue_from_payload(payload) is None


def test_voices_for_known_model():
    catalogue = openrouter_catalogue_from_payload(OPENROUTER_PAYLOAD)
    assert voice_options_for_model(catalogue, "hexgrad/kokoro-82m") == [
        {"value": "af_alloy", "label": "af_alloy"},
        {"value": "af_bella", "label": "af_bella"},
    ]


@pytest.mark.parametrize("model", ["fish-audio/s1", "unknown/model", None, ""])
def test_no_voices_means_typed_voice(model):
    """None, not an empty list, is what turns the picker into a text field."""
    catalogue = openrouter_catalogue_from_payload(OPENROUTER_PAYLOAD)
    assert voice_options_for_model(catalogue, model) is None


def test_voices_without_catalogue():
    assert voice_options_for_model(None, "hexgrad/kokoro-82m") is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"items": [{"id": "u1", "name": "Paul"}], "total": 1},
            [{"value": "u1", "label": "Paul"}],
        ),
        ({"data": [{"id": "alloy"}]}, [{"value": "alloy", "label": "alloy"}]),
        ({"voices": ["af_bella"]}, [{"value": "af_bella", "label": "af_bella"}]),
        (["Abigail.wav"], [{"value": "Abigail.wav", "label": "Abigail.wav"}]),
    ],
)
def test_voice_listing_shapes(payload, expected):
    assert voice_options_from_payload(payload) == expected


@pytest.mark.parametrize("payload", [None, 3, {}, {"voices": "x"}, [None, {}]])
def test_voice_listing_unusable_payload_is_none(payload):
    assert voice_options_from_payload(payload) is None


def test_plain_name_catalogues_allow_typing():
    # Kokoro-FastAPI answers with a bare list of names.
    options = voice_options_from_payload({"voices": ["af_bella", "am_michael"]})
    assert voice_picker_allows_typing(options)


def test_labelled_catalogues_do_not_allow_typing():
    # Mistral labels a UUID with a name, and custom_value would show the UUID.
    options = voice_options_from_payload(
        {"items": [{"id": "3f2a", "name": "Paul - Sad"}], "total": 1}
    )
    assert not voice_picker_allows_typing(options)


def test_empty_catalogue_does_not_allow_typing():
    assert not voice_picker_allows_typing([])
