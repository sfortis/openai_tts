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
import contextlib
import logging
from typing import Any, Dict, List, Optional, Set

from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    MediaPlayerEntityFeature,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    STATE_PLAYING,
)
from homeassistant.components.tts import DOMAIN as TTS_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    STATE_IDLE,
    STATE_OFF,
    STATE_STANDBY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .api_health import OpenAITTSHealthTracker, health_tracker_for
from .cache import DURATION_FAILED_SENTINEL, clear_stale_failure, lookup_duration
from .const import (
    CONF_ANNOUNCE_MODE,
    CONF_PAUSE_PLAYBACK,
    CONF_VOLUME_RESTORE,
    DEFAULT_ANNOUNCE_MODE,
)
from .utils import (
    call_media_player_service,
    get_media_player_state,
    set_media_player_volume,
)

_LOGGER = logging.getLogger(__name__)

# Buffering is a transient state Cast devices dwell in for ~200-800ms
# right after ``play_media`` while the receiver loads the URL. We
# can't treat it as ``playing`` (issuing pause to a buffering Cast
# triggers a play-then-pause cycle on the receiver firmware - the user
# hears a brief blast of audio), and we can't treat it as ``idle``
# (skipping pause/volume here is what caused the original spike when
# buffering rolled into playing during tts.speak). Instead, when we
# see buffering, we wait briefly for it to settle to a stable state.
_BUFFERING_STATE = "buffering"
_BUFFERING_SETTLE_TIMEOUT_S = 2.0
_BUFFERING_SETTLE_POLL_S = 0.1

# Don't bother seeking back after a Cast replay when the user was still
# in the opening seconds of the item - the seek round-trip is more
# disruptive than the couple of seconds it would recover.
_RESUME_SEEK_MIN_POSITION_S = 5.0

# Entities currently inside an announcement, counted because two
# announcements can overlap on one speaker.
#
# The auto-resume watcher needs this. It fires on any transition into
# ``playing`` on a target that was idle beforehand, and it cannot tell a
# platform's unwanted queue restore from a second announcement someone
# sent a moment later. Checking the proxy URL is not enough on its own:
# this module's own notes record that Music Assistant and Sonos do not
# surface it while announcing.
_ANNOUNCING: Dict[str, int] = {}


def _mark_announcing(entity_ids: List[str]) -> None:
    """Note that these entities are inside an announcement."""
    for eid in entity_ids:
        _ANNOUNCING[eid] = _ANNOUNCING.get(eid, 0) + 1


def _clear_announcing(entity_ids: List[str]) -> None:
    """Release entities once their announcement is finished."""
    for eid in entity_ids:
        remaining = _ANNOUNCING.get(eid, 0) - 1
        if remaining > 0:
            _ANNOUNCING[eid] = remaining
        else:
            _ANNOUNCING.pop(eid, None)


def _is_announcing(entity_id: str) -> bool:
    """True while any announcement holds this entity."""
    return _ANNOUNCING.get(entity_id, 0) > 0


# One lock per speaker, so announcements aimed at the same device run
# one after the other instead of on top of each other. Overlapping runs
# used to corrupt the volume snapshot: the first call lowered the
# speaker, the second read that lowered level as the original, and after
# both restored the speaker stayed quiet for good. Serialising also
# stops one announcement from cutting another off part-way through.
#
# Locks are created on demand and kept. An installation has a bounded
# number of media players, so the dict does not grow without limit, and
# keeping the lock means a queue that forms across several announcements
# stays in one place.
_SPEAKER_GATES: Dict[str, asyncio.Lock] = {}

# How long to wait for a speaker to come free. It has to outlast the
# longest legitimate announcement, which is bounded by the duration
# lookup (up to 60 s when our cache misses and Home Assistant's hits)
# plus the hold for the clip itself.
_ANNOUNCE_GATE_TIMEOUT_S = 90.0


async def _acquire_speaker_gate(entity_ids: List[str]) -> List[str]:
    """Take the announcement lock for each speaker, in a fixed order.

    Sorting the ids matters: two announcements covering the same pair of
    speakers in opposite orders would otherwise each hold what the other
    needs and neither could continue.

    A speaker that does not come free within the timeout is used
    anyway, with a warning. Waiting forever would mean losing the
    announcement entirely, and a message the user never hears is worse
    than a volume level that may need correcting afterwards.

    Returns the ids actually locked, to hand to ``_release_speaker_gate``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ANNOUNCE_GATE_TIMEOUT_S
    acquired: List[str] = []
    try:
        for entity_id in sorted(entity_ids):
            lock = _SPEAKER_GATES.setdefault(entity_id, asyncio.Lock())
            if not lock.locked():
                await lock.acquire()
                acquired.append(entity_id)
                continue

            remaining = deadline - loop.time()
            _LOGGER.debug(
                "%s is busy with another announcement, waiting up to %.1fs",
                entity_id, max(0.0, remaining),
            )
            if remaining <= 0:
                _LOGGER.warning(
                    "Gave up waiting for %s to finish its previous "
                    "announcement; announcing anyway, so its volume may "
                    "not restore correctly",
                    entity_id,
                )
                continue
            try:
                await asyncio.wait_for(lock.acquire(), remaining)
            except TimeoutError:
                _LOGGER.warning(
                    "%s did not finish its previous announcement within "
                    "%.0fs; announcing anyway, so its volume may not "
                    "restore correctly",
                    entity_id, _ANNOUNCE_GATE_TIMEOUT_S,
                )
                continue
            acquired.append(entity_id)
    except BaseException:
        # Cancelled while queueing. Whatever was already locked has to
        # go back, or the speakers stay blocked for every later call.
        _release_speaker_gate(acquired)
        raise
    return acquired


def _release_speaker_gate(entity_ids: List[str]) -> None:
    """Hand the speakers back. Safe to call with an empty list."""
    for entity_id in entity_ids:
        lock = _SPEAKER_GATES.get(entity_id)
        if lock is not None and lock.locked():
            lock.release()


# How long to keep watching for a platform-driven auto-resume after the
# announcement. Music Assistant's resume lands 1-3 s late, so the window
# has to outlast that; it runs on a timer rather than blocking the
# service call.
_LATE_RESUME_WATCH_S = 5.0

# How long the hands-off path waits for the engine to record a failure
# before declaring the announcement successful. The engine writes the
# sentinel from the streaming task, which completes just after
# ``tts.speak`` returns, so a single immediate read would race with it.
# Kept short: this delay is paid on every hands-off call, and a provider
# error is recorded as soon as the response arrives.
_FAILURE_SETTLE_TIMEOUT_S = 3.0


# Pre-speak readiness window. We block ``tts.speak`` until every
# target speaker has woken up out of ``off`` state, so they all
# receive the audio URL in roughly the same moment instead of one
# warm cast hearing the message a couple of seconds before its
# cold-cast peer. Capped to keep one stuck device from pinning the
# whole call.
SPEAKER_READY_TIMEOUT_S = 5.0

# A speaker that was powered off reports no volume until it has woken
# up, and leaving ``off`` does not mean its attributes have arrived. We
# wait a little longer for the level itself, because a level we never
# read is a level we cannot put back.
VOLUME_ATTRIBUTE_TIMEOUT_S = 3.0

# How long the gate holder waits for a speaker to report the level it was
# just restored to, before handing the speaker to the announcement queued
# behind it. Short, because it runs after the audio has finished and only
# guards the handover: a device that has not caught up by then is better
# released late than held.
VOLUME_SETTLE_TIMEOUT_S = 2.0
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


async def _wait_for_volume_level(
    hass: HomeAssistant,
    entity_id: str,
    *,
    timeout_s: float = VOLUME_ATTRIBUTE_TIMEOUT_S,
    poll_interval_s: float = 0.1,
) -> Optional[float]:
    """Wait for a woken speaker to report its volume, or give up.

    ``_wait_until_speakers_ready`` returns as soon as the state leaves
    ``off``, which says nothing about whether the attributes have caught
    up. Reading the level once at that moment often found nothing, and a
    speaker whose level was never recorded was then left at announcement
    volume for good, because both restore paths only walk the levels
    that were recorded.

    Returns the level, or ``None`` if it never appeared.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        state = hass.states.get(entity_id)
        if state is not None and state.state not in (
            STATE_UNAVAILABLE, STATE_UNKNOWN
        ):
            raw = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    _LOGGER.debug(
                        "%s reported a non-numeric volume %r", entity_id, raw,
                    )
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(poll_interval_s)


