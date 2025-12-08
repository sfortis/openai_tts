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
from typing import AsyncGenerator, Any

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
)
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
    MESSAGE_DURATIONS_KEY,
    CONF_PROFILE_NAME,
    SUPPORTED_LANGUAGES,
)

SUBENTRY_TYPE_PROFILE = "profile"
from .openaitts_engine import OpenAITTSEngine, StreamingAudioResponse
from .utils import get_media_duration, process_audio
from homeassistant.exceptions import MaxLengthExceeded

_LOGGER = logging.getLogger(__name__)

# Metadata key for duration stored in MP3 ID3 tags
DURATION_METADATA_KEY = "tts_duration_ms"


def embed_duration_in_audio(audio_data: bytes, duration_ms: int) -> bytes:
    """Embed duration metadata in MP3 audio using mutagen.

    This stores the duration in ID3 TXXX (user-defined text) frame,
    allowing it to be read back from HA's cached audio files.
    """
    import tempfile

    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import TXXX
    except ImportError:
        _LOGGER.warning("mutagen not available, skipping metadata embedding")
        return audio_data

    # Write audio to temp file
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio_data)
        tmp_path = f.name

    try:
        # Open and add ID3 tags
        try:
            audio = MP3(tmp_path)
        except Exception as e:
            _LOGGER.debug("Failed to open MP3 for metadata: %s", e)
            return audio_data

        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                # Tags might already exist in a different format
                pass

        if audio.tags is not None:
            # Remove existing duration tag if present
            audio.tags.delall(f"TXXX:{DURATION_METADATA_KEY}")
            # Add new duration tag
            audio.tags.add(TXXX(encoding=3, desc=DURATION_METADATA_KEY, text=str(duration_ms)))
            audio.save()
            _LOGGER.debug("Embedded duration %d ms in audio metadata", duration_ms)

        # Read back the modified file
        with open(tmp_path, "rb") as f:
            return f.read()

    except Exception as e:
        _LOGGER.warning("Failed to embed metadata: %s", e)
        return audio_data
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def read_duration_from_audio(audio_data: bytes) -> int | None:
    """Read duration metadata from MP3 audio using mutagen.

    Returns duration in milliseconds, or None if not found.
    """
    import tempfile

    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import TXXX
    except ImportError:
        return None

    # Write audio to temp file
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio_data)
        tmp_path = f.name

    try:
        audio = MP3(tmp_path)
        if audio.tags is None:
            return None

        # Look for our custom TXXX frame
        for tag in audio.tags.values():
            if isinstance(tag, TXXX) and tag.desc == DURATION_METADATA_KEY:
                try:
                    duration_ms = int(tag.text[0])
                    _LOGGER.debug("Read duration %d ms from audio metadata", duration_ms)
                    return duration_ms
                except (ValueError, IndexError):
                    pass

        return None

    except Exception as e:
        _LOGGER.debug("Failed to read metadata: %s", e)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

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
                    _LOGGER.debug("Found %d existing entities with unique_id %s, will be replaced", 
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

        # Cache for message-to-duration mapping (for cached audio)
        self._message_duration_cache = {}  # message_hash -> duration_ms
        self._max_cache_entries = 100  # Keep last 100 messages to match HA's TTS cache
        
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
            loop = asyncio.get_running_loop()
            duration_seconds = await loop.run_in_executor(None, get_media_duration, tmp_path)
            duration_ms = int(duration_seconds * 1000)
            return duration_ms
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _get_message_hash(self, message: str) -> str:
        """Get a hash for a message to use as cache key."""
        import hashlib
        # Create a simple hash of the message for lookup
        return hashlib.md5(message.encode()).hexdigest()[:16]

    def _store_message_duration(self, message: str, duration_ms: int) -> None:
        """Store duration for a message in the local and shared cache."""
        msg_hash = self._get_message_hash(message)
        self._message_duration_cache[msg_hash] = duration_ms

        # Limit cache size
        if len(self._message_duration_cache) > self._max_cache_entries:
            # Remove oldest entries (first in dict)
            oldest_keys = list(self._message_duration_cache.keys())[:-self._max_cache_entries]
            for key in oldest_keys:
                del self._message_duration_cache[key]

        # Also store in hass.data shared cache for volume_restore to access
        self._store_in_shared_cache(msg_hash, duration_ms)

        _LOGGER.debug("Stored duration %d ms for message hash %s", duration_ms, msg_hash)

    def _store_in_shared_cache(self, msg_hash: str, duration_ms: int) -> None:
        """Store duration in hass.data shared cache for cross-component access."""
        if DOMAIN not in self.hass.data:
            self.hass.data[DOMAIN] = {}
        if MESSAGE_DURATIONS_KEY not in self.hass.data[DOMAIN]:
            self.hass.data[DOMAIN][MESSAGE_DURATIONS_KEY] = {}

        # Store duration keyed by message hash
        self.hass.data[DOMAIN][MESSAGE_DURATIONS_KEY][msg_hash] = {
            'duration_ms': duration_ms,
            'timestamp': asyncio.get_running_loop().time(),
            'entity_id': self.entity_id,
        }

        # Limit shared cache size (keep last 50 messages)
        cache = self.hass.data[DOMAIN][MESSAGE_DURATIONS_KEY]
        if len(cache) > 50:
            # Remove oldest entries by timestamp
            sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get('timestamp', 0))
            for key in sorted_keys[:-50]:
                del cache[key]

        _LOGGER.info("Stored duration in shared cache: %d ms for hash %s", duration_ms, msg_hash)

    def get_duration_for_message(self, message: str) -> int | None:
        """Get cached duration for a message."""
        msg_hash = self._get_message_hash(message)
        duration = self._message_duration_cache.get(msg_hash)
        if duration:
            _LOGGER.debug("Found cached duration %d ms for message hash %s", duration, msg_hash)
        return duration

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        attrs = {}
        if hasattr(self, '_last_duration_ms'):
            attrs['media_duration'] = self._last_duration_ms  # Keep in milliseconds for volume_restore
        if hasattr(self, '_engine_active'):
            attrs['engine_active'] = self._engine_active
        # Include message duration cache for debugging
        if hasattr(self, '_message_duration_cache'):
            attrs['message_cache_size'] = len(self._message_duration_cache)
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
        return SUPPORTED_LANGUAGES

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
    def default_options(self) -> dict[str, Any]:
        """Return default options for TTS.

        This is critical for HA's TTS cache - options are part of the cache key.
        Without this, voice/model changes wouldn't invalidate cached audio.
        """
        return {
            CONF_VOICE: self._get_config_value(CONF_VOICE) or self._engine._voice,
            CONF_MODEL: self._get_config_value(CONF_MODEL) or self._engine._model,
            CONF_SPEED: self._get_config_value(CONF_SPEED) or self._engine._speed,
            CONF_CHIME_ENABLE: self._get_config_value(CONF_CHIME_ENABLE, False),
            CONF_CHIME_SOUND: self._get_config_value(CONF_CHIME_SOUND, "threetone.mp3"),
            CONF_NORMALIZE_AUDIO: self._get_config_value(CONF_NORMALIZE_AUDIO, False),
        }

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
            # For subentries, use the subentry's unique ID to create a separate device
            # This ensures the device is only associated with the subentry, not the parent
            device_unique_id = self._config.data.get(UNIQUE_ID)
            if not device_unique_id:
                # Fallback: generate based on profile name
                device_unique_id = f"{self._config.data.get(CONF_PROFILE_NAME, 'profile')}_{self._config.data.get(CONF_MODEL, 'tts-1')}"
        else:
            device_unique_id = self._config.data.get(UNIQUE_ID)
        
        if not device_unique_id:
            # Fallback to URL-based unique ID
            device_unique_id = self._config.data.get(CONF_URL, "openai_tts")
        
        # Create device info
        device_info = {
            "identifiers": {(DOMAIN, device_unique_id)},
            "manufacturer": "OpenAI",
            "sw_version": "1.0",
        }
        
        # Customize device info based on entry type
        if is_subentry:
            # Get agent name (profile name), model, and voice
            agent_name = self._config.data.get(CONF_PROFILE_NAME, "default")
            model = self._config.data.get(CONF_MODEL, "tts-1")
            voice = self._config.data.get(CONF_VOICE, "unknown")
            # Format: "agentname (model-voice)"
            device_info["name"] = f"{agent_name} ({model}-{voice})"
            device_info["model"] = f"{model} ({voice})"
        else:
            device_info["name"] = "OpenAI TTS"
            device_info["model"] = self._config.data.get(CONF_MODEL, "TTS API")
        
        return device_info

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
                # Restore last duration
                if 'last_duration_ms' in stored:
                    self._last_duration_ms = stored['last_duration_ms']
                    _LOGGER.debug("Restored last duration: %d ms", self._last_duration_ms)
                    # Update state immediately so it's available
                    self.async_write_ha_state()

                # Restore message duration cache
                if 'message_duration_cache' in stored:
                    self._message_duration_cache = stored['message_duration_cache']
                    _LOGGER.info("Restored %d message durations from storage", len(self._message_duration_cache))

                    # Also populate the shared hass.data cache
                    if DOMAIN not in self.hass.data:
                        self.hass.data[DOMAIN] = {}
                    if MESSAGE_DURATIONS_KEY not in self.hass.data[DOMAIN]:
                        self.hass.data[DOMAIN][MESSAGE_DURATIONS_KEY] = {}

                    for msg_hash, duration_ms in self._message_duration_cache.items():
                        self.hass.data[DOMAIN][MESSAGE_DURATIONS_KEY][msg_hash] = {
                            'duration_ms': duration_ms,
                            'timestamp': 0,  # Old timestamp, but still valid
                            'entity_id': self.entity_id,
                        }
                    _LOGGER.info("Populated shared cache with %d message durations", len(self._message_duration_cache))
        except Exception as e:
            _LOGGER.error("Failed to restore persisted state: %s", e)
    
    async def _save_persisted_state(self) -> None:
        """Save state data for persistence across restarts."""
        try:
            # Prepare data to save
            data = {
                'last_duration_ms': self._last_duration_ms,
                'last_updated': datetime.now().isoformat(),
                'message_duration_cache': self._message_duration_cache,
            }
            await self._store.async_save(data)
            _LOGGER.debug("Saved TTS state with %d cached message durations", len(self._message_duration_cache))
        except Exception as e:
            _LOGGER.error("Failed to save persisted state: %s", e)
    
    async def async_will_remove_from_hass(self) -> None:
        """Handle entity being removed from hass."""
        _LOGGER.debug("TTS entity %s being removed from hass", self.entity_id)
        # Save state before removal
        await self._save_persisted_state()
        await super().async_will_remove_from_hass()

    def _can_use_streaming(self, text: str, options: dict) -> bool:
        """Determine if streaming should be used.

        Streaming is beneficial for:
        - Long text responses (>60 chars)
        - When no audio processing is needed
        - When lower latency is desired
        """
        # Don't stream if audio processing is needed
        if options.get(CONF_CHIME_ENABLE) or options.get(CONF_NORMALIZE_AUDIO):
            _LOGGER.debug("Streaming disabled: audio processing required")
            return False

        # Don't stream for very short messages
        if len(text) < 60:
            _LOGGER.debug("Streaming disabled: text too short (%d chars)", len(text))
            return False

        _LOGGER.debug("Streaming enabled for text with %d chars", len(text))
        return True

    async def async_stream_tts_audio(
        self,
        request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Generate streaming TTS audio from incoming message stream.

        This method is called by Home Assistant when streaming is desired,
        typically for long responses from language models.
        """
        _LOGGER.info("async_stream_tts_audio called for entity %s", self.entity_id)

        # Set engine active flag
        self._engine_active = True
        self.async_write_ha_state()

        try:
            # Step 1: Accumulate text from the message generator
            # OpenAI API doesn't support incremental text input, so we need to collect it all
            full_text = ""
            chunk_count = 0
            async for text_chunk in request.message_gen:
                full_text += text_chunk
                chunk_count += 1
                _LOGGER.debug("Received text chunk %d: %s...",
                            chunk_count, text_chunk[:50] if len(text_chunk) > 50 else text_chunk)

            _LOGGER.info("Accumulated %d text chunks, total length: %d chars",
                        chunk_count, len(full_text))

            # Step 2: Extract options and configuration
            options = request.options or {}

            # Apply configuration defaults
            voice = options.get(CONF_VOICE) or self._get_config_value(CONF_VOICE) or self._engine._voice
            model = options.get(CONF_MODEL) or self._get_config_value(CONF_MODEL) or self._engine._model

            # Speed needs special handling
            speed = options.get(CONF_SPEED)
            if speed is None:
                speed = self._get_config_value(CONF_SPEED)
            if speed is None:
                speed = 1.0

            # Handle instructions
            service_instructions = options.get(CONF_INSTRUCTIONS)
            config_instructions = self._get_config_value(CONF_INSTRUCTIONS)
            instructions = service_instructions if service_instructions is not None else config_instructions

            # Step 3: Determine if we can use streaming
            can_stream = self._can_use_streaming(full_text, options)

            # Choose audio format - using mp3 for now as opus might have compatibility issues
            # TODO: Re-enable opus once streaming is working properly
            audio_format = "mp3"  # Was: "opus" if can_stream else "mp3"

            _LOGGER.info("Streaming TTS - voice: %s, model: %s, speed: %s, format: %s, streaming: %s",
                        voice, model, speed, audio_format, can_stream)

            # Step 4: Generate audio stream
            async def audio_generator() -> AsyncGenerator[bytes, None]:
                """Generate audio chunks."""
                try:
                    if can_stream:
                        # Use streaming for low latency
                        _LOGGER.debug("Using streaming mode with %s format", audio_format)

                        # Collect all chunks to calculate duration
                        all_chunks = []
                        async for chunk in self._engine.async_get_tts_stream(
                            text=full_text,
                            response_format=audio_format,
                            voice=voice,
                            model=model,
                            speed=speed,
                            instructions=instructions
                        ):
                            all_chunks.append(chunk)
                            yield chunk

                        # Streaming is complete - calculate duration from complete audio
                        if all_chunks:
                            complete_audio = b''.join(all_chunks)
                            total_bytes = len(complete_audio)
                            _LOGGER.info("Streaming completed, %d bytes total", total_bytes)

                            # Calculate duration from the complete audio
                            duration_ms = await self._get_audio_duration(complete_audio)
                            self._last_duration_ms = duration_ms
                            _LOGGER.info("Calculated streaming audio duration: %d ms", duration_ms)

                            # Store duration for this specific message
                            self._store_message_duration(full_text, duration_ms)

                            # Save to persistent storage for cache
                            await self._save_persisted_state()

                            # IMPORTANT: Clear engine active flag AFTER duration is set
                            # This ensures volume_restore sees the new duration
                            self._engine_active = False

                            # Single state update with both duration and engine_active=False
                            self.async_write_ha_state()

                            _LOGGER.info("Engine flag cleared with duration %d ms", duration_ms)

                            # Add metadata to the complete audio for caching
                            # This ensures cached files have duration info
                            # Note: This happens after streaming, so doesn't affect latency
                    else:
                        # Fall back to regular TTS for processed audio
                        _LOGGER.debug("Using non-streaming mode for audio processing")

                        # Get processed audio using the existing method
                        audio_data = await self._get_processed_audio_for_streaming(
                            full_text, request.language, options, voice, model, speed, instructions
                        )

                        # Calculate and store duration for non-streaming audio
                        duration_ms = await self._get_audio_duration(audio_data)
                        self._last_duration_ms = duration_ms
                        _LOGGER.info("Calculated non-streaming audio duration: %d ms", duration_ms)

                        # Store duration for this specific message
                        self._store_message_duration(full_text, duration_ms)

                        # Save to persistent storage
                        await self._save_persisted_state()

                        # Embed duration in audio metadata for HA cache
                        audio_data = await self.hass.async_add_executor_job(
                            embed_duration_in_audio, audio_data, duration_ms
                        )

                        # Yield in chunks for consistency
                        chunk_size = 8192
                        for i in range(0, len(audio_data), chunk_size):
                            yield audio_data[i:i + chunk_size]

                        # Non-streaming is complete - clear flag AFTER duration is set
                        self._engine_active = False
                        self.async_write_ha_state()
                        _LOGGER.info("Engine flag cleared with duration %d ms (non-streaming)", duration_ms)

                except Exception as e:
                    _LOGGER.error("Error during audio generation: %s", e, exc_info=True)
                    # Clear engine active flag on error
                    self._engine_active = False
                    self.async_write_ha_state()
                    raise

            # Return the streaming response
            return TTSAudioResponse(
                extension=audio_format,
                data_gen=audio_generator()
            )

        except Exception as e:
            _LOGGER.error("Error in async_stream_tts_audio: %s", e, exc_info=True)
            self._engine_active = False
            self.async_write_ha_state()
            raise

    async def _get_processed_audio_for_streaming(
        self,
        text: str,
        language: str,
        options: dict,
        voice: str,
        model: str,
        speed: float,
        instructions: str | None
    ) -> bytes:
        """Get processed audio for non-streaming cases.

        This handles audio processing like chimes and normalization.
        """
        # Audio processing options
        chime_enable = options.get(CONF_CHIME_ENABLE) or self._get_config_value(CONF_CHIME_ENABLE) or False
        chime_sound = options.get(CONF_CHIME_SOUND) or self._get_config_value(CONF_CHIME_SOUND)
        normalize_audio = options.get(CONF_NORMALIZE_AUDIO) or self._get_config_value(CONF_NORMALIZE_AUDIO) or False

        # Use the regular engine to get audio (non-streaming)
        loop = asyncio.get_running_loop()

        audio_task = loop.run_in_executor(
            None,
            partial(
                self._engine.get_tts,
                text,
                speed=speed,
                voice=voice,
                model=model,
                instructions=instructions,
                stream=False  # Don't use streaming for processed audio
            )
        )

        # Set a timeout for the TTS generation
        try:
            audio_response = await asyncio.wait_for(audio_task, timeout=30.0)
        except asyncio.TimeoutError:
            _LOGGER.error("TTS generation timed out after 30 seconds")
            raise

        if not audio_response or not audio_response.content:
            _LOGGER.error("No audio response received from TTS engine")
            raise ValueError("No audio data received")

        audio_data = audio_response.content

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

            # Process audio
            _, processed_audio, _ = await process_audio(
                self.hass,
                audio_data,
                chime_enabled=chime_enable,
                chime_path=chime_path,
                normalize_audio=normalize_audio
            )

            if processed_audio:
                audio_data = processed_audio
            else:
                _LOGGER.warning("Audio processing failed, using original audio")

        return audio_data

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
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
            loop = asyncio.get_running_loop()
            
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

            # Store duration in instance and shared cache
            self._last_duration_ms = total_duration_ms
            self._store_message_duration(message, total_duration_ms)
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
                    # Recalculate duration after processing and update cache
                    total_duration_ms = await self._get_audio_duration(audio_data)
                    self._last_duration_ms = total_duration_ms
                    self._store_message_duration(message, total_duration_ms)
                    self.async_write_ha_state()
                    await self._save_persisted_state()
                    _LOGGER.debug("Processed audio duration: %d ms", total_duration_ms)
                else:
                    _LOGGER.warning("Audio processing failed, using original audio")

            # Embed duration in MP3 metadata using mutagen (for HA cache retrieval)
            # This allows reading duration from cached audio files
            audio_with_metadata = await self.hass.async_add_executor_job(
                embed_duration_in_audio, audio_data, total_duration_ms
            )

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