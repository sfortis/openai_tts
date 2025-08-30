"""Diagnostics support for Groq TTS.

Provides config entry and device diagnostics with sensitive data redacted.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_API_KEY,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_MODEL,
    CONF_NORMALIZE_AUDIO,
    CONF_URL,
    CONF_VOICE,
    CONF_CACHE_SIZE,
)

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry with secrets redacted."""
    redacted_data = async_redact_data(dict(entry.data), TO_REDACT)
    redacted_options = async_redact_data(dict(entry.options), TO_REDACT)

    summary = {
        "endpoint": redacted_data.get(CONF_URL),
        "model": redacted_data.get(CONF_MODEL),
        "voice": redacted_options.get(CONF_VOICE, redacted_data.get(CONF_VOICE)),
        "chime_enabled": redacted_options.get(CONF_CHIME_ENABLE, False),
        "chime_sound": redacted_options.get(CONF_CHIME_SOUND),
        "normalize_audio": redacted_options.get(CONF_NORMALIZE_AUDIO, False),
        "cache_size": redacted_options.get(CONF_CACHE_SIZE),
    }

    return {
        "entry_data": redacted_data,
        "options": redacted_options,
        "summary": summary,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device.

    This integration exposes a single device per config entry; include the same redacted
    data with the device identifiers.
    """
    data = await async_get_config_entry_diagnostics(hass, entry)
    data["device"] = {
        "identifiers": list(device.identifiers),
        "name": device.name,
        "manufacturer": device.manufacturer,
        "model": device.model,
    }
    return data

