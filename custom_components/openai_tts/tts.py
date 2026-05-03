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
    CONF_AUDIO_FORMAT,
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
    DEFAULT_AUDIO_FORMAT,
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

        engine = OpenAITTSEngine(api_key, voice, model, speed, url, hass=hass)
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

        engine = OpenAITTSEngine(api_key, voice, model, speed, url, hass=hass)
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

        # Last computed audio duration in ms. No longer used for restore
        # timing (volume_restore drives off speaker state events) but
        # kept as an extra_state_attribute for UI/debug visibility.
        self._last_duration_ms: int | None = None
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
        # NOTE: every ``current_*`` field below is part of the duration
        # cache key. volume_restore reads them when an option is omitted
        # from the service call so its lookup hash matches what tts.py
        # used at store time. Removing one breaks cache lookups and
        # forces fallback timing.
        return {
            "media_duration": self._last_duration_ms,
            "failure_cache_size": self._duration_cache.size,
            "available_voices": VOICES,
            "current_voice": self._get_config_value(CONF_VOICE) or self._engine._voice,
            "current_model": self._get_config_value(CONF_MODEL) or self._engine._model,
            "current_speed": self._get_config_value(CONF_SPEED) or self._engine._speed,
            "current_instructions": self._get_config_value(CONF_INSTRUCTIONS),
            "current_chime_enable": self._get_config_value(CONF_CHIME_ENABLE) or False,
            "current_chime_sound": self._get_config_value(CONF_CHIME_SOUND),
            "current_extra_payload": self._get_config_value(CONF_EXTRA_PAYLOAD),
        }

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_languages(self) -> list[str]:
        return SUPPORTED_LANGUAGES

    @property
    def supported_options(self) -> list[str]:
        # ``preferred_format`` MUST be declared here even though we never
        # read it from service options ourselves: HA core only honours it
        # for URL extension and ffmpeg conversion when the entity claims
        # support for it (otherwise it is popped from options and the URL
        # defaults to .mp3, breaking opus/wav/etc. delivery to Cast).
        return [
            CONF_VOICE,
            CONF_MODEL,
            CONF_SPEED,
            CONF_CHIME_ENABLE,
            CONF_CHIME_SOUND,
            CONF_NORMALIZE_AUDIO,
            CONF_INSTRUCTIONS,
            CONF_EXTRA_PAYLOAD,
            CONF_AUDIO_FORMAT,
            "preferred_format",
        ]

    @property
    def default_options(self) -> dict[str, Any]:
        """Default option values that participate in the HA TTS cache key.

        Every key in here ends up in HA's TTS-cache hash, so anything
        that materially changes the produced audio MUST be listed -
        otherwise editing the profile (e.g. swapping
        ``instructions`` or ``extra_payload``) leaves stale cached
        audio playable for the same text.

        ``preferred_format`` is HA's own key (``ATTR_PREFERRED_FORMAT``):
        it controls both the proxy URL extension (``<token>.<ext>``) and
        the optional ffmpeg conversion before the audio reaches the
        media player. Without it the URL falls back to ``.mp3`` while
        we may be streaming opus / wav / etc., and Cast targets reject
        the content-type / extension mismatch.
        """
        audio_format = self._get_config_value(CONF_AUDIO_FORMAT, DEFAULT_AUDIO_FORMAT)
        return {
            CONF_VOICE: self._get_config_value(CONF_VOICE) or self._engine._voice,
            CONF_MODEL: self._get_config_value(CONF_MODEL) or self._engine._model,
            CONF_SPEED: self._get_config_value(CONF_SPEED) or self._engine._speed,
            CONF_CHIME_ENABLE: self._get_config_value(CONF_CHIME_ENABLE, False),
            CONF_CHIME_SOUND: self._get_config_value(CONF_CHIME_SOUND, "threetone.mp3"),
            CONF_NORMALIZE_AUDIO: self._get_config_value(CONF_NORMALIZE_AUDIO, False),
            CONF_INSTRUCTIONS: self._get_config_value(CONF_INSTRUCTIONS),
            CONF_EXTRA_PAYLOAD: self._get_config_value(CONF_EXTRA_PAYLOAD),
            CONF_AUDIO_FORMAT: audio_format,
            "preferred_format": audio_format,
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
            "audio_format": (
                opts.get(CONF_AUDIO_FORMAT)
                or self._get_config_value(CONF_AUDIO_FORMAT)
                or DEFAULT_AUDIO_FORMAT
            ),
        }

    async def _engine_get_blocking(
        self, text: str, resolved: dict[str, Any]
    ) -> bytes:
        """Run the blocking engine in an executor and return the raw audio.

        The whole HTTP body is read INSIDE the executor (the engine no
        longer offers a lazy variant), so the event loop never blocks
        on socket I/O.
        """
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
                response_format=resolved.get("audio_format", DEFAULT_AUDIO_FORMAT),
            ),
        )
        audio_response = await asyncio.wait_for(audio_task, timeout=30.0)

        if not audio_response or not audio_response.content:
            raise OpenAIInvalidResponseError("Empty audio response")
        return audio_response.content

    async def _maybe_post_process(
        self, audio_data: bytes, resolved: dict[str, Any]
    ) -> bytes:
        """Apply chime + normalization when requested.

        When chime/normalize are off, returns the engine bytes unchanged
        regardless of format - the streaming path or HA's own
        ``preferred_format`` ffmpeg layer handles delivery to the
        media_player. Only chime/normalize need the heavy local
        transcode (and that path always outputs mp3).
        """
        chime_enable = resolved["chime_enable"]
        normalize_audio = resolved["normalize_audio"]

        if not (chime_enable or normalize_audio):
            return audio_data

        requested_format = resolved.get("audio_format", DEFAULT_AUDIO_FORMAT)

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
            input_format=requested_format,
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
        # Persist measured duration so volume_restore can look it up
        # even on subsequent HA-cache hits where the engine doesn't run.
        r = resolved or {}
        self._duration_cache.store_duration(
            message, duration_ms,
            voice=r.get("voice"), model=r.get("model"), speed=r.get("speed"),
            instructions=r.get("instructions"),
            chime=r.get("chime_enable"), chime_sound=r.get("chime_sound"),
            extra_payload=r.get("extra_payload"),
        )
        self.async_write_ha_state()
        await self._save_persisted_state()
        if self._health_tracker is not None:
            self._health_tracker.record_success()
        return duration_ms

    def _mark_failed_with_resolved(
        self,
        message: str,
        resolved: dict[str, Any] | None,
    ) -> None:
        """Stamp a failure sentinel on the cache key derived from ``resolved``."""
        r = resolved or {}
        self._duration_cache.mark_failed(
            message,
            voice=r.get("voice"), model=r.get("model"), speed=r.get("speed"),
            instructions=r.get("instructions"),
            chime=r.get("chime_enable"), chime_sound=r.get("chime_sound"),
            extra_payload=r.get("extra_payload"),
        )

    def _clear_failure_sentinel(
        self,
        message: str,
        resolved: dict[str, Any] | None,
    ) -> None:
        """Drop any stale failure sentinel for the given resolved key.

        Called at the START of every engine invocation so a retry
        doesn't inherit the previous attempt's failure flag.
        """
        r = resolved or {}
        self._duration_cache.clear_failure(
            message,
            voice=r.get("voice"), model=r.get("model"), speed=r.get("speed"),
            instructions=r.get("instructions"),
            chime=r.get("chime_enable"), chime_sound=r.get("chime_sound"),
            extra_payload=r.get("extra_payload"),
        )

    def _record_failure(
        self,
        message: str,
        error: BaseException,
        resolved: dict[str, Any] | None = None,
    ) -> None:
        """Centralised bookkeeping for any TTS failure path.

        Marks the message as failed so volume_restore skips the playback
        wait (no audio is coming) AND surfaces the error to the health
        tracker so the API-status sensor reflects reality.
        """
        self._mark_failed_with_resolved(message, resolved)
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

        # Default {} so the except blocks below can pass it to _record_failure
        # even if _resolve_options() were to raise (it currently can't, but
        # this future-proofs against that path).
        resolved: dict[str, Any] = {}

        try:
            resolved = self._resolve_options(options)
            # Drop stale failure sentinel from a previous run on the
            # same key so volume_restore doesn't trigger an immediate
            # restore against a now-recovering call.
            self._clear_failure_sentinel(message, resolved)

            try:
                audio_data = await self._engine_get_blocking(message, resolved)
            except asyncio.TimeoutError as err:
                _LOGGER.error("TTS generation timed out after 30 seconds")
                self._record_failure(message, err, resolved)
                return (None, None)

            requested_format = resolved.get("audio_format", DEFAULT_AUDIO_FORMAT)
            if not is_valid_audio(audio_data, expected_format=requested_format):
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
            actual_format = detect_audio_format(audio_data)
            return (actual_format, audio_with_metadata)

        except MaxLengthExceeded as err:
            _LOGGER.error("Maximum message length exceeded: %s", err)
            self._record_failure(message, err, resolved)
            raise
        except CancelledError:
            _LOGGER.debug("TTS generation was cancelled")
            raise
        except OpenAITTSError as err:
            # Mark the cache BEFORE _handle_engine_error: that helper
            # raises ConfigEntryAuthFailed on auth errors and would
            # otherwise skip the post-call mark_failed, leaving
            # volume_restore without its immediate-restore signal.
            self._mark_failed_with_resolved(message, resolved)
            await self._handle_engine_error(err)
            return (None, None)
        except Exception as err:
            _LOGGER.error("Error generating TTS: %s", err, exc_info=True)
            self._record_failure(message, err, resolved)
            return (None, None)

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

        full_text = ""
        async for text_chunk in request.message_gen:
            full_text += text_chunk

        options = request.options or {}
        resolved = self._resolve_options(options)
        # Drop any failure sentinel from a previous attempt with the
        # same key BEFORE volume_restore can read it. Otherwise a
        # retry sees the stale 0 immediately after tts.speak returns
        # and triggers an immediate-restore + raise even though the
        # current stream is still in flight and may succeed.
        self._clear_failure_sentinel(full_text, resolved)
        audio_format = resolved.get("audio_format", DEFAULT_AUDIO_FORMAT)
        can_stream = self._can_use_streaming(full_text, options)

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
            audio_data = await self._engine_get_blocking(full_text, resolved)
        except asyncio.TimeoutError as err:
            _LOGGER.error("TTS atomic generation timed out")
            self._record_failure(full_text, err, resolved)
            return self._empty_response(audio_format)
        except OpenAITTSError as err:
            # See note in _stream_with_validation: mark_failed must
            # run before _handle_engine_error or auth failures bypass
            # the sentinel write.
            self._mark_failed_with_resolved(full_text, resolved)
            await self._handle_engine_error(err)
            return self._empty_response(audio_format)
        except Exception as err:
            _LOGGER.error("Atomic TTS unexpected error: %s", err, exc_info=True)
            self._record_failure(full_text, err, resolved)
            return self._empty_response(audio_format)

        # Validate the raw engine response BEFORE post-processing: the
        # validator checks magic bytes against the requested format, but
        # post-processing always emits mp3 regardless of input.
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
            return self._empty_response(audio_format)

        try:
            audio_data = await self._maybe_post_process(audio_data, resolved)
        except Exception as err:
            _LOGGER.error("Atomic TTS post-processing failed: %s", err, exc_info=True)
            self._record_failure(full_text, err, resolved)
            return self._empty_response(audio_format)

        # Post-processing now stays in the requested format end to end:
        # ``ensure_chime_in_format`` transcodes the chime to match, and
        # ``build_ffmpeg_command`` picks the right encoder for the
        # output. The bytes that come out of ``_maybe_post_process``
        # therefore match the requested ``audio_format`` regardless of
        # whether chime/normalize ran. HA still has its
        # ``preferred_format`` ffmpeg layer as a safety net if the
        # downstream player needs a different container.
        delivered_format = audio_format

        duration_ms = await self._record_duration(full_text, audio_data, resolved)
        _LOGGER.info(
            "Atomic audio ready: %d bytes, %d ms (delivered as %s)",
            len(audio_data), duration_ms, delivered_format,
        )

        return TTSAudioResponse(
            extension=delivered_format,
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
            # Mark the cache BEFORE delegating to _handle_engine_error
            # because that helper raises ConfigEntryAuthFailed on auth
            # errors, which would skip the post-call mark_failed and
            # leave volume_restore polling for 60s without the
            # immediate-restore signal.
            self._mark_failed_with_resolved(text, resolved)
            await self._handle_engine_error(err)
            raise
        except Exception as err:
            self._record_failure(text, err, resolved)
            raise

        # Stream completed cleanly: store the measured duration so
        # volume_restore can use it on this AND on subsequent HA-cache
        # hits, and tell the health tracker the API is responsive.
        if all_chunks:
            complete_audio = b"".join(all_chunks)
            duration_ms = await self._get_audio_duration(complete_audio)
            self._last_duration_ms = duration_ms
            r = resolved or {}
            self._duration_cache.store_duration(
                text, duration_ms,
                voice=r.get("voice"), model=r.get("model"), speed=r.get("speed"),
                instructions=r.get("instructions"),
                chime=r.get("chime_enable"), chime_sound=r.get("chime_sound"),
                extra_payload=r.get("extra_payload"),
            )
            self.async_write_ha_state()
            await self._save_persisted_state()
            if self._health_tracker is not None:
                self._health_tracker.record_success()
            _LOGGER.info(
                "Streaming complete: %d bytes, %d ms",
                len(complete_audio), duration_ms,
            )

    @staticmethod
    async def _yield_in_chunks(
        audio_data: bytes, chunk_size: int = 8192
    ) -> AsyncGenerator[bytes, None]:
        for i in range(0, len(audio_data), chunk_size):
            yield audio_data[i : i + chunk_size]

    @staticmethod
    def _empty_response(audio_format: str) -> TTSAudioResponse:
        """Return a generator that raises so HA refuses to cache the failure.

        HA persists the bytes from ``data_gen`` to disk under
        ``<cache_key>.<extension>``. A previous version of this helper
        returned an empty generator, which made HA happily store a
        0-byte file - then on the next request with the same cache_key
        HA served those 0 bytes and ffmpeg blew up with "Invalid data
        found when processing input" (issue #64 cache poisoning, opus
        edition). Raising inside the generator triggers HA's
        ``_load_data_into_cache`` exception path, which discards the
        mem-cache entry and skips the disk write.
        """

        async def _fail() -> AsyncGenerator[bytes, None]:
            raise OpenAIInvalidResponseError(
                "TTS engine returned no audio for this request"
            )
            yield b""  # pragma: no cover - keeps this an async generator

        return TTSAudioResponse(extension=audio_format, data_gen=_fail())
