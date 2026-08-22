"""Diagnostics support for OpenAI TTS."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api_health import health_tracker_for
from .const import CONF_API_KEY, DOMAIN, MESSAGE_DURATIONS_KEY

# Keys to redact from diagnostics
TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # Redact sensitive data from entry
    data = {
        "entry": {
            "entry_id": entry.entry_id,
            "version": f"{entry.version}.{entry.minor_version}",
            "domain": entry.domain,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }

    # Add subentries info if present
    if hasattr(entry, 'subentries') and entry.subentries:
        data["subentries"] = []
        for subentry_id, subentry in entry.subentries.items():
            subentry_info = {
                "subentry_id": subentry_id,
                "title": subentry.title,
                "subentry_type": getattr(subentry, 'subentry_type', None),
                "data": async_redact_data(dict(subentry.data), TO_REDACT),
            }
            data["subentries"].append(subentry_info)

    # Add TTS entity states
    tts_entities = []
    for state in hass.states.async_all("tts"):
        if state.entity_id.startswith("tts.openai_tts"):
            tts_entities.append({
                "entity_id": state.entity_id,
                "state": state.state,
                "attributes": {
                    k: v for k, v in state.attributes.items()
                    if k not in TO_REDACT
                },
            })

    data["tts_entities"] = tts_entities

    # Runtime state worth seeing in a report. The old version counted
    # keys in ``hass.data`` and reported whether a ``main_entry`` slot
    # existed; both described bookkeeping that no longer exists, and the
    # count was of entry ids rather than anything a reader could act on.
    tracker = health_tracker_for(entry)
    data["runtime"] = {
        "api_status": tracker.data.get("status") if tracker else None,
        "last_error_at": tracker.data.get("last_error_at") if tracker else None,
        "health_tracker_present": tracker is not None,
        "cached_durations": len(
            hass.data.get(DOMAIN, {}).get(MESSAGE_DURATIONS_KEY, {})
        ),
    }

    return data
