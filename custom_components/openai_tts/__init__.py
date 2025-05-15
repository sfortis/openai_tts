# __init__.py
"""Custom integration for OpenAI TTS."""
from __future__ import annotations

import logging
import os
from typing import List, Dict, Any, Optional, Union

import voluptuous as vol
from homeassistant.const import Platform, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN

from .const import DOMAIN
from .volume_restore import announce_with_volume_restore
from .utils import normalize_entity_ids

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
        vol.Optional("entity_id"): cv.entity_ids,  # For direct entity targeting
        vol.Optional("device_id"): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string])),  # For device targeting
        vol.Optional("area_id"): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string]))     # For area targeting
    }, extra=vol.ALLOW_EXTRA
)

def _get_entities_from_target(
    hass: HomeAssistant, 
    target: dict | None
) -> list[str]:
    """
    Extract entity IDs from service target more efficiently.
    
    Args:
        hass: Home Assistant instance
        target: Service call target dictionary
        
    Returns:
        List of entity IDs
    """
    if not target:
        return []
    
    _LOGGER.debug("Target: %s", target)
    entities = []
    
    # Handle direct entity_ids - normalize to always work with lists
    if entity_ids := target.get("entity_id"):
        entities.extend(normalize_entity_ids(entity_ids))
        _LOGGER.debug("Added entity_ids from target: %s", entities)
    
    # Get entity registry only once if needed
    entity_reg = None
    device_reg = None
    
    if any(key in target for key in ["area_id", "device_id"]):
        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)
    
    # Handle area_ids
    if area_ids := target.get("area_id"):
        # Normalize to always work with lists
        area_ids = normalize_entity_ids(area_ids)
        _LOGGER.debug("Processing area_ids: %s", area_ids)
        
        if entity_reg:
            # First, get all device IDs in these areas
            area_device_ids = set()
            
            # Find devices in these areas
            if device_reg:
                for device in device_reg.devices.values():
                    if device.area_id in area_ids:
                        area_device_ids.add(device.id)
                _LOGGER.debug("Found devices in areas: %s", area_device_ids)
            
            # Get all media player entities for devices in these areas
            for entry in entity_reg.entities.values():
                # Check if entity is directly in area
                if (entry.area_id in area_ids and 
                    entry.domain == MP_DOMAIN and 
                    entry.entity_id not in entities):
                    entities.append(entry.entity_id)
                    _LOGGER.debug("Added entity %s from area %s", entry.entity_id, entry.area_id)
                
                # Also check if entity's device is in area
                elif (entry.device_id in area_device_ids and
                      entry.domain == MP_DOMAIN and
                      entry.entity_id not in entities):
                    entities.append(entry.entity_id)
                    _LOGGER.debug("Added entity %s from device %s in area", entry.entity_id, entry.device_id)
    
    # Handle device_ids
    if device_ids := target.get("device_id"):
        # Normalize to always work with lists
        device_ids = normalize_entity_ids(device_ids)
        _LOGGER.debug("Processing device_ids: %s", device_ids)
        
        if entity_reg:
            # Get all media player entities for specified devices
            for entry in entity_reg.entities.values():
                if (entry.device_id in device_ids and 
                    entry.domain == MP_DOMAIN and 
                    entry.entity_id not in entities):
                    entities.append(entry.entity_id)
                    _LOGGER.debug("Added entity %s from device %s", entry.entity_id, entry.device_id)
    
    _LOGGER.debug("Final entities from target: %s", entities)
    return entities

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
        
        # Extract media players from target and data
        media_players = []
        
        # Combine target from both places (call.target attribute and data)
        target_data = {}
        
        # First check call.target attribute (preferred way)
        if hasattr(call, "target") and call.target:
            # Convert call.target to dict if it's not already
            target_data = dict(call.target) if not isinstance(call.target, dict) else call.target
            _LOGGER.debug("Processing target from call.target: %s", target_data)
        
        # Also check data for targeting parameters
        for target_key in ["entity_id", "device_id", "area_id"]:
            if target_key in data:
                target_data[target_key] = data[target_key]
                _LOGGER.debug("Found %s in data: %s", target_key, data[target_key])
        
        # Extract entities using our helper
        if target_data:
            media_players = _get_entities_from_target(hass, target_data)
            _LOGGER.debug("Media players from target data: %s", media_players)
        
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

    # Register service without advanced targeting options 
    # (using simpler registration compatible with older HA versions)
    hass.services.async_register(
        DOMAIN,
        SERVICE_NAME,
        _handle_say,
        schema=SAY_SCHEMA
    )
    
    _LOGGER.info("OpenAI TTS service 'say' registered successfully")

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_NAME)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)