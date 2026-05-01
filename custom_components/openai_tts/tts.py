"""TTS entity for the OpenAI TTS integration.

The entity implements both the legacy ``async_get_tts_audio`` contract and
the modern ``async_stream_tts_audio`` streaming contract introduced in
HA 2025.7. Audio bytes are validated against magic-byte signatures before
they are returned to Home Assistant, so a failed API call can never poison
the HA TTS cache (issue #64).
"""
from __future__ import annotations

import asyncio
import logging
import os
from asyncio import CancelledError
from datetime import datetime
from functools import partial
from typing import Any, AsyncGenerator

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, MaxLengthExceeded
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store

from .api_health import OpenAITTSHealthTracker
from .audio_metadata import embed_duration_in_audio
from .cache import MessageDurationCache
from .const import (
    CONF_API_KEY,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_EXTRA_PAYLOAD,
    CONF_INSTRUCTIONS,
    CONF_MODEL,
    CONF_NORMALIZE_AUDIO,
    CONF_PROFILE_NAME,
    CONF_SPEED,
    CONF_URL,
    CONF_VOICE,
    DOMAIN,
    SUPPORTED_LANGUAGES,
    UNIQUE_ID,
    VOICES,
)
from .entity_helpers import is_subentry, sanitize_profile_name
from .exceptions import (
    OpenAIAuthError,
    OpenAIInvalidResponseError,
    OpenAIQuotaExceededError,
    OpenAIRateLimitError,
    OpenAITTSError,
)
from .openaitts_engine import OpenAITTSEngine
from .utils import detect_audio_format, get_media_duration, is_valid_audio, process_audio

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE_PROFILE = "profile"
STORAGE_VERSION = 1
STORAGE_KEY = "openai_tts_state"
HEALTH_TRACKER_KEY = "_health_tracker"

# Stream as soon as we have anything to say. The previous 60-char floor
# was meant to avoid streaming overhead for trivially short clips, but in
# practice every TTS response (>= ~2s of audio) benefits from streaming -
# atomic mode adds 5+ seconds of silence before playback starts. We keep
# the threshold variable instead of removing the check so behaviour stays
# easy to tune from one place.
MIN_STREAMING_TEXT_LENGTH = 1


