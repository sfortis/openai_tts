"""Chime sounds: the built-in ones and the user's own.

The built-in sounds ship inside the integration folder, which a HACS
update replaces, so a file a user dropped there disappears with the next
update. A user's own sounds therefore live in a folder of their own
under the Home Assistant configuration directory, which no update
touches: ``<config>/openai_tts/chime``. Both folders are listed together,
and a user's file wins over a built-in one with the same name.

A chime is named by its file name alone. The name can come from an
action call, so anything that is not a plain ``.mp3`` file name is
refused rather than joined onto a folder path, which would otherwise let
``../`` reach any file on the disk.

This module imports nothing from Home Assistant, so it can be tested on
its own. Its functions read the disk and belong in an executor.
"""
from __future__ import annotations

import logging
import os

_LOGGER = logging.getLogger(__name__)

BUILTIN_CHIME_DIR = os.path.join(os.path.dirname(__file__), "chime")

# The user's folder, relative to the Home Assistant configuration
# directory. Callers pass it through ``hass.config.path``.
USER_CHIME_DIR = os.path.join("openai_tts", "chime")

CHIME_EXTENSION = ".mp3"


def is_chime_name(name: str | None) -> bool:
    """True for a plain ``.mp3`` file name with no folder in it."""
    return (
        bool(name)
        and os.path.basename(name) == name
        and name not in (".", "..")
        and name.lower().endswith(CHIME_EXTENSION)
    )


def _chime_files(folder: str) -> set[str]:
    """The chime file names in ``folder``, or none when it is missing."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return set()
    except OSError as err:
        _LOGGER.error("Could not list the chime folder %s: %s", folder, err)
        return set()
    return {
        name for name in names
        if is_chime_name(name) and os.path.isfile(os.path.join(folder, name))
    }


def chime_options(
    user_dir: str, builtin_dir: str = BUILTIN_CHIME_DIR
) -> list[dict[str, str]]:
    """Selector options for every chime, sorted by label.

    A user's own file is labelled as such, so it can be told apart from
    a built-in sound, and it replaces a built-in one of the same name.
    """
    own = _chime_files(user_dir)
    builtin = _chime_files(builtin_dir) - own
    options = [
        {"value": name, "label": _label(name)} for name in builtin
    ] + [
        {"value": name, "label": f"{_label(name)} (your own)"} for name in own
    ]
    options.sort(key=lambda option: option["label"].lower())
    return options


def chime_path(
    name: str | None, user_dir: str, builtin_dir: str = BUILTIN_CHIME_DIR
) -> str | None:
    """The file to play for chime ``name``, or ``None`` when there is none.

    The user's folder is looked in first, the same precedence the
    option list shows.
    """
    if not is_chime_name(name):
        return None
    for folder in (user_dir, builtin_dir):
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _label(name: str) -> str:
    return os.path.splitext(name)[0].replace("_", " ").title()
