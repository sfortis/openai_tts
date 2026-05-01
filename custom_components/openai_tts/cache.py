"""Per-message duration cache used by the TTS entity.

Home Assistant's TTS layer caches the audio bytes but does not surface the
playback duration back to us on a cache hit. ``volume_restore`` needs the
duration to know how long to wait before restoring the speaker volume, so
we maintain our own message-hash-to-duration map (in memory + persisted via
the entity's ``Store``).

A second copy lives in ``hass.data[DOMAIN][MESSAGE_DURATIONS_KEY]`` so the
``volume_restore`` module can look up durations without holding a reference
to the entity.
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

# Sentinel value written to the shared cache to signal that TTS generation
# for this message FAILED. Lets ``volume_restore`` short-circuit its polling
# loop instead of waiting 3 seconds + falling back to a 10s default duration.
DURATION_FAILED_SENTINEL = 0


def hash_message(
    message: str,
    *,
    voice: str | None = None,
    model: str | None = None,
    speed: float | None = None,
    entity_id: str | None = None,
) -> str:
    """Return a stable short hash that uniquely identifies a TTS request.

    The hash is keyed on **everything that affects audio duration**, not
    just the message text:

    * different voices/models/speeds produce different durations even for
      the same text, so a message-only key would return stale durations
      (volume_restore would then mis-time the restoration);
    * the same profile name might exist on two parent entries, so include
      the entity_id to keep their caches isolated.

    All extra parameters are optional for backwards compatibility - older
    callers that only have ``message`` will keep working but will not get
    cross-profile isolation.
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
    payload = "".join(parts).encode()
    return hashlib.md5(payload).hexdigest()[:16]


class MessageDurationCache:
    """In-memory + shared duration cache for a single TTS entity.

    Two storage layers:

    1. ``self._local`` – owned by the entity, persisted to ``Store`` so it
       survives HA restarts.
    2. ``hass.data[DOMAIN][MESSAGE_DURATIONS_KEY]`` – shared, read by
       ``volume_restore`` which doesn't have direct entity access.
    """

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
        """Return a shallow copy suitable for persistence."""
        return dict(self._local)

    def restore(self, stored: dict[str, int]) -> None:
        """Restore from persisted state and re-populate the shared cache."""
        if not isinstance(stored, dict):
            return
        self._local = dict(stored)
        shared = self._ensure_shared_dict()
        for msg_hash, duration_ms in self._local.items():
            shared[msg_hash] = {
                "duration_ms": duration_ms,
                "timestamp": 0,
                "entity_id": self._entity_id,
            }
        _LOGGER.info(
            "Restored %d message durations into local + shared cache",
            len(self._local),
        )

    def store(
        self,
        message: str,
        duration_ms: int,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
    ) -> None:
        """Record duration for ``message`` in both local and shared caches.

        ``voice``/``model``/``speed`` are folded into the cache key so the
        same text under different profiles/voices doesn't collide.
        """
        msg_hash = hash_message(
            message, voice=voice, model=model, speed=speed,
            entity_id=self._entity_id,
        )
        self._local[msg_hash] = duration_ms

        if len(self._local) > self._max_local:
            keep = list(self._local.items())[-self._max_local:]
            self._local = dict(keep)

        self._store_shared(msg_hash, duration_ms)
        _LOGGER.debug(
            "Stored duration %d ms for message hash %s", duration_ms, msg_hash
        )

    def get(
        self,
        message: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
    ) -> Optional[int]:
        """Return cached duration for ``message`` or None."""
        return self._local.get(
            hash_message(
                message, voice=voice, model=model, speed=speed,
                entity_id=self._entity_id,
            )
        )

    def mark_failed(
        self,
        message: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
    ) -> None:
        """Publish a 'TTS failed' sentinel to the shared cache.

        Does NOT touch ``self._local`` (we don't want failed messages
        persisted across restarts). The sentinel is consumed by
        ``volume_restore`` to abort its polling loop early.
        """
        msg_hash = hash_message(
            message, voice=voice, model=model, speed=speed,
            entity_id=self._entity_id,
        )
        shared = self._ensure_shared_dict()
        shared[msg_hash] = {
            "duration_ms": DURATION_FAILED_SENTINEL,
            "timestamp": asyncio.get_running_loop().time(),
            "entity_id": self._entity_id,
        }
        _LOGGER.debug(
            "Marked TTS as failed for hash %s (volume_restore will skip wait)",
            msg_hash,
        )

    def _store_shared(self, msg_hash: str, duration_ms: int) -> None:
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