def _resolve_health_tracker(
    hass: HomeAssistant, parent_entry_id: str | None
) -> OpenAITTSHealthTracker | None:
    """Look up the parent entry's health tracker, if registered."""
    if not parent_entry_id:
        return None
    return hass.data.get(DOMAIN, {}).get(
        f"{parent_entry_id}{HEALTH_TRACKER_KEY}"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenAI TTS entities for a config entry."""
    _LOGGER.debug("Setting up OpenAI TTS for config entry %s", config_entry.entry_id)

    entity_registry = er.async_get(hass)

    is_legacy = (
        (config_entry.data.get(CONF_MODEL) is not None
         or config_entry.data.get(CONF_VOICE) is not None)
        and (config_entry.version < 2
             or (config_entry.version == 2 and config_entry.minor_version < 1))
    )
    has_subentries = bool(getattr(config_entry, "subentries", None))

    if is_legacy:
        _LOGGER.info("Creating TTS entity for legacy entry: %s", config_entry.title)
        api_key = config_entry.data.get(CONF_API_KEY)
        url = config_entry.data.get(CONF_URL)
        model = config_entry.options.get(CONF_MODEL, config_entry.data.get(CONF_MODEL))
        voice = config_entry.options.get(CONF_VOICE, config_entry.data.get(CONF_VOICE))
        speed = config_entry.options.get(CONF_SPEED, config_entry.data.get(CONF_SPEED, 1.0))

        engine = OpenAITTSEngine(api_key, voice, model, speed, url)
        async_add_entities([OpenAITTSEntity(hass, config_entry, engine)])

    if not has_subentries:
        if not is_legacy:
            _LOGGER.info("Modern parent entry with no subentries; no entities created")
        return

    _LOGGER.info(
        "Processing %d subentries for %s entry %s",
        len(config_entry.subentries),
        "legacy" if is_legacy else "parent",
        config_entry.entry_id,
    )

    for subentry_id, subentry in config_entry.subentries.items():
        if getattr(subentry, "subentry_type", None) != SUBENTRY_TYPE_PROFILE:
            continue

        api_key = config_entry.data.get(CONF_API_KEY)
        url = config_entry.data.get(CONF_URL)
        model = subentry.data.get(CONF_MODEL, "tts-1")
        voice = subentry.data.get(CONF_VOICE, "shimmer")
        speed = subentry.data.get(CONF_SPEED, 1.0)

        unique_id = subentry.data.get(UNIQUE_ID)
        if unique_id:
            existing = [
                eid for eid, entity in entity_registry.entities.items()
                if entity.unique_id == unique_id and entity.platform == DOMAIN
            ]
            if existing:
                _LOGGER.debug(
                    "Found %d existing entities with unique_id %s, will be replaced",
                    len(existing), unique_id,
                )

        engine = OpenAITTSEngine(api_key, voice, model, speed, url)
        entity = OpenAITTSEntity(hass, subentry, engine, config_entry)
        async_add_entities([entity], config_subentry_id=subentry_id)


class OpenAITTSEntity(TextToSpeechEntity, RestoreEntity):
    """Home Assistant TTS entity backed by the OpenAI TTS API."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        config: ConfigEntry,
        engine: OpenAITTSEngine,
        parent_entry: ConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self._engine = engine
        self._config = config
        self._parent_entry = parent_entry

        self._attr_unique_id = config.data.get(UNIQUE_ID)
        if not self._attr_unique_id:
            import hashlib
            config_str = (
                f"{config.data.get(CONF_URL)}_{config.data.get(CONF_MODEL)}"
                f"_{config.data.get(CONF_VOICE)}"
            )
            self._attr_unique_id = hashlib.sha256(config_str.encode()).hexdigest()[:32]

        if hasattr(config, "subentry_id"):
            self._attr_config_entry_id = config.subentry_id
        elif hasattr(config, "entry_id"):
            self._attr_config_entry_id = config.entry_id
        else:
            self._attr_config_entry_id = parent_entry.entry_id if parent_entry else None

        self._configure_entity_id_and_name()

        self._engine_active = False
        self._last_duration_ms: int | None = None
        # ``playback_mode`` lets ``volume_restore`` know whether the cast
        # device has been playing while the engine is still active
        # (``streaming``) or has been idle waiting for full audio
        # (``atomic``). Exposed via extra_state_attributes so volume_restore
        # doesn't have to guess from timing heuristics.
        self._playback_mode: str | None = None
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{self.entity_id}")
        self._stored_data: dict = {}
        # Key the duration cache on ``unique_id`` (stable across user-initiated
        # entity renames), NOT ``entity_id`` (which is whatever the user has
        # in the registry and can drift from the profile-derived entity_id we
        # compute internally). volume_restore looks up by unique_id for the
        # same reason.
        self._duration_cache = MessageDurationCache(hass, self._attr_unique_id)

        # The health tracker lives on the parent entry. Subentries inherit it
        # via parent_entry; legacy entries are their own parent.
        parent_entry_id = (
            parent_entry.entry_id if parent_entry is not None
            else getattr(config, "entry_id", None)
        )
        self._health_tracker = _resolve_health_tracker(hass, parent_entry_id)

        _LOGGER.info(
            "OpenAI TTS entity created: %s (engine speed: %s)",
            self.entity_id, self._engine._speed,
        )

    def _configure_entity_id_and_name(self) -> None:
        if is_subentry(self._config):
            profile_name = self._config.data.get(CONF_PROFILE_NAME, "profile")
            safe = sanitize_profile_name(profile_name)
            self.entity_id = f"tts.openai_tts_{safe}"
            self._attr_name = f"OpenAI TTS {profile_name}"
            return

        model = self._config.data.get(CONF_MODEL)
        if model:
            model_suffix = model.replace("-", "_").replace(".", "_")
            self.entity_id = f"tts.openai_tts_{model_suffix}"
            self._attr_name = f"OpenAI TTS ({model})"
            return

        self.entity_id = "tts.openai_tts"
        self._attr_name = "OpenAI TTS"

    # --- Persistent state --------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_persisted_state()
        _LOGGER.info("TTS entity %s registered with Home Assistant", self.entity_id)

    async def async_will_remove_from_hass(self) -> None:
        _LOGGER.debug("TTS entity %s being removed from hass", self.entity_id)
        await self._save_persisted_state()
        await super().async_will_remove_from_hass()

    async def _restore_persisted_state(self) -> None:
        try:
            stored = await self._store.async_load()
            if not stored:
                return
            self._stored_data = stored
            if "last_duration_ms" in stored:
                self._last_duration_ms = stored["last_duration_ms"]
                self.async_write_ha_state()
            if "message_duration_cache" in stored:
                self._duration_cache.restore(stored["message_duration_cache"])
        except Exception as e:
            _LOGGER.error("Failed to restore persisted state: %s", e)

    async def _save_persisted_state(self) -> None:
        try:
            data = {
                "last_duration_ms": self._last_duration_ms,
                "last_updated": datetime.now().isoformat(),
                "message_duration_cache": self._duration_cache.snapshot,
            }
            await self._store.async_save(data)
        except Exception as e:
            _LOGGER.error("Failed to save persisted state: %s", e)

    # --- Entity properties -------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "media_duration": self._last_duration_ms,
            "engine_active": self._engine_active,
            "playback_mode": self._playback_mode,
            "message_cache_size": self._duration_cache.size,
            "available_voices": VOICES,
            "current_voice": self._get_config_value(CONF_VOICE) or self._engine._voice,
            "current_model": self._get_config_value(CONF_MODEL) or self._engine._model,
            "current_speed": self._get_config_value(CONF_SPEED) or self._engine._speed,
        }

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_languages(self) -> list[str]:
        return SUPPORTED_LANGUAGES

    @property
    def supported_options(self) -> list[str]:
        return [
            CONF_VOICE,
            CONF_MODEL,
            CONF_SPEED,
            CONF_CHIME_ENABLE,
            CONF_CHIME_SOUND,
            CONF_NORMALIZE_AUDIO,
            CONF_INSTRUCTIONS,
            CONF_EXTRA_PAYLOAD,
        ]

    @property
    def default_options(self) -> dict[str, Any]:
        """Default option values that participate in the HA TTS cache key."""
        return {
            CONF_VOICE: self._get_config_value(CONF_VOICE) or self._engine._voice,
            CONF_MODEL: self._get_config_value(CONF_MODEL) or self._engine._model,
            CONF_SPEED: self._get_config_value(CONF_SPEED) or self._engine._speed,
            CONF_CHIME_ENABLE: self._get_config_value(CONF_CHIME_ENABLE, False),
            CONF_CHIME_SOUND: self._get_config_value(CONF_CHIME_SOUND, "threetone.mp3"),
            CONF_NORMALIZE_AUDIO: self._get_config_value(CONF_NORMALIZE_AUDIO, False),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        if is_subentry(self._config):
            device_unique_id = (
                self._config.data.get(UNIQUE_ID)
                or f"{self._config.data.get(CONF_PROFILE_NAME, 'profile')}"
                   f"_{self._config.data.get(CONF_MODEL, 'tts-1')}"
            )
        else:
            device_unique_id = (
                self._config.data.get(UNIQUE_ID)
                or self._config.data.get(CONF_URL, "openai_tts")
            )

        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, device_unique_id)},
            "manufacturer": "OpenAI",
            "sw_version": "1.0",
        }

        if is_subentry(self._config):
            agent_name = self._config.data.get(CONF_PROFILE_NAME, "default")
            model = self._config.data.get(CONF_MODEL, "tts-1")
            voice = self._config.data.get(CONF_VOICE, "unknown")
            info["name"] = f"{agent_name} ({model}-{voice})"
            info["model"] = f"{model} ({voice})"
        else:
            info["name"] = "OpenAI TTS"
            info["model"] = self._config.data.get(CONF_MODEL, "TTS API")

        return info

    def _get_config_value(self, key: str, default: Any = None) -> Any:
        if is_subentry(self._config):
            return self._config.data.get(key, default)
        if hasattr(self._config, "options"):
            options_value = self._config.options.get(key)
            if options_value is not None:
                return options_value
        data_value = self._config.data.get(key)
        return data_value if data_value is not None else default

    def get_duration_for_message(self, message: str) -> int | None:
        """Public lookup used by ``volume_restore``."""
        return self._duration_cache.get(message)

    # --- TTS generation ----------------------------------------------------

    async def _get_audio_duration(self, audio_data: bytes) -> int:
        """Return audio duration in milliseconds via ffprobe."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            tmp_file.write(audio_data)
            tmp_path = tmp_file.name
        try:
            loop = asyncio.get_running_loop()
            duration_seconds = await loop.run_in_executor(
                None, get_media_duration, tmp_path
            )
            return int(duration_seconds * 1000)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _can_use_streaming(self, text: str, options: dict) -> bool:
        if options.get(CONF_CHIME_ENABLE) or options.get(CONF_NORMALIZE_AUDIO):
            return False
        return len(text) >= MIN_STREAMING_TEXT_LENGTH

    def _resolve_options(self, options: dict | None) -> dict[str, Any]:
        """Merge service-call options with entity defaults."""
        opts = options or {}
        speed = opts.get(CONF_SPEED)
        if speed is None:
            speed = self._get_config_value(CONF_SPEED)
        if speed is None:
            speed = 1.0

        service_instructions = opts.get(CONF_INSTRUCTIONS)
        config_instructions = self._get_config_value(CONF_INSTRUCTIONS)
        instructions = (
            service_instructions
            if service_instructions is not None
            else config_instructions
        )

        # Booleans: only fall back to config when the option is *absent*,
        # so an explicit `False` from the service call wins over an `True`
        # in config (former bug: chime override was impossible to disable).
        chime_enable = (
            opts[CONF_CHIME_ENABLE]
            if CONF_CHIME_ENABLE in opts
            else (self._get_config_value(CONF_CHIME_ENABLE) or False)
        )
        normalize_audio = (
            opts[CONF_NORMALIZE_AUDIO]
            if CONF_NORMALIZE_AUDIO in opts
            else (self._get_config_value(CONF_NORMALIZE_AUDIO) or False)
        )

        return {
            "voice": (
                opts.get(CONF_VOICE)
                or self._get_config_value(CONF_VOICE)
                or self._engine._voice
            ),
            "model": (
                opts.get(CONF_MODEL)
                or self._get_config_value(CONF_MODEL)
                or self._engine._model
            ),
            "speed": speed,
            "instructions": instructions,
            "extra_payload": (
                opts.get(CONF_EXTRA_PAYLOAD)
                or self._get_config_value(CONF_EXTRA_PAYLOAD)
            ),
            "chime_enable": chime_enable,
            "chime_sound": (
                opts.get(CONF_CHIME_SOUND)
                or self._get_config_value(CONF_CHIME_SOUND)
            ),
            "normalize_audio": normalize_audio,
        }

    async def _engine_get_blocking(
        self, text: str, resolved: dict[str, Any], stream: bool
    ) -> bytes:
        """Run the blocking engine in an executor and return the raw audio."""
        loop = asyncio.get_running_loop()
        audio_task = loop.run_in_executor(
            None,
            partial(
                self._engine.get_tts,
                text,
                speed=resolved["speed"],
                voice=resolved["voice"],
                model=resolved["model"],
                instructions=resolved["instructions"],
                extra_payload=resolved["extra_payload"],
                stream=stream,
            ),
        )
        audio_response = await asyncio.wait_for(audio_task, timeout=30.0)

        if not audio_response:
            raise OpenAIInvalidResponseError("No audio response received")

        if hasattr(audio_response, "read_all"):
            audio_data = audio_response.read_all()
        else:
            audio_data = audio_response.content

        if not audio_data:
            raise OpenAIInvalidResponseError("Empty audio response")
        return audio_data

    async def _maybe_post_process(
        self, audio_data: bytes, resolved: dict[str, Any]
    ) -> bytes:
        """Apply chime / normalization / WAV->MP3 conversion when needed."""
        is_wav = detect_audio_format(audio_data) == "wav"
        chime_enable = resolved["chime_enable"]
        normalize_audio = resolved["normalize_audio"]

        if not (chime_enable or normalize_audio or is_wav):
            return audio_data

        chime_path = None
        if chime_enable and resolved["chime_sound"]:
            chime_folder = os.path.join(os.path.dirname(__file__), "chime")
            candidate = os.path.join(chime_folder, resolved["chime_sound"])
            if os.path.exists(candidate):
                chime_path = candidate
            else:
                _LOGGER.warning("Chime file not found: %s", candidate)

        _, processed_audio, _ = await process_audio(
            self.hass,
            audio_data,
            chime_enabled=chime_enable,
            chime_path=chime_path,
            normalize_audio=normalize_audio,
        )
        if not processed_audio:
            _LOGGER.warning("Audio processing failed, using original audio")
            return audio_data
        return processed_audio

    async def _record_duration(
        self,
        message: str,
        audio_data: bytes,
        resolved: dict[str, Any] | None = None,
    ) -> int:
        duration_ms = await self._get_audio_duration(audio_data)
        self._last_duration_ms = duration_ms
        self._duration_cache.store(
            message, duration_ms,
            voice=(resolved or {}).get("voice"),
            model=(resolved or {}).get("model"),
            speed=(resolved or {}).get("speed"),
        )
        self.async_write_ha_state()
        await self._save_persisted_state()
        # A successful TTS call is also the most reliable signal that the
        # API is healthy; the health tracker uses this to clear any prior
        # quota/auth/rate-limit state.
        if self._health_tracker is not None:
            self._health_tracker.record_success()
        return duration_ms

    def _record_failure(
        self,
        message: str,
        error: BaseException,
        resolved: dict[str, Any] | None = None,
    ) -> None:
        """Centralised bookkeeping for any TTS failure path.

        Marks the duration cache as failed (so volume_restore short-circuits
        its polling) AND surfaces the error to the health tracker (so the
        sensor reflects reality even for non-OpenAITTSError failures like
        TimeoutError, ValueError, generic Exception).
        """
        self._duration_cache.mark_failed(
            message,
            voice=(resolved or {}).get("voice"),
            model=(resolved or {}).get("model"),
            speed=(resolved or {}).get("speed"),
        )
        if self._health_tracker is not None:
            self._health_tracker.record_error(error)

    async def _handle_engine_error(self, err: BaseException) -> None:
        """Translate engine errors into the right HA-side reaction."""
        if self._health_tracker is not None:
            self._health_tracker.record_error(err)
        if isinstance(err, OpenAIAuthError):
            _LOGGER.error(
                "OpenAI TTS auth failed for %s, raising reauth: %s",
                self.entity_id, err,
            )
            raise ConfigEntryAuthFailed(str(err)) from err
        if isinstance(err, OpenAIQuotaExceededError):
            _LOGGER.error(
                "OpenAI TTS quota exhausted for %s: %s. "
                "Returning no audio so HA will NOT cache; cached entries are unaffected.",
                self.entity_id, err,
            )
            return
        if isinstance(err, OpenAIRateLimitError):
            _LOGGER.warning(
                "OpenAI TTS rate-limited for %s: %s", self.entity_id, err
            )
            return
        if isinstance(err, OpenAITTSError):
            _LOGGER.error("OpenAI TTS error for %s: %s", self.entity_id, err)
            return

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
    ) -> tuple[str | None, bytes | None]:
        """Legacy non-streaming TTS contract.

        Returns ``(None, None)`` whenever the result must NOT be cached
        (auth failure, quota exhausted, invalid audio, etc.). HA only caches
        when both elements of the tuple are non-None, so this is the safe
        signal to refuse cache entry.
        """
        _LOGGER.info(
            "async_get_tts_audio for %s (msg=%r, lang=%s)",
            self.entity_id, message[:50], language,
        )

        # Reset mode FIRST so volume_restore can distinguish "engine running
        # right now" from "engine never ran" (= HA cache hit).
        self._playback_mode = None
        self._engine_active = True
        self.async_write_ha_state()

        # Default {} so the except blocks below can pass it to _record_failure
        # even if _resolve_options() were to raise (it currently can't, but
        # this future-proofs against that path).
        resolved: dict[str, Any] = {}

        try:
            resolved = self._resolve_options(options)
            can_stream = (
                not resolved["chime_enable"] and not resolved["normalize_audio"]
            )
            # The legacy contract path is always atomic from the
            # media_player's perspective: HA buffers the whole tuple
            # before play_media.
            self._playback_mode = "atomic"
            self.async_write_ha_state()

            try:
                audio_data = await self._engine_get_blocking(
                    message, resolved, stream=can_stream
                )
            except asyncio.TimeoutError as err:
                _LOGGER.error("TTS generation timed out after 30 seconds")
                self._record_failure(message, err, resolved)
                return (None, None)

            if not is_valid_audio(audio_data, expected_format="mp3"):
                _LOGGER.error(
                    "TTS response failed audio validation (size=%d). "
                    "Refusing cache to prevent corruption (issue #64).",
                    len(audio_data) if audio_data else 0,
                )
                err = OpenAIInvalidResponseError(
                    f"Invalid audio response (size={len(audio_data) if audio_data else 0})"
                )
                self._record_failure(message, err, resolved)
                return (None, None)

            await self._record_duration(message, audio_data, resolved)

            audio_data = await self._maybe_post_process(audio_data, resolved)

            # Recalculate after post-processing changes the bytes.
            if resolved["chime_enable"] or resolved["normalize_audio"] or (
                detect_audio_format(audio_data) == "wav"
            ):
                await self._record_duration(message, audio_data, resolved)

            audio_with_metadata = await self.hass.async_add_executor_job(
                embed_duration_in_audio, audio_data, self._last_duration_ms or 0
            )
            return ("mp3", audio_with_metadata)

        except MaxLengthExceeded as err:
            _LOGGER.error("Maximum message length exceeded: %s", err)
            self._record_failure(message, err, resolved)
            raise
        except CancelledError:
            _LOGGER.debug("TTS generation was cancelled")
            raise
        except OpenAITTSError as err:
            # _handle_engine_error already records into the health tracker;
            # _record_failure here just adds the duration sentinel without
            # double-counting on the tracker.
            await self._handle_engine_error(err)
            self._duration_cache.mark_failed(
                message,
                voice=resolved.get("voice"), model=resolved.get("model"),
                speed=resolved.get("speed"),
            )
            return (None, None)
        except Exception as err:
            _LOGGER.error("Error generating TTS: %s", err, exc_info=True)
            self._record_failure(message, err, resolved)
            return (None, None)
        finally:
            self._engine_active = False
            self.async_write_ha_state()

    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Modern streaming TTS contract.

        Strategy depends on whether post-processing (chime / normalization)
        is required:

        * **No post-processing** -> stream-with-first-chunk-validation.
          Chunks are yielded as they arrive from OpenAI for low first-byte
          latency. The first ~1 KB is validated against MP3 magic bytes
          BEFORE being yielded; if it fails (e.g. JSON error body served
          with HTTP 200 by a misbehaving backend) we raise immediately so
          HA discards the half-written cache file.

        * **Post-processing required** -> atomic mode. Chime/normalize need
          the complete audio anyway, so we collect-then-validate-then-yield.

        On a true mid-stream network drop the engine raises and HA discards
        the partial file (HA's TTS cache only commits after the generator
        completes successfully).
        """
        _LOGGER.info("async_stream_tts_audio called for entity %s", self.entity_id)

        # Reset mode FIRST so volume_restore can distinguish "engine running
        # right now" from "engine never ran" (= HA cache hit).
        self._playback_mode = None
        self._engine_active = True
        self.async_write_ha_state()

        full_text = ""
        async for text_chunk in request.message_gen:
            full_text += text_chunk

        options = request.options or {}
        resolved = self._resolve_options(options)
        audio_format = "mp3"
        can_stream = self._can_use_streaming(full_text, options)

        # Tell volume_restore deterministically which path we're taking, so
        # it doesn't have to infer from speak-call latency. Must be set
        # BEFORE the response is returned to HA, since volume_restore reads
        # it the moment tts.speak completes.
        self._playback_mode = "streaming" if can_stream else "atomic"
        self.async_write_ha_state()

        _LOGGER.info(
            "Streaming TTS - voice: %s, model: %s, speed: %s, format: %s, "
            "mode: %s",
            resolved["voice"], resolved["model"], resolved["speed"],
            audio_format, "stream+validate" if can_stream else "atomic+postprocess",
        )

        if can_stream:
            return TTSAudioResponse(
                extension=audio_format,
                data_gen=self._stream_with_validation(
                    full_text, resolved, audio_format
                ),
            )

        # Atomic path: chime / normalize need the complete audio first.
        try:
            audio_data = await self._engine_get_blocking(
                full_text, resolved, stream=False
            )
            audio_data = await self._maybe_post_process(audio_data, resolved)
        except asyncio.TimeoutError as err:
            _LOGGER.error("TTS atomic generation timed out")
            self._record_failure(full_text, err, resolved)
            self._engine_active = False
            self.async_write_ha_state()
            return self._empty_response(audio_format)
        except OpenAITTSError as err:
            await self._handle_engine_error(err)
            self._duration_cache.mark_failed(
                full_text,
                voice=resolved.get("voice"), model=resolved.get("model"),
                speed=resolved.get("speed"),
            )
            self._engine_active = False
            self.async_write_ha_state()
            return self._empty_response(audio_format)
        except Exception as err:
            _LOGGER.error("Atomic TTS unexpected error: %s", err, exc_info=True)
            self._record_failure(full_text, err, resolved)
            self._engine_active = False
            self.async_write_ha_state()
            return self._empty_response(audio_format)

        if not is_valid_audio(audio_data, expected_format=audio_format):
            _LOGGER.error(
                "Atomic TTS response failed audio validation (size=%d). "
                "Refusing cache to prevent corruption (issue #64).",
                len(audio_data),
            )
            err = OpenAIInvalidResponseError(
                f"Invalid atomic audio (size={len(audio_data)})"
            )
            self._record_failure(full_text, err, resolved)
            self._engine_active = False
            self.async_write_ha_state()
            return self._empty_response(audio_format)

        duration_ms = await self._record_duration(full_text, audio_data, resolved)
        _LOGGER.info(
            "Atomic audio ready: %d bytes, %d ms", len(audio_data), duration_ms
        )

        self._engine_active = False
        self.async_write_ha_state()

        return TTSAudioResponse(
            extension=audio_format,
            data_gen=self._yield_in_chunks(audio_data),
        )

    async def _stream_with_validation(
        self,
        text: str,
        resolved: dict[str, Any],
        audio_format: str,
    ) -> AsyncGenerator[bytes, None]:
        """Stream chunks as they arrive; validate the first chunk before yielding.

        Engine guarantees the first yielded chunk is at least
        ``INITIAL_BUFFER_BYTES`` (1 KB) of buffered data, which is plenty to
        check magic bytes. Subsequent chunks pass through untouched.
        """
        all_chunks: list[bytes] = []
        first_yielded = False

        try:
            try:
                async for chunk in self._engine.async_get_tts_stream(
                    text=text,
                    response_format=audio_format,
                    voice=resolved["voice"],
                    model=resolved["model"],
                    speed=resolved["speed"],
                    instructions=resolved["instructions"],
                    extra_payload=resolved["extra_payload"],
                ):
                    all_chunks.append(chunk)

                    if not first_yielded:
                        if not is_valid_audio(chunk, expected_format=audio_format):
                            _LOGGER.error(
                                "First streamed chunk failed audio validation "
                                "(size=%d). Aborting to prevent cache poisoning "
                                "(issue #64).",
                                len(chunk),
                            )
                            raise OpenAIInvalidResponseError(
                                "First chunk is not valid audio"
                            )
                        first_yielded = True
                        _LOGGER.debug(
                            "First chunk passed validation (%d bytes), streaming",
                            len(chunk),
                        )

                    yield chunk

            except OpenAITTSError as err:
                await self._handle_engine_error(err)
                self._duration_cache.mark_failed(
                    text,
                    voice=resolved.get("voice"), model=resolved.get("model"),
                    speed=resolved.get("speed"),
                )
                raise
            except Exception as err:
                self._record_failure(text, err, resolved)
                raise

            # Stream completed cleanly: record duration so volume_restore
            # can find it in the shared cache by message hash, and tell the
            # health tracker that the API is responsive.
            if all_chunks:
                complete_audio = b"".join(all_chunks)
                duration_ms = await self._get_audio_duration(complete_audio)
                self._last_duration_ms = duration_ms
                self._duration_cache.store(
                    text, duration_ms,
                    voice=resolved.get("voice"), model=resolved.get("model"),
                    speed=resolved.get("speed"),
                )
                self.async_write_ha_state()
                await self._save_persisted_state()
                if self._health_tracker is not None:
                    self._health_tracker.record_success()
                _LOGGER.info(
                    "Streaming complete: %d bytes, %d ms",
                    len(complete_audio), duration_ms,
                )

        finally:
            self._engine_active = False
            self.async_write_ha_state()

    @staticmethod
    async def _yield_in_chunks(
        audio_data: bytes, chunk_size: int = 8192
    ) -> AsyncGenerator[bytes, None]:
        for i in range(0, len(audio_data), chunk_size):
            yield audio_data[i : i + chunk_size]

    @staticmethod
    def _empty_response(audio_format: str) -> TTSAudioResponse:
        """Return a zero-byte response so HA fails fast without caching anything."""

        async def _empty() -> AsyncGenerator[bytes, None]:
            if False:  # pragma: no cover - intentionally empty generator
                yield b""

        return TTSAudioResponse(extension=audio_format, data_gen=_empty())
