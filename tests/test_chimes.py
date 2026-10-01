"""Tests for how chime sounds are listed and found."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from chimes import chime_options, chime_path, is_chime_name


@pytest.fixture
def folders(tmp_path):
    builtin = tmp_path / "builtin"
    own = tmp_path / "own"
    builtin.mkdir()
    own.mkdir()
    (builtin / "threetone.mp3").write_bytes(b"builtin")
    (builtin / "signal1.mp3").write_bytes(b"builtin")
    (builtin / "notes.txt").write_text("not a chime")
    (own / "door_bell.mp3").write_bytes(b"own")
    (own / "signal1.mp3").write_bytes(b"own")
    return builtin, own


def test_options_list_both_folders_and_mark_the_users_own(folders):
    builtin, own = folders
    assert chime_options(str(own), str(builtin)) == [
        {"value": "door_bell.mp3", "label": "Door Bell (your own)"},
        {"value": "signal1.mp3", "label": "Signal1 (your own)"},
        {"value": "threetone.mp3", "label": "Threetone"},
    ]


def test_a_missing_user_folder_lists_only_the_built_in_sounds(folders, tmp_path):
    builtin, _ = folders
    options = chime_options(str(tmp_path / "absent"), str(builtin))
    assert [option["value"] for option in options] == ["signal1.mp3", "threetone.mp3"]


def test_the_users_file_wins_over_a_built_in_one_of_the_same_name(folders):
    builtin, own = folders
    assert chime_path("signal1.mp3", str(own), str(builtin)) == str(own / "signal1.mp3")
    assert chime_path("threetone.mp3", str(own), str(builtin)) == str(builtin / "threetone.mp3")


def test_an_unknown_chime_has_no_path(folders):
    builtin, own = folders
    assert chime_path("missing.mp3", str(own), str(builtin)) is None


@pytest.mark.parametrize(
    "name",
    ["../secrets.yaml", "../../threetone.mp3", "/etc/passwd", "sub/threetone.mp3",
     "notes.txt", "", None, "..", "."],
)
def test_only_a_plain_mp3_file_name_is_accepted(folders, name):
    builtin, own = folders
    assert not is_chime_name(name)
    assert chime_path(name, str(own), str(builtin)) is None


def test_a_plain_mp3_name_is_accepted():
    assert is_chime_name("threetone.mp3")
    assert is_chime_name("Door Bell.MP3")
