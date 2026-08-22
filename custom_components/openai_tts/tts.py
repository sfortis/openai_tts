"""TTS entity for the OpenAI TTS integration.

The entity implements the streaming ``async_stream_tts_audio`` contract
introduced in HA 2025.7, which is the minimum version this integration
supports. The legacy ``async_get_tts_audio`` contract is deliberately
absent: Home Assistant only falls back to it when an entity does not
override the streaming method, so on every supported version it was
unreachable code.

Audio bytes are validated against magic-byte signatures before they are
returned to Home Assistant, so a failed API call can never poison the HA
TTS cache (issue #64).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from functools import partial
from typing import Any, AsyncGenerator, AsyncIterable

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    Voice,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .api_health import OpenAITTSHealthTracker, health_tracker_for
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
    CONF_STREAM_PIPELINING,
    CONF_PROVIDER,
    CONF_SPEED,
    CONF_URL,
    CONF_VOICE,
    DEFAULT_AUDIO_FORMAT,
    DOMAIN,
    SUPPORTED_LANGUAGES,
    UNIQUE_ID,
    preset_for,
    is_openai_endpoint,
    voices_for_model,
)
from .entity_helpers import is_subentry, sanitize_profile_name
from .exceptions import (
    OpenAIAuthError,
    OpenAIInvalidResponseError,
    OpenAIQuotaExceededError,
    OpenAIRateLimitError,
    OpenAITTSError,
    OpenAIVoiceDeletedError,
)
from .openaitts_engine import OpenAITTSEngine
from .streaming import PIPELINEABLE_FORMATS, pipelined_audio_stream
from .repairs import create_voice_deleted_issue
from .voice_listing import CATALOGUE_TTL_S, async_fetch_voice_options
from .utils import (
    is_valid_audio,
    measure_audio_duration,
    process_audio,
    resolve_ffmpeg_paths,
)

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE_PROFILE = "profile"
# Entities here never poll: the TTS entity is driven by service calls
# and the health tracker pushes its own updates. Declaring this keeps
# Home Assistant from serialising entity updates it does not need to.
PARALLEL_UPDATES = 0

STORAGE_VERSION = 1
STORAGE_KEY = "openai_tts_state"

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
    """Look up the parent entry's health tracker, if it has one."""
    if not parent_entry_id:
        return None
    return health_tracker_for(
        hass.config_entries.async_get_entry(parent_entry_id)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
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

    # Attributes the recorder should not keep. The voice catalogue and
    # the ``current_*`` fields describe configuration, not history: the
    # catalogue is answered properly by ``async_get_supported_voices``,
    # and the ``current_*`` fields exist only so ``volume_restore`` can
    # rebuild a cache key from the live state machine, which never reads
    # history. ``failure_cache_size`` is a counter that changes with
    # almost every announcement, and it is that churn which made each
    # new attribute set carry another copy of everything else.
    #
    # ``media_duration`` is deliberately still recorded: the measured
    # length of what was spoken is the one value here worth charting.
    _unrecorded_attributes = frozenset({
        "available_voices",
        "failure_cache_size",
        "current_voice",
        "current_model",
        "current_speed",
        "current_instructions",
        "current_chime_enable",
        "current_chime_sound",
        "current_extra_payload",
    })

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
        # Keyed on the unique id, not the entity id. The entity id here
        # is only this class's suggestion, which drifts from the
        # registered one and is not unique: two profiles whose names
        # reduce to the same fragment shared a single state file.
        self._store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}_{self._attr_unique_id}"
        )
        self._stored_data: dict = {}
        # Key the duration cache on ``unique_id`` (stable across user-initiated
        # entity renames), NOT ``entity_id`` (which is whatever the user has
        # in the registry and can drift from the profile-derived entity_id we
        # compute internally). volume_restore looks up by unique_id for the
        # same reason.
        self._duration_cache = MessageDurationCache(hass, self._attr_unique_id)

        # Voices reported by the backend, for providers that can list
        # them. ``None`` means nothing has been fetched yet, which is
        # different from an empty list.
        self._voice_catalogue: list[dict[str, str]] | None = None
        self._voice_catalogue_at: float = 0.0
        self._voice_refresh_running = False

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
        """Suggest the entity id and set the display name.

        Every branch produces a valid entity id. Home Assistant only
        takes the id as a suggestion, and it keeps whatever is already
        registered against this unique id, so the ids below are what a
        newly created entity gets, not a rename of an existing one.
        """
        if is_subentry(self._config):
            profile_name = self._config.data.get(CONF_PROFILE_NAME, "profile")
            safe = sanitize_profile_name(profile_name)
            self.entity_id = f"tts.openai_tts_{safe}" if safe else "tts.openai_tts"
            self._attr_name = f"OpenAI TTS {profile_name}"
            return

        model = self._config.data.get(CONF_MODEL)
        if model:
            # Model names are free text for self-hosted backends, so they
            # arrive with capitals and slashes in them ("XTTS-v2",
            # "Kokoro/v1.0"). Slugify for the same reason as the profile
            # name: the id Home Assistant derives is unchanged, the
            # invalid suggestion is not made.
            model_suffix = slugify(model.replace("-", "_").replace(".", "_"))
            self.entity_id = (
                f"tts.openai_tts_{model_suffix}" if model_suffix
                else "tts.openai_tts"
            )
            self._attr_name = f"OpenAI TTS ({model})"
            return

        self.entity_id = "tts.openai_tts"
        self._attr_name = "OpenAI TTS"

    def _legacy_store_key(self) -> str:
        """Reproduce the storage key this entity used before 3.8.2.

        The per-entity store was keyed on the entity id this class
        computed, which the two changes above alter for names that were
        not already valid. Migration reads this key once so a profile
        called "Living Room - Main" keeps the durations it had measured.
        Deliberately duplicates the old string building rather than
        calling the shared helpers, which no longer produce it.
        """
        if is_subentry(self._config):
            name = self._config.data.get(CONF_PROFILE_NAME, "profile")
            lowered = name.lower().replace(" ", "_").replace("-", "_")
            safe = "".join(c for c in lowered if c.isalnum() or c == "_")
            old_entity_id = f"tts.openai_tts_{safe}"
        else:
            model = self._config.data.get(CONF_MODEL)
            old_entity_id = (
                f"tts.openai_tts_{model.replace('-', '_').replace('.', '_')}"
                if model else "tts.openai_tts"
            )
        return f"{STORAGE_KEY}_{old_entity_id}"

    # --- Persistent state --------------------------------------------------

    @property
    def available(self) -> bool:
        """False while the API is refusing every request.

        The health tracker blocks on a revoked key or an exhausted
        quota, and every call made in that state fails before it
        reaches the provider. Reporting the entity as available
        throughout, which is what this used to do, left the user with a
        speaker that silently said nothing and a working-looking entity.

        The block ages out on its own, so availability comes back
        without a reload once the underlying problem is fixed.
        """
        tracker = self._health_tracker
        return tracker is None or not tracker.blocks_requests()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_persisted_state()
        if self._supports_voice_listing():
            # In the background: a backend that takes the whole timeout
            # would otherwise hold up setup for every profile.
            self._schedule_voice_refresh()
        if self._health_tracker is not None:
            # Availability is derived from the tracker, so the entity has
            # to re-render when the tracker changes. Without this the
            # state only catches up on the next write from elsewhere.
            self.async_on_remove(
                self._health_tracker.async_add_listener(self.async_write_ha_state)
            )
        _LOGGER.info("TTS entity %s registered with Home Assistant", self.entity_id)

    async def async_will_remove_from_hass(self) -> None:
        _LOGGER.debug("TTS entity %s being removed from hass", self.entity_id)
        await self._save_persisted_state()
        await super().async_will_remove_from_hass()

    async def _restore_persisted_state(self) -> None:
        try:
            stored = await self._store.async_load()
            if not stored:
                stored = await self._adopt_legacy_store()
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

    async def _adopt_legacy_store(self) -> dict | None:
        """Move state written under the old entity-id key, once.

        Runs only when the unique-id key holds nothing, which is true on
        the first start after the upgrade and on a genuinely new entity.
        The old file is removed after a successful copy so it does not
        sit in ``.storage`` forever.
        """
        legacy_key = self._legacy_store_key()
        if legacy_key == f"{STORAGE_KEY}_{self._attr_unique_id}":
            return None
        legacy_store = Store(self.hass, STORAGE_VERSION, legacy_key)
        try:
            stored = await legacy_store.async_load()
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("Could not read %s: %s", legacy_key, err)
            return None
        if not stored:
            return None
        _LOGGER.info(
            "Migrating stored state for %s from %s to the unique-id key",
            self.entity_id, legacy_key,
        )
        await self._store.async_save(stored)
        try:
            await legacy_store.async_remove()
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("Could not remove %s: %s", legacy_key, err)
        return stored

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
            "available_voices": self._available_voice_ids(),
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

    def _available_voice_ids(self) -> list[str]:
        """Voice names for the attribute, matching what the entity offers.

        The same answer ``async_get_supported_voices`` gives, reduced to
        plain ids. It used to be the whole static table regardless of
        provider or model, so a profile on a self-hosted backend
        advertised thirteen OpenAI voices it could not speak, and a
        profile on ``tts-1`` advertised four its model rejects.
        """
        voices = self.async_get_supported_voices(self.default_language)
        return [voice.voice_id for voice in voices or []]

    @callback
    def async_get_supported_voices(self, language: str) -> list[Voice] | None:
        """Return the voices to offer for this entity.

        This fills the voice dropdown Home Assistant shows for a TTS
        engine, on the voice assistant screens and in the developer
        tools. Home Assistant calls it synchronously, so it can only
        read what has already been fetched; the fetch runs in the
        background from ``async_added_to_hass`` and is refreshed from
        here once it has gone stale.

        Which answer is right depends on the provider. OpenAI publishes
        no voices endpoint, so its catalogue is the static table, and it
        is filtered by the profile's model because ``tts-1`` and
        ``tts-1-hd`` reject the newer voices. Everything else is asked
        directly, since a Mistral account's voices are cloned by its
        owner and a Kokoro server's are whatever is installed there.
        """
        if not self._supports_voice_listing():
            model = self._get_config_value(CONF_MODEL) or self._engine._model
            return [
                Voice(voice_id=name, name=name.capitalize())
                for name in voices_for_model(model)
            ]

        if self._voice_catalogue is None:
            # Nothing fetched yet. Offering the configured voice alone
            # beats an empty dropdown, and the background refresh will
            # fill it in.
            current = self._get_config_value(CONF_VOICE) or self._engine._voice
            return [Voice(voice_id=current, name=current)] if current else None

        if self.hass.loop.time() - self._voice_catalogue_at > CATALOGUE_TTL_S:
            self._schedule_voice_refresh()

        return [
            Voice(voice_id=option["value"], name=option["label"])
            for option in self._voice_catalogue
        ] or None

    def _supports_voice_listing(self) -> bool:
        """True when this profile's provider can be asked for its voices.

        Asking is the default. Only OpenAI opts out, and it does so
        because it demonstrably has no such endpoint.
        """
        parent = self._parent_entry or self._config
        provider = parent.data.get(CONF_PROVIDER) if parent is not None else None
        if provider:
            return (
                preset_for(provider).get("supports_voice_listing", True) is not False
            )
        # No provider recorded, which is every entry created before the
        # presets existed. ``preset_for(None)`` answers OpenAI, so asking
        # it here would silence discovery for exactly the self-hosted
        # entries that need it. Decide by the endpoint instead, the same
        # way the config flow does when the key is missing.
        return not is_openai_endpoint(self._speech_url())

    def _speech_url(self) -> str | None:
        """The configured speech endpoint, from wherever it is stored."""
        parent = self._parent_entry or self._config
        return self._config.data.get(CONF_URL) or (
            parent.data.get(CONF_URL) if parent is not None else None
        )

    @callback
    def _schedule_voice_refresh(self) -> None:
        """Fetch the catalogue in the background, one fetch at a time."""
        if self._voice_refresh_running:
            return
        entry = self._parent_entry or self._config
        creator = getattr(entry, "async_create_background_task", None)
        if creator is None:
            return
        self._voice_refresh_running = True
        creator(
            self.hass,
            self._async_refresh_voice_catalogue(),
            f"openai_tts voice catalogue {self.entity_id}",
        )

    async def _async_refresh_voice_catalogue(self) -> None:
        """Ask the backend for its voices and remember the answer."""
        try:
            speech_url = self._speech_url()
            if not speech_url:
                return
            parent = self._parent_entry or self._config
            api_key = parent.data.get(CONF_API_KEY) if parent is not None else None
            options = await async_fetch_voice_options(
                self.hass, speech_url, api_key
            )
            if options is None:
                # Keep whatever was there. A backend that is briefly
                # unreachable should not empty the dropdown.
                _LOGGER.debug(
                    "No voice catalogue from %s; keeping the %d cached",
                    speech_url, len(self._voice_catalogue or []),
                )
                return
            self._voice_catalogue = options
            self._voice_catalogue_at = self.hass.loop.time()
            _LOGGER.debug(
                "Voice catalogue for %s: %d voices", self.entity_id, len(options)
            )
        finally:
            self._voice_refresh_running = False

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
        """Return audio duration in milliseconds via ffprobe.

        The temporary file that ffprobe needs is written and deleted
        inside the executor along with the probe itself, so none of the
        three blocking steps runs on the event loop.
        """
        _, ffprobe = resolve_ffmpeg_paths(self.hass)
        duration_seconds = await self.hass.async_add_executor_job(
            partial(measure_audio_duration, audio_data, ffprobe=ffprobe)
        )
        return int(duration_seconds * 1000)

    def _can_use_streaming(self, text: str, options: dict) -> bool:
        if options.get(CONF_CHIME_ENABLE) or options.get(CONF_NORMALIZE_AUDIO):
            return False
        # Some backends (older self-hosted servers, pocket-tts variants
        # that don't speak chunked HTTP cleanly) deliver corrupted audio
        # via the streaming path even when a single blocking read works.
        # The parent entry's preset can flip this off, which sends the
        # request down the atomic branch of ``async_stream_tts_audio``:
        # one blocking fetch, validate, then yield the whole clip.
        parent = self._parent_entry or self._config
        provider_key = (
            parent.data.get(CONF_PROVIDER) if parent is not None else None
        )
        if not preset_for(provider_key).get("supports_streaming", True):
            return False
        return len(text) >= MIN_STREAMING_TEXT_LENGTH

    def _pipelining_allowed(self, options: dict, audio_format: str) -> bool:
        """True when this request may synthesise sentence by sentence.

        Deliberately does not look at the text: the whole point is to
        decide before the text has been read. Whether pipelining
        actually happens still depends on the text arriving gradually,
        which ``pipelined_audio_stream`` works out for itself. A plain
        ``openai_tts.say`` message reaches it as one chunk and collapses
        to a single request.
        """
        if not self._get_config_value(CONF_STREAM_PIPELINING):
            return False
        if audio_format not in PIPELINEABLE_FORMATS:
            # mp3 and friends would need several container headers in one
            # stream. See the note in streaming.py.
            _LOGGER.debug(
                "Pipelining is enabled but format %s cannot be joined; "
                "falling back to a single request", audio_format,
            )
            return False
        if options.get(CONF_CHIME_ENABLE) or options.get(CONF_NORMALIZE_AUDIO):
            # Both need the finished audio, so there is nothing to gain.
            return False
        parent = self._parent_entry or self._config
        provider_key = (
            parent.data.get(CONF_PROVIDER) if parent is not None else None
        )
        return bool(preset_for(provider_key).get("supports_streaming", True))

    async def _pipelined_stream(
        self,
        message_gen: AsyncIterable[str],
        resolved: dict[str, Any],
        audio_format: str,
    ) -> AsyncGenerator[bytes, None]:
        """Bridge the pipelining helper to this entity's engine.

        Whether a duration gets recorded depends on how the run went.

        When the text arrived complete and went out as one request, the
        emitted bytes are a whole clip and its duration is recorded
        exactly as the non-pipelined path does. That case matters more
        than it looks: ``openai_tts.say`` always lands there, and
        ``volume_restore`` waits up to a minute for that cache entry
        before falling back. Skipping the write cost the service call 77
        seconds instead of 9.

        When the reply was synthesised in pieces, no duration is
        written. The total was never known in one place, and a wrong
        figure would mis-size a volume hold, whereas a missing one makes
        the restorer fall back cleanly.
        """

        async def _synthesize(text: str) -> AsyncGenerator[bytes, None]:
            first = True
            async for chunk in self._engine.async_get_tts_stream(
                text=text,
                response_format=audio_format,
                voice=resolved["voice"],
                model=resolved["model"],
                speed=resolved["speed"],
                instructions=resolved["instructions"],
                extra_payload=resolved["extra_payload"],
            ):
                if first:
                    # Same guard as the single-request path: a backend
                    # serving a JSON error body with HTTP 200 must not
                    # reach the cache (issue #64).
                    if not is_valid_audio(chunk, expected_format=audio_format):
                        raise OpenAIInvalidResponseError(
                            "First pipelined chunk is not valid audio"
                        )
                    first = False
                yield chunk

        def _spawn(coro: Any, name: str) -> Any:
            return self.hass.async_create_background_task(coro, name)

        stats: dict[str, Any] = {}
        collected: list[bytes] = []
        try:
            async for chunk in pipelined_audio_stream(
                message_gen, _synthesize, audio_format, _spawn, stats
            ):
                collected.append(chunk)
                yield chunk
        except OpenAITTSError as err:
            await self._handle_engine_error(err)
            raise
        else:
            if self._health_tracker is not None:
                self._health_tracker.record_success()
            if stats.get("single_request") and collected:
                await self._record_duration(
                    stats.get("raw_text", ""), b"".join(collected), resolved
                )

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
        audio_task = self.hass.async_add_executor_job(
            partial(
                self._engine.get_tts,
                text,
                speed=resolved["speed"],
                voice=resolved["voice"],
                model=resolved["model"],
                instructions=resolved["instructions"],
                extra_payload=resolved["extra_payload"],
                response_format=resolved.get("audio_format", DEFAULT_AUDIO_FORMAT),
            )
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
        transcode, and that path keeps the profile's own format rather
        than forcing mp3.
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
                "OpenAI TTS auth failed for %s, asking the user to "
                "reauthenticate: %s",
                self.entity_id, err,
            )
            self._start_reauth()
            return
        if isinstance(err, OpenAIVoiceDeletedError):
            self._raise_voice_deleted_repair(err)
            return
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

    def _start_reauth(self) -> None:
        """Ask Home Assistant to show the reauthentication card.

        This used to raise ``ConfigEntryAuthFailed`` instead. Home
        Assistant only turns that exception into a reauth flow when it
        comes out of entry setup or a coordinator refresh, never from an
        entity method, so on a revoked key the user saw a raw exception
        out of ``tts.speak`` and the whole reauth step in the config flow
        was unreachable.

        The flow is started on the entry that actually holds the
        credentials. For a profile that is a subentry, that is its
        parent. Starting a flow while one is already in progress is a
        no-op in Home Assistant, so a burst of failed announcements
        produces a single card.
        """
        entry = self._parent_entry or self._config
        starter = getattr(entry, "async_start_reauth", None)
        if starter is None:
            # A subentry with no parent recorded. Nothing here can carry
            # a reauth flow, so the error has already been logged and
            # the health tracker has it.
            _LOGGER.debug(
                "No config entry available to start reauth for %s",
                self.entity_id,
            )
            return
        starter(self.hass)

    def _raise_voice_deleted_repair(self, err: "OpenAIVoiceDeletedError") -> None:
        """Surface a Repairs panel issue for an upstream-deleted voice.

        Raised when we have no static catalogue that could have caught
        the bad voice at configuration time. Two cases qualify:
        providers whose catalogue changes between sessions (Mistral's
        user-cloned voices, Kokoro's installed voicepacks) and
        self-hosted backends, where the voice is free text and only the
        backend knows what is valid.

        Skipped for providers with a static catalogue (OpenAI's 13
        built-ins, Groq's 6 Orpheus voices, Lemonfox's Kokoro list).
        There the config flow already offered the valid names, so the
        same error really means the user typed something incompatible.
        That is a config mistake, not an external state change, and a
        Repairs notice on top of a typo is noise.

        Best-effort: needs both the parent entry id (to scope the
        issue correctly) and the subentry id (one issue per profile).
        Legacy entries that don't carry a subentry id silently fall
        back to plain logging.
        """
        _LOGGER.error(
            "OpenAI TTS voice %s rejected upstream for %s: %s",
            err.voice, self.entity_id, err,
        )

        parent_entry = self._parent_entry or self._config
        provider_key = (
            parent_entry.data.get(CONF_PROVIDER) if parent_entry is not None else None
        )
        preset = preset_for(provider_key)
        endpoint_url = (
            parent_entry.data.get(CONF_URL) if parent_entry is not None else None
        )
        # A static catalogue exists either as an explicit preset list or,
        # for OpenAI itself, as the built-in ``VOICES`` the picker is
        # locked to. Note the endpoint check rather than the preset name:
        # an entry predating the provider wizard resolves to the OpenAI
        # preset even when it points at a self-hosted backend, and those
        # users do need the notice.
        has_static_catalog = (
            bool(preset.get("voice_catalog"))
            or is_openai_endpoint(endpoint_url)
        )
        if has_static_catalog and not preset.get("supports_voice_listing"):
            _LOGGER.debug(
                "Not raising a repair for %s: provider has a static voice "
                "catalogue, so the rejected voice is a configuration typo",
                self.entity_id,
            )
            return

        parent_entry_id = (
            self._parent_entry.entry_id if self._parent_entry else None
        )
        subentry_id = getattr(self._config, "subentry_id", None)
        profile_name = self._config.data.get(CONF_PROFILE_NAME) or self.entity_id
        if parent_entry_id and subentry_id:
            create_voice_deleted_issue(
                self.hass,
                parent_entry_id,
                subentry_id,
                profile_name,
                err.voice,
            )

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

        options = request.options or {}
        resolved = self._resolve_options(options)

        # Decide on pipelining BEFORE touching the generator: draining it
        # is exactly the thing that makes early synthesis impossible.
        pipeline_format = resolved.get("audio_format", DEFAULT_AUDIO_FORMAT)
        if self._pipelining_allowed(options, pipeline_format):
            _LOGGER.info(
                "Streaming TTS - voice: %s, model: %s, format: %s, "
                "mode: sentence-pipelined",
                resolved["voice"], resolved["model"], pipeline_format,
            )
            return TTSAudioResponse(
                extension=pipeline_format,
                data_gen=self._pipelined_stream(
                    request.message_gen, resolved, pipeline_format
                ),
            )

        full_text = ""
        async for text_chunk in request.message_gen:
            full_text += text_chunk

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

        # Validate the raw engine response BEFORE post-processing.
        # Post-processing re-encodes, so a bad payload could come out the
        # far side wearing valid magic bytes for the requested format and
        # slip into the cache.
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

        # Post-processing stays in the requested format end to end. The
        # ``filter_complex`` graph in ``build_ffmpeg_command`` re-samples
        # both the chime and the TTS input to a common PCM layout before
        # concat and picks the right encoder for the output. HA still has
        # its ``preferred_format`` ffmpeg layer as a safety net if the
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
            # Mark the cache BEFORE delegating to _handle_engine_error.
            # The sentinel is what tells volume_restore that no audio is
            # coming; writing it first means the signal is already in
            # place whatever that helper decides to do with the error.
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