async def _wait_for_reported_volume(
    hass: HomeAssistant,
    entity_id: str,
    expected: float,
    *,
    timeout_s: float = VOLUME_SETTLE_TIMEOUT_S,
    poll_interval_s: float = 0.1,
) -> bool:
    """Wait until the speaker reports ``expected``, or give up.

    Setting a volume tells Home Assistant to dispatch the command; the
    entity keeps reporting the old level until the device answers. An
    announcement released from the gate at that moment reads the level
    the previous one had lowered to and adopts it as the original, which
    leaves the speaker quiet for good. Waiting here for the value to
    appear is what makes the handover honest.

    Returns whether the level actually arrived, so the caller can say so
    rather than assume it.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        state = hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # Nothing will be reported while the entity is gone, and a
            # Cast target routinely disappears for a while once its
            # session ends. Do not burn the timeout on it.
            return False
        raw = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
        if raw is not None:
            try:
                if abs(float(raw) - expected) < 0.01:
                    return True
            except (TypeError, ValueError):
                return False
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(poll_interval_s)


async def _await_buffering_settle(
    hass: HomeAssistant,
    entity_id: str,
    *,
    timeout_s: float = _BUFFERING_SETTLE_TIMEOUT_S,
    poll_s: float = _BUFFERING_SETTLE_POLL_S,
) -> str:
    """Poll ``entity_id`` until its state leaves ``buffering``.

    Returns the resolved state, or ``"buffering"`` on timeout. Cast
    Default Receiver dwells in ``buffering`` for a few hundred ms after
    a fresh ``play_media``; we don't want to issue ``media_pause`` while
    it's there because that triggers a play-then-pause cycle on the
    receiver firmware (audible blast). Bounded so we don't pin the
    announcement on a stuck stream.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    state_obj = hass.states.get(entity_id)
    if state_obj is None:
        return STATE_UNKNOWN
    state = state_obj.state
    while state == _BUFFERING_STATE:
        if asyncio.get_event_loop().time() >= deadline:
            _LOGGER.debug(
                "prepare(): %s still buffering after %.1fs, treating as-is",
                entity_id, timeout_s,
            )
            break
        await asyncio.sleep(poll_s)
        state_obj = hass.states.get(entity_id)
        if state_obj is None:
            return STATE_UNKNOWN
        state = state_obj.state
    _LOGGER.debug(
        "prepare(): %s settled out of buffering -> %s", entity_id, state,
    )
    return state


# ---------------------------------------------------------------------------
# Entry / tracker resolution helpers
# ---------------------------------------------------------------------------


def _resolve_config_entry(
    hass: HomeAssistant, tts_entity: str
) -> ConfigEntry | None:
    """Return the config entry that owns ``tts_entity``, if any."""
    er = entity_registry.async_get(hass)
    registry_entry = er.async_get(tts_entity)
    if registry_entry is None or registry_entry.config_entry_id is None:
        return None
    return hass.config_entries.async_get_entry(registry_entry.config_entry_id)


def _resolve_profile_flag(
    hass: HomeAssistant, tts_entity: str, key: str, default: Any
) -> Any:
    """Read a boolean-ish setting for ``tts_entity`` from its own config.

    Lookup order, most specific first:

    1. The profile subentry's ``data`` - where per-profile settings
       live for entries created by the profile wizard.
    2. The parent entry's ``options`` - where the legacy options flow
       writes, and the only place these toggles have a UI today.
    3. The parent entry's ``data`` - pre-options-flow entries.

    Reading only the parent's ``options`` was wrong for profile-based
    entries: a parent that owns subentries has an empty ``options``
    dict, so every profile silently fell through to the default no
    matter what the user had configured.
    """
    parent = _resolve_config_entry(hass, tts_entity)
    if parent is None:
        return default

    er = entity_registry.async_get(hass)
    registry_entry = er.async_get(tts_entity)
    if registry_entry is None:
        return default

    subentry_id = getattr(registry_entry, "config_subentry_id", None)
    if subentry_id:
        subentry = (getattr(parent, "subentries", None) or {}).get(subentry_id)
        if subentry is not None and key in subentry.data:
            return subentry.data[key]

    if key in parent.options:
        return parent.options[key]
    if key in parent.data:
        return parent.data[key]
    return default


def _resolve_health_tracker(
    hass: HomeAssistant, tts_entity: str
) -> OpenAITTSHealthTracker | None:
    """Find the health tracker that owns ``tts_entity``."""
    return health_tracker_for(_resolve_config_entry(hass, tts_entity))


def _is_cast_platform(hass: HomeAssistant, entity_id: str) -> bool:
    """True when ``entity_id`` is owned by the Chromecast platform."""
    er = entity_registry.async_get(hass)
    entry = er.async_get(entity_id)
    return entry is not None and entry.platform == "cast"


