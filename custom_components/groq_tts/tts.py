"""
Setting up TTS entity.
"""
from __future__ import annotations
import io
import logging
import os
import tempfile
import time
import asyncio
from asyncio import CancelledError

from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import generate_entity_id
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
    )
    async_add_entities([GroqTTSEntity(hass, config_entry, engine)])

class GroqTTSEntity(TextToSpeechEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, config: ConfigEntry, engine: GroqTTSEngine) -> None:
        self.hass = hass
        self._engine = engine
        self._config = config
        self._attr_unique_id = config.data.get(UNIQUE_ID)
        if not self._attr_unique_id:
            self._attr_unique_id = f"{config.data.get(CONF_URL)}_{config.data.get(CONF_MODEL)}"
        base_name = self._config.data.get(CONF_MODEL, "").upper()
        self.entity_id = generate_entity_id("tts.groq_tts_{}", base_name.lower(), hass=hass)

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_options(self) -> list:
        return ["chime"]
        
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

            effective_voice = self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE))

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
            normalize_audio = self._config.options.get(
                CONF_NORMALIZE_AUDIO, self._config.data.get(CONF_NORMALIZE_AUDIO, False)
            )
            _LOGGER.debug("Chime enabled: %s", chime_enabled)
            _LOGGER.debug("Normalization option: %s", normalize_audio)

            async def run_ffmpeg(cmd):
                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await process.communicate()
                if process.returncode != 0:
                    _LOGGER.error("ffmpeg error: %s", stderr.decode())
                    raise Exception("ffmpeg failed")

            if chime_enabled or normalize_audio:
                with tempfile.TemporaryDirectory() as tmpdir:
                    tts_path = os.path.join(tmpdir, "speech.mp3")
                    with open(tts_path, "wb") as f:
                        f.write(audio_content)

                    output_path = os.path.join(tmpdir, "out.mp3")

                    if chime_enabled:
                        chime_file = self._config.options.get(
                            CONF_CHIME_SOUND, self._config.data.get(CONF_CHIME_SOUND, "threetone.mp3")
                        )
                        chime_path = os.path.join(os.path.dirname(__file__), "chime", chime_file)

                        if normalize_audio:
                            cmd = [
                                "ffmpeg",
                                "-y",
                                "-i",
                                chime_path,
                                "-i",
                                tts_path,
                                "-filter_complex",
                                "[1:a]loudnorm=I=-16:TP=-1:LRA=5[tts_norm];[0:a][tts_norm]concat=n=2:v=0:a=1[out]",
                                "-map",
                                "[out]",
                                "-ac",
                                "1",
                                "-ar",
                                "24000",
                                "-b:a",
                                "128k",
                                output_path,
                            ]
                        else:
                            list_path = os.path.join(tmpdir, "list.txt")
                            with open(list_path, "w") as list_file:
                                list_file.write(f"file '{chime_path}'\n")
                                list_file.write(f"file '{tts_path}'\n")
                            cmd = [
                                "ffmpeg",
                                "-y",
                                "-f",
                                "concat",
                                "-safe",
                                "0",
                                "-i",
                                list_path,
                                "-ac",
                                "1",
                                "-ar",
                                "24000",
                                "-b:a",
                                "128k",
                                output_path,
                            ]
                    else:
                        cmd = [
                            "ffmpeg",
                            "-y",
                            "-i",
                            tts_path,
                            "-ac",
                            "1",
                            "-ar",
                            "24000",
                            "-b:a",
                            "128k",
                            "-af",
                            "loudnorm=I=-16:TP=-1:LRA=5",
                            output_path,
                        ]

                    await run_ffmpeg(cmd)

                    with open(output_path, "rb") as out_f:
                        audio_content = out_f.read()

            overall_duration = (time.monotonic() - overall_start) * 1000
            _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
            return "mp3", audio_content

        except CancelledError:
            _LOGGER.exception("TTS task cancelled")
            return None, None
        except Exception:
            _LOGGER.exception("Unknown error in async_get_tts_audio")
        return None, None
