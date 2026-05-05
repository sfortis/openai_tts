"""TTS announcement orchestration with volume save/restore.

Holds the speaker volume for the duration of the TTS clip - measured
deterministically by the engine (ffprobe over the produced audio
bytes) and cached so subsequent HA-cache hits look up the same value
without re-running the engine. Ordering:

1. Pre-flight: abort early when the API tracker reports a persistent
   failure (auth/quota) - avoids waking the speaker for no reason and
   surfaces the problem to the caller.
2. Snapshot original volumes, turn cold devices on, optionally pause
   currently-playing media, set the announcement volume.
3. Call ``tts.speak``.
4. If the entity marked the message as failed, restore now and
   raise - no audio is coming.
5. Otherwise look up the audio duration (cache → media_player
   fallback → static fallback), hold the announcement volume for
   ``duration + 1.5s buffer``, then restore.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    STATE_PLAYING,
)
from homeassistant.components.tts import DOMAIN as TTS_DOMAIN
from homeassistant.const import (
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry
from homeassistant.helpers.event import async_track_state_change_event

from .api_health import (
    API_STATUS_AUTH_FAILED,
    API_STATUS_QUOTA_EXCEEDED,
    OpenAITTSHealthTracker,
)
from .cache import DURATION_FAILED_SENTINEL, clear_stale_failure, lookup_duration
from .const import CONF_PAUSE_PLAYBACK, CONF_VOLUME_RESTORE, DOMAIN
from .utils import (
    call_media_player_service,
    get_media_player_state,
    set_media_player_volume,
)

_LOGGER = logging.getLogger(__name__)

# Health statuses that guarantee the next API call will fail. When the
# entity's tracker is in one of these, ``announce()`` aborts before
# ``tts.speak`` runs - calling it anyway only wakes the speaker (chime
# + music interrupt) for audio that will never arrive.
PERSISTENT_FAILURE_STATUSES = frozenset(
    {API_STATUS_AUTH_FAILED, API_STATUS_QUOTA_EXCEEDED}
)
HEALTH_TRACKER_KEY_SUFFIX = "_health_tracker"

# Pre-speak readiness window. We block ``tts.speak`` until every
# target speaker has woken up out of ``off`` state, so they all
# receive the audio URL in roughly the same moment instead of one
# warm cast hearing the message a couple of seconds before its
# cold-cast peer. Capped to keep one stuck device from pinning the
# whole call.
SPEAKER_READY_TIMEOUT_S = 5.0
NOT_READY_STATES = frozenset({STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN})


async def _wait_until_speakers_ready(
    hass: HomeAssistant,
    entity_ids: List[str],
    *,
    timeout_s: float = SPEAKER_READY_TIMEOUT_S,
) -> None:
    """Block until every speaker has left ``off``/``unavailable``.

    Cold casts going from ``off`` to ``idle`` typically take ~1s, but
    that's variable per device and per network. Polling on a fixed
    sleep penalises everyone for the slowest peer; event-driven
    waiting lets fast casts unblock immediately while the slow one
    catches up. Times out so a single stuck device doesn't hang the
    announcement - we'd rather take the sync hit than freeze.

    No-op if every target is already ready when called.
    """
    pending = {
        eid for eid in entity_ids
        if (s := hass.states.get(eid)) is not None
        and s.state in NOT_READY_STATES
    }
    if not pending:
        return

    ready_event = asyncio.Event()
    disposers: List = []

    @callback
    def _on_change(event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        eid = event.data.get("entity_id")
        if new_state.state not in NOT_READY_STATES and eid in pending:
            pending.discard(eid)
            if not pending:
                ready_event.set()

    for entity_id in list(pending):
        disposers.append(
            async_track_state_change_event(hass, entity_id, _on_change)
        )

    try:
        await asyncio.wait_for(ready_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _LOGGER.warning(
            "Speakers not ready within %.1fs, proceeding anyway: %s",
            timeout_s, sorted(pending),
        )
    finally:
        for d in disposers:
            try:
                d()
            except Exception as exc:  # pragma: no cover - defensive
                _LOGGER.debug("Listener dispose failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry / tracker resolution helpers
# ---------------------------------------------------------------------------


def _resolve_config_entry(hass: HomeAssistant, tts_entity: str):
    """Return the parent ConfigEntry that owns ``tts_entity``, or None.

    Used so flags like ``volume_restore`` and ``pause_playback`` come
    from the integration entry that actually produced the entity, not
    from an unrelated ``entries[0]`` in multi-account setups.
    """
    er = entity_registry.async_get(hass)
    entry = er.async_get(tts_entity)
    if entry is None or entry.config_entry_id is None:
        return None
    return hass.config_entries.async_get_entry(entry.config_entry_id)


def _resolve_health_tracker(
    hass: HomeAssistant, tts_entity: str
) -> OpenAITTSHealthTracker | None:
    """Find the health tracker that owns ``tts_entity``."""
    er = entity_registry.async_get(hass)
    entry = er.async_get(tts_entity)
    if entry is None or entry.config_entry_id is None:
        return None
    return hass.data.get(DOMAIN, {}).get(
        f"{entry.config_entry_id}{HEALTH_TRACKER_KEY_SUFFIX}"
    )


def _is_cast_platform(hass: HomeAssistant, entity_id: str) -> bool:
    """True when ``entity_id`` is owned by the Chromecast platform."""
    er = entity_registry.async_get(hass)
    entry = er.async_get(entity_id)
    return entry is not None and entry.platform == "cast"


def _resolve_unique_id(hass: HomeAssistant, tts_entity: str) -> str | None:
    """Return the registry unique_id for ``tts_entity``, or None.

    The cache keys on unique_id (not entity_id) so user renames don't
    invalidate failure sentinels.
    """
    er = entity_registry.async_get(hass)
    entry = er.async_get(tts_entity)
    return entry.unique_id if entry else None


# ---------------------------------------------------------------------------
# VolumeRestorer - snapshot, set, restore one speaker at a time
# ---------------------------------------------------------------------------


class _VolumeRestorer:
    """Snapshot original volumes, set the announcement level, restore on demand.

    Restoration runs after a deterministic ``duration_ms + buffer``
    sleep driven by the engine-measured audio length. The failure
    path skips the wait entirely and rolls volumes back immediately.
    """

    def __init__(self, hass: HomeAssistant, entity_ids: List[str]) -> None:
        self.hass = hass
        self.entity_ids = entity_ids
        self._original_volumes: Dict[str, float] = {}
        self._paused_media: Set[str] = set()

    async def prepare(
        self,
        target_volume: Optional[float],
        pause_playback: bool,
    ) -> None:
        """Turn devices on, snapshot volumes, optionally pause, set level."""
        states = await asyncio.gather(
            *(get_media_player_state(self.hass, eid) for eid in self.entity_ids),
            return_exceptions=True,
        )

        turn_on_tasks = []
        pause_tasks = []
        capture_after_on: List[str] = []

        for entity_id, state_or_exc in zip(self.entity_ids, states):
            if isinstance(state_or_exc, Exception):
                _LOGGER.warning("Skipping %s (state lookup failed: %s)",
                                entity_id, state_or_exc)
                continue
            state, attrs = state_or_exc
            if state is None or attrs is None:
                _LOGGER.warning("Media player %s not available", entity_id)
                continue

            volume = attrs.get(ATTR_MEDIA_VOLUME_LEVEL)
            if volume is not None:
                self._original_volumes[entity_id] = float(volume)
            elif state.lower() == "off":
                # Volume isn't reported until the device wakes - we'll
                # capture it once turn-on completes.
                capture_after_on.append(entity_id)

            if state.lower() == "off":
                turn_on_tasks.append(
                    call_media_player_service(self.hass, "turn_on", entity_id)
                )

            if pause_playback and state == STATE_PLAYING:
                self._paused_media.add(entity_id)
                pause_tasks.append(
                    call_media_player_service(
                        self.hass, SERVICE_MEDIA_PAUSE, entity_id
                    )
                )

        if turn_on_tasks:
            await asyncio.gather(*turn_on_tasks, return_exceptions=True)
        # Block until ALL targets are out of off/unavailable. This
        # keeps cold and warm casts from drifting in their start-of-
        # playback time when the announcement spans multiple speakers.
        await _wait_until_speakers_ready(self.hass, self.entity_ids)
        if capture_after_on:
            for entity_id in capture_after_on:
                state, attrs = await get_media_player_state(self.hass, entity_id)
                if state and attrs:
                    actual = attrs.get(ATTR_MEDIA_VOLUME_LEVEL)
                    if actual is not None:
                        self._original_volumes[entity_id] = float(actual)

        if pause_tasks:
            await asyncio.gather(*pause_tasks, return_exceptions=True)
            # Pause is async on most media platforms - the service call
            # returns instantly but the device takes a few hundred ms
            # to actually mute its output. Without this settle, the
            # subsequent volume bump is audible as a brief loudness
            # spike on the music that's still playing while pause
            # propagates.
            await asyncio.sleep(0.4)

        if target_volume is not None:
            await self._set_announcement_volume(target_volume)

        # Cast multi-room sync compensation. When more than one cast device
        # is targeted, give the slowest receiver app a ~1s head start to
        # finish loading before tts.speak hands it the URL. Without this
        # warm-up the warm cast hears the message ~800ms before the cold
        # one, which sounds like a delayed echo in the same room.
        # Single-cast announcements skip this overhead.
        cast_targets = [
            eid for eid in self.entity_ids
            if _is_cast_platform(self.hass, eid)
        ]
        if len(cast_targets) > 1:
            _LOGGER.debug(
                "Multi-cast warm-up: %d cast targets, holding 1s before speak",
                len(cast_targets),
            )
            await asyncio.sleep(1.0)

    async def _set_announcement_volume(self, target: float) -> None:
        """Push every speaker to ``target`` (skip ones already there)."""
        tasks = []
        for entity_id in self.entity_ids:
            current = self._original_volumes.get(entity_id)
            if current is None or abs(current - target) > 0.01:
                _LOGGER.info(
                    "Setting volume for %s -> %.2f", entity_id, target
                )
                tasks.append(set_media_player_volume(self.hass, entity_id, target))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            # Brief settle so the volume actually lands before tts.speak
            # starts streaming chunks at the device.
            await asyncio.sleep(0.3)

    async def restore_immediate(self, *, restore_volumes: bool = True) -> None:
        """Restore all speakers right now - failure path, no wait.

        ``restore_volumes`` mirrors the post-playback method: pause-only
        calls don't want any volume change rolled back, just the
        previously-paused media resumed.
        """
        if restore_volumes:
            await asyncio.gather(
                *(
                    self._restore_one(eid, vol)
                    for eid, vol in self._original_volumes.items()
                ),
                return_exceptions=True,
            )
            # Same settle as the happy-path restore: let the volume
            # change reach the device before we unpause, so the music
            # doesn't briefly come back at the announcement level.
            if self._paused_media:
                await asyncio.sleep(0.4)
        await self._resume_paused_media()

    async def restore_after_playback(
        self,
        duration_ms: int,
        buffer_ms: int = 1500,
        *,
        elapsed_ms: int = 0,
        restore_volumes: bool = True,
    ) -> None:
        """Sleep for the remaining playback window, then unwind state.

        ``elapsed_ms`` is wall time the caller already spent waiting
        on duration resolution (e.g. polling the cache while the
        stream finished). It's subtracted from the hold so we don't
        over-hold past the actual end of audio.

        ``restore_volumes`` separates the two reasons we'd hold the
        speaker for the playback window: a volume override that needs
        rolling back when the announcement ends (True), or a
        pause/resume that just needs the music resumed without
        touching volume (False).
        """
        wait_s = max(
            0.0,
            (duration_ms + buffer_ms - elapsed_ms) / 1000.0,
        )
        _LOGGER.info(
            "Holding for %.1fs (audio %d ms + buffer %d ms - elapsed %d ms), then %s",
            wait_s, duration_ms, buffer_ms, elapsed_ms,
            "restoring volume + resuming" if restore_volumes else "resuming media",
        )
        await asyncio.sleep(wait_s)
        if restore_volumes:
            await asyncio.gather(
                *(
                    self._restore_one(eid, vol)
                    for eid, vol in self._original_volumes.items()
                ),
                return_exceptions=True,
            )
            # Volume changes are async on the device side too, so give
            # the speaker a moment to settle on the original level
            # before we unpause - otherwise the music would briefly
            # come back at the announcement volume while the restore
            # request is still propagating.
            if self._paused_media:
                await asyncio.sleep(0.4)
        await self._resume_paused_media()

    async def _restore_one(self, entity_id: str, original_volume: float) -> bool:
        try:
            state, attrs = await get_media_player_state(self.hass, entity_id)
            if state is None or attrs is None:
                return False
            current = attrs.get(ATTR_MEDIA_VOLUME_LEVEL)
            if current is None:
                return False
            if abs(float(current) - original_volume) <= 0.01:
                return True
            await set_media_player_volume(self.hass, entity_id, original_volume)
            return True
        except Exception as exc:
            _LOGGER.error("Failed to restore volume for %s: %s", entity_id, exc)
            return False

    async def _resume_paused_media(self) -> None:
        if not self._paused_media:
            return
        await asyncio.gather(
            *(
                call_media_player_service(self.hass, SERVICE_MEDIA_PLAY, eid)
                for eid in self._paused_media
            ),
            return_exceptions=True,
        )

# ---------------------------------------------------------------------------
# announce() - top-level orchestration
# ---------------------------------------------------------------------------


async def announce(
    hass: HomeAssistant,
    tts_entity: str,
    media_players: List[str],
    message: str,
    language: str = "en",
    options: Optional[Dict[str, Any]] = None,
    tts_volume: Optional[float] = None,
    pause_playback: Optional[bool] = None,
) -> None:
    """Run a TTS announcement with automatic volume save/restore.

    Raises ``HomeAssistantError`` when the call cannot complete - either
    because the API is in a persistent failure state, or because the
    underlying ``tts.speak`` exhausted its retries. Silent success-on-
    failure was the previous behaviour and made automations think a
    speech happened when nothing reached the speakers.
    """
    options = (options or {}).copy()

    _abort_if_persistent_failure(hass, tts_entity)

    config_entry = _resolve_config_entry(hass, tts_entity)
    restore_enabled = (
        tts_volume is not None
        or (config_entry and config_entry.options.get(CONF_VOLUME_RESTORE, False))
    )
    pause_enabled = (
        pause_playback if pause_playback is not None
        else bool(
            config_entry and config_entry.options.get(CONF_PAUSE_PLAYBACK, False)
        )
    )

    available_players = _filter_available(hass, media_players)
    if not available_players:
        _LOGGER.warning("No available media players")
        return

    _LOGGER.info(
        "Playing TTS on %d players with%s volume control",
        len(available_players), "" if restore_enabled else "out",
    )

    # Build a restorer when EITHER feature is requested - pause needs
    # the same prepare-and-resume scaffolding that volume restore uses,
    # so a call with ``pause_playback=True`` but no volume change still
    # has to go through ``_VolumeRestorer``. Without this, pause was a
    # silent no-op on calls that didn't also enable volume restore.
    needs_restorer = restore_enabled or pause_enabled
    restorer = (
        _VolumeRestorer(hass, available_players) if needs_restorer else None
    )

    if restorer is not None:
        await restorer.prepare(
            target_volume=tts_volume if restore_enabled else None,
            pause_playback=pause_enabled,
        )

    # Time the entire speak window. Some platforms (cast multi-target)
    # return from ``tts.speak`` almost immediately while the audio is
    # still streaming, while others (Music Assistant native, blocking
    # TTS on Sonos, etc.) only return AFTER the clip has finished
    # playing. Using ``speak_started_at`` instead of speak-completed
    # captures both: the elapsed time we subtract from the post-speak
    # hold ends up being ~0 for fire-and-forget targets and ~audio
    # duration for blocking targets, so we don't double-hold.
    # Subscribe to the speakers' state bus BEFORE the speak call. On
    # synchronous platforms (Sonos / MA) the speak service only returns
    # after the announcement has finished playing; subscribing after
    # would miss every relevant transition. Whole orchestration runs
    # under a single ``try/finally`` so a CancelledError (or any other
    # BaseException) in the middle still tears down the listener.
    watcher = _TtsPlaybackWatcher(hass, available_players) \
        if restorer is not None else None
    if watcher is not None:
        watcher.start()

    speak_started_at = asyncio.get_running_loop().time()
    try:
        try:
            await _call_tts_speak(hass, tts_entity, message, language,
                                  options, available_players)
        except Exception as err:
            if restorer is not None:
                await restorer.restore_immediate(restore_volumes=restore_enabled)
            raise HomeAssistantError(
                f"TTS speak failed: {err}"
            ) from err

        if restorer is None:
            # No volume restore and no pause - speak already returned, we're
            # done. We don't probe the failure sentinel here:
            # ``_call_tts_speak`` already propagated any engine failure as
            # an exception, so reaching this point means HA either streamed
            # fresh audio or served from its own TTS cache. A stale sentinel
            # from a prior $0-balance attempt would cause a false-positive
            # error after audio actually played (issue #64), so we trust
            # speak's outcome.
            return

        cached = await _wait_for_cached_duration(
            hass, tts_entity, message, options, timeout_s=60.0
        )
        if cached == DURATION_FAILED_SENTINEL:
            # Same rationale as above: tts.speak returned successfully, so
            # something played (likely from HA's TTS cache). The sentinel
            # is from a previous attempt and no longer reflects reality.
            # Drop it so the next call doesn't false-trip again, and fall
            # through to media_player / fallback duration for restore timing.
            _LOGGER.debug(
                "Stale failure sentinel for %s after successful speak; clearing",
                tts_entity,
            )
            clear_stale_failure(
                hass, message,
                entity_id=_resolve_unique_id(hass, tts_entity),
                **_resolved_render_args(hass, tts_entity, options),
            )
            cached = None

        duration_ms = cached
        if duration_ms is None or duration_ms <= 0:
            duration_ms = _media_player_duration_ms(hass, available_players)
        if duration_ms is None or duration_ms <= 0:
            duration_ms = _DEFAULT_FALLBACK_DURATION_MS
            _LOGGER.warning(
                "No duration found in cache or media_player attributes; "
                "using fallback %d ms",
                duration_ms,
            )

        # End-of-playback handling combines two deterministic signals:
        #
        # * Targets that surfaced the TTS proxy URL in their state during
        #   speak (typically Cast) get a state-based drain: when their
        #   ``state`` rolls off ``playing`` / ``buffering`` while their
        #   ``media_content_id`` was the TTS URL, the audio has ended.
        # * Targets that never surfaced it (Sonos with music underneath
        #   ducks the announcement at the device level, so HA never sees
        #   the URL) are handled by ``tts.speak``'s synchronous blocking
        #   contract -- by the time speak returned, those announcements
        #   were already done.
        #
        # If neither signal applies (e.g. cache miss in flight, or an
        # integration that exposes neither the URL nor a synchronous
        # speak), we fall back to the duration-based timer.
        if watcher.any_seen_tts():
            # Cast-style targets surface the TTS proxy URL on the
            # speaker; wait for them to roll off it before restoring.
            drain_timeout_s = max(30.0, (duration_ms + 5000) / 1000.0)
            await watcher.wait_for_drain(timeout_s=drain_timeout_s)
            await asyncio.sleep(0.3)  # settle so the unmute doesn't clip
            elapsed_ms = duration_ms
        elif _all_targets_sync_speak(hass, available_players):
            # Music Assistant: speak's blocking already covered the
            # entire announcement, audio is already done. Collapse the
            # hold to the unmute buffer only, otherwise we'd add a
            # second copy of the audio_duration on top of what speak
            # already waited for (~28s for a 12s clip).
            elapsed_ms = duration_ms
        else:
            # Fire-and-forget targets (Sonos, anything not in the sync
            # set): tts.speak dispatched the audio and returned before
            # playback. Time spent inside speak was local prep
            # (ffmpeg + network round-trip), NOT playback. Hold the
            # full audio duration so the volume restore doesn't
            # interrupt the announcement (Sonos: chops it to just the
            # leading chime).
            elapsed_ms = 0
    finally:
        if watcher is not None:
            watcher.stop()

    await restorer.restore_after_playback(
        duration_ms,
        elapsed_ms=elapsed_ms,
        restore_volumes=restore_enabled,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _abort_if_persistent_failure(hass: HomeAssistant, tts_entity: str) -> None:
    """Raise if the API tracker is in a state guaranteed to fail.

    Done before any speaker prep so the cast/Sonos doesn't wake up for
    a request that can't possibly produce audio.
    """
    tracker = _resolve_health_tracker(hass, tts_entity)
    if tracker is None or tracker.status not in PERSISTENT_FAILURE_STATUSES:
        return
    last_error = tracker.data.get("last_error_message")
    msg = (
        f"Skipping TTS announcement on {tts_entity}: API status is "
        f"{tracker.status}. Resolve the issue (recharge balance / "
        f"fix API key), then retry. Last error: {last_error}"
    )
    _LOGGER.warning(msg)
    raise HomeAssistantError(msg)


def _filter_available(hass: HomeAssistant, media_players: List[str]) -> List[str]:
    out: List[str] = []
    for entity_id in media_players:
        state = hass.states.get(entity_id)
        if state and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            out.append(entity_id)
        else:
            _LOGGER.warning(
                "Media player %s is not available (state: %s)",
                entity_id, state.state if state else "None",
            )
    return out


async def _call_tts_speak(
    hass: HomeAssistant,
    tts_entity: str,
    message: str,
    language: str,
    options: Dict[str, Any],
    media_players: List[str],
) -> None:
    """Invoke HA's ``tts.speak`` exactly once.

    Engine-level retries already happen inside ``async_get_tts_audio`` /
    ``async_stream_tts_audio``, where they're safe (audio hasn't been
    delivered to a speaker yet). Retrying at the speak level instead can
    replay audio that already started playing on one of the targets - a
    blocking ``tts.speak`` waits for playback completion, so by the time
    we'd see an exception (e.g. an internal ``quote_from_bytes`` bug in
    HA's URL helper) the message is often already audible. Surfacing the
    failure once is preferable to playing it twice.
    """
    service_data = {
        "message": message,
        "language": language,
        "options": options,
        "media_player_entity_id": media_players,
    }
    await hass.services.async_call(
        TTS_DOMAIN, "speak", service_data,
        target={"entity_id": tts_entity}, blocking=True,
    )


_DEFAULT_FALLBACK_DURATION_MS = 10_000


def _resolved_render_args(
    hass: HomeAssistant, tts_entity: str, options: Dict[str, Any]
) -> Dict[str, Any]:
    """Resolve the cache-key dimensions for the upcoming render.

    Uses service-call options first, falling back to the entity's
    published ``current_*`` attributes (set by tts.py from the
    resolved profile config). Mirrors what tts.py uses when storing
    the duration so the lookup hash matches exactly.
    """
    state = hass.states.get(tts_entity)
    attrs = state.attributes if state else {}

    voice = options.get("voice") or attrs.get("current_voice")
    model = options.get("model") or attrs.get("current_model")
    raw_speed = options.get("speed")
    if raw_speed is None:
        raw_speed = attrs.get("current_speed")
    try:
        speed = float(raw_speed) if raw_speed is not None else None
    except (TypeError, ValueError):
        speed = None

    if "instructions" in options:
        instructions = options["instructions"]
    else:
        instructions = attrs.get("current_instructions")

    chime = options.get("chime")
    if chime is None:
        chime = attrs.get("current_chime_enable")
    chime_sound = options.get("chime_sound")
    if chime_sound is None:
        chime_sound = attrs.get("current_chime_sound")

    if "extra_payload" in options:
        extra_payload = options["extra_payload"]
    else:
        extra_payload = attrs.get("current_extra_payload")

    return {
        "voice": voice,
        "model": model,
        "speed": speed,
        "instructions": instructions,
        "chime": chime,
        "chime_sound": chime_sound,
        "extra_payload": extra_payload,
    }


def _lookup_audio_duration(
    hass: HomeAssistant,
    tts_entity: str,
    message: str,
    options: Dict[str, Any],
) -> Optional[int]:
    """Cache lookup for the audio duration of this exact request.

    Returns ``None`` when nothing is cached, ``DURATION_FAILED_SENTINEL``
    (0) when a previous attempt failed, or a positive int for a real
    measured duration.
    """
    return lookup_duration(
        hass, message,
        entity_id=_resolve_unique_id(hass, tts_entity),
        **_resolved_render_args(hass, tts_entity, options),
    )


async def _wait_for_cached_duration(
    hass: HomeAssistant,
    tts_entity: str,
    message: str,
    options: Dict[str, Any],
    *,
    timeout_s: float = 4.0,
    poll_interval_s: float = 0.1,
) -> Optional[int]:
    """Poll the cache briefly so streaming mode can populate it.

    In streaming mode the engine writes the measured duration AFTER
    the final chunk completes - which can be a few seconds after
    ``tts.speak`` returns. We poll on a short cadence until either:

    * The cache hits a real duration (positive int) -> return it
    * The cache hits the failure sentinel (0) -> return it (caller
      treats as immediate-restore signal)
    * ``timeout_s`` elapses -> return whatever we have (None on
      first-ever call, prior cached value otherwise) and let the
      caller fall back to media_player.media_duration / static.

    Returns immediately when a value is already present.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    last: Optional[int] = None
    while True:
        last = _lookup_audio_duration(hass, tts_entity, message, options)
        if last is not None:
            return last
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(poll_interval_s)


