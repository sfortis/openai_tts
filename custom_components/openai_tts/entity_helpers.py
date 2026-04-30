"""Shared helpers for entity setup and config-entry classification.

The ``is_subentry``/``is_legacy_entry`` checks are repeated in 6+ places
across ``__init__.py``, ``tts.py`` and ``config_flow.py``. Centralizing
them here makes the rules a single source of truth and prevents drift.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import CONF_MODEL, CONF_PROFILE_NAME, CONF_VOICE

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


def has_subentries(entry: ConfigEntry) -> bool:
    """Return True when ``entry`` is a parent that owns at least one subentry."""
    subs = getattr(entry, "subentries", None)
    return bool(subs)


def is_legacy_entry(entry: ConfigEntry) -> bool:
    """Return True for pre-migration entries (model/voice in data, version < 2.1).

    These keep their model/voice configuration directly on the parent entry
    rather than in a subentry, and create their TTS entity inline.
    """
    has_model_or_voice = (
        entry.data.get(CONF_MODEL) is not None
        or entry.data.get(CONF_VOICE) is not None
    )
    if not has_model_or_voice:
        return False
    if has_subentries(entry):
        return False
    if entry.version < 2:
        return True
    if entry.version == 2 and entry.minor_version < 1:
        return True
    return False


def is_modern_parent(entry: ConfigEntry) -> bool:
    """Return True for fully-migrated parent entries (no profile data on parent)."""
    if is_subentry(entry):
        return False
    if is_legacy_entry(entry):
        return False
    return True


def sanitize_profile_name(profile_name: str) -> str:
    """Return a profile name lowered/underscored and stripped of unsafe chars."""
    safe = profile_name.lower().replace(" ", "_").replace("-", "_")
    return "".join(c for c in safe if c.isalnum() or c == "_")
