"""
Setting up TTS entity.
"""
from __future__ import annotations
import logging
import os
import time
import asyncio
from asyncio import CancelledError

from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_VOICE,
    CONF_URL,
    DOMAIN,
    UNIQUE_ID,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
    CONF_CACHE_SIZE,
    DEFAULT_CACHE_SIZE,
)
from .groqtts_engine import GroqTTSEngine

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    api_key = config_entry.data.get(CONF_API_KEY)
    engine = GroqTTSEngine(
        api_key,
        config_entry.data[CONF_VOICE],
        config_entry.data[CONF_MODEL],
        config_entry.data[CONF_URL],
        cache_max=config_entry.options.get(CONF_CACHE_SIZE, DEFAULT_CACHE_SIZE),
    )
    async_add_entities([GroqTTSEntity(hass, config_entry, engine)])

class GroqTTSEntity(TextToSpeechEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, config: ConfigEntry, engine: GroqTTSEngine) -> None:
        self.hass = hass
        self._engine = engine
        self._config = config
        # Prefer the config entry unique_id; fall back to stored value for backward compatibility
        self._attr_unique_id = getattr(config, "unique_id", None) or config.data.get(UNIQUE_ID)
        if not self._attr_unique_id:
            self._attr_unique_id = f"{config.data.get(CONF_URL)}_{config.data.get(CONF_MODEL)}"
        # Let the registry generate the entity_id based on name/device info

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_options(self) -> list:
        # Must match option keys actually read from service/data
        return [CONF_CHIME_ENABLE, CONF_VOICE, CONF_NORMALIZE_AUDIO]

    @property
    def default_options(self) -> dict:
        """Advertise default options for the TTS service."""
        return {
            CONF_CHIME_ENABLE: False,
            CONF_NORMALIZE_AUDIO: False,
            CONF_VOICE: self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE)),
        }
        
    @property
    def supported_languages(self) -> list:
        return self._engine.get_supported_langs()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "model": self._config.data.get(CONF_MODEL),
            "manufacturer": "Groq",
        }

    @property
    def name(self) -> str:
        return self._config.data.get(CONF_MODEL, "").upper()

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict | None = None,
    ) -> tuple[str, bytes] | tuple[None, None]:
        """Generate TTS audio asynchronously and optionally merge chime or normalize."""
        overall_start = time.monotonic()

        options = options or {}

        try:
            if len(message) > 4096:
                raise Exception("Message exceeds maximum allowed length")

            effective_voice = options.get(
                CONF_VOICE,
                self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE)),
            )

            _LOGGER.debug("Creating TTS API request")
            api_start = time.monotonic()
            speech = await self._engine.async_get_tts(self.hass, message, voice=effective_voice)
            api_duration = (time.monotonic() - api_start) * 1000
            _LOGGER.debug("TTS API call completed in %.2f ms", api_duration)
            audio_content = speech.content

            chime_enabled = options.get(
                CONF_CHIME_ENABLE,
                self._config.options.get(CONF_CHIME_ENABLE, self._config.data.get(CONF_CHIME_ENABLE, False)),
            )
            normalize_audio = options.get(
                CONF_NORMALIZE_AUDIO,
                self._config.options.get(
                    CONF_NORMALIZE_AUDIO, self._config.data.get(CONF_NORMALIZE_AUDIO, False)
                ),
            )
            _LOGGER.debug("Chime enabled: %s", chime_enabled)
            _LOGGER.debug("Normalization option: %s", normalize_audio)

            async def run_ffmpeg(cmd, input_bytes):
                try:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except FileNotFoundError:
                    _LOGGER.error("ffmpeg executable not found. Please install ffmpeg or adjust PATH.")
                    raise Exception("ffmpeg not found")
                stdout, stderr = await process.communicate(input=input_bytes)
                if process.returncode != 0:
                    _LOGGER.error("ffmpeg error: %s", stderr.decode())
                    raise Exception("ffmpeg failed")
                return stdout

            if chime_enabled or normalize_audio:
                if chime_enabled:
                    chime_file = self._config.options.get(
                        CONF_CHIME_SOUND, self._config.data.get(CONF_CHIME_SOUND, "threetone.mp3")
                    )
                    chime_path = os.path.join(os.path.dirname(__file__), "chime", chime_file)
                    if not os.path.exists(chime_path):
                        _LOGGER.error("Chime file not found: %s", chime_path)
                        return None, None

                    if normalize_audio:
                        cmd = [
                            "ffmpeg",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            chime_path,
                            "-i",
                            "pipe:0",
                            "-filter_complex",
                            "[1:a]loudnorm=I=-16:TP=-1:LRA=5[tts];[0:a][tts]concat=n=2:v=0:a=1[out]",
                            "-map",
                            "[out]",
                            "-ac",
                            "1",
                            "-ar",
                            "24000",
                            "-b:a",
                            "128k",
                            "-f",
                            "mp3",
                            "pipe:1",
                        ]
                    else:
                        cmd = [
                            "ffmpeg",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            chime_path,
                            "-i",
                            "pipe:0",
                            "-filter_complex",
                            "[0:a][1:a]concat=n=2:v=0:a=1[out]",
                            "-map",
                            "[out]",
                            "-ac",
                            "1",
                            "-ar",
                            "24000",
                            "-b:a",
                            "128k",
                            "-f",
                            "mp3",
                            "pipe:1",
                        ]
                else:
                    cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        "pipe:0",
                        "-ac",
                        "1",
                        "-ar",
                        "24000",
                        "-b:a",
                        "128k",
                        "-af",
                        "loudnorm=I=-16:TP=-1:LRA=5",
                        "-f",
                        "mp3",
                        "pipe:1",
                    ]

                audio_content = await run_ffmpeg(cmd, audio_content)

            overall_duration = (time.monotonic() - overall_start) * 1000
            _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
            return "mp3", audio_content

        except CancelledError:
            _LOGGER.debug("TTS task cancelled")
            return None, None
        except Exception:
            _LOGGER.exception("Unknown error in async_get_tts_audio")
        return None, None
