"""Tests for the selector option helpers.

These run without Home Assistant installed, which is the point: the
defect they cover shipped in 3.9 and broke every profile settings form,
and a test needing a full Home Assistant test harness would not have
existed in time to catch it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from selector_options import ensure_selectable, option_values

MAPPINGS = [
    {"value": "signal1.mp3", "label": "Signal1"},
    {"value": "threetone.mp3", "label": "Threetone"},
]
STRINGS = ["tts-1", "tts-1-hd"]


def test_option_values_reads_both_shapes():
    assert option_values(MAPPINGS) == ["signal1.mp3", "threetone.mp3"]
    assert option_values(STRINGS) == ["tts-1", "tts-1-hd"]


def test_saved_value_already_present_is_not_appended():
    """The comparison must look at the value, not the whole option.

    Comparing the saved string against the mappings themselves is never
    equal, so the guard passed every time and appended on every render.
    """
    assert ensure_selectable(MAPPINGS, "threetone.mp3") == MAPPINGS
    assert ensure_selectable(STRINGS, "tts-1") == STRINGS


def test_missing_value_is_appended_in_the_same_shape():
    """A mapping list must stay a mapping list.

    A bare string appended to a list of mappings is the exact list Home
    Assistant's select selector refuses, which is what stopped the form
    from opening.
    """
    out = ensure_selectable(MAPPINGS, "gone.mp3")
    assert len(out) == len(MAPPINGS) + 1
    assert out[-1] == {"value": "gone.mp3", "label": "gone.mp3 (saved)"}
    assert all(isinstance(opt, dict) for opt in out)

    out = ensure_selectable(STRINGS, "omnivoice")
    assert out[-1] == "omnivoice"
    assert all(isinstance(opt, str) for opt in out)


@pytest.mark.parametrize("options", [MAPPINGS, STRINGS, []])
@pytest.mark.parametrize("value", ["gone.mp3", "threetone.mp3", "tts-1", None, ""])
def test_result_is_never_a_mixed_list(options, value):
    """Whatever the inputs, one uniform shape comes out."""
    out = ensure_selectable(options, value)
    kinds = {type(opt).__name__ for opt in out}
    assert len(kinds) <= 1, f"mixed option types {kinds}"


def test_saved_value_is_always_selectable_afterwards():
    for options in (MAPPINGS, STRINGS):
        for value in ("gone.mp3", "threetone.mp3", "tts-1"):
            assert value in option_values(ensure_selectable(options, value))


def test_empty_value_leaves_the_list_alone():
    assert ensure_selectable(MAPPINGS, None) == MAPPINGS
    assert ensure_selectable(MAPPINGS, "") == MAPPINGS
