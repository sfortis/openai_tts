# __init__.py
"""Custom integration for OpenAI TTS."""
from __future__ import annotations

import logging
import uuid

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .api_health import OpenAITTSHealthTracker
from .const import (
    DOMAIN,
    CONF_MODEL,
    CONF_VOICE,
    CONF_SPEED,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
    CONF_INSTRUCTIONS,
    CONF_PROFILE_NAME,
    CONF_ANNOUNCE_MODE,
    CONF_PAUSE_PLAYBACK,
    DEFAULT_URL,
    CONF_API_KEY,
    UNIQUE_ID,
    CONF_URL,
)
from .repairs import clear_repairs_for_entry
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [Platform.TTS, Platform.SENSOR]
SUBENTRY_TYPE_PROFILE = "profile"

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the ``say`` action, once, independently of any entry.

    Home Assistant calls this before any config entry is set up and
    never calls it again, which is what the action needs: it is
    addressed by entity id and resolves everything it touches through
    the registries, so it does not belong to a particular entry.
    """
    async_setup_services(hass)
    return True


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


def _seed_announce_mode_option(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give ``announce_mode`` a starting value on pre-existing entries.

    Announcement mode was introduced after ``pause_playback`` and
    defaults to on. Turning it on unconditionally would change the
    behaviour of installs where the user had deliberately left media
    management off, so a saved ``pause_playback`` value carries over as
    the initial ``announce_mode``. Entries that never saved either key
    fall through to the default at read time.

    Idempotent: once ``announce_mode`` is present the function does
    nothing, so it is safe to call on every setup.
    """
    if CONF_ANNOUNCE_MODE in entry.options:
        return
    if CONF_PAUSE_PLAYBACK not in entry.options:
        return

    carried = bool(entry.options[CONF_PAUSE_PLAYBACK])
    _LOGGER.info(
        "Seeding announce_mode=%s on entry %s from its saved "
        "pause_playback setting",
        carried, entry.entry_id,
    )
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_ANNOUNCE_MODE: carried},
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one config entry.

    Per-entry runtime state lives on the entry itself. Two things stay in
    ``hass.data``: the shared duration cache, which ``volume_restore``
    reads without holding an entry, and the migration flag, which is set
    before the entry is set up and therefore before it could carry
    anything.
    """
    _LOGGER.debug("async_setup_entry called for %s (version %s.%s)",
                 entry.entry_id, entry.version, entry.minor_version)
    hass.data.setdefault(DOMAIN, {})

    _seed_announce_mode_option(hass, entry)

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
    
    # Each parent entry owns one health tracker that the sensor and the
    # TTS entity share, carried on the entry itself. Subentries inherit
    # their parent's tracker and carry nothing of their own.
    if not is_subentry:
        entry.runtime_data = OpenAITTSHealthTracker(hass, entry)

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

        # Subentry reconfigures route through this listener too. Clear
        # any per-subentry Repairs issues (e.g. voice_deleted) raised
        # by an earlier failed TTS call - the next call recreates them
        # if the new configuration is still broken.
        clear_repairs_for_entry(hass, entry)

        _LOGGER.info("Config entry updated for OpenAI TTS entry %s, reloading", entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
    
    entry.async_on_unload(entry.add_update_listener(update_listener))


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
    
    # Whether the entry is legacy or a modern parent no longer changes
    # anything here: both unload the same platforms, and neither leaves
    # anything behind to clean up.

    # Unload platforms first
    unload_ok = True
    if not is_subentry:
        # Unload platforms for both legacy entries and modern parents
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # The action is registered in ``async_setup`` and stays registered.
    # Removing it here, as this used to, meant that reloading the only
    # entry withdrew the action for the length of the reload and an
    # automation firing in that window was told it did not exist.

    # Nothing to clear here any more. The health tracker lives on the
    # entry, which Home Assistant discards with it, and the shared
    # duration cache in ``hass.data`` is deliberately domain-wide:
    # ``volume_restore`` reads it without holding an entry.

    return unload_ok