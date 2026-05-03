"""Per-message audio duration + failure sentinel cache.

``volume_restore`` needs to hold the speaker volume for the length of
the TTS playback before restoring it. The cleanest deterministic
source for that length is what the engine itself measured from the
generated audio bytes (ffprobe). That measurement is stable across
runs - identical ``(message, voice, model, speed, instructions, chime,
chime_sound, extra_payload)`` produces identical audio bytes, hence
identical duration. We cache that measurement so volume_restore can
look it up regardless of whether the engine ran for THIS specific
call (e.g. an HA cache hit where the engine is bypassed entirely).

Two storage layers:

1. ``self._local`` - per-entity, persisted via ``Store`` so durations
   and failure sentinels survive an HA restart.
2. ``hass.data[DOMAIN][MESSAGE_DURATIONS_KEY]`` - shared, read by
   ``volume_restore`` which doesn't have direct entity access.

A failure sentinel (``DURATION_FAILED_SENTINEL = 0``) signals that the
last engine attempt for this message failed, so volume_restore should
short-circuit the playback wait instead of holding volume for audio
that will never play.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from .const import DOMAIN, MESSAGE_DURATIONS_KEY

_LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_LOCAL_ENTRIES = 100
DEFAULT_MAX_SHARED_ENTRIES = 50

# Sentinel value indicating the last TTS attempt for this message
# failed. Keeps the value space simple: the only thing the cache ever
# stores is the literal 0; any present entry means "skip the playback
# wait, the audio isn't coming."
DURATION_FAILED_SENTINEL = 0


def hash_message(
    message: str,
    *,
    entity_id: str | None = None,
    voice: str | None = None,
    model: str | None = None,
    speed: float | None = None,
    instructions: str | None = None,
    chime: bool | None = None,
    chime_sound: str | None = None,
    extra_payload: str | None = None,
) -> str:
    """Return a short stable hash that uniquely identifies a TTS request.

    Folds in every dimension that materially changes the produced
    audio bytes - and therefore the playback duration:

    * ``voice``/``model``/``speed`` - render-engine settings
    * ``instructions`` - GPT-4o-mini-tts pacing/emotion changes
    * ``chime`` (+ ``chime_sound`` when chime is on) - prepends extra audio
    * ``extra_payload`` - opaque custom-backend params

    ``normalize_audio`` is intentionally NOT in the key because it
    only affects loudness, not length.
    """
    parts = [message]
    if entity_id:
        parts.append(f"|e={entity_id}")
    if voice:
        parts.append(f"|v={voice}")
    if model:
        parts.append(f"|m={model}")
    if speed is not None:
        parts.append(f"|s={speed}")
    if instructions:
        parts.append(f"|i={instructions}")
    if chime:
        parts.append("|c=1")
        if chime_sound:
            parts.append(f"|cs={chime_sound}")
    if extra_payload:
        parts.append(f"|x={extra_payload}")
    return hashlib.md5("".join(parts).encode()).hexdigest()[:16]


class MessageDurationCache:
    """Tracks per-message audio duration AND failure sentinel."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        max_local_entries: int = DEFAULT_MAX_LOCAL_ENTRIES,
        max_shared_entries: int = DEFAULT_MAX_SHARED_ENTRIES,
    ) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._local: dict[str, int] = {}
        self._max_local = max_local_entries
        self._max_shared = max_shared_entries

    @property
    def size(self) -> int:
        return len(self._local)

    @property
    def snapshot(self) -> dict[str, int]:
        return dict(self._local)

    def restore(self, stored: dict[str, int]) -> None:
        """Restore durations + failure sentinels from persisted state."""
        if not isinstance(stored, dict):
            return
        self._local = {k: int(v) for k, v in stored.items()
                       if isinstance(v, (int, float))}
        shared = self._ensure_shared_dict()
        for msg_hash, dur in self._local.items():
            shared[msg_hash] = {
                "duration_ms": dur,
                "timestamp": 0,
                "entity_id": self._entity_id,
            }
        if self._local:
            _LOGGER.info(
                "Restored %d cached entries (durations + sentinels)",
                len(self._local),
            )

    def store_duration(
        self,
        message: str,
        duration_ms: int,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
        instructions: str | None = None,
        chime: bool | None = None,
        chime_sound: str | None = None,
        extra_payload: str | None = None,
    ) -> None:
        """Persist the measured audio duration after a successful generation.

        Subsequent calls with identical render parameters (including
        HA-cache hits where our engine is bypassed) will look up this
        value via ``get_duration``.
        """
        msg_hash = self._hash(
            message, voice, model, speed, instructions,
            chime, chime_sound, extra_payload,
        )
        self._local[msg_hash] = duration_ms
        self._evict_local()
        self._publish_shared(msg_hash, duration_ms)

    def get_duration(
        self,
        message: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
        instructions: str | None = None,
        chime: bool | None = None,
        chime_sound: str | None = None,
        extra_payload: str | None = None,
    ) -> int | None:
        """Return cached duration for the request, or None if absent.

        Returns ``DURATION_FAILED_SENTINEL`` (0) when the last attempt
        failed; callers should treat that as "skip the playback wait".
        """
        msg_hash = self._hash(
            message, voice, model, speed, instructions,
            chime, chime_sound, extra_payload,
        )
        return self._local.get(msg_hash)

    def clear_failure(self, message: str, **render_args) -> None:
        """Drop a failure sentinel after a successful run.

        Note: when the success path also calls ``store_duration``, this
        is redundant (store overwrites). Kept for paths that succeed
        without measuring duration (e.g. cache-hit success signals).
        """
        msg_hash = self._hash_kwargs(message, render_args)
        if self._local.get(msg_hash) == DURATION_FAILED_SENTINEL:
            self._local.pop(msg_hash, None)
            shared = self._ensure_shared_dict()
            shared.pop(msg_hash, None)

    def mark_failed(self, message: str, **render_args) -> None:
        """Record that the last TTS attempt for ``message`` failed."""
        msg_hash = self._hash_kwargs(message, render_args)
        self._local[msg_hash] = DURATION_FAILED_SENTINEL
        self._evict_local()
        self._publish_shared(msg_hash, DURATION_FAILED_SENTINEL)
        _LOGGER.debug("Marked TTS as failed for hash %s", msg_hash)

    def _hash(self, message, voice, model, speed, instructions,
              chime, chime_sound, extra_payload) -> str:
        return hash_message(
            message, entity_id=self._entity_id,
            voice=voice, model=model, speed=speed,
            instructions=instructions, chime=chime,
            chime_sound=chime_sound, extra_payload=extra_payload,
        )

    def _hash_kwargs(self, message: str, kw: dict) -> str:
        return self._hash(
            message, kw.get("voice"), kw.get("model"), kw.get("speed"),
            kw.get("instructions"), kw.get("chime"),
            kw.get("chime_sound"), kw.get("extra_payload"),
        )

    def _evict_local(self) -> None:
        if len(self._local) > self._max_local:
            keep = list(self._local.items())[-self._max_local:]
            self._local = dict(keep)

    def _publish_shared(self, msg_hash: str, duration_ms: int) -> None:
        shared = self._ensure_shared_dict()
        shared[msg_hash] = {
            "duration_ms": duration_ms,
            "timestamp": asyncio.get_running_loop().time(),
            "entity_id": self._entity_id,
        }
        if len(shared) > self._max_shared:
            sorted_keys = sorted(
                shared.keys(), key=lambda k: shared[k].get("timestamp", 0)
            )
            for key in sorted_keys[: -self._max_shared]:
                del shared[key]

    def _ensure_shared_dict(self) -> dict:
        domain_data = self._hass.data.setdefault(DOMAIN, {})
        return domain_data.setdefault(MESSAGE_DURATIONS_KEY, {})