def _is_ma_platform(hass: HomeAssistant, entity_id: str) -> bool:
    """True when ``entity_id`` is owned by the Music Assistant integration.

    MA's ``media_pause`` does NOT stick on DLNA-backed players (observed
    on JBL Authentics via universal_player + DLNA): the queue auto-
    resumes within ~300ms of the pause command landing. ``media_stop``
    is sticky and preserves the current track + position, so resuming
    later with ``media_play`` continues from where the queue was.
    """
    er = entity_registry.async_get(hass)
    entry = er.async_get(entity_id)
    return entry is not None and entry.platform == "music_assistant"


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

    def __init__(
        self,
        hass: HomeAssistant,
        entity_ids: List[str],
        entry: ConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self.entity_ids = entity_ids
        # The config entry that owns the TTS profile behind this
        # announcement. Background work is registered against it so
        # Home Assistant cancels anything still running when the entry
        # unloads, rather than letting a stray task call volume_set for
        # an entry that no longer exists.
        self._entry = entry
        self._original_volumes: Dict[str, float] = {}
        # Targets we paused, mapped to a snapshot of what they were
        # playing at that moment. The snapshot lets the resume step
        # notice when ``tts.speak`` replaced the transport item on a
        # renderer shared with another controller (a DLNA speaker that
        # Music Assistant streams to, for example) - a plain
        # ``media_play`` there would replay the announcement clip
        # instead of the music.
        self._paused_media: Dict[str, Dict[str, Any]] = {}
        # Playing targets we could not pause or stop (a DLNA renderer
        # mid live-stream advertises neither action). The announcement
        # may still replace their transport item, killing the session
        # with nothing recorded to bring it back. Snapshot them here;
        # the resume step replays the original URL only when the
        # current item is provably our announcement clip.
        self._left_playing: Dict[str, Dict[str, Any]] = {}
        # Cast Default Receiver leaks ~150-400ms of the previous URL
        # right when ``play_media announce=True`` arrives over a paused
        # session - the receiver firmware briefly resumes the held
        # stream while it loads the announcement. ``media_stop`` instead
        # clears the receiver state entirely, so there's nothing to
        # leak. We snapshot the URL here so we can replay it after the
        # announcement (regular ``media_play`` doesn't restart a
        # stopped session).
        self._stopped_media: Dict[str, Dict[str, Any]] = {}
        # Targets that were idle/off/standby right before the
        # announcement. Music Assistant's announce flow auto-resumes
        # the previously queued track once the announcement ends, even
        # if the queue had been paused and the user expected silence.
        # We use this set after restore to detect that uninvited
        # playback and ``media_stop`` it.
        self._was_inactive: Set[str] = set()
        # Targets that the device's announcement layer is handling, so
        # ``_set_announcement_volume`` and ``restore`` know to leave
        # them alone. Populated by ``prepare()`` when announce_mode is on.
        self._native_announce_skipped: Set[str] = set()
        # Targets whose volume this announcement actually changed. The
        # restore reads this rather than comparing against reported
        # state, because reported state is not always current: a DLNA
        # renderer whose event subscription never reached Home
        # Assistant still said 0.27 while the speaker itself was at
        # 0.30, so the comparison decided there was nothing to undo.
        self._volume_changed: Set[str] = set()

    def _spawn(self, coro: Any, name: str) -> None:
        """Run ``coro`` in the background, tied to the config entry.

        Falls back to an entry-less task only when the owning entry
        could not be resolved, which happens for a TTS entity that is
        not in the registry. Those tasks are the ones Home Assistant
        cannot cancel on unload, so the fallback exists to keep the
        announcement working rather than because it is preferable.
        """
        if self._entry is not None:
            self._entry.async_create_background_task(self.hass, coro, name)
            return
        self.hass.async_create_task(coro, name)

    async def prepare(
        self,
        target_volume: Optional[float],
        pause_playback: bool,
        announce_mode: bool = True,
        force_manual: bool = False,
    ) -> None:
        """Turn devices on, snapshot volumes, optionally pause, set level.

        Native-announce targets are ALWAYS skipped by the manual flow.
        Those are the ones ``_native_announce_targets`` identifies:
        Sonos and Music Assistant by platform, plus anything that
        advertises ``MEDIA_ANNOUNCE`` at runtime. There is a single
        ``tts.speak`` call for every target regardless of platform;
        what differs is that HA's speak already carries
        ``announce=True`` into ``play_media``, so those devices duck
        and restore on their own. A parallel ``volume_set`` from this
        side would just spike the underlying music a second time.

        The ``announce_mode`` flag still controls behaviour for the
        non-native targets (Cast without the feature bit / Bluetooth):

        * ``True`` (default): manual pause + speak + resume so the
          user doesn't lose the music on Cast.
        * ``False``: hands-off, the speaker handles the incoming
          media however it normally does (Cast replaces, BT
          overlays).
        """
        _LOGGER.debug(
            "prepare(): targets=%s target_volume=%s pause_playback=%s announce_mode=%s",
            self.entity_ids, target_volume, pause_playback, announce_mode,
        )
        # Native-announce targets are owned by the device's
        # announcement layer when no explicit volume override is in
        # play. With a service-call volume present we instead force
        # all targets onto the manual pause + speak + resume flow:
        # MA's native announce_volume hits the same global volume as
        # a manual volume_set on speakers without per-stream support
        # (JBL Authentics et al), so the user couldn't actually
        # control the announcement loudness. Pausing first and
        # bumping the volume after avoids the audible spike and
        # gives reliable per-call volume control - we trade MA's
        # auto-ducking for it on this single announcement.
        if force_manual:
            skip_protection: Set[str] = set()
            _LOGGER.debug(
                "prepare(): force_manual=True (volume override active); "
                "treating all targets as manual"
            )
        else:
            skip_protection = _native_announce_targets(
                self.hass, self.entity_ids
            )
        self._native_announce_skipped = skip_protection
        if skip_protection:
            _LOGGER.debug(
                "prepare(): skipping pause/volume for native-announce targets: %s",
                sorted(skip_protection),
            )
        states = await asyncio.gather(
            *(get_media_player_state(self.hass, eid) for eid in self.entity_ids),
            return_exceptions=True,
        )

        # Buffering pre-pass. A target that is mid-load has to settle
        # before we can decide whether to pause it, but the settle is a
        # wait, not work: doing it inside the per-entity loop below made
        # N buffering targets cost N x the settle timeout, with the
        # music still playing at its original level the whole time.
        # Settle them all at once, then re-read their state so the
        # decisions below see post-settle attributes rather than the
        # stale pre-settle snapshot.
        buffering_idx = [
            i for i, sx in enumerate(states)
            if not isinstance(sx, Exception)
            and sx[0] == _BUFFERING_STATE
        ]
        if buffering_idx:
            buffering_ids = [self.entity_ids[i] for i in buffering_idx]
            _LOGGER.debug(
                "prepare(): settling %d buffering target(s) concurrently: %s",
                len(buffering_ids), buffering_ids,
            )
            await asyncio.gather(
                *(
                    _await_buffering_settle(self.hass, eid)
                    for eid in buffering_ids
                ),
                return_exceptions=True,
            )
            refreshed = await asyncio.gather(
                *(
                    get_media_player_state(self.hass, eid)
                    for eid in buffering_ids
                ),
                return_exceptions=True,
            )
            for i, fresh in zip(buffering_idx, refreshed):
                states[i] = fresh

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

            # Native-announce targets are owned by the device's
            # announcement layer in this mode; skip the volume snapshot
            # and the pause so we don't fight the firmware.
            #
            # Powering the device on is NOT skipped. A speaker sitting
            # in ``off`` cannot render an announcement no matter who
            # owns the ducking, and ``_wait_until_speakers_ready``
            # below needs the turn_on to have been issued before it can
            # block on readiness. Skipping this is how a Music
            # Assistant player in ``off`` ended up receiving
            # ``tts.speak`` while still powered down.
            if entity_id in skip_protection:
                needs_power = state.lower() == "off"
                _LOGGER.debug(
                    "prepare(): %s state=%s -> native announce, untouched%s",
                    entity_id, state,
                    " (turn_on queued)" if needs_power else "",
                )
                if needs_power:
                    turn_on_tasks.append(
                        call_media_player_service(self.hass, "turn_on", entity_id)
                    )
                # Record inactive native-announce targets before skipping
                # the rest. These are exactly the ones the auto-resume
                # watcher covers: the platform's own announcement layer
                # restores their queue when the clip ends. Recording it
                # after the ``continue`` below is what silently disabled
                # ``_stop_unintended_playback`` entirely, since it also
                # requires membership of ``_native_announce_skipped``.
                if state in (STATE_IDLE, STATE_OFF, STATE_STANDBY):
                    self._was_inactive.add(entity_id)
                continue

            volume = attrs.get(ATTR_MEDIA_VOLUME_LEVEL)
            if volume is not None:
                self._original_volumes[entity_id] = float(volume)
            elif state.lower() == "off":
                # Volume isn't reported until the device wakes - we'll
                # capture it once turn-on completes.
                capture_after_on.append(entity_id)

            _LOGGER.debug(
                "prepare(): %s state=%s vol=%s",
                entity_id, state, volume,
            )

            # Snapshot inactive targets so we can detect / cancel any
            # auto-resume by the platform's announce layer (see
            # ``_was_inactive`` rationale in __init__).
            if state in (STATE_IDLE, STATE_OFF, STATE_STANDBY):
                self._was_inactive.add(entity_id)

            if state.lower() == "off":
                turn_on_tasks.append(
                    call_media_player_service(self.hass, "turn_on", entity_id)
                )

            # Idle device with a queued media URL: send ``media_stop`` so
            # the platform's announce layer doesn't have an active
            # playback session to interleave with the announcement. We
            # deliberately don't ``clear_playlist`` here because the
            # user's curated queue must survive the announcement -
            # ``media_stop`` halts playback but leaves the queue intact.
            #
            # Note: this alone doesn't prevent Music Assistant's
            # post-announcement auto-resume (MA's announce service is
            # hardcoded to resume the queue when the announcement ends,
            # even if it was paused before). That leak is handled by
            # ``_stop_unintended_playback`` below, which watches for
            # the unwanted resume and pauses immediately to preserve
            # the queue position.
            if (
                state in (STATE_IDLE, STATE_STANDBY)
                and attrs.get("media_content_id")
                and _supports_media_stop(self.hass, entity_id)
            ):
                _LOGGER.info(
                    "prepare(): %s idle with queued media - "
                    "media_stop pre-TTS",
                    entity_id,
                )
                pause_tasks.append(
                    call_media_player_service(
                        self.hass, "media_stop", entity_id
                    )
                )

            if pause_playback and state == STATE_PLAYING:
                # A speaker that cannot be paused or stopped is left
                # playing. Asking anyway logs an error, wastes the
                # settle, and used to record the target as paused, so
                # the restore sent a ``media_play`` to something that
                # was never interrupted. A Nest Hub showing a dashboard
                # is the case that surfaced this.
                can_pause = _supports_media_pause(self.hass, entity_id)
                can_stop = _supports_media_stop(self.hass, entity_id)
                if not can_pause and not can_stop:
                    _LOGGER.debug(
                        "prepare(): %s supports neither pause nor stop, "
                        "leaving it playing",
                        entity_id,
                    )
                    self._left_playing[entity_id] = _pause_snapshot(attrs)
                elif _is_cast_platform(self.hass, entity_id):
                    # Cast: media_stop + snapshot URL for replay. See
                    # ``_stopped_media`` rationale on __init__.
                    #
                    # Only when the snapshot is actually replayable.
                    # An app-driven session (Spotify, YouTube, Plex)
                    # reports an id ``play_media`` cannot resolve, so
                    # stopping it would lose the music permanently.
                    content_id = attrs.get("media_content_id") or ""
                    if _is_replayable_media_id(content_id) and can_stop:
                        self._stopped_media[entity_id] = {
                            "media_content_id": content_id,
                            "media_content_type": (
                                attrs.get("media_content_type") or "music"
                            ),
                            # ``play_media`` always restarts at zero, so
                            # remember where we were and seek back after
                            # the announcement when the device allows it.
                            "media_position": attrs.get("media_position") or 0,
                        }
                        pause_tasks.append(
                            call_media_player_service(
                                self.hass, "media_stop", entity_id
                            )
                        )
                    else:
                        # Nothing we can replay, or nothing we can
                        # stop. Fall back to pause so the session
                        # survives, even if the Cast handover blip may
                        # still occur.
                        _LOGGER.debug(
                            "prepare(): %s content_id %r is not replayable, "
                            "pausing instead of stopping",
                            entity_id, content_id,
                        )
                        self._paused_media[entity_id] = _pause_snapshot(attrs)
                        pause_tasks.append(
                            call_media_player_service(
                                self.hass, SERVICE_MEDIA_PAUSE, entity_id
                            )
                        )
                elif _is_ma_platform(self.hass, entity_id):
                    # Music Assistant: ``media_pause`` doesn't stick
                    # (queue auto-resumes within ~300ms). Use
                    # ``media_stop`` instead - it sticks and preserves
                    # the queue position so a later ``media_play`` (in
                    # ``_resume_paused_media``) resumes from the same
                    # spot.
                    action = "media_stop" if can_stop else SERVICE_MEDIA_PAUSE
                    self._paused_media[entity_id] = _pause_snapshot(attrs)
                    pause_tasks.append(
                        call_media_player_service(self.hass, action, entity_id)
                    )
                else:
                    action = SERVICE_MEDIA_PAUSE if can_pause else "media_stop"
                    self._paused_media[entity_id] = _pause_snapshot(attrs)
                    pause_tasks.append(
                        call_media_player_service(self.hass, action, entity_id)
                    )

        if turn_on_tasks:
            await asyncio.gather(*turn_on_tasks, return_exceptions=True)
        # Block until ALL targets are out of off/unavailable. This
        # keeps cold and warm casts from drifting in their start-of-
        # playback time when the announcement spans multiple speakers.
        await _wait_until_speakers_ready(self.hass, self.entity_ids)
        if capture_after_on:
            for entity_id in capture_after_on:
                actual = await _wait_for_volume_level(self.hass, entity_id)
                if actual is not None:
                    self._original_volumes[entity_id] = actual
                    _LOGGER.debug(
                        "prepare(): %s woke up reporting vol=%.2f",
                        entity_id, actual,
                    )
                else:
                    _LOGGER.warning(
                        "%s never reported its volume after waking, so this "
                        "announcement leaves its level alone",
                        entity_id,
                    )

        if pause_tasks:
            _LOGGER.debug(
                "prepare(): awaiting %d pause task(s) before volume change",
                len(pause_tasks),
            )
            await asyncio.gather(*pause_tasks, return_exceptions=True)
            # Pause is async on most media platforms - the service call
            # returns instantly but the device takes a few hundred ms
            # to actually mute its output. Without this settle, the
            # subsequent volume bump is audible as a brief loudness
            # spike on the music that's still playing while pause
            # propagates.
            await asyncio.sleep(0.4)
            _LOGGER.debug("prepare(): pause settle done (0.4s)")
        elif pause_playback:
            _LOGGER.debug(
                "prepare(): pause_playback=True but no playing targets, skipping pause"
            )

        if target_volume is not None:
            _LOGGER.debug(
                "prepare(): about to set announcement volume %.2f "
                "(playing-and-not-paused targets: %s)",
                target_volume,
                [
                    eid for eid in self.entity_ids
                    if eid not in self._paused_media
                    and eid not in self._stopped_media
                    and (s := self.hass.states.get(eid)) is not None
                    and s.state == STATE_PLAYING
                ],
            )
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
        """Push every speaker to ``target`` (skip ones already there).

        Targets we marked as native-announce in ``prepare()`` are
        skipped: their device-level announcement layer already ducks
        the music for us, and a global volume_set would spike the
        underlying playback for the few seconds before the speak
        actually starts.
        """
        tasks = []
        for entity_id in self.entity_ids:
            if entity_id in self._native_announce_skipped:
                _LOGGER.debug(
                    "Skipping volume override for %s (native announce target)",
                    entity_id,
                )
                continue

            current = self._original_volumes.get(entity_id)
            if current is None:
                # No recorded level, so there is nothing to put back
                # afterwards. Changing it here is how a speaker ended up
                # stuck at announcement volume: the restore paths walk
                # the recorded levels only, so this one would never be
                # visited. Leaving it as it is costs the announcement its
                # volume override and nothing else.
                _LOGGER.warning(
                    "Not changing the volume of %s: it never reported a "
                    "level, so it could not be restored afterwards",
                    entity_id,
                )
                continue
            if abs(current - target) > 0.01:
                _LOGGER.info(
                    "Setting volume for %s -> %.2f", entity_id, target
                )
                self._volume_changed.add(entity_id)
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
            if self._paused_media or self._stopped_media:
                await asyncio.sleep(0.4)
        await self._resume_paused_media()
        self._stop_unintended_playback()

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
            if self._paused_media or self._stopped_media:
                await asyncio.sleep(0.4)
        await self._resume_paused_media()
        self._stop_unintended_playback()

    async def await_volume_settled(
        self, timeout_s: float = VOLUME_SETTLE_TIMEOUT_S
    ) -> None:
        """Wait for the levels this announcement restored to be reported.

        Only the targets whose volume was actually changed are waited
        on, and only until they report the level they were put back to.
        The caller uses this to hold the speaker gate a moment longer,
        so the next announcement snapshots a real level.
        """
        pending = [
            (eid, self._original_volumes[eid])
            for eid in self._volume_changed
            if eid in self._original_volumes
        ]
        if not pending:
            return
        results = await asyncio.gather(
            *(
                _wait_for_reported_volume(
                    self.hass, eid, level, timeout_s=timeout_s
                )
                for eid, level in pending
            ),
            return_exceptions=True,
        )
        late = [
            eid for (eid, _), ok in zip(pending, results) if ok is not True
        ]
        if late:
            _LOGGER.debug(
                "await_volume_settled: %s had not reported the restored "
                "level within %.1fs; releasing anyway",
                ", ".join(late), timeout_s,
            )

    async def _restore_one(self, entity_id: str, original_volume: float) -> bool:
        """Put one speaker back to the level it had before the announcement.

        A target this announcement actually changed is always sent the
        old level, without consulting reported state first. That check
        used to decide the outcome, and it got it wrong twice over: a
        speaker that reports no volume was never restored at all, and
        one whose reported level had not caught up looked as if it were
        already correct. Both left the speaker where the announcement
        put it.

        Targets that were only snapshotted, never changed, still go
        through the comparison, so an announcement that touched nothing
        does not issue pointless service calls.
        """
        try:
            if entity_id in self._volume_changed:
                await set_media_player_volume(
                    self.hass, entity_id, original_volume, force=True
                )
                return True
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

    def _stop_unintended_playback(self) -> None:
        """Cancel auto-resume by platform-level announce flows.

        Music Assistant's ``play_announcement`` (invoked by HA's
        ``tts.speak`` for MA-served media_players that advertise the
        MEDIA_ANNOUNCE feature) restores the previously queued track
        when the announcement ends, even if the queue was paused and
        the user expected silence. MA does NOT expose a "don't resume"
        flag - the resume is hardcoded in its announce flow.

        Scope: only targets whose announcement is owned by the device
        or the platform, and which were inactive before the
        announcement. Targets we paused ourselves are resumed by
        ``_resume_paused_media``, and a target we merely powered on has
        no queue to auto-resume, so pausing it would only truncate the
        announcement it was turned on for.

        Anything currently rendering the TTS clip itself is left alone
        for the same reason: ``media_content_id`` still carries the TTS
        proxy URL while our own announcement is audible, and pausing
        there cuts the message off mid-sentence.

        Non-blocking by design. The immediate check is fired as a
        background task and the late-resume listener tears itself down
        on a timer, so ``openai_tts.say`` returns as soon as volumes
        are restored instead of waiting out the watch window.
        """
        if not self._was_inactive:
            return

        targets = {
            eid for eid in self._was_inactive
            if eid not in self._paused_media
            and eid not in self._stopped_media
            and eid in self._native_announce_skipped
        }
        if not targets:
            return

        handled: Set[str] = set()

        def _can_interrupt(eid: str) -> bool:
            """Whether this speaker can be silenced at all."""
            return (
                _supports_media_pause(self.hass, eid)
                or _supports_media_stop(self.hass, eid)
            )

        def _stop_action(eid: str) -> str:
            # MA's media_pause auto-resumes within ~300ms on DLNA
            # backends; media_stop sticks and still preserves the queue
            # position (a later media_play continues from the saved
            # offset). For non-MA platforms, media_pause is the right
            # tool and keeps position too.
            if _is_ma_platform(self.hass, eid) and _supports_media_stop(self.hass, eid):
                return "media_stop"
            if _supports_media_pause(self.hass, eid):
                return SERVICE_MEDIA_PAUSE
            return "media_stop"

        def _is_announcement_playing(eid: str, state: Any) -> bool:
            """True when this playback is an announcement, not a stray resume.

            Two independent signals, because neither covers every
            platform. The proxy URL appears in ``media_content_id`` on
            Cast but not on Sonos or Music Assistant, which announce
            below the level Home Assistant reports. The registry covers
            those: if any announcement still holds this entity, whatever
            is playing belongs to it.
            """
            if _is_announcing(eid):
                return True
            if state is None:
                return False
            content_id = state.attributes.get("media_content_id") or ""
            return _TTS_PROXY_MARKER in content_id

        async def _pause_now(eid: str, action: str) -> None:
            await call_media_player_service(self.hass, action, eid)

        # Immediate check: MA may have already resumed by the time we
        # got here (race with the hold window). Catch those first.
        for eid in sorted(targets):
            state = self.hass.states.get(eid)
            if state is None or state.state not in (STATE_PLAYING, _BUFFERING_STATE):
                continue
            if _is_announcement_playing(eid, state):
                _LOGGER.debug(
                    "%s is playing an announcement, not pausing", eid,
                )
                continue
            if not _can_interrupt(eid):
                _LOGGER.debug(
                    "%s resumed on its own but cannot be paused or "
                    "stopped, leaving it", eid,
                )
                continue
            action = _stop_action(eid)
            _LOGGER.info(
                "Pausing unintended auto-resume on %s via %s "
                "(pre-TTS was inactive, now %s)",
                eid, action, state.state,
            )
            handled.add(eid)
            self._spawn(
                _pause_now(eid, action), f"openai_tts pause auto-resume {eid}"
            )

        # Late-resume watch: MA's auto-resume can fire 1-3s after the
        # announcement ends, which can be AFTER the immediate check
        # above. Listen for any state -> playing transition on the
        # remaining targets so we react in ~100-200ms instead of
        # waiting for another poll. Bounded so we don't accidentally
        # pause a deliberate play action that the user kicks off 30s
        # later.
        remaining = targets - handled
        if not remaining:
            return

        disposers: List[Any] = []

        def _teardown() -> None:
            while disposers:
                d = disposers.pop()
                try:
                    d()
                except Exception:  # pragma: no cover - defensive
                    pass

        @callback
        def _on_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            eid = event.data.get("entity_id")
            if eid in handled or eid not in remaining:
                return
            if new_state.state not in (STATE_PLAYING, _BUFFERING_STATE):
                return
            if _is_announcement_playing(eid, new_state):
                return
            if not _can_interrupt(eid):
                return
            handled.add(eid)
            action = _stop_action(eid)
            _LOGGER.info(
                "Late auto-resume on %s, sending %s "
                "(preserves queue position)",
                eid, action,
            )
            self._spawn(
                _pause_now(eid, action),
                f"openai_tts pause late auto-resume {eid}",
            )
            if handled >= remaining:
                _teardown()

        disposers.extend(
            async_track_state_change_event(self.hass, eid, _on_change)
            for eid in remaining
        )

        @callback
        def _on_timeout(_now: Any) -> None:
            _teardown()

        # The unsub goes into the same list as the state listeners, so
        # a watch that finishes early also cancels its own timer
        # instead of leaving it pending. Cancelling a timer that has
        # already fired is a no-op.
        disposers.append(
            async_call_later(self.hass, _LATE_RESUME_WATCH_S, _on_timeout)
        )

    async def _wait_for_announce_done(
        self,
        entity_id: str,
        *,
        timeout_s: float = 25.0,
        poll_s: float = 0.4,
    ) -> None:
        """For MA entities, wait until the device leaves the announce
        playback state before sending resume commands.

        Music Assistant sets ``ANNOUNCEMENT_IN_PROGRESS=True`` on the
        player during its announce flow and explicitly ignores incoming
        queue commands (play/pause/next) while that flag is set ("Ignore
        queue command: An announcement is in progress" in MA's source).
        If we ``media_play`` while the announce is still running on the
        device, MA drops it on the floor and the queue stays idle when
        the announcement ends - the user gets no resume.

        We watch the HA state instead (we don't have direct access to
        MA's internal flag) and treat ``idle`` / ``paused`` as the
        announce being released. Bounded by ``timeout_s`` so a stuck
        announce never freezes our restore path.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            s = self.hass.states.get(entity_id)
            if s is None or s.state not in (STATE_PLAYING, _BUFFERING_STATE):
                return
            await asyncio.sleep(poll_s)
        _LOGGER.debug(
            "_resume_paused_media: %s still playing after %.1fs, "
            "sending media_play anyway",
            entity_id, timeout_s,
        )

    async def _resume_paused_media(self) -> None:
        async def _resume_one(eid: str, snapshot: Dict[str, Any]) -> None:
            # Music Assistant ignores queue commands while its
            # announcement is still in progress. Wait for the
            # announcement to release the player before sending play.
            if _is_ma_platform(self.hass, eid):
                await self._wait_for_announce_done(eid)
            # On a player without ``MEDIA_ANNOUNCE`` the speak call
            # replaced the transport item with the announcement clip -
            # always, by DLNA semantics, regardless of what the entity
            # currently reports (dlna_dmr polls every 10s, so its view
            # of ``media_content_id`` is routinely stale here; trusting
            # it replayed the announcement instead of the music). Never
            # blind-``media_play`` these: rebuild the paused session
            # from the snapshot, or leave the player stopped.
            paused_id = snapshot.get("media_content_id")
            if not _supports_media_announce(self.hass, eid):
                if _is_replayable_media_id(paused_id):
                    _LOGGER.debug(
                        "_resume_paused_media: %s has no announce "
                        "support, the clip replaced its stream - "
                        "replaying the original URL",
                        eid,
                    )
                    await _replay_one(eid, snapshot)
                else:
                    _LOGGER.info(
                        "_resume_paused_media: announcement replaced "
                        "the stream on %s and %r cannot be replayed, "
                        "leaving it stopped",
                        eid, paused_id,
                    )
                return
            # Announce-capable player: its announce layer restored the
            # original item, so ``media_play`` resumes the music. Keep
            # a positive-evidence check for the odd case where the clip
            # is still the current item.
            _, attrs = await get_media_player_state(self.hass, eid)
            current_id = (attrs or {}).get("media_content_id") or ""
            if _TTS_PROXY_MARKER in current_id:
                if _is_replayable_media_id(paused_id):
                    _LOGGER.debug(
                        "_resume_paused_media: %s still holds the clip, "
                        "replaying the original instead of media_play",
                        eid,
                    )
                    await _replay_one(eid, snapshot)
                return
            await call_media_player_service(
                self.hass, SERVICE_MEDIA_PLAY, eid
            )

        async def _replay_one(eid: str, snapshot: Dict[str, Any]) -> None:
            # Cast targets we ``media_stop``-ed need a fresh
            # ``play_media`` to pick up the URL again -
            # SERVICE_MEDIA_PLAY on a stopped receiver is a no-op since
            # there's no current item.
            await self.hass.services.async_call(
                "media_player", "play_media",
                {
                    "entity_id": eid,
                    "media_content_id": snapshot["media_content_id"],
                    "media_content_type": snapshot["media_content_type"],
                },
                blocking=True,
            )
            # ``play_media`` restarts the item from the beginning. Seek
            # back to where the user was, so a stopped 40-minute
            # podcast doesn't silently rewind to zero. Skipped when the
            # device can't seek or we were near the start anyway.
            position = snapshot.get("media_position") or 0
            if position < _RESUME_SEEK_MIN_POSITION_S:
                return
            if not _supports_media_seek(self.hass, eid):
                _LOGGER.debug(
                    "_resume_paused_media: %s cannot seek, resuming from 0 "
                    "(was at %.0fs)",
                    eid, position,
                )
                return
            try:
                await self.hass.services.async_call(
                    "media_player", "media_seek",
                    {"entity_id": eid, "seek_position": position},
                    blocking=False,
                )
            except HomeAssistantError as err:
                _LOGGER.debug(
                    "_resume_paused_media: seek to %.0fs on %s failed: %s",
                    position, eid, err,
                )

        async def _rebuild_one(eid: str, snapshot: Dict[str, Any]) -> None:
            # A target we left playing because it could not be paused.
            # Replay only on positive evidence that the announcement
            # took over its transport: the current item is our TTS
            # clip. Anything else (still on its own stream, cleared,
            # unreadable) is left alone.
            paused_id = snapshot.get("media_content_id")
            if not _is_replayable_media_id(paused_id):
                return
            _, attrs = await get_media_player_state(self.hass, eid)
            current_id = (attrs or {}).get("media_content_id") or ""
            if _TTS_PROXY_MARKER not in current_id:
                return
            _LOGGER.info(
                "_resume_paused_media: the announcement replaced the "
                "stream on %s (could not be paused first), replaying "
                "the original URL",
                eid,
            )
            await _replay_one(eid, snapshot)

        resume_tasks = [
            _resume_one(eid, snapshot)
            for eid, snapshot in self._paused_media.items()
        ]
        resume_tasks.extend(
            _rebuild_one(eid, snapshot)
            for eid, snapshot in self._left_playing.items()
        )
        resume_tasks.extend(
            _replay_one(eid, snapshot)
            for eid, snapshot in self._stopped_media.items()
        )
        if not resume_tasks:
            return
        await asyncio.gather(*resume_tasks, return_exceptions=True)

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
    announce: Optional[bool] = None,
) -> None:
    """Run a TTS announcement with automatic volume save/restore.

    ``announce`` is the modern flag (``True`` by default at the call
    sites). Behaviour:

    * ``announce=True``  - speakers exposing ``MEDIA_ANNOUNCE`` skip
      our manual pause / volume_set / restore so the device's native
      announcement layer handles ducking and auto-resume; speakers
      without the feature fall back to manual pause + speak + resume
      so the user doesn't lose what was playing.
    * ``announce=False`` - fire-and-forget (Cast replaces the current
      media, Bluetooth overlays, Sonos still ducks at the firmware
      level). Use when an automation explicitly wants the speaker's
      default behaviour.

    ``pause_playback`` is kept for backwards compatibility with
    existing automations: it's mapped 1:1 onto ``announce`` when the
    new flag isn't provided.

    Raises ``HomeAssistantError`` when the call cannot complete - either
    because the API is in a persistent failure state, or because the
    underlying ``tts.speak`` exhausted its retries. Silent success-on-
    failure was the previous behaviour and made automations think a
    speech happened when nothing reached the speakers.
    """
    options = (options or {}).copy()

    _abort_if_persistent_failure(hass, tts_entity)

    owning_entry = _resolve_config_entry(hass, tts_entity)

    restore_enabled = (
        tts_volume is not None
        or bool(
            _resolve_profile_flag(
                hass, tts_entity, CONF_VOLUME_RESTORE, False
            )
        )
    )
    # Resolution order for the announcement-mode flag:
    #  1. Explicit ``announce`` argument (the service field)
    #  2. Explicit ``pause_playback`` argument (legacy service alias,
    #     kept so old automations keep working)
    #  3. The profile's stored ``announce_mode`` setting
    #  4. Default: announcement mode on
    if announce is not None:
        announce_enabled = bool(announce)
    elif pause_playback is not None:
        announce_enabled = bool(pause_playback)
    else:
        announce_enabled = bool(
            _resolve_profile_flag(
                hass, tts_entity, CONF_ANNOUNCE_MODE, DEFAULT_ANNOUNCE_MODE
            )
        )

    # The legacy stored ``pause_playback`` toggle keeps its original
    # meaning: force a pause of whatever is playing, independently of
    # announcement mode. It only ever adds pausing, never removes it.
    stored_pause_playback = bool(
        _resolve_profile_flag(hass, tts_entity, CONF_PAUSE_PLAYBACK, False)
    )

    available_players = _filter_available(hass, media_players)
    if not available_players:
        _LOGGER.warning("No available media players")
        return

    _LOGGER.info(
        "Playing TTS on %d players with%s volume control",
        len(available_players), "" if restore_enabled else "out",
    )

    # Build a restorer when ANY of the manual-protection features
    # is requested. Even when ``announce_enabled=True`` we still need
    # the restorer to handle non-native targets (Cast / Bluetooth):
    # for those, "announce mode" means "pause + speak + resume" so
    # the user's music isn't lost. ``pause_playback=True`` is the
    # legacy path that always pauses; we collapse it onto the same
    # manual flow.
    # Explicit per-call volume override forces all targets through
    # the manual pause+volume+resume path. This gives the user
    # reliable per-announcement volume control even on MA-served
    # speakers without per-stream volume support (JBL Authentics,
    # most consumer Cast wraps), at the cost of losing MA's
    # native auto-ducking for this single announcement.
    #
    # A volume override also implies pause: bumping the device
    # volume while the music is still playing audibly spikes it for
    # the few seconds before tts.speak takes over. Pausing first
    # makes the volume change inaudible and the resume on the way
    # out brings the music back at the original level.
    force_manual = tts_volume is not None
    pause_for_manual = (
        announce_enabled
        or pause_playback is True
        or stored_pause_playback
        or force_manual
    )
    needs_restorer = restore_enabled or pause_for_manual or force_manual
    restorer = (
        _VolumeRestorer(hass, available_players, owning_entry)
        if needs_restorer
        else None
    )

    # Wait for any announcement already running on these speakers to
    # finish, so this one starts from the speaker's real volume rather
    # than from a level the previous announcement had lowered.
    gated_players = await _acquire_speaker_gate(available_players)

    # Claim the targets for the duration of this announcement, so a
    # concurrent one is not mistaken for an unwanted auto-resume.
    _mark_announcing(available_players)

    # Everything from here on runs guarded. ``prepare()`` lowers volumes
    # and pauses media, and every one of those changes has to be undone
    # on any exit, including cancellation. Home Assistant cancels this
    # coroutine routinely: an automation with ``mode: restart`` that
    # announces twice in quick succession, or a shutdown mid-clip. When
    # that happened the speaker stayed at announcement volume and paused
    # music never came back, and the next announcement then snapshotted
    # the lowered volume as the original, making it permanent.
    restored = False
    claim_released = False
    watcher: _TtsPlaybackWatcher | None = None

    def _release_claim() -> None:
        """Stop the watcher and release the targets. Safe to call twice."""
        nonlocal claim_released
        if claim_released:
            return
        claim_released = True
        if watcher is not None:
            watcher.stop()
        # Release BEFORE restoring. The restore runs the auto-resume
        # watcher, which must see a zero count for these targets or it
        # would skip its own work. If the count is still positive here
        # another announcement holds the speaker and must not be cut off.
        # The speaker gate above normally prevents that overlap; it can
        # still happen when the gate timed out and let this call through.
        _clear_announcing(available_players)

    # Subscribe to the speakers' state bus BEFORE the speak call. On
    # synchronous platforms (Sonos / MA) the speak service only returns
    # after the announcement has finished playing; subscribing after
    # would miss every relevant transition. Whole orchestration runs
    # under a single ``try/finally`` so a CancelledError (or any other
    # BaseException) in the middle still tears down the listener.
    #
    # How much of the announcement has already elapsed by the time
    # speak returns is not measured from the clock here. It is decided
    # per signal further down (drain / synchronous speak / fallback),
    # because wall time inside speak means completely different things
    # on a fire-and-forget target and on a blocking one.
    if restorer is not None:
        watcher = _TtsPlaybackWatcher(hass, available_players)

    # Drop any failure sentinel left over from an earlier attempt BEFORE
    # speaking. That turns an ambiguous "is this sentinel current?"
    # question into a decisive one: whatever is present after the speak
    # was written by this attempt.
    #
    # Both halves matter. Trusting an old sentinel produced false
    # failures after audio had actually played from HA's own TTS cache
    # (issue #64). Ignoring a current one is worse: HA swallows errors
    # raised inside the streaming path, so ``tts.speak`` returns
    # normally even when nothing reached the speaker, and the caller
    # would be told the announcement succeeded.
    render_args = _resolved_render_args(hass, tts_entity, options)
    resolved_unique_id = _resolve_unique_id(hass, tts_entity)
    if clear_stale_failure(
        hass, message, entity_id=resolved_unique_id, **render_args
    ):
        _LOGGER.debug(
            "Cleared a pre-existing failure sentinel for %s before speaking",
            tts_entity,
        )

    async def _attempt_failed() -> bool:
        """True when this attempt wrote a failure sentinel.

        Polls briefly rather than reading once. The engine marks the
        failure from the streaming task, which finishes shortly AFTER
        ``tts.speak`` returns, so an immediate read races with the write
        and reports success for a call that actually failed. The window
        is short because a failure is recorded as soon as the provider
        answers.
        """
        cached = await _wait_for_duration_ms(
            hass, tts_entity, message, options,
            timeout_s=_FAILURE_SETTLE_TIMEOUT_S,
        )
        return cached == DURATION_FAILED_SENTINEL

    try:
        if restorer is not None:
            await restorer.prepare(
                target_volume=tts_volume if restore_enabled else None,
                pause_playback=pause_for_manual,
                announce_mode=announce_enabled,
                force_manual=force_manual,
            )
        if watcher is not None:
            watcher.start()

        try:
            await _call_tts_speak(hass, tts_entity, message, language,
                                  options, available_players,
                                  tts_volume=tts_volume)
        except Exception as err:
            if restorer is not None:
                await restorer.restore_immediate(restore_volumes=restore_enabled)
            raise HomeAssistantError(
                f"TTS speak failed: {err}"
            ) from err

        if restorer is None:
            # No volume restore and no pause, so there is nothing to hold
            # or roll back. The sentinel is still worth probing: it was
            # cleared before the speak, so finding one here means the
            # engine failed during this attempt and no audio played.
            if await _attempt_failed():
                raise HomeAssistantError(
                    f"TTS generation failed for {tts_entity}; no audio was "
                    "delivered to the speakers. Check the integration log "
                    "for the provider error."
                )
            return

        # Named for what it is rather than where it came from: this may
        # be the engine's measurement out of the cache or a length the
        # speakers reported themselves.
        resolved_ms = await _wait_for_duration_ms(
            hass, tts_entity, message, options,
            media_players=available_players, timeout_s=60.0,
        )
        if resolved_ms == DURATION_FAILED_SENTINEL:
            # Written by this attempt, since the pre-speak clear removed
            # anything older. No audio played, so roll the volumes back
            # now instead of holding for a clip that does not exist, and
            # tell the caller.
            _LOGGER.error(
                "TTS failed for %s during this attempt; restoring volumes "
                "without holding", tts_entity,
            )
            await restorer.restore_immediate(restore_volumes=restore_enabled)
            raise HomeAssistantError(
                f"TTS generation failed for {tts_entity}; no audio was "
                "delivered to the speakers. Check the integration log for "
                "the provider error."
            )

        duration_ms = resolved_ms
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
        # Whether to wait for the drain is decided from what the
        # targets are, not from what they have managed to do so far.
        # Sampling ``any_seen_tts()`` here was a race: on a short clip
        # our generator finishes before the speaker has even loaded the
        # URL, so the check said no, and the announcement was held for
        # its full duration on a device that could have told us exactly
        # when it stopped. Cast surfaces the URL, so ask Cast to prove
        # it; anything already observed on the URL qualifies too.
        expect_drain = watcher.any_seen_tts() or any(
            _is_cast_platform(hass, eid) for eid in available_players
        )
        if expect_drain:
            drain_timeout_s = max(30.0, (duration_ms + 5000) / 1000.0)
            drained = await watcher.wait_for_drain(timeout_s=drain_timeout_s)
            await asyncio.sleep(0.3)  # settle so the unmute doesn't clip
            # A wait that timed out is not a wait that succeeded. The
            # result used to be discarded, which meant a speaker that
            # never rolled off the URL was treated as having played the
            # whole clip, and the hold collapsed underneath audio that
            # may still have been going.
            elapsed_ms = duration_ms if drained else 0
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
        _release_claim()
        await restorer.restore_after_playback(
            duration_ms,
            elapsed_ms=elapsed_ms,
            restore_volumes=restore_enabled,
        )
        restored = True
    finally:
        _release_claim()
        try:
            if restorer is not None and not restored:
                # Undo whatever prepare() changed. Shielded because we
                # may be here precisely because this coroutine was
                # cancelled: the shield keeps the unwind running even
                # though awaiting it raises again, so volumes and paused
                # media still recover.
                unwind = asyncio.shield(
                    restorer.restore_immediate(restore_volumes=restore_enabled)
                )
                with contextlib.suppress(asyncio.CancelledError):
                    await unwind
        finally:
            # Last, and after the unwind on purpose: the announcement
            # waiting behind this one must find the speaker back at its
            # own volume, not at the level this one set. Dispatching the
            # restore is not enough for that, because the entity goes on
            # reporting the old level until the device answers, so wait
            # for the value to actually appear before handing over.
            if restorer is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(restorer.await_volume_settled())
            _release_speaker_gate(gated_players)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _abort_if_persistent_failure(hass: HomeAssistant, tts_entity: str) -> None:
    """Raise if the API tracker is in a state guaranteed to fail.

    Done before any speaker prep so the cast/Sonos doesn't wake up for
    a request that can't possibly produce audio.

    The tracker decides, via ``blocks_requests``, which also ages the
    block out after a while so a resolved balance or key is discovered
    without needing a config entry reload.
    """
    tracker = _resolve_health_tracker(hass, tts_entity)
    if tracker is None or not tracker.blocks_requests():
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
    tts_volume: Optional[float] = None,
) -> None:
    """Invoke HA's ``tts.speak`` exactly once.

    Engine-level retries already happen inside
    ``async_stream_tts_audio``, where they're safe (audio hasn't been
    delivered to a speaker yet). Retrying at the speak level instead can
    replay audio that already started playing on one of the targets - a
    blocking ``tts.speak`` waits for playback completion, so by the time
    we'd see an exception (e.g. an internal ``quote_from_bytes`` bug in
    HA's URL helper) the message is often already audible. Surfacing the
    failure once is preferable to playing it twice.

    Every target goes through this one call - there is no per-platform
    announcement service. HA's ``tts.speak`` sets ``announce=True`` on
    the resulting ``play_media``, which is what lets devices exposing
    ``MEDIA_ANNOUNCE`` duck and restore by themselves.

    ``tts_volume`` is accepted for symmetry with the caller's signature
    but isn't used here: HA's ``tts.speak`` doesn't carry per-call
    volume to ``play_media``, so the actual loudness control happens in
    the manual pause+volume+resume flow inside ``_VolumeRestorer``.
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


