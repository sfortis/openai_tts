"""Shared helpers for entity setup.

This module once aimed to be the single source of truth for classifying
config entries as legacy, parent or subentry. That never landed: the
inline checks in ``__init__.py``, ``tts.py`` and ``config_flow.py`` are
still there and have drifted apart, and the helpers written for them
went unused and were removed. Anyone picking that work up again should
know the copies are not equivalent, because ``config_flow`` deliberately
skips the entry-version check that the others apply, so unifying them
changes which entries offer subentries.

What remains is what is actually used.
"""
from __future__ import annotations

from typing import Any

from homeassistant.util import slugify

from .const import CONF_PROFILE_NAME

SUBENTRY_TYPE_PROFILE = "profile"


def is_subentry(config: Any) -> bool:
    """Return True when ``config`` represents a TTS profile subentry.

    Three signals indicate a subentry:
    1. Modern HA: ``config.subentry_type == "profile"``.
    2. Older HA: ``config.parent_entry_id`` is set.
    3. Fallback marker: a ``profile_name`` field in ``config.data``.
    """
    if (
        getattr(config, "subentry_type", None) == SUBENTRY_TYPE_PROFILE
    ):
        return True
    if getattr(config, "parent_entry_id", None) is not None:
        return True
    data = getattr(config, "data", None) or {}
    return data.get(CONF_PROFILE_NAME) is not None


def sanitize_profile_name(profile_name: str) -> str:
    """Return the entity-id fragment for a profile name, or "" if there is none.

    Two passes, and the first one has to stay. It lowercases, maps spaces
    and hyphens to underscores and drops everything else, which can leave
    runs of underscores or non-ASCII letters: "Living Room - Main" came
    out as "living_room___main". Home Assistant treats an entity id set
    by an integration as a suggestion and slugifies it before use, so the
    entity really was called ``tts.openai_tts_living_room_main`` all
    along, but suggesting the invalid form logs a deprecation that
    becomes an error in Home Assistant 2027.2.

    Slugifying the first pass's output yields exactly the string Home
    Assistant was deriving anyway, so no entity id changes. Slugifying
    the raw name instead would not: slugify turns punctuation into a
    separator where the first pass drops it, so "A+B" would move from
    ``ab`` to ``a_b``.

    The empty return matters. A name with nothing alphanumeric in it,
    "!!!" or a run of spaces, leaves no fragment, and slugify answers
    "unknown" for the second of those. Callers use the empty string to
    fall back to the bare ``tts.openai_tts``, which is what Home
    Assistant produces for those names today.
    """
    safe = profile_name.lower().replace(" ", "_").replace("-", "_")
    stripped = "".join(c for c in safe if c.isalnum() or c == "_")
    if not any(c.isalnum() for c in stripped):
        return ""
    return slugify(stripped)
