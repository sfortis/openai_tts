"""
Setting up TTS entity.
"""
from __future__ import annotations
import io
import math
import re
import struct
import wave
import logging

from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import generate_entity_id
from .const import CONF_API_KEY, CONF_MODEL, CONF_SPEED, CONF_VOICE, CONF_URL, DOMAIN, UNIQUE_ID
from .openaitts_engine import OpenAITTSEngine
from homeassistant.exceptions import MaxLengthExceeded

_LOGGER = logging.getLogger(__name__)

# --- Helper Functions - Chime & silence synthesis --

def synthesize_chime(sample_rate: int = 44100, channels: int = 1, sampwidth: int = 2, duration: float = 1.0) -> bytes:
    _LOGGER.debug("Synthesizing chime: sample_rate=%d, channels=%d, sampwidth=%d, duration=%.2f", sample_rate, channels, sampwidth, duration)
    frequency1 = 440.0   # Note A
    frequency2 = 587.33  # Note D
    amplitude = 0.8
    num_samples = int(sample_rate * duration)
    output = io.BytesIO()
    with wave.open(output, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        for i in range(num_samples):
            t = i / sample_rate
            fade = 1.0 - (i / num_samples)
            sample1 = math.sin(2 * math.pi * frequency1 * t)
            sample2 = math.sin(2 * math.pi * frequency2 * t)
            sample = amplitude * fade * ((sample1 + sample2) / 2)
            int_sample = int(sample * 32767)
            wf.writeframes(struct.pack('<h', int_sample))
    chime_data = output.getvalue()
    _LOGGER.debug("Chime synthesized, length: %d bytes", len(chime_data))
    return chime_data

def synthesize_silence(sample_rate: int, channels: int, sampwidth: int, duration: float = 0.3) -> bytes:
    _LOGGER.debug("Synthesizing silence: sample_rate=%d, channels=%d, sampwidth=%d, duration=%.2f", sample_rate, channels, sampwidth, duration)
    num_samples = int(sample_rate * duration)
    output = io.BytesIO()
    with wave.open(output, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        for _ in range(num_samples):
            wf.writeframes(struct.pack('<h', 0))
    silence_data = output.getvalue()
    _LOGGER.debug("Silence synthesized, length: %d bytes", len(silence_data))
    return silence_data

def combine_wav_files(chime_bytes: bytes, pause_bytes: bytes, tts_bytes: bytes) -> bytes:
    _LOGGER.debug("Combining WAV files: chime (%d bytes), pause (%d bytes), TTS (%d bytes)",
                  len(chime_bytes), len(pause_bytes), len(tts_bytes))
    chime_io = io.BytesIO(chime_bytes)
    pause_io = io.BytesIO(pause_bytes)
    tts_io = io.BytesIO(tts_bytes)
    
    with wave.open(chime_io, 'rb') as w1, wave.open(pause_io, 'rb') as w2, wave.open(tts_io, 'rb') as w3:
        params1 = w1.getparams()
        params2 = w2.getparams()
        params3 = w3.getparams()
        if params1[:3] != params2[:3] or params1[:3] != params3[:3]:
            raise Exception("WAV parameters do not match among chime, pause, and TTS audio")
        frames_chime = w1.readframes(w1.getnframes())
        frames_pause = w2.readframes(w2.getnframes())
        frames_tts = w3.readframes(w3.getnframes())
    
    output = io.BytesIO()
    with wave.open(output, 'wb') as wout:
        wout.setparams(params1)
        wout.writeframes(frames_chime)
        wout.writeframes(frames_pause)
        wout.writeframes(frames_tts)
    combined_data = output.getvalue()
    _LOGGER.debug("Combined WAV file length: %d bytes", len(combined_data))
    return combined_data

def _map_model(model: str) -> str:
    """Map the model value to a short label for entity display."""
    model = model.lower()
    if model == "tts-1":
        return "SD"
    elif model == "tts-1-hd":
        return "HD"
    elif model.startswith("tts-"):
        return model[4:].upper()
    return model.upper()

# --- End Helper Functions ---

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
        config_entry.data[CONF_URL]
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
        # Use the mapped model as the base for the entity_id.
        base_name = _map_model(config.data.get(CONF_MODEL, ""))
        self.entity_id = generate_entity_id("tts.openai_tts_{}", base_name.lower(), hass=hass)

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_languages(self) -> list:
        return self._engine.get_supported_langs()

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "model": self._config.data.get(CONF_MODEL),
            "manufacturer": "OpenAI"
        }

    @property
    def name(self) -> str:
        return _map_model(self._config.data.get(CONF_MODEL, "")).upper()

    def get_tts_audio(self, message: str, language: str, options: dict | None = None) -> tuple[str, bytes] | tuple[None, None]:
        try:
            if len(message) > 4096:
                raise MaxLengthExceeded("Message exceeds maximum allowed length")
            # Re-read speed and voice from options if available.
            current_speed = self._config.options.get(CONF_SPEED, self._config.data.get(CONF_SPEED, 1.0))
            effective_voice = self._config.options.get(CONF_VOICE, self._config.data.get(CONF_VOICE))
            _LOGGER.debug("Effective speed: %s", current_speed)
            _LOGGER.debug("Effective voice: %s", effective_voice)
            # Call get_tts with the current speed and voice.
            speech = self._engine.get_tts(message, speed=current_speed, voice=effective_voice)
            audio_content = speech.content
            # Determine effective chime setting.
            chime_enabled = self._config.options.get("chime", self._config.data.get("chime", False))
            _LOGGER.debug("Effective chime option: %s", chime_enabled)
            if chime_enabled:
                _LOGGER.debug("Chime option enabled; synthesizing chime and pause.")
                tts_io = io.BytesIO(audio_content)
                with wave.open(tts_io, 'rb') as tts_wave:
                    sample_rate = tts_wave.getframerate()
                    channels = tts_wave.getnchannels()
                    sampwidth = tts_wave.getsampwidth()
                    tts_frames = tts_wave.getnframes()
                _LOGGER.debug("TTS parameters: sample_rate=%d, channels=%d, sampwidth=%d, frames=%d",
                              sample_rate, channels, sampwidth, tts_frames)
                chime_audio = synthesize_chime(sample_rate=sample_rate, channels=channels, sampwidth=sampwidth, duration=1.0)
                pause_audio = synthesize_silence(sample_rate=sample_rate, channels=channels, sampwidth=sampwidth, duration=0.3)
                try:
                    combined_audio = combine_wav_files(chime_audio, pause_audio, audio_content)
                    _LOGGER.debug("Combined audio generated (chime -> pause -> TTS).")
                    return "wav", combined_audio
                except Exception as ce:
                    _LOGGER.error("Error combining audio: %s", ce)
                    return "wav", audio_content
            else:
                _LOGGER.debug("Chime option disabled; returning TTS audio only.")
                return "wav", audio_content
        except MaxLengthExceeded as mle:
            _LOGGER.error("Maximum message length exceeded: %s", mle)
        except Exception as e:
            _LOGGER.error("Unknown error in get_tts_audio: %s", e)
        return None, None
