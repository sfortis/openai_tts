"""
Setting up TTS entity with custom caching.
"""
from __future__ import annotations
import logging
import asyncio
import os
from functools import partial
from asyncio import CancelledError
from datetime import datetime

from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_SPEED,
    CONF_VOICE,
    CONF_INSTRUCTIONS,
    CONF_URL,
    DOMAIN,
    UNIQUE_ID,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
    VOICES,
)

CONF_PROFILE_NAME = "profile_name"
SUBENTRY_TYPE_PROFILE = "profile"
from .openaitts_engine import OpenAITTSEngine, StreamingAudioResponse
from .utils import get_media_duration, process_audio
# Custom cache removed - using HA's built-in cache with embedded metadata
from homeassistant.exceptions import MaxLengthExceeded

_LOGGER = logging.getLogger(__name__)

# Storage version and key
STORAGE_VERSION = 1
STORAGE_KEY = "openai_tts_state"

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenAI TTS entities from a config entry."""
    _LOGGER.debug("Setting up OpenAI TTS for config entry %s", config_entry.entry_id)
    
    # Get entity registry to check for existing entities
    entity_registry = er.async_get(hass)
    
    # Check if this is a legacy entry (has model/voice data AND version < 2.1)
    is_legacy = (
        (config_entry.data.get(CONF_MODEL) is not None or config_entry.data.get(CONF_VOICE) is not None) and
        (config_entry.version < 2 or (config_entry.version == 2 and config_entry.minor_version < 1))
    )
    has_subentries = hasattr(config_entry, 'subentries') and config_entry.subentries
    
    entities_added = []
    
    # Legacy entries (pre-migration) should create their own entity
    if is_legacy:
        _LOGGER.info("Creating TTS entity for legacy entry: %s", config_entry.title)
        
        api_key = config_entry.data.get(CONF_API_KEY)
        url = config_entry.data.get(CONF_URL)
        
        # Use options if available, otherwise fall back to data
        model = config_entry.options.get(CONF_MODEL, config_entry.data.get(CONF_MODEL))
        voice = config_entry.options.get(CONF_VOICE, config_entry.data.get(CONF_VOICE))
        speed = config_entry.options.get(CONF_SPEED, config_entry.data.get(CONF_SPEED, 1.0))
        
        _LOGGER.debug("Creating legacy entity with model=%s, voice=%s, speed=%s", 
                     model, voice, speed)
        
        engine = OpenAITTSEngine(api_key, voice, model, speed, url)
        entity = OpenAITTSEntity(hass, config_entry, engine)
        async_add_entities([entity])
        entities_added.append(entity)
    
    # Process subentries if they exist (for both modern parents AND legacy entries with subentries)
    if has_subentries:
        _LOGGER.info("Processing %d subentries for %s entry %s", 
                    len(config_entry.subentries), 
                    "legacy" if is_legacy else "parent",
                    config_entry.entry_id)
        
        entities = []
        for subentry_id, subentry in config_entry.subentries.items():
            # Only create entities for profile subentries
            if getattr(subentry, 'subentry_type', None) != SUBENTRY_TYPE_PROFILE:
                _LOGGER.debug("Skipping non-profile subentry: %s", subentry_id)
                continue
                
            _LOGGER.info("Creating TTS entity for subentry: %s (%s)", 
                        subentry.title, subentry_id)
            
            # Get API credentials from parent entry
            api_key = config_entry.data.get(CONF_API_KEY)
            url = config_entry.data.get(CONF_URL)
            
            # Get voice configuration from subentry
            model = subentry.data.get(CONF_MODEL, "tts-1")
            voice = subentry.data.get(CONF_VOICE, "shimmer")
            speed = subentry.data.get(CONF_SPEED, 1.0)
            
            _LOGGER.debug("Creating entity with model=%s, voice=%s, speed=%s", 
                         model, voice, speed)
            
            # Check if an entity with this unique_id already exists
            unique_id = subentry.data.get(UNIQUE_ID)
            if unique_id:
                # Look for existing entities with this unique_id
                existing_entities = [
                    entity_id for entity_id, entity in entity_registry.entities.items()
                    if entity.unique_id == unique_id and entity.platform == DOMAIN
                ]
                if existing_entities:
                    _LOGGER.warning("Found %d existing entities with unique_id %s, will be replaced", 
                                  len(existing_entities), unique_id)
            
            # Create engine and entity
            engine = OpenAITTSEngine(api_key, voice, model, speed, url)
            entity = OpenAITTSEntity(hass, subentry, engine, config_entry)
            entities.append((entity, subentry_id))
        
        # Add all entities with their associated subentry IDs
        for entity, subentry_id in entities:
            async_add_entities([entity], config_subentry_id=subentry_id)
            entities_added.append(entity)
        
        if not entities and not is_legacy:
            _LOGGER.warning("No profile subentries found for parent entry %s", 
                          config_entry.entry_id)
        return
    
    # If no entities were added, log a message
    if not entities_added:
        if not is_legacy and not has_subentries:
            _LOGGER.info("Modern parent entry with no subentries - no entities created")
        else:
            _LOGGER.warning("No entities created for entry %s", config_entry.entry_id)


class OpenAITTSEntity(TextToSpeechEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, config: ConfigEntry, engine: OpenAITTSEngine, parent_entry: ConfigEntry = None) -> None:
        _LOGGER.debug("OpenAITTSEntity.__init__ called")
        self.hass = hass
        self._engine = engine
        self._config = config
        self._parent_entry = parent_entry  # Store parent entry reference if this is a subentry
        
        # Ensure unique_id is set and consistent
        self._attr_unique_id = config.data.get(UNIQUE_ID)
        if not self._attr_unique_id:
            # Generate a unique ID based on the configuration
            import hashlib
            config_str = f"{config.data.get(CONF_URL)}_{config.data.get(CONF_MODEL)}_{config.data.get(CONF_VOICE)}"
            self._attr_unique_id = hashlib.md5(config_str.encode()).hexdigest()
        
        _LOGGER.debug("Entity initialized with unique_id: %s", self._attr_unique_id)
        
        # Set the config entry ID for proper entity registry association
        # For subentries, we need to use the subentry_id if available
        if hasattr(config, 'subentry_id'):
            self._attr_config_entry_id = config.subentry_id
            _LOGGER.debug("Entity %s associated with subentry_id: %s", self.entity_id, config.subentry_id)
        elif hasattr(config, 'entry_id'):
            self._attr_config_entry_id = config.entry_id
            _LOGGER.debug("Entity %s associated with entry_id: %s", self.entity_id, config.entry_id)
        else:
            self._attr_config_entry_id = parent_entry.entry_id if parent_entry else None
            _LOGGER.warning("Entity %s using parent entry_id: %s", self.entity_id, self._attr_config_entry_id)
        
        # Duration cache to track durations by message hash
        self._duration_cache = {}
        
        # Generate entity_id based on whether this is a profile or main entry
        # Check if this is a subentry (same logic as in async_setup_entry)
        is_subentry = (
            hasattr(config, 'subentry_type') and config.subentry_type == SUBENTRY_TYPE_PROFILE
        ) or (
            hasattr(config, 'parent_entry_id') and config.parent_entry_id is not None
        ) or (
            config.data.get(CONF_PROFILE_NAME) is not None
        )
        
        if is_subentry:
            # This is a profile subentry
            profile_name = config.data.get(CONF_PROFILE_NAME, "profile")
            # Sanitize profile name for entity_id
            safe_profile_name = profile_name.lower().replace(" ", "_").replace("-", "_")
            # Remove any non-alphanumeric characters (except underscore)
            safe_profile_name = ''.join(c for c in safe_profile_name if c.isalnum() or c == '_')
            self.entity_id = f"tts.openai_tts_{safe_profile_name}"
            self._attr_name = f"OpenAI TTS {profile_name}"
        else:
            # This is the main entry or legacy entry
            # For legacy entries with model in data, use model in entity_id to make them unique
            if config.data.get(CONF_MODEL):
                model_suffix = config.data.get(CONF_MODEL, "").replace("-", "_").replace(".", "_")
                # Don't add unique suffix - keep entity_id stable for service calls
                self.entity_id = f"tts.openai_tts_{model_suffix}"
                self._attr_name = f"OpenAI TTS ({config.data.get(CONF_MODEL)})"
            else:
                # New-style main entry
                self.entity_id = "tts.openai_tts"
                self._attr_name = "OpenAI TTS"
        
        # No custom cache needed - using HA's cache with embedded metadata
        
        # Initialize state flags
        self._engine_active = False
        self._last_duration_ms = None
        
        # Initialize storage for persistent state
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{self.entity_id}")
        self._stored_data = {}
        
        _LOGGER.debug("TTS entity initialized with ID: %s", self.entity_id)
        _LOGGER.info("OpenAI TTS entity created: %s (engine speed: %s)", self.entity_id, self._engine._speed)

    async def _get_audio_duration(self, audio_data: bytes) -> int:
        """Get duration of audio data in milliseconds."""
        import tempfile
        
        # Create a temporary file to calculate duration
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            tmp_file.write(audio_data)
            tmp_path = tmp_file.name
        
        try:
            loop = asyncio.get_event_loop()
            duration_seconds = await loop.run_in_executor(None, get_media_duration, tmp_path)
            duration_ms = int(duration_seconds * 1000)
            return duration_ms
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except:
                pass
    
    def _generate_cache_key(self, message: str, language: str, options: dict) -> str:
        """Generate a cache key for a TTS request."""
        import hashlib
        import json
        
        # Create a deterministic key from all parameters
        key_data = {
            "message": message,
            "language": language,
            "voice": options.get(CONF_VOICE, self._get_config_value(CONF_VOICE)),
            "model": options.get(CONF_MODEL, self._get_config_value(CONF_MODEL)),
            "speed": options.get(CONF_SPEED, self._get_config_value(CONF_SPEED)),
            "instructions": options.get(CONF_INSTRUCTIONS, self._get_config_value(CONF_INSTRUCTIONS)),
        }
        
        # Sort and stringify for consistent hashing
        key_string = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_string.encode()).hexdigest()
    
    async def _add_duration_metadata(self, audio_data: bytes, duration_ms: int) -> bytes:
        """Add duration metadata to MP3 audio data."""
        import tempfile
        import subprocess
        
        # Create temporary files
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as input_file:
            input_file.write(audio_data)
            input_path = input_file.name
        
        output_path = input_path + "_metadata.mp3"
        
        try:
            # Add metadata using ffmpeg
            cmd = [
                "ffmpeg",
                "-i", input_path,
                "-c:a", "copy",  # Copy audio codec, don't re-encode
                "-metadata", f"TXXX:tts_duration_ms={duration_ms}",
                "-metadata", "TXXX:cache_version=2.0",
                "-y",  # Overwrite output
                output_path
            ]
            
            # Run ffmpeg in executor
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            )
            
            # Read the output file
            audio_with_metadata = await self.hass.async_add_executor_job(
                lambda: open(output_path, "rb").read()
            )
            
            _LOGGER.debug("Added duration metadata (%d ms) to audio", duration_ms)
            return audio_with_metadata
            
        except Exception as e:
            _LOGGER.warning("Failed to add metadata to audio: %s. Returning original audio.", e)
            return audio_data
        finally:
            # Clean up temp files
            try:
                os.unlink(input_path)
                if os.path.exists(output_path):
                    os.unlink(output_path)
            except:
                pass
    
    def get_cached_duration(self, message: str, language: str, options: dict) -> int | None:
        """Get cached duration for a message."""
        cache_key = self._generate_cache_key(message, language, options)
        duration = self._duration_cache.get(cache_key)
        if duration:
            _LOGGER.debug("Found cached duration %d ms for key %s", duration, cache_key[:8])
        return duration

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        attrs = {}
        if hasattr(self, '_last_duration_ms'):
            attrs['media_duration'] = self._last_duration_ms  # Keep in milliseconds for volume_restore
        if hasattr(self, '_engine_active'):
            attrs['engine_active'] = self._engine_active
        # Include cache size for debugging
        if hasattr(self, '_duration_cache'):
            attrs['duration_cache_size'] = len(self._duration_cache)
        # Include available voices
        attrs['available_voices'] = VOICES
        # Include current configuration
        attrs['current_voice'] = self._get_config_value(CONF_VOICE) or self._engine._voice
        attrs['current_model'] = self._get_config_value(CONF_MODEL) or self._engine._model
        attrs['current_speed'] = self._get_config_value(CONF_SPEED) or self._engine._speed
        return attrs

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_languages(self) -> list[str]:
        return ["en"]

    @property
    def supported_options(self) -> list[str]:
        """Return list of supported options."""
        return [
            CONF_VOICE,
            CONF_MODEL,
            CONF_SPEED,
            CONF_CHIME_ENABLE,
            CONF_CHIME_SOUND,
            CONF_NORMALIZE_AUDIO,
            CONF_INSTRUCTIONS,
        ]

    @property
    def device_info(self):
        """Return device info for the entity."""
        # Check if this is a subentry (same comprehensive check)
        is_subentry = (
            hasattr(self._config, 'subentry_type') and self._config.subentry_type == SUBENTRY_TYPE_PROFILE
        ) or (
            hasattr(self._config, 'parent_entry_id') and self._config.parent_entry_id is not None
        ) or (
            self._config.data.get(CONF_PROFILE_NAME) is not None
        )
        
        # Get the unique ID for device grouping
        if is_subentry:
            # Try to get parent's unique ID for consistent device grouping
            if self._parent_entry:
                device_unique_id = self._parent_entry.data.get(UNIQUE_ID)
            elif hasattr(self._config, 'parent_entry') and self._config.parent_entry:
                device_unique_id = self._config.parent_entry.data.get(UNIQUE_ID)
            else:
                # Try to get from main entry in hass.data
                main_entry = self.hass.data.get(DOMAIN, {}).get("main_entry")
                if main_entry:
                    device_unique_id = main_entry.data.get(UNIQUE_ID)
                else:
                    device_unique_id = self._config.data.get(UNIQUE_ID)
        else:
            device_unique_id = self._config.data.get(UNIQUE_ID)
        
        if not device_unique_id:
            # Fallback to URL-based unique ID
            device_unique_id = self._config.data.get(CONF_URL, "openai_tts")
        
        return {
            "identifiers": {(DOMAIN, device_unique_id)},
            "name": "OpenAI TTS",
            "manufacturer": "OpenAI",
            "model": self._config.data.get(CONF_MODEL, "TTS API"),
            "sw_version": "1.0",
        }

    def _get_config_value(self, key: str, default=None):
        """Get config value from options or data, handling subentries."""
        # For subentries, options don't exist, only use data
        is_subentry = (
            hasattr(self._config, 'subentry_type') and self._config.subentry_type == SUBENTRY_TYPE_PROFILE
        ) or (
            hasattr(self._config, 'parent_entry_id') and self._config.parent_entry_id is not None
        ) or (
            self._config.data.get(CONF_PROFILE_NAME) is not None
        )
        
        if is_subentry:
            value = self._config.data.get(key, default)
            _LOGGER.debug("Getting config value for %s from subentry data: %s (entry_id: %s)", 
                         key, value, getattr(self._config, 'entry_id', getattr(self._config, 'subentry_id', 'unknown')))
            return value
        # For regular entries, check options first, then data
        if hasattr(self._config, 'options'):
            options_value = self._config.options.get(key)
            data_value = self._config.data.get(key)
            value = options_value if options_value is not None else data_value
            if value is None:
                value = default
            _LOGGER.debug("Getting config value for %s: options=%s, data=%s, final=%s (entry_id: %s)", 
                         key, options_value, data_value, value, getattr(self._config, 'entry_id', 'unknown'))
            return value
        return self._config.data.get(key, default)

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        
        # Restore persisted state
        await self._restore_persisted_state()
        
        # Check if entry has options set
        _LOGGER.debug("Entity added to hass. Config data: %s", self._config.data)
        if hasattr(self._config, 'options'):
            _LOGGER.debug("Entity added to hass. Config options: %s", self._config.options)
        
        # Log entity registration
        _LOGGER.info("TTS entity %s registered with Home Assistant", self.entity_id)
    
    async def _restore_persisted_state(self) -> None:
        """Restore persisted state data including duration cache."""
        try:
            stored = await self._store.async_load()
            if stored:
                self._stored_data = stored
                # Restore duration cache
                if 'duration_cache' in stored:
                    self._duration_cache = stored['duration_cache']
                    _LOGGER.info("Restored %d cached durations from persistent storage", len(self._duration_cache))
                # Restore last duration
                if 'last_duration_ms' in stored:
                    self._last_duration_ms = stored['last_duration_ms']
                    _LOGGER.debug("Restored last duration: %d ms", self._last_duration_ms)
                    # Update state immediately so it's available
                    self.async_write_ha_state()
        except Exception as e:
            _LOGGER.error("Failed to restore persisted state: %s", e)
    
    async def _save_persisted_state(self) -> None:
        """Save state data for persistence across restarts."""
        try:
            # Prepare data to save
            data = {
                'duration_cache': self._duration_cache,
                'last_duration_ms': self._last_duration_ms,
                'last_updated': datetime.now().isoformat(),
            }
            await self._store.async_save(data)
            _LOGGER.debug("Saved TTS state with %d cached durations", len(self._duration_cache))
        except Exception as e:
            _LOGGER.error("Failed to save persisted state: %s", e)
    
    async def async_will_remove_from_hass(self) -> None:
        """Handle entity being removed from hass."""
        _LOGGER.debug("TTS entity %s being removed from hass", self.entity_id)
        # Save state before removal
        await self._save_persisted_state()
        await super().async_will_remove_from_hass()

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, any] | None = None
    ) -> tuple[str | None, bytes | None]:
        _LOGGER.info("async_get_tts_audio called for entity %s with message: %s, language: %s, options: %s", 
                     self.entity_id, message[:50], language, options)
        _LOGGER.info("Engine state - voice: %s, model: %s, speed: %s", 
                     self._engine._voice, self._engine._model, self._engine._speed)
        _LOGGER.debug("Entity unique_id: %s, config id: %s", self._attr_unique_id, 
                     getattr(self._config, 'entry_id', getattr(self._config, 'subentry_id', 'unknown')))
        
        # Set engine active flag
        self._engine_active = True
        self.async_write_ha_state()
        
        if options is None:
            options = {}
        
        # Apply configuration defaults
        # First check options, then fall back to data
        voice = options.get(CONF_VOICE) or self._get_config_value(CONF_VOICE) or self._engine._voice
        model = options.get(CONF_MODEL) or self._get_config_value(CONF_MODEL) or self._engine._model
        
        # Speed needs special handling because 0.5 is a valid value but evaluates to falsy in some contexts
        speed = options.get(CONF_SPEED)
        if speed is None:
            speed = self._get_config_value(CONF_SPEED)
        if speed is None:
            speed = 1.0  # Default speed instead of engine's potentially outdated value
        
        # Debug logging to trace speed value
        _LOGGER.debug("Speed value tracing:")
        _LOGGER.debug("  - From options: %s", options.get(CONF_SPEED))
        _LOGGER.debug("  - From config (options/data): %s", self._get_config_value(CONF_SPEED))
        _LOGGER.debug("  - Final speed value: %s", speed)
        
        _LOGGER.debug("TTS parameters - voice: %s, model: %s, speed: %s", voice, model, speed)
        
        # Handle instructions - merge service-level with config-level
        service_instructions = options.get(CONF_INSTRUCTIONS)
        config_instructions = self._get_config_value(CONF_INSTRUCTIONS)
        
        _LOGGER.debug("Instructions - service: %s, config: %s", service_instructions, config_instructions)
        
        # If service provides instructions, use them; otherwise use config
        instructions = service_instructions if service_instructions is not None else config_instructions
        
        # Audio processing options
        chime_enable = options.get(CONF_CHIME_ENABLE) or self._get_config_value(CONF_CHIME_ENABLE) or False
        chime_sound = options.get(CONF_CHIME_SOUND) or self._get_config_value(CONF_CHIME_SOUND)
        normalize_audio = options.get(CONF_NORMALIZE_AUDIO) or self._get_config_value(CONF_NORMALIZE_AUDIO) or False
        
        # Note: HA handles caching - we just need to ensure metadata is embedded
        
        _LOGGER.info("TTS request - voice: %s, model: %s, speed: %s, instructions: %s, chime_enable: %s",
                     voice, model, speed, instructions, chime_enable)
        
        try:
            # No custom cache - Home Assistant handles caching
            
            # Generate new audio
            _LOGGER.debug("Generating new audio for message: %s", message[:50])
            
            # Determine if we can use streaming (no post-processing needed)
            can_stream = not chime_enable and not normalize_audio
            
            # Use the OpenAI engine to get audio
            loop = asyncio.get_event_loop()
            
            # Pass model parameter to the engine as well
            audio_task = loop.run_in_executor(
                None,
                partial(
                    self._engine.get_tts,
                    message,
                    speed=speed,
                    voice=voice,
                    model=model,  # Pass model parameter
                    instructions=instructions,
                    stream=can_stream
                )
            )
            
            # Set a timeout for the TTS generation
            try:
                audio_response = await asyncio.wait_for(audio_task, timeout=30.0)
            except asyncio.TimeoutError:
                _LOGGER.error("TTS generation timed out after 30 seconds")
                return (None, None)
            
            if not audio_response:
                _LOGGER.error("No audio response received from TTS engine")
                return (None, None)
            
            # Handle streaming vs regular response
            if hasattr(audio_response, 'read_all'):
                # Streaming response
                _LOGGER.debug("Using streaming response")
                audio_data = audio_response.read_all()
            else:
                # Regular response
                if not audio_response.content:
                    _LOGGER.error("No audio data in response")
                    return (None, None)
                audio_data = audio_response.content
            
            # Calculate duration before any processing
            total_duration_ms = await self._get_audio_duration(audio_data)
            _LOGGER.debug("Generated audio duration: %d ms", total_duration_ms)
            
            # Store duration in instance for volume_restore to access
            self._last_duration_ms = total_duration_ms
            
            # Store duration in cache by message key
            cache_key = self._generate_cache_key(message, language, options)
            self._duration_cache[cache_key] = total_duration_ms
            _LOGGER.debug("Stored duration %d ms for cache key %s", total_duration_ms, cache_key[:8])
            
            # Update state so volume_restore can see it
            self.async_write_ha_state()
            
            # Save state to persistent storage
            await self._save_persisted_state()
            
            # Process audio if needed (chime, normalization)
            if chime_enable or normalize_audio:
                _LOGGER.debug("Processing audio with chime=%s, normalize=%s", chime_enable, normalize_audio)
                
                # Get chime file path
                chime_path = None
                if chime_enable and chime_sound:
                    chime_folder = os.path.join(os.path.dirname(__file__), "chime")
                    chime_path = os.path.join(chime_folder, chime_sound)
                    if not os.path.exists(chime_path):
                        _LOGGER.warning("Chime file not found: %s", chime_path)
                        chime_path = None
                
                # Process audio (it's already async)
                _, processed_audio, _ = await process_audio(
                    self.hass,
                    audio_data,
                    chime_enabled=chime_enable,
                    chime_path=chime_path,
                    normalize_audio=normalize_audio
                )
                
                if processed_audio:
                    audio_data = processed_audio
                    # Recalculate duration after processing
                    total_duration_ms = await self._get_audio_duration(audio_data)
                    self._last_duration_ms = total_duration_ms
                    _LOGGER.debug("Processed audio duration: %d ms", total_duration_ms)
                    # Update state with new duration
                    self.async_write_ha_state()
                    # Save to persistent storage
                    await self._save_persisted_state()
                else:
                    _LOGGER.warning("Audio processing failed, using original audio")
            
            # Add duration metadata to the MP3 before returning
            audio_with_metadata = await self._add_duration_metadata(audio_data, total_duration_ms)
            
            # Clear engine active flag before returning
            self._engine_active = False
            self.async_write_ha_state()
            
            return ("mp3", audio_with_metadata)
            
        except MaxLengthExceeded as err:
            _LOGGER.error("Maximum message length exceeded: %s", err)
            self._engine_active = False
            self.async_write_ha_state()
            raise
        except CancelledError:
            _LOGGER.debug("TTS generation was cancelled")
            self._engine_active = False
            self.async_write_ha_state()
            raise
        except Exception as err:
            _LOGGER.error("Error generating TTS: %s", err, exc_info=True)
            self._engine_active = False
            self.async_write_ha_state()
            return (None, None)