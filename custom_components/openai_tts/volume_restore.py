"""Optimized volume restore utility with parallel operations for reduced latency."""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Set, Any

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    STATE_PLAYING,
)
from homeassistant.components.tts import DOMAIN as TTS_DOMAIN
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EVENT_STATE_CHANGED,
)
from homeassistant.helpers import entity_registry

from .const import DOMAIN, CONF_VOLUME_RESTORE, CONF_PAUSE_PLAYBACK, MESSAGE_DURATIONS_KEY
from .utils import (
    get_media_player_state,
    call_media_player_service,
    set_media_player_volume,
)

_LOGGER = logging.getLogger(__name__)

# Platform-specific buffer times (milliseconds)
PLATFORM_BUFFERS = {
    "sonos": 800,       # Sonos network sync buffer (reduced from 1200)
    "cast": 700,        # Chromecast casting buffer (reduced from 1000)
    "alexa": 600,       # Alexa cloud buffer (reduced from 800)
    "default": 300      # Default buffer for local players (reduced from 400)
}

# Platform-specific volume change delays (milliseconds)
PLATFORM_VOLUME_DELAYS = {
    "sonos": 100,       # Minimal delay for Sonos (announcement feature handles transition)
    "cast": 500,        # Standard delay for Chromecast
    "alexa": 500,       # Standard delay for Alexa
    "default": 500      # Standard delay for other players
}

# Fallback duration when TTS duration cannot be determined
FALLBACK_DURATION_MS = 10000  # 10 seconds in milliseconds


def _get_message_hash(message: str) -> str:
    """Get a hash for a message to use as cache key (must match tts.py)."""
    import hashlib
    return hashlib.md5(message.encode()).hexdigest()[:16]


def _get_cached_duration(hass: HomeAssistant, message: str) -> int | None:
    """Get cached duration for a message from the shared cache."""
    msg_hash = _get_message_hash(message)
    cache = hass.data.get(DOMAIN, {}).get(MESSAGE_DURATIONS_KEY, {})
    cached = cache.get(msg_hash)
    if cached:
        duration_ms = cached.get('duration_ms')
        if duration_ms:
            _LOGGER.info("Found cached duration for message: %d ms (hash: %s)", duration_ms, msg_hash)
            return duration_ms
    _LOGGER.debug("No cached duration found for message hash: %s (cache has %d entries)", msg_hash, len(cache))
    return None


