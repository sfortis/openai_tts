"""
Setting up TTS entity.
"""
from __future__ import annotations
import io
import logging
import os
import subprocess
import tempfile
import time
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
from homeassistant.exceptions import MaxLengthExceeded

_LOGGER = logging.getLogger(__name__)


def get_media_duration(file_path: str) -> float:
    """Get the duration of a media file in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        duration_str = result.stdout.strip()
        return float(duration_str) if duration_str else 0.0
    except Exception as e:
        _LOGGER.error("Error getting media duration: %s", e)
        return 0.0


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
        
        # Format media_duration as milliseconds
        media_duration_display = None
        if self._last_media_duration_ms is not None:
            media_duration_display = f"{int(self._last_media_duration_ms)} msec"
        
        return {
            "engine_active": self._engine_active,
            "last_api_time": f"{int(self._last_api_time)} msec" if self._last_api_time is not None else None,
            "last_ffmpeg_time": f"{int(self._last_ffmpeg_time)} msec" if self._last_ffmpeg_time is not None else None,
            "last_total_time": f"{int(self._last_total_time)} msec" if self._last_total_time is not None else None,
            "media_duration": self._last_media_duration_ms,  # Raw milliseconds value
            "media_duration_display": media_duration_display,  # Formatted display
            "model": model,
            "voice": voice,
            "speed": speed,
            "chime_enabled": chime,
            "normalization_enabled": normalization,
        }

    def get_tts_audio(
        self, message: str, language: str, options: dict | None = None
    ) -> tuple[str, bytes] | tuple[None, None]:
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
                
            # Retrieve settings.
            current_speed = self._config.options.get(CONF_SPEED, self._config.data.get(CONF_SPEED, 1.0))
            effective_voice = self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE))
            
            # Instructions - checks runtime options first
            instructions = options.get(CONF_INSTRUCTIONS, self._config.options.get(CONF_INSTRUCTIONS, self._config.data.get(CONF_INSTRUCTIONS)))
            
            _LOGGER.debug("Effective speed: %s", current_speed)
            _LOGGER.debug("Effective voice: %s", effective_voice)
            _LOGGER.debug("Instructions: %s", instructions)

            _LOGGER.debug("Creating TTS API request")
            api_start = time.monotonic()
            speech = self._engine.get_tts(message, speed=current_speed, voice=effective_voice, instructions=instructions)
            self._last_api_time = (time.monotonic() - api_start) * 1000
            _LOGGER.debug("TTS API call completed in %.2f ms", self._last_api_time)
            audio_content = speech.content

            # Retrieve options with proper fallback: runtime options → config options → config data
            
            # 1. Chime enabled
            chime_enabled = options.get(CONF_CHIME_ENABLE, self._config.options.get(CONF_CHIME_ENABLE, self._config.data.get(CONF_CHIME_ENABLE, False)))
            
            # 2. Normalize audio
            normalize_audio = options.get(CONF_NORMALIZE_AUDIO, self._config.options.get(CONF_NORMALIZE_AUDIO, self._config.data.get(CONF_NORMALIZE_AUDIO, False)))
            
            _LOGGER.debug("Chime enabled: %s", chime_enabled)
            _LOGGER.debug("Normalization option: %s", normalize_audio)

            if chime_enabled:
                # Write TTS audio to a temp file.
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tts_file:
                    tts_file.write(audio_content)
                    tts_path = tts_file.name
                _LOGGER.debug("TTS audio written to temp file: %s", tts_path)

                # 3. Chime sound file
                chime_file = options.get(CONF_CHIME_SOUND, self._config.options.get(CONF_CHIME_SOUND, self._config.data.get(CONF_CHIME_SOUND, "threetone.mp3")))
                
                chime_path = os.path.join(os.path.dirname(__file__), "chime", chime_file)
                _LOGGER.debug("Using chime file at: %s", chime_path)

                # Create a temporary output file.
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as out_file:
                    merged_output_path = out_file.name

                if normalize_audio:
                    _LOGGER.debug("Both chime and normalization enabled; using filter_complex to normalize TTS audio and merge with chime in one pass.")
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-i", chime_path,
                        "-i", tts_path,
                        "-filter_complex", "[1:a]loudnorm=I=-16:TP=-1:LRA=5[tts_norm]; [0:a][tts_norm]concat=n=2:v=0:a=1[out]",
                        "-map", "[out]",
                        "-ac", "1",
                        "-ar", "24000",
                        "-b:a", "128k",
                        "-preset", "superfast",
                        "-threads", "4",
                        merged_output_path,
                    ]
                    _LOGGER.debug("Executing ffmpeg command: %s", " ".join(cmd))
                    ffmpeg_start = time.monotonic()
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self._last_ffmpeg_time = (time.monotonic() - ffmpeg_start) * 1000
                else:
                    _LOGGER.debug("Chime enabled without normalization; merging using concat method.")
                    with tempfile.NamedTemporaryFile(mode="w", delete=False) as list_file:
                        list_file.write(f"file '{chime_path}'\n")
                        list_file.write(f"file '{tts_path}'\n")
                        list_path = list_file.name
                    _LOGGER.debug("FFmpeg file list created: %s", list_path)
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-f", "concat",
                        "-safe", "0",
                        "-i", list_path,
                        "-ac", "1",
                        "-ar", "24000",
                        "-b:a", "128k",
                        "-preset", "superfast",
                        "-threads", "4",
                        merged_output_path,
                    ]
                    _LOGGER.debug("Executing ffmpeg command: %s", " ".join(cmd))
                    ffmpeg_start = time.monotonic()
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self._last_ffmpeg_time = (time.monotonic() - ffmpeg_start) * 1000
                    try:
                        os.remove(list_path)
                    except Exception:
                        pass

                with open(merged_output_path, "rb") as merged_file:
                    final_audio = merged_file.read()
                overall_duration = (time.monotonic() - overall_start) * 1000
                _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
                self._last_total_time = overall_duration
                # Compute media duration in milliseconds before cleaning up.
                duration_seconds = get_media_duration(merged_output_path)
                self._last_media_duration_ms = int(duration_seconds * 1000)
                
                # DO NOT call self.async_write_ha_state() here - thread safety issue
                # It will be called from the async_get_tts_audio method
                
                # Cleanup temporary files.
                try:
                    os.remove(tts_path)
                    os.remove(merged_output_path)
                except Exception:
                    pass
                return "mp3", final_audio

            else:
                if normalize_audio:
                    _LOGGER.debug("Normalization enabled without chime; processing TTS audio via ffmpeg.")
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tts_file:
                        tts_file.write(audio_content)
                        norm_input_path = tts_file.name
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as out_file:
                        norm_output_path = out_file.name
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-i", norm_input_path,
                        "-ac", "1",
                        "-ar", "24000",
                        "-b:a", "128k",
                        "-preset", "superfast",
                        "-threads", "4",
                        "-af", "loudnorm=I=-16:TP=-1:LRA=5",
                        norm_output_path,
                    ]
                    _LOGGER.debug("Executing ffmpeg command: %s", " ".join(cmd))
                    ffmpeg_start = time.monotonic()
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self._last_ffmpeg_time = (time.monotonic() - ffmpeg_start) * 1000
                    with open(norm_output_path, "rb") as norm_file:
                        normalized_audio = norm_file.read()
                    overall_duration = (time.monotonic() - overall_start) * 1000
                    _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
                    self._last_total_time = overall_duration
                    # Compute media duration in milliseconds for the normalized file.
                    duration_seconds = get_media_duration(norm_output_path)
                    self._last_media_duration_ms = int(duration_seconds * 1000)
                    
                    # DO NOT call self.async_write_ha_state() here - thread safety issue
                    # It will be called from the async_get_tts_audio method
                    
                    try:
                        os.remove(norm_input_path)
                        os.remove(norm_output_path)
                    except Exception:
                        pass
                    return "mp3", normalized_audio
                else:
                    _LOGGER.debug("Chime and normalization disabled; returning TTS MP3 audio only.")
                    # Write audio_content to a temporary file to compute duration.
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
                        tmp_file.write(audio_content)
                        tmp_path = tmp_file.name
                    duration_seconds = get_media_duration(tmp_path)
                    self._last_media_duration_ms = int(duration_seconds * 1000)
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    overall_duration = (time.monotonic() - overall_start) * 1000
                    _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
                    self._last_total_time = overall_duration
                    self._last_ffmpeg_time = 0  # No ffmpeg processing used.
                    
                    # DO NOT call self.async_write_ha_state() here - thread safety issue
                    # It will be called from the async_get_tts_audio method
                    
                    return "mp3", audio_content

        except CancelledError as ce:
            _LOGGER.exception("TTS task cancelled")
            return None, None
        except MaxLengthExceeded as mle:
            _LOGGER.exception("Maximum message length exceeded")
        except Exception as e:
            _LOGGER.exception("Unknown error in get_tts_audio")
        return None, None

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict | None = None,
    ) -> tuple[str, bytes] | tuple[None, None]:
        from functools import partial
        import asyncio
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