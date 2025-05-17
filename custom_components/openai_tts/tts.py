"""
Setting up TTS entity.
"""
from __future__ import annotations
import logging
import asyncio
from functools import partial
from asyncio import CancelledError

from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.restore_state import RestoreEntity

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
)
from .openaitts_engine import OpenAITTSEngine
from .utils import get_media_duration, process_audio
from homeassistant.exceptions import MaxLengthExceeded

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    api_key = config_entry.data.get(CONF_API_KEY)
    # Use options if available, otherwise fall back to the original data.
    model = config_entry.options.get(CONF_MODEL, config_entry.data.get(CONF_MODEL))
    voice = config_entry.options.get(CONF_VOICE, config_entry.data.get(CONF_VOICE))
    speed = config_entry.options.get(CONF_SPEED, config_entry.data.get(CONF_SPEED, 1.0))
    url = config_entry.data.get(CONF_URL)
    engine = OpenAITTSEngine(
        api_key,
        voice,
        model,
        speed,
        url,
    )
    async_add_entities([OpenAITTSEntity(hass, config_entry, engine)])


class OpenAITTSEntity(TextToSpeechEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, config: ConfigEntry, engine: OpenAITTSEngine) -> None:
        self.hass = hass
        self._engine = engine
        self._config = config
        self._attr_unique_id = config.data.get(UNIQUE_ID)
        if not self._attr_unique_id:
            self._attr_unique_id = f"{config.data.get(CONF_URL)}_{config.data.get(CONF_MODEL)}"
        base_name = self._config.data.get(CONF_MODEL, "").upper()
        self.entity_id = generate_entity_id("tts.openai_tts_{}", base_name.lower(), hass=hass)
        # New flags and timing variables.
        self._engine_active = False
        self._last_api_time = None
        self._last_ffmpeg_time = None
        self._last_total_time = None
        self._last_media_duration_ms = None  # Store in milliseconds

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass, restore previous state."""
        await super().async_added_to_hass()
        
        # Restore previous state if it exists
        last_state = await self.async_get_last_state()
        
        if last_state is not None and last_state.attributes:
            # Restore from attributes
            self._engine_active = last_state.attributes.get("engine_active", False)
            
            # Restore time values
            api_time_str = last_state.attributes.get("last_api_time")
            if api_time_str and " msec" in api_time_str:
                self._last_api_time = int(api_time_str.replace(" msec", ""))
            
            ffmpeg_time_str = last_state.attributes.get("last_ffmpeg_time")
            if ffmpeg_time_str and " msec" in ffmpeg_time_str:
                self._last_ffmpeg_time = int(ffmpeg_time_str.replace(" msec", ""))
            
            total_time_str = last_state.attributes.get("last_total_time")
            if total_time_str and " msec" in total_time_str:
                self._last_total_time = int(total_time_str.replace(" msec", ""))
            
            # Restore media duration directly (stored as raw milliseconds)
            self._last_media_duration_ms = last_state.attributes.get("media_duration")
            
            _LOGGER.debug(
                "Restored OpenAI TTS entity state: api_time=%s, ffmpeg_time=%s, total_time=%s, media_duration=%s", 
                self._last_api_time, 
                self._last_ffmpeg_time, 
                self._last_total_time,
                self._last_media_duration_ms
            )

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_options(self) -> list:
        return ["instructions", "chime", "normalize_audio", "chime_sound"]

    @property
    def supported_languages(self) -> list:
        return self._engine.get_supported_langs()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "model": self._config.data.get(CONF_MODEL),
            "manufacturer": "OpenAI",
        }

    @property
    def name(self) -> str:
        return self._config.data.get(CONF_MODEL, "").upper()

    @property
    def extra_state_attributes(self) -> dict:
        # Retrieve configured values from options or data.
        model = self._config.data.get(CONF_MODEL)
        voice = self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE))
        speed = self._config.options.get(CONF_SPEED, self._config.data.get(CONF_SPEED, 1.0))
        chime = self._config.options.get(CONF_CHIME_ENABLE, self._config.data.get(CONF_CHIME_ENABLE, False))
        normalization = self._config.options.get(CONF_NORMALIZE_AUDIO, self._config.data.get(CONF_NORMALIZE_AUDIO, False))
        
        return {
            "engine_active": self._engine_active,
            "last_api_time": f"{int(self._last_api_time)} msec" if self._last_api_time is not None else None,
            "last_ffmpeg_time": f"{int(self._last_ffmpeg_time)} msec" if self._last_ffmpeg_time is not None else None,
            "last_total_time": f"{int(self._last_total_time)} msec" if self._last_total_time is not None else None,
            "media_duration": self._last_media_duration_ms,  # Raw milliseconds value
            "model": model,
            "voice": voice,
            "speed": speed,
            "chime_enabled": chime,
            "normalization_enabled": normalization,
        }
    
    def get_tts_audio(
        self, message: str, language: str, options: dict | None = None
    ) -> tuple[str, bytes] | tuple[None, None]:
        """Generate TTS audio from message with optional processing."""
        import time
        import os
        
        overall_start = time.monotonic()

        _LOGGER.debug(" -------------------------------------------")
        _LOGGER.debug("|  OpenAI TTS                               |")
        _LOGGER.debug("|  https://github.com/sfortis/openai_tts    |")
        _LOGGER.debug(" -------------------------------------------")

        try:
            if len(message) > 4096:
                raise MaxLengthExceeded("Message exceeds maximum allowed length")
                
            # Ensure options is not None
            if options is None:
                options = {}
                
            # Get effective settings with proper cascade
            current_speed = self._config.options.get(CONF_SPEED, self._config.data.get(CONF_SPEED, 1.0))
            effective_voice = self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE))
            
            # Instructions - checks runtime options first
            instructions = options.get(
                CONF_INSTRUCTIONS, 
                self._config.options.get(CONF_INSTRUCTIONS, self._config.data.get(CONF_INSTRUCTIONS))
            )
            
            _LOGGER.debug("Effective speed: %s", current_speed)
            _LOGGER.debug("Effective voice: %s", effective_voice)
            _LOGGER.debug("Instructions: %s", instructions)

            # Generate TTS audio via API
            _LOGGER.debug("Creating TTS API request")
            api_start = time.monotonic()
            speech = self._engine.get_tts(
                message, 
                speed=current_speed, 
                voice=effective_voice, 
                instructions=instructions
            )
            self._last_api_time = (time.monotonic() - api_start) * 1000
            _LOGGER.debug("TTS API call completed in %.2f ms", self._last_api_time)
            
            # Get raw audio content
            audio_content = speech.content

            # Get audio processing options
            chime_enabled = options.get(
                CONF_CHIME_ENABLE, 
                self._config.options.get(CONF_CHIME_ENABLE, self._config.data.get(CONF_CHIME_ENABLE, False))
            )
            
            normalize_audio = options.get(
                CONF_NORMALIZE_AUDIO, 
                self._config.options.get(CONF_NORMALIZE_AUDIO, self._config.data.get(CONF_NORMALIZE_AUDIO, False))
            )
            
            _LOGGER.debug("Chime enabled: %s", chime_enabled)
            _LOGGER.debug("Normalization option: %s", normalize_audio)
            
            # Get chime file path if needed
            chime_path = None
            if chime_enabled:
                chime_file = options.get(
                    CONF_CHIME_SOUND, 
                    self._config.options.get(CONF_CHIME_SOUND, self._config.data.get(CONF_CHIME_SOUND, "threetone.mp3"))
                )
                chime_path = os.path.join(os.path.dirname(__file__), "chime", chime_file)
                _LOGGER.debug("Using chime file at: %s", chime_path)
            
            # Process audio based on options
            # Use direct synchronous processing instead of asyncio.run to avoid event loop conflicts
            processing_result = None
            
            try:
                # Use direct synchronous processing to avoid event loop issues
                _LOGGER.debug("Processing audio synchronously to avoid event loop conflicts")
                
                import tempfile
                import subprocess
                import time
                from .utils import build_ffmpeg_command, get_media_duration
                
                process_start_time = time.monotonic()
                
                # Create a temporary file for TTS audio
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tts_file:
                    tts_file.write(audio_content)
                    tts_path = tts_file.name
                
                # Determine if we need to process the audio
                if not chime_enabled and not normalize_audio:
                    # No processing needed, just use the original audio
                    with open(tts_path, "rb") as f:
                        processed_audio = f.read()
                    
                    # Get duration
                    duration = get_media_duration(tts_path)
                    
                    # Clean up and return
                    os.remove(tts_path)
                    
                    process_time = (time.monotonic() - process_start_time) * 1000
                    processing_result = ("mp3", processed_audio, process_time)
                else:
                    # We need to process the audio
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as out_file:
                        out_path = out_file.name
                    
                    # Build ffmpeg command
                    if chime_enabled and normalize_audio:
                        # Chime + normalization
                        cmd = build_ffmpeg_command(
                            out_path,
                            [chime_path, tts_path],
                            normalize_audio=True
                        )
                    elif chime_enabled:
                        # Chime only (using concat demuxer)
                        with tempfile.NamedTemporaryFile(mode="w", delete=False) as list_file:
                            list_file.write(f"file '{chime_path}'\n")
                            list_file.write(f"file '{tts_path}'\n")
                            list_path = list_file.name
                        
                        cmd = build_ffmpeg_command(
                            out_path,
                            [chime_path, tts_path],
                            normalize_audio=False,
                            is_concat=True,
                            concat_list_path=list_path
                        )
                    else:
                        # Normalization only
                        cmd = build_ffmpeg_command(
                            out_path,
                            [tts_path],
                            normalize_audio=True
                        )
                    
                    # Run ffmpeg command
                    _LOGGER.debug("Executing ffmpeg command: %s", " ".join(cmd))
                    ffmpeg_start = time.monotonic()
                    
                    try:
                        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        
                        ffmpeg_time = (time.monotonic() - ffmpeg_start) * 1000
                        _LOGGER.debug("ffmpeg processing completed in %.2f ms", ffmpeg_time)
                        
                        # Read the processed file
                        with open(out_path, "rb") as f:
                            processed_audio = f.read()
                        
                        # Get duration
                        duration = get_media_duration(out_path)
                        
                        # Clean up temp files
                        os.remove(tts_path)
                        os.remove(out_path)
                        
                        if chime_enabled and not normalize_audio and 'list_path' in locals():
                            os.remove(list_path)
                        
                        process_time = (time.monotonic() - process_start_time) * 1000
                        processing_result = ("mp3", processed_audio, process_time)
                    
                    except Exception as ffmpeg_err:
                        _LOGGER.error("Error during ffmpeg processing: %s", ffmpeg_err)
                        # Clean up on error
                        try:
                            os.remove(tts_path)
                            os.remove(out_path)
                            if 'list_path' in locals():
                                os.remove(list_path)
                        except:
                            pass
                        raise
            
            except Exception as e:
                _LOGGER.error("Error in audio processing: %s", e)
                raise
            
            if not processing_result:
                _LOGGER.error("Audio processing failed to return a result")
                return None, None
                
            # Unpack the result
            audio_format, processed_audio, processing_time = processing_result
            
            # Update timing information
            self._last_total_time = processing_time
            
            # If we processed the audio, update the ffmpeg time
            if chime_enabled or normalize_audio:
                # Estimate ffmpeg time - this assumes API time + some overhead was the rest
                # Not perfect but maintains the pattern of the original code
                self._last_ffmpeg_time = processing_time - self._last_api_time
                if self._last_ffmpeg_time < 0:
                    self._last_ffmpeg_time = 0
            else:
                self._last_ffmpeg_time = 0
            
            # Compute media duration from the processed audio
            # This requires writing the audio to a temporary file
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
                tmp_file.write(processed_audio)
                tmp_path = tmp_file.name
            
            duration_seconds = get_media_duration(tmp_path)
            self._last_media_duration_ms = int(duration_seconds * 1000)
            
            # Clean up temp file
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            
            _LOGGER.debug("Overall TTS processing time: %.2f ms", self._last_total_time)
            
            # DO NOT call self.async_write_ha_state() here - thread safety issue
            # It will be called from the async_get_tts_audio method
            
            return audio_format, processed_audio

        except CancelledError:
            _LOGGER.exception("TTS task cancelled")
            return None, None
        except MaxLengthExceeded:
            _LOGGER.exception("Maximum message length exceeded")
        except Exception as e:
            _LOGGER.exception("Unknown error in get_tts_audio: %s", e)
        
        return None, None

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict | None = None,
    ) -> tuple[str, bytes] | tuple[None, None]:
        try:
            self._engine_active = True
            self.async_write_ha_state()
            
            result = await asyncio.shield(
                self.hass.async_add_executor_job(
                    partial(self.get_tts_audio, message, language, options=options)
                )
            )
            
            # Update the entity state from within the event loop
            self.async_write_ha_state()
            
            return result
        except asyncio.CancelledError:
            _LOGGER.exception("async_get_tts_audio cancelled")
            raise
        finally:
            self._engine_active = False
            self.async_write_ha_state()