async def _wait_for_duration_ms(
    hass: HomeAssistant,
    tts_entity: str,
    message: str,
    options: Dict[str, Any],
    *,
    media_players: Optional[List[str]] = None,
    timeout_s: float = 4.0,
    poll_interval_s: float = 0.1,
) -> Optional[int]:
    """Poll for the length of the clip that is about to play.

    Two sources are checked on every pass rather than one after the
    other. The cache goes first because it is the only one that can
    report a failure, and because the engine's own measurement is
    exact. The speakers are asked next: a target that surfaced the TTS
    proxy URL reports the length itself, and reading it costs a state
    lookup.

    Checking the speakers inside the loop is what keeps an announcement
    served from Home Assistant's own TTS cache from stalling. On such a
    call our engine never runs, so nothing ever writes to the cache, and
    polling the cache alone burned the full timeout with the volume down
    and the music paused before anything else was tried.

    Returns as soon as either source answers:

    * a positive duration in milliseconds, from either source
    * ``DURATION_FAILED_SENTINEL`` (0) from the cache, which tells the
      caller no audio is coming
    * ``None`` when the timeout expires with neither source answering

    The timeout stays long on purpose. With no length to work from, the
    caller falls back to a fixed ten seconds, and restoring the volume
    that early cuts a longer announcement off part-way through, which on
    Sonos leaves the user hearing only the leading chime. Waiting too
    long holds the volume down; giving up too early loses the message.

    In streaming mode the engine writes its measurement after the final
    chunk, which can land a few seconds after ``tts.speak`` returns, so
    a single read would miss it.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        cached = _lookup_audio_duration(hass, tts_entity, message, options)
        if cached is not None:
            return cached
        if media_players:
            from_speakers = _media_player_duration_ms(hass, media_players)
            if from_speakers:
                _LOGGER.debug(
                    "Duration not in cache; the speakers report %d ms",
                    from_speakers,
                )
                return from_speakers
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

# Platforms whose ``tts.speak`` flows through a native announcement
# layer that already handles ducking + per-announcement volume at the
# device firmware level. Pushing a manual ``volume_set`` over those
# targets actually changes the underlying music volume because the
# device exposes a single global volume - we hear that as a brief
# loudness spike on whatever is playing while the announcement
# warms up. For these platforms the safe default is "let the
# device decide the announcement loudness" and skip our volume bump
# unless the caller also asked for ``pause_playback`` (in which case
# the music has stopped, so the volume change is no longer audible).
_NATIVE_ANNOUNCE_PLATFORMS: frozenset[str] = frozenset({
    # Sonos firmware ducks the underlying playback at the device
    # level when an announcement arrives.
    "sonos",
    # Music Assistant has its own announcement controller (chime +
    # TTS + post-announce restore) that, even on DLNA/Cast wraps,
    # behaves better than our manual stop+speak+resume - the latter
    # fights MA's internal ANNOUNCEMENT_IN_PROGRESS lock and ends up
    # leaving the queue idle or advanced when our resume gets
    # ignored. We trust MA's native flow here. Volume overrides
    # still force the manual path via ``force_manual=True`` in
    # ``announce()`` so explicit per-call volume control keeps
    # working.
    "music_assistant",
})


def _pause_snapshot(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Capture what a target was playing at pause time.

    Mirrors the ``_stopped_media`` snapshot shape so ``_replay_one``
    can rebuild the session when the announcement replaced it.
    """
    return {
        "media_content_id": attrs.get("media_content_id"),
        "media_content_type": attrs.get("media_content_type") or "music",
        "media_position": attrs.get("media_position") or 0,
    }


