"""Tests for matching a speaker's native entity to its Music Assistant entity.

The ids are the device registry entries of a real installation: a LyraT
reached by Music Assistant over Sendspin, a Sonos Move and a Nest Audio
on Cast.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from ma_twins import find_twin, native_ids, normalise_id

MUSIC_ASSISTANT = {
    "media_player.jbl_living_room_lyrat": ["upd48c4952e73c"],
    "media_player.sonos_move_ma": ["RINCON_F0F6C15D8BA601400"],
    "media_player.mancave_speaker": ["c39e5072-adfa-c148-6f7f-80e12af79fe2"],
}


def test_esphome_mac_matches_the_sendspin_player():
    ids = native_ids(identifiers=[], connections=[("mac", "d4:8c:49:52:e7:3c")])
    assert find_twin(ids, MUSIC_ASSISTANT) == "media_player.jbl_living_room_lyrat"


def test_sonos_identifier_matches():
    ids = native_ids(
        identifiers=[("sonos", "RINCON_F0F6C15D8BA601400")],
        connections=[
            ("upnp", "uuid:RINCON_F0F6C15D8BA601400"),
            ("mac", "f0:f6:c1:5d:8b:a6"),
        ],
    )
    assert find_twin(ids, MUSIC_ASSISTANT) == "media_player.sonos_move_ma"


def test_cast_uuid_matches_with_or_without_dashes():
    ids = native_ids(identifiers=[("cast", "c39e5072adfac1486f7f80e12af79fe2")], connections=[])
    assert find_twin(ids, MUSIC_ASSISTANT) == "media_player.mancave_speaker"


def test_unrelated_speaker_has_no_twin():
    ids = native_ids(
        identifiers=[("jbl_integration", "2C:1B:3A:62:8D:17", "51968fb6-8ace-3a3a-ba32-6e80fbcc81f8")],
        connections=[],
    )
    assert find_twin(ids, MUSIC_ASSISTANT) is None


def test_short_ids_are_never_compared():
    """An IP address could appear inside an unrelated player id by chance."""
    ids = native_ids(identifiers=[("jbl_integration", "192.168.1.15")], connections=[])
    assert ids == set()
    assert find_twin({"4952"}, MUSIC_ASSISTANT) is None


def test_no_music_assistant_players():
    ids = native_ids(identifiers=[], connections=[("mac", "d4:8c:49:52:e7:3c")])
    assert find_twin(ids, {}) is None


def test_normalise_id_keeps_letters_and_digits_only():
    assert normalise_id("uuid:RINCON_F0F6") == "uuidrinconf0f6"