def lookup_duration(
    hass: HomeAssistant,
    message: str,
    *,
    entity_id: Optional[str] = None,
    voice: str | None = None,
    model: str | None = None,
    speed: float | None = None,
    instructions: str | None = None,
    chime: bool | None = None,
    chime_sound: str | None = None,
    extra_payload: str | None = None,
) -> Optional[int]:
    """Return cached audio duration for the request, or None.

    * Positive int: real audio duration in ms
    * 0 (``DURATION_FAILED_SENTINEL``): the last attempt failed -
      caller should skip the playback wait entirely
    * None: nothing cached for this message+settings yet
    """
    shared = hass.data.get(DOMAIN, {}).get(MESSAGE_DURATIONS_KEY, {})
    msg_hash = hash_message(
        message, entity_id=entity_id,
        voice=voice, model=model, speed=speed,
        instructions=instructions, chime=chime,
        chime_sound=chime_sound, extra_payload=extra_payload,
    )
    entry = shared.get(msg_hash)
    if entry is None:
        return None
    return entry.get("duration_ms")


def clear_stale_failure(
    hass: HomeAssistant,
    message: str,
    *,
    entity_id: Optional[str] = None,
    voice: str | None = None,
    model: str | None = None,
    speed: float | None = None,
    instructions: str | None = None,
    chime: bool | None = None,
    chime_sound: str | None = None,
    extra_payload: str | None = None,
) -> bool:
    """Drop a failure sentinel from the shared cache.

    Used by volume_restore when ``tts.speak`` succeeded (likely from HA's
    own TTS cache) yet our duration cache still holds a sentinel from a
    prior failed attempt. Returns True when an entry was actually popped.
    """
    shared = hass.data.get(DOMAIN, {}).get(MESSAGE_DURATIONS_KEY, {})
    msg_hash = hash_message(
        message, entity_id=entity_id,
        voice=voice, model=model, speed=speed,
        instructions=instructions, chime=chime,
        chime_sound=chime_sound, extra_payload=extra_payload,
    )
    entry = shared.get(msg_hash)
    if entry is None or entry.get("duration_ms") != DURATION_FAILED_SENTINEL:
        return False
    shared.pop(msg_hash, None)
    return True
