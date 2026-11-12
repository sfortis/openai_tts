"""Tests for the events of the Realtime read-aloud request.

This module imports nothing from Home Assistant, so it can be tested on
a plain checkout with pytest alone.
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from realtime_protocol import (
    ERROR_AUTH,
    ERROR_QUOTA,
    ERROR_RATE_LIMIT,
    ERROR_REQUEST,
    ERROR_SERVER,
    READ_ALOUD_INSTRUCTIONS,
    SPEED_MAX,
    SPEED_MIN,
    build_instructions,
    build_request_events,
    clamp_speed,
    error_kind,
    parse_event,
    redact_keys,
    session_url,
    transcript_matches,
    wrap_text,
)


def test_session_url_sets_the_model():
    url = session_url("wss://api.openai.com/v1/realtime", "gpt-realtime-2.1-mini")
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini"


def test_session_url_replaces_an_existing_query():
    url = session_url("wss://example.test/v1/realtime?model=old", "new")
    assert url == "wss://example.test/v1/realtime?model=new"


def test_speed_is_clamped_to_the_realtime_range():
    assert clamp_speed(4.0) == SPEED_MAX
    assert clamp_speed(0.1) == SPEED_MIN
    assert clamp_speed(1.2) == 1.2
    assert clamp_speed(None) is None


def test_instructions_keep_the_read_aloud_rule_first():
    assert build_instructions(None) == READ_ALOUD_INSTRUCTIONS
    assert build_instructions("   ") == READ_ALOUD_INSTRUCTIONS
    combined = build_instructions("Speak calmly.")
    assert combined.startswith(READ_ALOUD_INSTRUCTIONS)
    assert combined.endswith("Speak calmly.")


def test_request_events_carry_text_voice_and_format():
    update, item, response = build_request_events("Hello there", "marin", 2.0, None)
    assert update["type"] == "session.update"
    session = update["session"]
    assert session["output_modalities"] == ["audio"]
    output = session["audio"]["output"]
    assert output["voice"] == "marin"
    assert output["speed"] == SPEED_MAX
    assert output["format"] == {"type": "audio/pcm", "rate": 24000}
    assert item["item"]["content"] == [{"type": "input_text", "text": wrap_text("Hello there")}]
    assert response == {"type": "response.create"}


def test_text_travels_in_a_verbatim_envelope():
    wrapped = json.loads(wrap_text("Καλημέρα. \"Quoted\" text"))
    assert wrapped == {
        "require_repeat_verbatim": True,
        "response_text": "Καλημέρα. \"Quoted\" text",
    }
    # Greek stays readable in the message rather than becoming escapes.
    assert "Καλημέρα" in wrap_text("Καλημέρα")


def test_request_events_leave_out_missing_voice_and_speed():
    update = build_request_events("Hi", None, None, None)[0]
    output = update["session"]["audio"]["output"]
    assert "voice" not in output
    assert "speed" not in output


def test_audio_delta_is_decoded():
    pcm = b"\x01\x02\x03\x04"
    event = parse_event({
        "type": "response.output_audio.delta",
        "delta": base64.b64encode(pcm).decode(),
    })
    assert event.kind == "audio"
    assert event.audio == pcm


def test_transcript_delta_and_completed_response():
    event = parse_event({"type": "response.output_audio_transcript.delta", "delta": "Hel"})
    assert (event.kind, event.text) == ("transcript", "Hel")
    done = parse_event({"type": "response.done", "response": {"status": "completed"}})
    assert done.kind == "done"


def test_failed_response_becomes_an_error():
    event = parse_event({
        "type": "response.done",
        "response": {
            "status": "failed",
            "status_details": {
                "type": "failed",
                "error": {"type": "server_error", "code": "x", "message": "boom"},
            },
        },
    })
    assert event.kind == "error"
    assert event.error["message"] == "boom"
    assert error_kind(event.error) == ERROR_SERVER


def test_incomplete_response_without_error_object_still_fails():
    event = parse_event({
        "type": "response.done",
        "response": {
            "status": "incomplete",
            "status_details": {"type": "incomplete", "reason": "max_output_tokens"},
        },
    })
    assert event.kind == "error"
    assert "incomplete" in event.error["message"]
    assert "max_output_tokens" in event.error["message"]


def test_other_events_are_ignored():
    assert parse_event({"type": "session.updated"}).kind == "other"


def test_error_kinds():
    assert error_kind({"code": "invalid_api_key"}) == ERROR_AUTH
    assert error_kind({"code": "insufficient_quota"}) == ERROR_QUOTA
    assert error_kind({"code": "rate_limit_exceeded"}) == ERROR_RATE_LIMIT
    assert error_kind({"type": "server_error"}) == ERROR_SERVER
    assert error_kind({"type": "invalid_request_error", "code": "invalid_value"}) == ERROR_REQUEST


def test_transcript_comparison_ignores_case_accents_and_punctuation():
    assert transcript_matches(
        "Did anyone close the garage door?", "did anyone close the garage door"
    )
    assert transcript_matches(
        "Καλημέρα. Ξέχασες την πόρτα;", "καλημερα ξεχασες την πορτα"
    )


def test_transcript_comparison_catches_added_words():
    assert not transcript_matches(
        "Did anyone close the garage door?",
        "Sure! Did anyone close the garage door?",
    )
    assert not transcript_matches("Hi.", "Hi there! How can I help you today?")


def test_transcript_comparison_accepts_a_parenthesis_read_as_a_clause():
    assert transcript_matches(
        "About 239,000 miles (384,000 kilometers) from our planet.",
        "About 239,000 miles, or about 384,000 kilometers, from our planet.",
    )
    assert transcript_matches(
        "Περίπου 384.000 χιλιόμετρα (239.000 μίλια) μακριά.",
        "Περίπου 384.000 χιλιόμετρα, δηλαδή περίπου 239.000 μίλια, μακριά.",
    )


def test_transcript_comparison_catches_a_preamble_an_answer_and_a_translation():
    text = "Έκλεισε κανείς την πόρτα του γκαράζ;"
    assert not transcript_matches(text, "Εντάξει, θα το πω πιο καθαρά. " + text)
    assert not transcript_matches(
        "Tell me a joke.", "What do you call a snowman in July? A puddle!"
    )
    assert not transcript_matches(
        "These historic missions marked a significant achievement.",
        "Estas históricas misiones representaron un logro importante.",
    )


def test_transcript_comparison_catches_missing_words():
    assert not transcript_matches(
        "Did anyone close the garage door?", "Did anyone close the door?"
    )
    assert not transcript_matches("Did anyone close the garage door?", "")


def test_key_fragments_are_masked_in_error_messages():
    assert redact_keys(
        "Incorrect API key provided: sk-proj-ab12***********cd34. Check it."
    ) == "Incorrect API key provided: sk-***. Check it."
    assert redact_keys("Rate limit reached for gpt-realtime-2.1-mini") == (
        "Rate limit reached for gpt-realtime-2.1-mini"
    )