_TTS_PROXY_MARKER = "/api/tts_proxy/"

# Platforms whose ``tts.speak`` blocking call only returns AFTER the
# announcement has finished playing (their integration drives a
# native announcement primitive that HA waits on synchronously).
#
# Music Assistant: ``play_announcement`` keeps the service open until
# the device finishes the clip. For these targets the post-speak hold
# only needs the unmute buffer; otherwise we'd over-hold by a full
# audio_duration since the audio already played during speak.
#
# Sonos is intentionally NOT in this set. Sonos's HA integration uses
# ``websocket.play_clip`` which dispatches and returns immediately
# (~300ms even for a 24s announcement). The audio plays AFTER speak
# returns. Treating Sonos as sync would collapse the hold and restore
# volume mid-announcement, which interrupts Sonos's native ducking
# and chops the audio to just the leading chime.
_SYNC_SPEAK_PLATFORMS: frozenset[str] = frozenset({
    "music_assistant",
})


def _all_targets_sync_speak(
    hass: HomeAssistant, entity_ids: List[str]
) -> bool:
    """True iff every target's integration drives a synchronous
    ``tts.speak`` (Sonos / Music Assistant). One non-sync target
    forces the whole announcement onto the fire-and-forget hold so
    we never under-hold a Cast peer in a mixed group.

    An empty target list returns False on purpose: there's nothing to
    rely on for sync semantics, so the caller should fall through to
    the timer-based hold rather than silently treating "no targets"
    as "everything is sync".
    """
    if not entity_ids:
        return False
    er = entity_registry.async_get(hass)
    for eid in entity_ids:
        entry = er.async_get(eid)
        if entry is None or entry.platform not in _SYNC_SPEAK_PLATFORMS:
            return False
    return True


