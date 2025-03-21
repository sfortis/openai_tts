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

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    api_key = config_entry.data.get(CONF_API_KEY)
    engine = OpenAITTSEngine(
        api_key,
        config_entry.data[CONF_VOICE],
        config_entry.data[CONF_MODEL],
        config_entry.data.get(CONF_SPEED, 1.0),
        config_entry.data[CONF_URL],
    )
    async_add_entities([OpenAITTSEntity(hass, config_entry, engine)])

class OpenAITTSEntity(TextToSpeechEntity):
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

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_options(self) -> list:
        return ["instructions", "chime"]
        
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
            # Retrieve settings.
            current_speed = self._config.options.get(CONF_SPEED, self._config.data.get(CONF_SPEED, 1.0))
            effective_voice = self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE))
            instructions = options.get(CONF_INSTRUCTIONS, self._config.options.get(CONF_INSTRUCTIONS, self._config.data.get(CONF_INSTRUCTIONS)))
            _LOGGER.debug("Effective speed: %s", current_speed)
            _LOGGER.debug("Effective voice: %s", effective_voice)
            _LOGGER.debug("Instructions: %s", instructions)

            _LOGGER.debug("Creating TTS API request")
            api_start = time.monotonic()
            speech = self._engine.get_tts(message, speed=current_speed, voice=effective_voice, instructions=instructions)
            api_duration = (time.monotonic() - api_start) * 1000
            _LOGGER.debug("TTS API call completed in %.2f ms", api_duration)
            audio_content = speech.content

            # Retrieve options.
            chime_enabled = options.get(CONF_CHIME_ENABLE,self._config.options.get(CONF_CHIME_ENABLE, self._config.data.get(CONF_CHIME_ENABLE, False)))
            normalize_audio = self._config.options.get(CONF_NORMALIZE_AUDIO, self._config.data.get(CONF_NORMALIZE_AUDIO, False))
            _LOGGER.debug("Chime enabled: %s", chime_enabled)
            _LOGGER.debug("Normalization option: %s", normalize_audio)

            if chime_enabled:
                # Write TTS audio to a temp file.
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tts_file:
                    tts_file.write(audio_content)
                    tts_path = tts_file.name
                _LOGGER.debug("TTS audio written to temp file: %s", tts_path)

                # Determine chime file path.
                chime_file = self._config.options.get(CONF_CHIME_SOUND, self._config.data.get(CONF_CHIME_SOUND, "threetone.mp3"))
                chime_path = os.path.join(os.path.dirname(__file__), "chime", chime_file)
                _LOGGER.debug("Using chime file at: %s", chime_path)

                # Create a temporary output file.
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as out_file:
                    merged_output_path = out_file.name

                if normalize_audio:
                    _LOGGER.debug("Both chime and normalization enabled; " +
                                  "using filter_complex to normalize TTS audio and merge with chime in one pass.")
                    # Use filter_complex to normalize the TTS audio and then concatenate with the chime.
                    # First input: chime audio, second input: TTS audio (to be normalized).
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
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                else:
                    _LOGGER.debug("Chime enabled without normalization; merging using concat method.")
                    # Create a file list for concatenation.
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
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        os.remove(list_path)
                    except Exception:
                        pass

                with open(merged_output_path, "rb") as merged_file:
                    final_audio = merged_file.read()
                overall_duration = (time.monotonic() - overall_start) * 1000
                _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
                # Cleanup temporary files.
                try:
                    os.remove(tts_path)
                    os.remove(merged_output_path)
                except Exception:
                    pass
                return "mp3", final_audio

            else:
                # Chime disabled.
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
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    with open(norm_output_path, "rb") as norm_file:
                        normalized_audio = norm_file.read()
                    overall_duration = (time.monotonic() - overall_start) * 1000
                    _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
                    try:
                        os.remove(norm_input_path)
                        os.remove(norm_output_path)
                    except Exception:
                        pass
                    return "mp3", normalized_audio
                else:
                    _LOGGER.debug("Chime and normalization disabled; returning TTS MP3 audio only.")
                    overall_duration = (time.monotonic() - overall_start) * 1000
                    _LOGGER.debug("Overall TTS processing time: %.2f ms", overall_duration)
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
            return await asyncio.shield(
                self.hass.async_add_executor_job(
                    partial(self.get_tts_audio, message, language, options=options)
                )
            )
        except asyncio.CancelledError:
            _LOGGER.exception("async_get_tts_audio cancelled")
            raise
