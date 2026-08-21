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
    """Return a profile name lowered/underscored and stripped of unsafe chars."""
    safe = profile_name.lower().replace(" ", "_").replace("-", "_")
    return "".join(c for c in safe if c.isalnum() or c == "_")
