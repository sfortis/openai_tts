"""Diagnostics support for OpenAI TTS."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, DOMAIN

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

    # Add integration domain data (without sensitive info)
    domain_data = hass.data.get(DOMAIN, {})
    data["domain_data"] = {
        "entry_count": len([k for k in domain_data.keys() if not k.startswith("_")]),
        "has_main_entry": "main_entry" in domain_data,
    }

    return data