class _TtsPlaybackWatcher:
    """Detect end-of-TTS-playback via HA state-change events.

    The TTS announcement window is exactly the period during which a
    speaker's ``media_content_id`` contains ``/api/tts_proxy/``. We
    subscribe to ``state_changed`` for the targets, set ``pickup`` once
    every target has reached the TTS URL at least once, and ``drain``
    once every previously-on-TTS target has rolled back off it.

    Crucially, the watcher must be started BEFORE ``tts.speak``. On
    synchronous platforms (Sonos / Music Assistant) the speak service
    only returns AFTER the announcement has finished playing -- if we
    subscribe afterwards, both the pickup and drain transitions have
    already happened on the bus and we'd see nothing.
    """

    def __init__(self, hass: HomeAssistant, media_players: List[str]) -> None:
        self.hass = hass
        self.media_players = list(media_players)
        self.drain = asyncio.Event()
        self._seen_tts: Dict[str, bool] = {e: False for e in media_players}
        self._on_tts: Dict[str, bool] = {e: False for e in media_players}
        self._unsub = None

    def start(self) -> None:
        self._unsub = async_track_state_change_event(
            self.hass, self.media_players, self._listener
        )
        for eid in self.media_players:
            state = self.hass.states.get(eid)
            if self._is_on_tts(state):
                self._on_tts[eid] = True
                self._seen_tts[eid] = True
        self._check_drain()

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _listener(self, event: Event) -> None:
        eid = event.data.get("entity_id")
        if eid not in self._seen_tts:
            return
        new_state = event.data.get("new_state")
        currently = self._is_on_tts(new_state)
        was = self._on_tts[eid]
        self._on_tts[eid] = currently
        if currently and not was:
            self._seen_tts[eid] = True
            self._check_drain()  # cancel any premature drain
        elif was and not currently:
            self._check_drain()

    @staticmethod
    def _is_on_tts(state) -> bool:
        """A speaker is "on TTS" iff it's actively playing the TTS
        proxy URL. Both conditions must hold: the URL alone isn't
        enough (Cast keeps the URL in ``media_content_id`` after the
        audio ends, so we'd never see a drain otherwise), and an
        active state alone isn't enough (a Sonos with Deezer
        underneath stays ``playing`` throughout the announcement
        without ever loading the TTS URL).
        """
        if state is None:
            return False
        if state.state not in ("playing", "buffering"):
            return False
        cid = str(state.attributes.get("media_content_id") or "")
        return _TTS_PROXY_MARKER in cid

    def any_seen_tts(self) -> bool:
        """At least one target was observed actively playing the TTS URL."""
        return any(self._seen_tts.values())

    def all_drained(self) -> bool:
        """Every target that ever picked up TTS has rolled off it.

        Targets that never picked up are excluded from the gate --
        their integration didn't surface the TTS URL via state
        attributes (e.g. Sonos ducks the announcement under the
        existing queue), so we have no event to wait on. The caller
        treats those as handled by ``tts.speak``'s synchronous
        blocking semantics.
        """
        return self.any_seen_tts() and not any(
            self._on_tts[e] for e in self.media_players if self._seen_tts[e]
        )

    def targets_still_on_tts(self) -> List[str]:
        """Diagnostic: which targets are still actively on the TTS URL."""
        return [
            e for e in self.media_players
            if self._seen_tts[e] and self._on_tts[e]
        ]

    def _check_drain(self) -> None:
        if self.all_drained():
            self.drain.set()
        else:
            # State change brought a target back onto TTS; a future
            # transition will need to re-fire drain.
            self.drain.clear()

    async def wait_for_drain(self, *, timeout_s: float) -> bool:
        """Block until every previously-on-TTS target has rolled off.

        Robust against the asyncio.Event "set then clear before the
        waiter is scheduled" race: if the event fires but a new pickup
        clears it before our coroutine resumes, the waiter would have
        woken up spuriously and ``all_drained()`` would still be False.
        We re-check after every wakeup and re-arm the wait, capped by
        the same deadline.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self.all_drained():
            remaining = deadline - loop.time()
            if remaining <= 0:
                _LOGGER.warning(
                    "TTS playback drain timed out after %.1fs on %s; "
                    "restoring anyway (still on TTS: %s)",
                    timeout_s, self.media_players,
                    self.targets_still_on_tts(),
                )
                return False
            # Clear so the next wait blocks on a *future* transition,
            # not on a stale set from a previous pickup/drain cycle.
            self.drain.clear()
            try:
                await asyncio.wait_for(self.drain.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "TTS playback drain timed out after %.1fs on %s; "
                    "restoring anyway (still on TTS: %s)",
                    timeout_s, self.media_players,
                    self.targets_still_on_tts(),
                )
                return False
        return True


def _media_player_duration_ms(
    hass: HomeAssistant, entity_ids: List[str]
) -> Optional[int]:
    """Best-effort fallback: ask the speakers what TTS duration they see.

    Hit when our cache has no record (e.g. very first call after a
    fresh install). We ONLY trust the speaker's ``media_duration`` if
    its ``media_content_id`` currently points at the HA TTS proxy -
    otherwise the speaker is still parked on whatever it played
    before the announcement (e.g. a 6-minute Deezer track via Music
    Assistant) and that duration would put the restore wait into
    minute territory.
    """
    durations: List[int] = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        media_id = str(state.attributes.get("media_content_id") or "")
        if _TTS_PROXY_MARKER not in media_id:
            continue  # speaker still on a previous track
        raw = state.attributes.get("media_duration")
        if raw is None:
            continue
        try:
            durations.append(int(float(raw) * 1000))
        except (TypeError, ValueError):
            continue
    return max(durations) if durations else None