def _is_replayable_media_id(content_id: str | None) -> bool:
    """True when ``play_media`` can restore this item on its own.

    ``media_content_id`` is only a real address for URL-backed
    playback. When a Cast session is driven by a receiver app (Spotify,
    YouTube, Plex) the id is an app-internal reference such as
    ``spotify:track:...`` or an opaque provider string, and handing it
    back to ``play_media`` restores nothing - the user's music is gone
    for good. Those sessions get paused instead, which costs the brief
    Cast handover blip but always comes back.
    """
    if not content_id:
        return False
    return content_id.startswith(("http://", "https://"))


def _has_feature(
    hass: HomeAssistant, entity_id: str, feature: MediaPlayerEntityFeature
) -> bool:
    """True when the entity advertises ``feature`` right now.

    Read from the live state rather than the entity registry, because
    ``supported_features`` is a runtime property: a Cast device shows a
    different set idle than it does while an app is running. A target
    that is unavailable reports nothing and answers False.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return False
    try:
        features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0)
    except (TypeError, ValueError):
        return False
    return bool(features & feature)


def _supports_media_seek(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity accepts ``media_seek``."""
    return _has_feature(hass, entity_id, MediaPlayerEntityFeature.SEEK)


def _supports_media_pause(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity accepts ``media_pause``.

    Worth checking before calling. A Nest Hub showing a dashboard
    reports no pause feature, and asking it anyway logs an error, waits
    out the pause settle for nothing, and, worse, used to leave the
    entity recorded as paused so the restore sent it a ``media_play``
    it never needed.
    """
    return _has_feature(hass, entity_id, MediaPlayerEntityFeature.PAUSE)


def _supports_media_stop(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity accepts ``media_stop``."""
    return _has_feature(hass, entity_id, MediaPlayerEntityFeature.STOP)


def _supports_media_announce(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity advertises ``MEDIA_ANNOUNCE``.

    HA's ``tts.speak`` hands ``play_media`` an ``announce=True`` flag.
    Integrations that declare ``MediaPlayerEntityFeature.MEDIA_ANNOUNCE``
    act on it: the device ducks whatever is playing, plays the clip,
    and restores the previous stream itself. For those targets our own
    pause / volume_set / resume sequence is not just redundant, it
    fights the device.

    A target that is unavailable reports nothing, in which case the
    caller falls back on the platform allowlist.
    """
    return _has_feature(
        hass, entity_id, MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    )


def _native_announce_targets(
    hass: HomeAssistant, entity_ids: List[str]
) -> Set[str]:
    """Return the subset of ``entity_ids`` that announce natively.

    A target qualifies either by advertising ``MEDIA_ANNOUNCE`` at
    runtime or by sitting on a platform in
    ``_NATIVE_ANNOUNCE_PLATFORMS``. The allowlist is kept as a
    supplement rather than replaced: Sonos and Music Assistant duck at
    a level our capability check cannot see (Sonos does it in device
    firmware, MA in its own announcement controller), and either can
    report the feature bit inconsistently depending on the underlying
    player wrap.

    Used by ``_set_announcement_volume`` and ``prepare()`` to avoid
    forcing a global volume bump on devices that already duck for us.
    """
    if not entity_ids:
        return set()
    er = entity_registry.async_get(hass)
    out: Set[str] = set()
    for eid in entity_ids:
        entry = er.async_get(eid)
        if entry and entry.platform in _NATIVE_ANNOUNCE_PLATFORMS:
            out.add(eid)
            continue
        if _supports_media_announce(hass, eid):
            _LOGGER.debug(
                "%s advertises MEDIA_ANNOUNCE - treating as native announce",
                eid,
            )
            out.add(eid)
    return out


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
