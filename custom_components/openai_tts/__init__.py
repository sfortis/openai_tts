# __init__.py
"""Custom integration for OpenAI TTS."""
from __future__ import annotations

import logging
import os
import voluptuous as vol
from homeassistant.const import Platform, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN

from .const import DOMAIN
from .volume_restore import announce_with_volume_restore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [Platform.TTS]
SERVICE_NAME = "say"

def get_chime_options() -> list[str]:
    """Scan chime folder and return list of available chime files."""
    chime_folder = os.path.join(os.path.dirname(__file__), "chime")
    try:
        files = os.listdir(chime_folder)
    except Exception as err:
        _LOGGER.error("Error listing chime folder: %s", err)
        files = []
    
    chime_files = []
    for file in files:
        if file.lower().endswith(".mp3"):
            chime_files.append(file)
    
    chime_files.sort()
    return chime_files

# Service Schema
SAY_SCHEMA = vol.Schema(
    {
        vol.Required("tts_entity"): cv.entity_id,
        vol.Required("message"): cv.string,
        vol.Optional("language", default="en"): cv.string,
        vol.Optional("chime", default=False): cv.boolean,
        vol.Optional("chime_sound"): cv.string,
        vol.Optional("normalize_audio", default=False): cv.boolean,
        vol.Optional("instructions"): cv.string,
        vol.Optional("volume"): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
        vol.Optional("pause_playback"): cv.boolean,  # Added pause_playback
        vol.Optional("entity_id"): cv.entity_ids,  # Accept but don't use entity_id
    }, extra=vol.ALLOW_EXTRA
)

def _get_entities_from_target(
    hass: HomeAssistant, 
    target: dict | None
) -> list[str]:
    """Extract entity IDs from service target."""
    if not target:
        return []
    
    entities = []
    
    # Handle direct entity_ids
    if entity_ids := target.get("entity_id"):
        if isinstance(entity_ids, str):
            entities.append(entity_ids)
        else:
            entities.extend(entity_ids)
    
    # Handle area_ids
    if area_ids := target.get("area_id"):
        entity_reg = er.async_get(hass)
        if isinstance(area_ids, str):
            area_ids = [area_ids]
        
        for area_id in area_ids:
            for entry in entity_reg.entities.values():
                if entry.area_id == area_id and entry.domain == MP_DOMAIN:
                    entities.append(entry.entity_id)
    
    # Handle device_ids
    if device_ids := target.get("device_id"):
        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)
        
        if isinstance(device_ids, str):
            device_ids = [device_ids]
        
        for device_id in device_ids:
            # Find all entities for this device
            for entry in entity_reg.entities.values():
                if entry.device_id == device_id and entry.domain == MP_DOMAIN:
                    entities.append(entry.entity_id)
    
    # Remove duplicates while preserving order
    return list(dict.fromkeys(entities))

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OpenAI TTS and register the openai_tts.say service."""
    # Forward to the built-in TTS platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _handle_say(call: ServiceCall) -> None:
        """Handle the say service call."""
        data = call.data
        
        # Debug logging
        _LOGGER.debug("Service call data: %s", data)
        _LOGGER.debug("Service call target: %s", getattr(call, 'target', None))
        
        # Extract media players from target
        media_players = []
        if hasattr(call, "target") and call.target:
            media_players = _get_entities_from_target(hass, dict(call.target))
            _LOGGER.debug("Media players from target: %s", media_players)
        
        # Also check if entity_id was passed in data (for backward compatibility)
        if "entity_id" in data:
            _LOGGER.debug("Found entity_id in data (legacy format), extracting...")
            entity_ids = data.get("entity_id", [])
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            # Add these to media_players if they're not already there
            for entity_id in entity_ids:
                if entity_id not in media_players:
                    media_players.append(entity_id)
            _LOGGER.debug("Media players after adding from data: %s", media_players)
        
        # Validate TTS entity
        tts_entity = data["tts_entity"]
        if not hass.states.get(tts_entity):
            raise ValueError(f"TTS entity {tts_entity} not found")
        
        # Get service data (excluding entity_id)
        message = data["message"]
        language = data.get("language", "en")
        options = {
            "chime": data.get("chime", False),
            "chime_sound": data.get("chime_sound"),
            "normalize_audio": data.get("normalize_audio", False),
            "instructions": data.get("instructions"),
        }
        
        # Remove None values
        options = {k: v for k, v in options.items() if v is not None}
        
        tts_volume = data.get("volume")
        pause_playback = data.get("pause_playback")  # Get pause_playback from service call
        
        _LOGGER.debug(
            "Calling announce_with_volume_restore with: tts_entity=%s, media_players=%s, message=%s, pause_playback=%s",
            tts_entity, media_players, message, pause_playback
        )
        
        # Call our helper with pause_playback parameter
        await announce_with_volume_restore(
            hass,
            tts_entity=tts_entity,
            media_players=media_players,
            message=message,
            language=language,
            options=options,
            tts_volume=tts_volume,
            pause_playback=pause_playback,  # Pass the parameter
        )

    # Register service
    hass.services.async_register(
        DOMAIN,
        SERVICE_NAME,
        _handle_say,
        schema=SAY_SCHEMA,
    )
    
    _LOGGER.info("OpenAI TTS service 'say' registered successfully")

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_NAME)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)