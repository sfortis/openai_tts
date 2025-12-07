# __init__.py
"""Custom integration for OpenAI TTS."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import List, Dict, Any, Optional, Union

import voluptuous as vol
from homeassistant.const import Platform, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN

from .const import (
    DOMAIN, 
    CONF_MODEL, 
    CONF_VOICE, 
    CONF_SPEED, 
    VOICES,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
    CONF_INSTRUCTIONS,
    CONF_VOLUME_RESTORE,
    CONF_PAUSE_PLAYBACK,
    CONF_PROFILE_NAME,
    DEFAULT_URL,
    CONF_API_KEY,
    UNIQUE_ID,
    CONF_URL,
)
from .volume_restore import announce
from .utils import normalize_entity_ids

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [Platform.TTS]
SERVICE_NAME = "say"
SUBENTRY_TYPE_PROFILE = "profile"

# Service Schema
SAY_SCHEMA = vol.Schema(
    {
        vol.Required("tts_entity"): cv.entity_id,
        vol.Required("message"): cv.string,
        vol.Optional("language", default="en"): cv.string,
        vol.Optional("voice"): vol.In(VOICES),
        vol.Optional("speed"): vol.All(vol.Coerce(float), vol.Range(min=0.25, max=4.0)),
        vol.Optional("instructions"): cv.string,
        vol.Optional("chime", default=False): cv.boolean,
        vol.Optional("chime_sound"): cv.string,
        vol.Optional("normalize_audio", default=False): cv.boolean,
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

async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating configuration from version %s.%s", config_entry.version, config_entry.minor_version)
    _LOGGER.debug("Entry data contains: model=%s, voice=%s", 
                 config_entry.data.get(CONF_MODEL), config_entry.data.get(CONF_VOICE))

    if config_entry.version > 2:
        # This means the user has downgraded from a future version
        return False

    if config_entry.version == 1:
        # Migration from version 1 to 2
        # Legacy entries keep their model/voice data and continue working as before
        # We just bump the version to track that migration was attempted
        new_data = {**config_entry.data}
        
        # Mark as legacy if it has model/voice data
        if new_data.get(CONF_MODEL) or new_data.get(CONF_VOICE):
            _LOGGER.info("Migrating legacy entry %s to version 2 (keeping as legacy)", config_entry.entry_id)
        
        hass.config_entries.async_update_entry(config_entry, data=new_data, minor_version=0, version=2)

    if config_entry.version == 2 and config_entry.minor_version < 1:
        # Migration from 2.0 to 2.1: Convert legacy entries to parent+subentry structure
        # Only migrate if we have model/voice data (legacy entry) AND haven't migrated yet
        if config_entry.data.get(CONF_MODEL) or config_entry.data.get(CONF_VOICE):
            # Check if we already have subentries - if so, skip migration
            has_subentries = hasattr(config_entry, 'subentries') and config_entry.subentries
            if has_subentries:
                _LOGGER.debug("Entry already has %d subentries, skipping migration", len(config_entry.subentries))
                # Just update the version
                hass.config_entries.async_update_entry(config_entry, minor_version=1)
                return True
            
            _LOGGER.info("Migrating legacy entry %s to parent+subentry structure", config_entry.entry_id)
            
            # Set migration flag to prevent reload during migration
            hass.data.setdefault(DOMAIN, {})
            hass.data[DOMAIN][f"{config_entry.entry_id}_migrating"] = True
            
            # Extract voice configuration from the entry
            model = config_entry.data.get(CONF_MODEL, "tts-1")
            voice = config_entry.data.get(CONF_VOICE, "shimmer")
            speed = config_entry.data.get(CONF_SPEED, 1.0)
            
            # Get options that should move to subentry
            chime = config_entry.options.get(CONF_CHIME_ENABLE, config_entry.data.get(CONF_CHIME_ENABLE, False))
            chime_sound = config_entry.options.get(CONF_CHIME_SOUND, config_entry.data.get(CONF_CHIME_SOUND, "threetone.mp3"))
            normalize = config_entry.options.get(CONF_NORMALIZE_AUDIO, config_entry.data.get(CONF_NORMALIZE_AUDIO, False))
            instructions = config_entry.options.get(CONF_INSTRUCTIONS, config_entry.data.get(CONF_INSTRUCTIONS))
            volume_restore = config_entry.options.get(CONF_VOLUME_RESTORE, config_entry.data.get(CONF_VOLUME_RESTORE, False))
            pause_playback = config_entry.options.get(CONF_PAUSE_PLAYBACK, config_entry.data.get(CONF_PAUSE_PLAYBACK, False))
            
            # Create parent entry data (only API config)
            parent_data = {
                CONF_API_KEY: config_entry.data[CONF_API_KEY],
                CONF_URL: config_entry.data.get(CONF_URL, DEFAULT_URL),
                UNIQUE_ID: config_entry.data.get(UNIQUE_ID, str(uuid.uuid4())),
            }
            
            # Create default subentry data from the legacy configuration
            # Use the original unique ID to preserve entity ID
            original_unique_id = config_entry.data.get(UNIQUE_ID)
            if not original_unique_id:
                # If no unique ID, create one based on URL and model (same as legacy)
                original_unique_id = f"{config_entry.data.get(CONF_URL)}_{model}"
            
            # Use just the model name as profile name to preserve entity ID
            # This ensures tts.openai_tts_tts_1 instead of tts.openai_tts_default_tts_1
            profile_name = model
            
            subentry_data = {
                CONF_PROFILE_NAME: profile_name,  # Just use model name to preserve entity ID
                CONF_MODEL: model,
                CONF_VOICE: voice,
                CONF_SPEED: speed,
                CONF_CHIME_ENABLE: chime,
                CONF_CHIME_SOUND: chime_sound,
                CONF_NORMALIZE_AUDIO: normalize,
                UNIQUE_ID: original_unique_id,  # Preserve original unique ID
            }
            if instructions:
                subentry_data[CONF_INSTRUCTIONS] = instructions
            
            # Create the subentry first
            from types import MappingProxyType
            
            subentry = ConfigSubentry(
                data=MappingProxyType(subentry_data),
                subentry_type=SUBENTRY_TYPE_PROFILE,
                title=profile_name,  # Use profile name (which is just the model)
                unique_id=original_unique_id,
            )
            
            # Add the subentry to the parent
            hass.config_entries.async_add_subentry(config_entry, subentry)
            
            # Update the parent entry AFTER subentry is created
            # This ensures if subentry creation fails, migration won't be marked complete
            from urllib.parse import urlparse
            hostname = urlparse(parent_data.get(CONF_URL, DEFAULT_URL)).hostname
            
            hass.config_entries.async_update_entry(
                config_entry, 
                data=parent_data,
                options={},  # Clear options as they've moved to subentry
                title=f"OpenAI TTS ({hostname})",
                minor_version=1,
                version=2
            )
            
            _LOGGER.info("Successfully migrated legacy entry to parent+subentry structure")
            
            # Clear migration flag
            hass.data[DOMAIN].pop(f"{config_entry.entry_id}_migrating", None)
            
            # Fix device registry associations after migration
            # Devices should only be associated with subentries, not parent entries
            device_reg = dr.async_get(hass)
            entity_reg = er.async_get(hass)
            
            # Find all entities for this unique_id
            entities = [
                entity for entity in entity_reg.entities.values()
                if entity.unique_id == original_unique_id and entity.platform == DOMAIN
            ]
            
            # Update device associations to only reference the subentry
            for entity in entities:
                if entity.device_id:
                    device = device_reg.async_get(entity.device_id)
                    if device and config_entry.entry_id in device.config_entries:
                        # Remove parent association and ensure only subentry is associated
                        _LOGGER.debug("Updating device %s associations after migration", device.id)
                        device_reg.async_update_device(
                            device.id,
                            remove_config_entry_id=config_entry.entry_id
                        )
            
            # Don't schedule reload - let Home Assistant handle it
            # The entry will be reloaded automatically after migration
            
            return True
        else:
            # Not a legacy entry, just update version
            hass.config_entries.async_update_entry(config_entry, minor_version=1)

    _LOGGER.debug("Migration to configuration version %s.%s successful", config_entry.version, config_entry.minor_version)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OpenAI TTS and register the openai_tts.say service."""
    _LOGGER.debug("async_setup_entry called for %s (version %s.%s)", 
                 entry.entry_id, entry.version, entry.minor_version)
    # Store entry for reference
    hass.data.setdefault(DOMAIN, {})
    
    # Migration is now handled during async_migrate_entry, no need for pending migration
    
    # Determine entry type clearly
    is_subentry = (
        hasattr(entry, 'subentry_type') and entry.subentry_type == SUBENTRY_TYPE_PROFILE
    ) or (
        hasattr(entry, 'parent_entry_id') and entry.parent_entry_id is not None
    ) or (
        entry.data.get(CONF_PROFILE_NAME) is not None
    )
    
    # Check if this entry has subentries (making it a parent)
    has_subentries = hasattr(entry, 'subentries') and entry.subentries
    _LOGGER.debug("Entry %s has_subentries=%s (count=%s)", 
                 entry.entry_id, has_subentries, 
                 len(entry.subentries) if has_subentries else 0)
    
    # Legacy entries have model/voice data directly AND no subentries AND version < 2.1
    # After migration, entries with model/voice data are converted to parent+subentry
    is_legacy_entry = (
        not is_subentry and 
        not has_subentries and
        (entry.data.get(CONF_MODEL) or entry.data.get(CONF_VOICE)) and
        (entry.version < 2 or (entry.version == 2 and entry.minor_version < 1))
    )
    
    # Modern parent entries either:
    # 1. Have no model/voice data (pure parent)
    # 2. Have model/voice data BUT also have subentries (hybrid parent)
    is_modern_parent = not is_subentry and (not is_legacy_entry or has_subentries)
    
    _LOGGER.info(
        "Setting up entry: %s (title: %s, type: %s)", 
        entry.entry_id, 
        entry.title,
        "subentry" if is_subentry else "legacy" if is_legacy_entry else "modern_parent"
    )
    
    # Debug logging for subentry detection
    if hasattr(entry, 'subentry_type'):
        _LOGGER.debug("Entry has subentry_type: %s", entry.subentry_type)
    if hasattr(entry, 'parent_entry_id'):
        _LOGGER.debug("Entry has parent_entry_id: %s", entry.parent_entry_id)
    if entry.data.get(CONF_PROFILE_NAME):
        _LOGGER.debug("Entry has profile_name: %s", entry.data.get(CONF_PROFILE_NAME))
    if has_subentries:
        _LOGGER.debug("Entry has %d subentries", len(entry.subentries))
    
    # Store entry reference
    hass.data[DOMAIN][entry.entry_id] = entry
    
    # Forward to platforms based on entry type
    if is_subentry:
        # Subentries are handled by the parent's platform setup
        # Don't forward them individually
        _LOGGER.debug("Subentry detected, skipping platform forward")
    else:
        # Both legacy entries and modern parents need platform setup
        # Legacy entries create entities directly
        # Modern parents will have their subentries processed by the platform
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        
        if is_modern_parent:
            # Store as main entry for subentries to find
            hass.data[DOMAIN]["main_entry"] = entry
            _LOGGER.info("Modern parent entry forwarded to platforms (will process subentries)")
    
    # Setup update listener following official Home Assistant patterns
    async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Handle config entry updates."""
        # Don't reload during migration - migration handles its own reload
        if hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_migrating"):
            _LOGGER.debug("Skipping reload during migration for entry %s", entry.entry_id)
            return
        
        # Check if Home Assistant is still starting up
        # This prevents phantom reloads during startup
        if not hass.is_running:
            _LOGGER.debug("Skipping reload during Home Assistant startup for entry %s", entry.entry_id)
            return
        
        _LOGGER.info("Config entry updated for OpenAI TTS entry %s, reloading", entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
    
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Only register the service once - check if we should do it for this entry
    should_register_service = False
    
    # Register service for:
    # 1. First legacy entry encountered
    # 2. Main entry (new style)
    # But NOT for subentries
    if not is_subentry and not hass.services.has_service(DOMAIN, SERVICE_NAME):
        # Check if this is truly the first setup or a main entry
        all_entries = hass.config_entries.async_entries(DOMAIN)
        main_entries = [e for e in all_entries if not hasattr(e, 'parent_entry_id') or e.parent_entry_id is None]
        
        # Register if this is the first main/legacy entry being set up
        if main_entries and entry.entry_id == main_entries[0].entry_id:
            should_register_service = True
            _LOGGER.debug("This is the first main/legacy entry, will register service")
    
    _LOGGER.debug("Service registration check - is_subentry: %s, is_legacy: %s, has_service: %s, should_register: %s", 
                 is_subentry, is_legacy_entry, hass.services.has_service(DOMAIN, SERVICE_NAME), should_register_service)
    
    if should_register_service:
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
            tts_state = hass.states.get(tts_entity)
            if not tts_state:
                raise ValueError(f"TTS entity {tts_entity} not found")

            # Get TTS entity's default options from its config
            # Look up the entity to get its default_options property
            entity_defaults = {}
            entity_reg = er.async_get(hass)
            entity_entry = entity_reg.async_get(tts_entity)
            if entity_entry and entity_entry.config_subentry_id:
                # This is a subentry-based entity - find the parent and subentry
                for entry in hass.config_entries.async_entries(DOMAIN):
                    if hasattr(entry, 'subentries') and entry.subentries:
                        for subentry_id, subentry in entry.subentries.items():
                            if subentry_id == entity_entry.config_subentry_id:
                                entity_defaults = {
                                    "chime": subentry.data.get(CONF_CHIME_ENABLE, False),
                                    "chime_sound": subentry.data.get(CONF_CHIME_SOUND, "threetone.mp3"),
                                    "normalize_audio": subentry.data.get(CONF_NORMALIZE_AUDIO, False),
                                }
                                _LOGGER.debug("Found entity defaults from subentry: %s", entity_defaults)
                                break
            elif entity_entry and entity_entry.config_entry_id:
                # Legacy entry - get from config entry options
                config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
                if config_entry:
                    entity_defaults = {
                        "chime": config_entry.options.get(CONF_CHIME_ENABLE, config_entry.data.get(CONF_CHIME_ENABLE, False)),
                        "chime_sound": config_entry.options.get(CONF_CHIME_SOUND, config_entry.data.get(CONF_CHIME_SOUND, "threetone.mp3")),
                        "normalize_audio": config_entry.options.get(CONF_NORMALIZE_AUDIO, config_entry.data.get(CONF_NORMALIZE_AUDIO, False)),
                    }
                    _LOGGER.debug("Found entity defaults from config entry: %s", entity_defaults)

            # Get service data - use entity defaults for options not explicitly set
            message = data["message"]
            language = data.get("language", "en")

            # For chime/normalize_audio: use service call value if provided, else entity default
            # Note: data.get("chime") returns None if not in call, False if explicitly set to False
            chime_value = data.get("chime") if "chime" in data else entity_defaults.get("chime", False)
            normalize_value = data.get("normalize_audio") if "normalize_audio" in data else entity_defaults.get("normalize_audio", False)
            chime_sound_value = data.get("chime_sound") if "chime_sound" in data else entity_defaults.get("chime_sound")

            options = {
                "voice": data.get("voice"),
                "speed": data.get("speed"),
                "instructions": data.get("instructions"),
                "chime": chime_value,
                "chime_sound": chime_sound_value,
                "normalize_audio": normalize_value,
            }

            # Remove None values
            options = {k: v for k, v in options.items() if v is not None}
            
            tts_volume = data.get("volume")
            pause_playback = data.get("pause_playback")  # Get pause_playback from service call
            
            _LOGGER.debug(
                "Calling announce with: tts_entity=%s, media_players=%s, message=%s, pause_playback=%s",
                tts_entity, media_players, message, pause_playback
            )
            
            # Call our helper with pause_playback parameter
            await announce(
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
    # Check if this is a subentry - use same logic as setup
    is_subentry = False
    
    # Method 1: Check subentry_type attribute
    if hasattr(entry, 'subentry_type') and entry.subentry_type == SUBENTRY_TYPE_PROFILE:
        is_subentry = True
    
    # Method 2: Check if entry has parent_entry_id (for older HA versions)
    elif hasattr(entry, 'parent_entry_id') and entry.parent_entry_id is not None:
        is_subentry = True
    
    # Method 3: Check if data contains profile_name (our subentry marker)
    elif entry.data.get(CONF_PROFILE_NAME) is not None:
        is_subentry = True
    
    # Check if this entry has subentries (making it a parent)
    has_subentries = hasattr(entry, 'subentries') and entry.subentries
    
    # Check if this is a legacy entry (has voice/model config but not a subentry AND no subentries AND version < 2.1)
    is_legacy_entry = False
    if (not is_subentry and not has_subentries and 
        (entry.data.get(CONF_MODEL) or entry.data.get(CONF_VOICE)) and
        (entry.version < 2 or (entry.version == 2 and entry.minor_version < 1))):
        is_legacy_entry = True
    
    # Unload platforms first
    unload_ok = True
    if not is_subentry:
        # Unload platforms for both legacy entries and modern parents
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # Only remove service when unloading main entries or legacy entries (not subentries)
    if not is_subentry and hass.services.has_service(DOMAIN, SERVICE_NAME):
        # Check if there are other main/legacy entries that still need the service
        other_entries = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id and 
            not (hasattr(e, 'subentry_type') and e.subentry_type == SUBENTRY_TYPE_PROFILE)
        ]
        
        if not other_entries:
            hass.services.async_remove(DOMAIN, SERVICE_NAME)
            _LOGGER.info("OpenAI TTS service 'say' unregistered")
    
    # Remove stored entry
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN].pop(entry.entry_id)
    
    # If this is the main entry, check if we need to clean up
    if not is_subentry and not is_legacy_entry and DOMAIN in hass.data and "main_entry" in hass.data[DOMAIN]:
        hass.data[DOMAIN].pop("main_entry", None)
    
    return unload_ok