class OptimizedVolumeRestorer:
    """Optimized volume restoration handler with parallel operations."""
    
    def __init__(self, hass: HomeAssistant, entity_ids: List[str]):
        """Initialize the volume restorer."""
        self.hass = hass
        self.entity_ids = entity_ids
        self._original_volumes: Dict[str, float] = {}
        self._playing_media: Set[str] = set()
        self._platform_buffers: Dict[str, int] = {}
        self._preparation_complete = False
        self._used_default_volume: Set[str] = set()  # Track entities where we used a default volume
        
    
    
    async def prepare_parallel(self, target_volume: Optional[float] = None, pause_playback: bool = False) -> None:
        """Prepare media players for TTS playback in parallel."""
        _LOGGER.debug("Preparing %d players, target_volume=%s", 
                     len(self.entity_ids), target_volume)
        
        # Clear any previous state
        self._original_volumes.clear()
        self._playing_media.clear()
        self._platform_buffers.clear()
        self._used_default_volume.clear()
        
        
        # Gather all player states in parallel
        state_tasks = [
            get_media_player_state(self.hass, entity_id) 
            for entity_id in self.entity_ids
        ]
        states = await asyncio.gather(*state_tasks, return_exceptions=True)
        
        turn_on_tasks = []
        pause_tasks = []
        volume_tasks = []
        
        for i, entity_id in enumerate(self.entity_ids):
            if isinstance(states[i], Exception):
                _LOGGER.warning("Failed to get state for %s: %s", entity_id, states[i])
                continue
                
            state, attributes = states[i]
            if state is None or attributes is None:
                _LOGGER.warning("Media player %s not available (state=%s, attributes=%s)", 
                             entity_id, state, attributes)
                continue
            
            # Detect platform type
            platform = self._detect_platform(entity_id)
            self._platform_buffers[entity_id] = PLATFORM_BUFFERS.get(platform, PLATFORM_BUFFERS["default"])
            if platform != "default":
                _LOGGER.debug("Detected %s platform for %s, buffer %d ms", 
                             platform, entity_id, self._platform_buffers[entity_id])
            
            # Record original volume
            original_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if original_volume is not None:
                self._original_volumes[entity_id] = float(original_volume)
                _LOGGER.debug("Recorded volume %.2f for %s (state: %s)", original_volume, entity_id, state)
            else:
                # For devices that are off, we may need to capture volume after turning on
                if state.lower() == "off":
                    # Mark this device as needing volume capture after turn on
                    self._used_default_volume.add(entity_id)
                    _LOGGER.debug("No volume for %s (off), will capture after turn on", entity_id)
            
            # Schedule turn on if needed
            if state.lower() == "off":
                turn_on_tasks.append(
                    call_media_player_service(self.hass, "turn_on", entity_id)
                )
            
            # Track playing media for pause
            if pause_playback and state == STATE_PLAYING:
                self._playing_media.add(entity_id)
                _LOGGER.debug("Will pause media on %s", entity_id)
                pause_tasks.append(
                    call_media_player_service(self.hass, SERVICE_MEDIA_PAUSE, entity_id)
                )
            
            # Schedule volume change if needed
            if target_volume is not None:
                # Set volume even if we don't have original volume recorded
                should_set_volume = False
                if original_volume is not None:
                    # We have original volume - set if different
                    if abs(original_volume - target_volume) > 0.01:
                        should_set_volume = True
                        _LOGGER.info("Setting volume for %s: %.2f -> %.2f", 
                                     entity_id, original_volume, target_volume)
                else:
                    # No original volume recorded - always set the target volume
                    should_set_volume = True
                    _LOGGER.info("Setting volume for %s to %.2f (no original recorded)", 
                                 entity_id, target_volume)
                
                if should_set_volume:
                    volume_tasks.append(
                        set_media_player_volume(self.hass, entity_id, target_volume)
                    )
        
        # Execute all operations in coordinated phases
        all_tasks = []
        
        # Phase 1: Turn on devices first
        if turn_on_tasks:
            _LOGGER.info("Turning on %d players", len(turn_on_tasks))
            turn_on_results = await asyncio.gather(*turn_on_tasks, return_exceptions=True)
            
            # Log any errors
            for i, result in enumerate(turn_on_results):
                if isinstance(result, Exception):
                    _LOGGER.error("Failed to turn on player: %s", result)
            
            # Wait for devices to turn on
            await asyncio.sleep(1.0)
            
            # For devices that just turned on, capture their actual volume BEFORE setting target
            for entity_id in self.entity_ids:
                if entity_id in self._used_default_volume and entity_id not in self._original_volumes:
                    # This device was off and we need to capture its volume
                    state, attributes = await get_media_player_state(self.hass, entity_id)
                    if state and attributes:
                        actual_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
                        if actual_volume is not None:
                            self._original_volumes[entity_id] = float(actual_volume)
                            _LOGGER.info("Captured %s actual volume %.2f after turn on", 
                                        entity_id, actual_volume)
            
            _LOGGER.debug("Devices turned on, proceeding with volume and pause operations")
        
        # Phase 2: Pause and set volume (can be done in parallel)
        if pause_tasks:
            _LOGGER.debug("Pausing %d players", len(pause_tasks))
            all_tasks.extend(pause_tasks)
        
        if volume_tasks:
            _LOGGER.debug("Setting volume on %d players", len(volume_tasks))
            all_tasks.extend(volume_tasks)
        
        if all_tasks:
            # Run all preparation tasks in parallel
            results = await asyncio.gather(*all_tasks, return_exceptions=True)
            
            # Log any errors
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    _LOGGER.error("Preparation task %d failed: %s", i, result)
            
            # Brief wait for devices to be ready
            await asyncio.sleep(0.8)
        
        self._preparation_complete = True
        if self._original_volumes:
            _LOGGER.info("Player preparation complete. Original volumes: %s", self._original_volumes)
    
    async def restore_with_duration(self, duration_ms: int) -> None:
        """Restore volumes after TTS playback using known duration.

        Each speaker runs in its own async task with platform-specific timing.
        """
        # Ensure preparation is complete
        if not self._preparation_complete:
            _LOGGER.warning("Restoration called before preparation complete")
            await asyncio.sleep(0.5)

        task_start_time = asyncio.get_running_loop().time()
        _LOGGER.info("=== STARTING PARALLEL RESTORATION at %.3f ===", task_start_time)
        _LOGGER.info("Duration: %d ms, Speakers: %d", duration_ms, len(self._original_volumes))

        # Create independent tasks for each speaker
        restore_tasks = []

        for entity_id, original_volume in self._original_volumes.items():
            platform = self._detect_platform(entity_id)
            buffer_ms = self._platform_buffers.get(entity_id, PLATFORM_BUFFERS.get(platform, 500))

            # Sonos buffer - needs enough time for audio to finish
            if platform == "sonos":
                buffer_ms = 500

            _LOGGER.info("Creating task for %s: duration=%d, buffer=%d, total_wait=%d ms",
                        entity_id, duration_ms, buffer_ms, duration_ms + buffer_ms)

            # Create independent task for this speaker
            task = asyncio.create_task(
                self._restore_speaker_independent(entity_id, original_volume, duration_ms, buffer_ms)
            )
            restore_tasks.append(task)
            _LOGGER.info("Task created for %s at %.3f", entity_id, asyncio.get_running_loop().time())

        # Schedule media resume tasks (run after longest duration)
        if self._playing_media:
            max_wait = duration_ms + max(self._platform_buffers.values(), default=500)
            for entity_id in self._playing_media:
                task = asyncio.create_task(
                    self._resume_media_after_delay(entity_id, max_wait)
                )
                restore_tasks.append(task)

        # Wait for all restoration tasks to complete
        if restore_tasks:
            results = await asyncio.gather(*restore_tasks, return_exceptions=True)
            success_count = sum(1 for r in results if r is True)
            if success_count > 0:
                _LOGGER.info("Successfully restored %d speakers", success_count)

    async def _restore_speaker_independent(
        self, entity_id: str, original_volume: float, duration_ms: int, buffer_ms: int
    ) -> bool:
        """Restore a single speaker's volume independently with its own timing."""
        total_wait_ms = duration_ms + buffer_ms
        start_time = asyncio.get_running_loop().time()

        _LOGGER.info("%s: STARTING wait - %d ms (duration) + %d ms (buffer) = %.1f seconds",
                     entity_id, duration_ms, buffer_ms, total_wait_ms / 1000)

        # Wait for this speaker's specific duration
        await asyncio.sleep(total_wait_ms / 1000)

        elapsed = (asyncio.get_running_loop().time() - start_time) * 1000
        _LOGGER.info("%s: FINISHED wait after %.0f ms, now restoring volume",
                     entity_id, elapsed)

        # Restore volume
        result = await self._restore_volume_safe(entity_id, original_volume)

        _LOGGER.info("%s: Volume restore completed (success=%s)", entity_id, result)
        return result

    async def _resume_media_after_delay(self, entity_id: str, delay_ms: int) -> bool:
        """Resume media playback after a delay."""
        await asyncio.sleep(delay_ms / 1000)
        try:
            await call_media_player_service(self.hass, SERVICE_MEDIA_PLAY, entity_id)
            return True
        except Exception as e:
            _LOGGER.error("Failed to resume media on %s: %s", entity_id, e)
            return False
    
    async def _restore_all_parallel(self) -> None:
        """Restore volumes and resume media in parallel."""
        if self._original_volumes:
            _LOGGER.info("Restoring volumes for %d players: %s", 
                        len(self._original_volumes), list(self._original_volumes.keys()))
        
        restore_tasks = []
        resume_tasks = []
        
        # Prepare volume restoration tasks
        for entity_id, original_volume in self._original_volumes.items():
            restore_tasks.append(
                self._restore_volume_safe(entity_id, original_volume)
            )
        
        # Prepare media resume tasks
        for entity_id in self._playing_media:
            resume_tasks.append(
                call_media_player_service(self.hass, SERVICE_MEDIA_PLAY, entity_id)
            )
        
        # Execute all restoration in parallel
        all_tasks = restore_tasks + resume_tasks
        
        if all_tasks:
            results = await asyncio.gather(*all_tasks, return_exceptions=True)
            
            # Log results
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    _LOGGER.error("Restoration task %d failed: %s", i, result)
            
            # Count successes
            volume_restored = sum(1 for r in results[:len(restore_tasks)] if r is True)
            media_resumed = sum(1 for r in results[len(restore_tasks):] if not isinstance(r, Exception))
            
            if volume_restored > 0:
                _LOGGER.info("Successfully restored volumes for %d players", volume_restored)
            
            if media_resumed > 0:
                _LOGGER.info("Successfully resumed media on %d players", media_resumed)
    
    async def _restore_volume_safe(self, entity_id: str, original_volume: float) -> bool:
        """Safely restore volume for a single player."""
        try:
            # Get current state
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                _LOGGER.warning("Cannot restore volume for %s - no state available", entity_id)
                return False
            
            current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            # Skip restoration only if the speaker has no volume control at all
            if current_volume is None:
                return False
            
            volume_diff = abs(float(current_volume) - original_volume)
            if volume_diff > 0.01:
                _LOGGER.info("Restoring volume for %s: %.2f -> %.2f (diff: %.2f)",
                            entity_id, float(current_volume), original_volume, volume_diff)
                await set_media_player_volume(self.hass, entity_id, original_volume)
                return True
            
            return False
            
        except Exception as e:
            _LOGGER.error("Failed to restore volume for %s: %s", entity_id, e)
            return False
    
    async def _set_volume_for_all_players(self, target_volume: float, skip_delay: bool = False) -> None:
        """Set volume for all players with platform-specific timing.
        
        Args:
            target_volume: The volume level to set (0.0-1.0)
            skip_delay: If True, skip the post-volume-change delay (used for Sonos parallel execution)
        """
        volume_tasks = []
        platform_delays = {}  # Track delay needed for each platform
        
        for entity_id in self.entity_ids:
            # Get current state to check if volume control is available
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                continue
            
            current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if current_volume is not None:
                # Only set if different to avoid unnecessary calls
                if abs(current_volume - target_volume) > 0.01:
                    # Detect platform for this entity
                    platform = self._detect_platform(entity_id)
                    delay_ms = PLATFORM_VOLUME_DELAYS.get(platform, PLATFORM_VOLUME_DELAYS["default"])
                    platform_delays[platform] = delay_ms
                    
                    _LOGGER.debug("Setting volume for %s (%s): %.2f -> %.2f (delay: %dms)", 
                                 entity_id, platform, current_volume, target_volume, delay_ms)
                    volume_tasks.append(
                        set_media_player_volume(self.hass, entity_id, target_volume)
                    )
        
        if volume_tasks:
            # Set all volumes in parallel for faster execution
            await asyncio.gather(*volume_tasks, return_exceptions=True)
            
            # Only apply delay if not skipped (for non-Sonos or non-parallel execution)
            if not skip_delay and platform_delays:
                max_delay_ms = max(platform_delays.values())
                _LOGGER.debug("Waiting %dms after volume change (platforms: %s)", 
                             max_delay_ms, list(platform_delays.keys()))
                await asyncio.sleep(max_delay_ms / 1000)
    
    def _detect_platform(self, entity_id: str) -> str:
        """Detect platform type from entity's integration."""
        try:
            state = self.hass.states.get(entity_id)
            if not state:
                return "default"
            
            # Get the integration/platform from the entity registry
            er = entity_registry.async_get(self.hass)
            entry = er.async_get(entity_id)
            
            if entry and entry.platform:
                platform = entry.platform.lower()
                # Map known platforms to our platform types
                if platform == "cast":
                    return "cast"
                elif platform == "sonos":
                    return "sonos"
                elif platform == "alexa_media":
                    return "alexa"
                else:
                    return "default"
            else:
                return "default"
        except Exception as e:
            _LOGGER.error("Error detecting platform for %s: %s", entity_id, e)
            return "default"


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
    """Optimized TTS announcement with parallel operations for reduced latency."""
    options = options.copy() if options else {}
    
    # Get configuration
    entries = hass.config_entries.async_entries(DOMAIN)
    config_entry = entries[0] if entries else None
    
    restore_enabled = (
        tts_volume is not None or 
        (config_entry and config_entry.options.get(CONF_VOLUME_RESTORE, False))
    )
    
    pause_enabled = (
        pause_playback if pause_playback is not None 
        else (config_entry and config_entry.options.get(CONF_PAUSE_PLAYBACK, False))
    )
    
    # Filter available players - include "off" state for Google speakers
    available_players = []
    for entity_id in media_players:
        state = hass.states.get(entity_id)
        if state and state.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
            available_players.append(entity_id)
        else:
            _LOGGER.warning("Media player %s is not available (state: %s)", 
                           entity_id, state.state if state else "None")
    
    if not available_players:
        _LOGGER.warning("No available media players")
        return
    
    _LOGGER.info("Playing TTS on %d players with%s volume control", 
                len(available_players), "" if restore_enabled else "out")
    
    # Create volume restorer
    restorer = OptimizedVolumeRestorer(hass, available_players) if restore_enabled else None
    
    try:
        # Check if TTS entity exists and is available
        tts_entity_state = hass.states.get(tts_entity)
        if not tts_entity_state:
            _LOGGER.error("TTS entity %s not found in states!", tts_entity)
            raise ValueError(f"TTS entity {tts_entity} not found")
        
        _LOGGER.debug("TTS entity %s state: %s", tts_entity, tts_entity_state.state)
        
        # Start preparing players immediately (non-blocking)
        prepare_task = None
        if restorer:
            prepare_task = asyncio.create_task(
                restorer.prepare_parallel(
                    target_volume=None,  # Don't set volume yet
                    pause_playback=pause_enabled
                )
            )
        
        
        # Generate TTS and play with retry logic
        duration_ms = None
        max_retries = 3
        retry_delay = 0.5
        tts_success = False
        
        try:
            for attempt in range(max_retries):
                try:
                    if attempt == 0:
                        _LOGGER.debug("Generating TTS audio first (without players)")
                    
                    # Check if entity exists
                    tts_state = hass.states.get(tts_entity)
                    if not tts_state:
                        _LOGGER.error("TTS entity %s not found in states!", tts_entity)
                    else:
                        _LOGGER.debug("TTS entity %s found, state: %s", tts_entity, tts_state.state)
                    
                    # Ensure preparation is complete before attempting to play
                    if prepare_task:
                        if not prepare_task.done():
                            await prepare_task
                    
                    # Prepare service data for HA's speak service
                    # extra_payload is now in supported_options, so we pass all options
                    service_data = {
                        "message": message,
                        "language": language,
                        "options": options,
                        "media_player_entity_id": available_players,
                    }
                    
                    # Store whether we need to change volume later
                    need_volume_change = restorer and tts_volume is not None

                    # Initialize playback_start_time before try block to avoid undefined variable
                    playback_start_time = asyncio.get_running_loop().time()

                    # Capture duration BEFORE calling speak to detect changes
                    pre_speak_state = hass.states.get(tts_entity)
                    pre_speak_duration = pre_speak_state.attributes.get('media_duration') if pre_speak_state else None
                    pre_speak_timestamp = pre_speak_state.last_changed if pre_speak_state else None
                    _LOGGER.debug("Pre-speak duration: %s ms, timestamp: %s", pre_speak_duration, pre_speak_timestamp)

                    # Set volume BEFORE calling speak (audio starts playing during the call!)
                    if need_volume_change:
                        _LOGGER.info("Setting volume BEFORE speak (for streaming)")
                        await restorer._set_volume_for_all_players(tts_volume, skip_delay=True)

                    # Call TTS service to generate and play audio
                    tts_start = asyncio.get_running_loop().time()
                    await hass.services.async_call(
                        TTS_DOMAIN,
                        "speak",
                        service_data,
                        target={"entity_id": tts_entity},
                        blocking=True,
                    )

                    tts_end_time = asyncio.get_running_loop().time()
                    tts_generation_time = (tts_end_time - tts_start) * 1000
                    _LOGGER.info("TTS speak service call completed in %.0f ms", tts_generation_time)

                    # Check if TTS is actively generating audio
                    tts_state = hass.states.get(tts_entity)
                    engine_active = tts_state.attributes.get('engine_active', False) if tts_state else False

                    # Check if HA served cached audio (fast response + engine not active)
                    # HA cache hit: speak returns very quickly (< 200ms) and engine was never activated
                    ha_cache_hit = tts_generation_time < 200 and not engine_active

                    # Determine when playback actually started:
                    # - Streaming: playback starts during speak call (at tts_start)
                    # - Non-streaming (chime/normalize): playback starts AFTER speak returns (at tts_end_time)
                    # - HA cache hit: playback starts immediately when speak is called
                    if ha_cache_hit:
                        # HA served cached audio - playback started at speak call
                        playback_start_time = tts_start
                        _LOGGER.debug("HA cache hit - playback started at speak call")
                    elif tts_generation_time > 500:
                        # Long generation time = non-streaming, playback starts after speak returns
                        playback_start_time = tts_end_time
                        _LOGGER.debug("Non-streaming detected - playback starts after speak returns")
                    else:
                        # Short generation time but not cache = streaming, playback started at speak
                        playback_start_time = tts_start
                        _LOGGER.debug("Streaming detected - playback started at speak call")

                    # Only use cached duration if engine is NOT active (true cached audio from HA)
                    # If engine is active, audio is being regenerated and duration may change
                    if not engine_active:
                        cached_duration = _get_cached_duration(hass, message)
                        if cached_duration:
                            _LOGGER.info("Using cached duration (engine idle): %d ms", cached_duration)
                            duration_ms = cached_duration
                            tts_success = True
                            break
                        elif ha_cache_hit:
                            # HA served cached audio but we don't have duration - use fallback immediately
                            _LOGGER.warning("HA served cached audio but no duration in cache - using fallback")
                            duration_ms = FALLBACK_DURATION_MS
                            tts_success = True
                            break
                    else:
                        _LOGGER.info("Engine active - waiting for fresh duration (not using cache)")

                    # Not in shared cache yet - TTS is generating new audio
                    # Wait for TTS generation to complete
                    max_wait_time = 30  # seconds
                    check_interval = 0.2  # seconds
                    waited_time = 0
                    volume_changed = True  # Already set above

                    while waited_time < max_wait_time:
                        tts_state = hass.states.get(tts_entity)
                        engine_active = tts_state.attributes.get('engine_active', False) if tts_state else False

                        # Engine finished - check shared cache for duration
                        if not engine_active and (volume_changed or waited_time > 0.5):
                            # IMPORTANT: For chime/normalize audio, playback starts NOW
                            # (when engine finishes), not when speak was called!
                            playback_start_time = asyncio.get_running_loop().time()
                            _LOGGER.info("Engine finished - playback starts NOW (chime/normalize mode)")

                            # Wait briefly for duration to be stored
                            for _ in range(30):  # Up to 3 seconds
                                await asyncio.sleep(0.1)
                                cached_duration = _get_cached_duration(hass, message)
                                if cached_duration:
                                    duration_ms = cached_duration
                                    _LOGGER.info("Got duration from wait loop: %d ms (full duration, no elapsed)", duration_ms)
                                    tts_success = True
                                    break
                            else:
                                # Fallback: use entity state or default
                                entity_duration = tts_state.attributes.get('media_duration') if tts_state else None
                                if entity_duration and entity_duration != pre_speak_duration:
                                    duration_ms = int(entity_duration)
                                    _LOGGER.info("Using entity duration: %d ms", duration_ms)
                                    tts_success = True
                                else:
                                    duration_ms = FALLBACK_DURATION_MS
                                    _LOGGER.warning("Using fallback duration: %d ms", duration_ms)
                                    tts_success = True

                            break

                        await asyncio.sleep(check_interval)
                        waited_time += check_interval

                    if not tts_success:
                        _LOGGER.warning("TTS generation timed out")
                        duration_ms = FALLBACK_DURATION_MS
                        tts_success = True
                    
                    # Success - break out of retry loop
                    break
                
                except Exception as e:
                    if attempt < max_retries - 1:
                        _LOGGER.warning("Attempt %d failed: %s. Retrying in %.1f seconds...", 
                                       attempt + 1, e, retry_delay)
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        _LOGGER.error("All attempts to call TTS speak service failed: %s", e)
                        # Don't raise - we'll handle the failure gracefully below
        
        
        finally:
            pass  # No cleanup needed anymore
        
        # Only proceed with restoration if TTS was successful
        if tts_success:
            # If we still don't have duration, use a safe fallback
            if not duration_ms:
                _LOGGER.warning("No duration available from TTS entity, using 10 second fallback timer")
                duration_ms = FALLBACK_DURATION_MS

            # Handle restoration with the known duration
            if restorer:
                # Account for time already elapsed since playback started
                elapsed_ms = int((asyncio.get_running_loop().time() - playback_start_time) * 1000)
                remaining_ms = max(0, duration_ms - elapsed_ms)
                _LOGGER.info("TTS: %d ms elapsed, %d ms remaining of %d ms total",
                           elapsed_ms, remaining_ms, duration_ms)
                await restorer.restore_with_duration(remaining_ms)
        else:
            # TTS failed - restore volumes immediately without waiting
            _LOGGER.warning("TTS generation failed, restoring volumes immediately")
            if restorer:
                try:
                    await restorer._restore_all_parallel()
                except Exception as restore_err:
                    _LOGGER.error("Failed to restore volumes after TTS failure: %s", restore_err)
        
    except Exception as e:
        _LOGGER.error("Error during TTS announcement: %s", e)
        
        # Emergency restore
        if restorer:
            try:
                await restorer._restore_all_parallel()
            except Exception as restore_err:
                _LOGGER.error("Failed to restore after error: %s", restore_err)
        
        raise
