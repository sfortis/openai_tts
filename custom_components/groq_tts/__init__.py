"""Custom integration for Groq TTS."""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import logging

from .const import UNIQUE_ID

PLATFORMS: list[str] = [Platform.TTS]

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up entities."""
    # Reload the entry when options change so updates (like cache size)
    # take effect immediately without requiring a restart.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry data to new format.

    - Move legacy UNIQUE_ID stored in data to entry.unique_id
    """
    # If the entry already has a unique_id, nothing to do
    if entry.unique_id:
        return True

    # Migrate legacy unique id
    if isinstance(entry.data, dict) and UNIQUE_ID in entry.data:
        new_data = dict(entry.data)
        unique_id = new_data.pop(UNIQUE_ID)
        _LOGGER.debug("Migrating config entry to set unique_id and clean data")
        hass.config_entries.async_update_entry(entry, data=new_data, unique_id=unique_id)
        return True

    return True
