# volume_restore.py
"""Helper to snapshot & restore media_player volumes and media state around a TTS announcement."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_POSITION,
    ATTR_APP_NAME,
    DOMAIN as MP_DOMAIN,
    SERVICE_VOLUME_SET,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_PLAY_MEDIA,
    STATE_IDLE,
    STATE_PLAYING,
)
from homeassistant.components.tts import DOMAIN as TTS_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID, 
    STATE_UNKNOWN, 
    STATE_UNAVAILABLE,
    STATE_PAUSED,
)
from homeassistant.helpers.typing import StateType

from .const import DOMAIN, CONF_VOLUME_RESTORE, CONF_PAUSE_PLAYBACK
from .utils import (
    get_media_duration,
    get_media_player_state,
    call_media_player_service,
    normalize_entity_ids,
    set_media_player_volume,
    get_speaker_status,
)

_LOGGER = logging.getLogger(__name__)


async def get_audio_duration(hass: HomeAssistant, media_url: str) -> int | None:
    """Get duration of TTS audio from URL."""
    try:
        # Handle TTS proxy URL
        if media_url.startswith("/api/tts_proxy/"):
            full_url = f"{hass.config.internal_url}{media_url}"
            return await _download_audio(hass, full_url)
        
        # Handle direct HTTP URLs
        elif media_url.startswith("http"):
            return await _download_audio(hass, media_url)
        
        # Handle local file paths
        elif os.path.exists(media_url):
            # This is running in an async context, so use executor
            duration = await hass.async_add_executor_job(get_media_duration, media_url)
            return int(duration * 1000)
        
        return None
    except Exception as e:
        _LOGGER.error("Error getting TTS audio duration from URL: %s", e)
        return None


async def _download_audio(hass: HomeAssistant, url: str) -> int | None:
    """Download and get audio duration."""
    _LOGGER.debug("Downloading audio from URL: %s", url)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
                        content = await response.read()
                        tmp_file.write(content)
                        tmp_path = tmp_file.name
                    
                    # Get duration (always async in this context)
                    duration = await hass.async_add_executor_job(get_media_duration, tmp_path)
                    
                    # Clean up temp file
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    
                    return int(duration * 1000)  # Convert to milliseconds
        return None
    except Exception as e:
        _LOGGER.error("Error downloading audio: %s", e)
        return None


class MediaState:
    """Store media state for pause/resume functionality."""
    
    def __init__(self, entity_id: str, state: str, attributes: dict):
        """Initialize media state."""
        self.entity_id = entity_id
        self.state = state
        self.was_playing = state == STATE_PLAYING
        
        # Media attributes
        self.media_content_id = attributes.get(ATTR_MEDIA_CONTENT_ID)
        self.media_content_type = attributes.get(ATTR_MEDIA_CONTENT_TYPE)
        self.media_position = attributes.get(ATTR_MEDIA_POSITION)
        self.app_name = attributes.get(ATTR_APP_NAME)
        
        # Extended attributes for better playlist/queue support
        self.media_title = attributes.get("media_title")
        self.media_artist = attributes.get("media_artist")
        self.media_album = attributes.get("media_album_name")
        self.media_playlist = attributes.get("media_playlist")
        self.shuffle = attributes.get("shuffle", False)
        self.repeat = attributes.get("repeat", "off")
        
        # Extract Spotify context if available
        self.spotify_context = None
        if self.media_content_id and self.media_content_id.startswith("spotify:"):
            self.spotify_context = attributes.get("media_context_uri") or attributes.get("spotify_context")
        
    def should_resume(self) -> bool:
        """Check if media should be resumed."""
        return self.was_playing and self.media_content_id is not None


class VolumeRestorer:
    """Handle volume restoration for media players."""
    
    def __init__(self, hass: HomeAssistant, entity_ids: List[str]):
        """Initialize the volume restorer."""
        self.hass = hass
        self.entity_ids = entity_ids
        self._initial_volumes: Dict[str, float] = {}
        self._needs_restore: Dict[str, bool] = {}
        self._media_states: Dict[str, MediaState] = {}
    
    async def record_initial(self) -> None:
        """Record the initial volume for each media player."""
        for entity_id in self.entity_ids:
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                _LOGGER.warning("Media player %s not available", entity_id)
                continue
            
            volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if volume is not None:
                self._initial_volumes[entity_id] = float(volume)
                self._needs_restore[entity_id] = False
                _LOGGER.debug("Recorded initial volume %.2f for %s", volume, entity_id)

    async def record_media_state(self) -> None:
        """Record the current media state for each player."""
        _LOGGER.debug("Recording media state for players")
        
        for entity_id in self.entity_ids:
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                continue
                    
            # Store the media state
            self._media_states[entity_id] = MediaState(entity_id, state, attributes)
            
            media_state = self._media_states[entity_id]
            if state == STATE_PLAYING or state == STATE_PAUSED:
                _LOGGER.debug(
                    "Recorded %s media for %s: %s (position: %s)",
                    state, entity_id, media_state.media_content_id, media_state.media_position
                )

    async def pause_playing_media(self) -> None:
        """Pause any currently playing media."""
        pause_tasks = []
        
        for entity_id, media_state in self._media_states.items():
            if media_state.was_playing:
                _LOGGER.debug("Pausing media on %s", entity_id)
                pause_tasks.append(
                    call_media_player_service(
                        self.hass,
                        SERVICE_MEDIA_PAUSE,
                        entity_id
                    )
                )
        
        if pause_tasks:
            await asyncio.gather(*pause_tasks)
            await asyncio.sleep(0.5)  # Allow time to pause

    async def resume_media(self) -> None:
        """Resume previously playing media."""
        resume_tasks = []
        
        for entity_id, media_state in self._media_states.items():
            if media_state.should_resume():
                _LOGGER.debug("Resuming media on %s: %s", entity_id, media_state.media_content_id)
                resume_tasks.append(
                    call_media_player_service(
                        self.hass,
                        SERVICE_MEDIA_PLAY,
                        entity_id
                    )
                )
        
        if resume_tasks:
            await asyncio.gather(*resume_tasks)

    async def set_volume_if_needed(self, level: float) -> None:
        """Set media players to specified volume level."""
        _LOGGER.debug("Setting volume to %.2f for %d players", level, len(self.entity_ids))
        volume_tasks = []
        
        # Convert to float to ensure proper comparison
        level = float(level)
        
        # Track devices and their current volumes for logging
        device_volumes = {}
        
        for entity_id in self.entity_ids:
            # Check current state and volume
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                _LOGGER.debug("Player %s is unavailable, skipping", entity_id)
                continue
                
            current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if current_volume is None:
                _LOGGER.debug("Player %s has no volume attribute, skipping", entity_id)
                continue
                
            # Record current volume for reporting
            device_volumes[entity_id] = current_volume
                
            # Record initial volume if not already done
            if entity_id not in self._initial_volumes:
                self._initial_volumes[entity_id] = float(current_volume)
                _LOGGER.debug("Recorded initial volume for %s: %.2f (state: %s)", 
                             entity_id, float(current_volume), state)
            
            # Skip if already at target volume (with small tolerance)
            if abs(float(current_volume) - level) < 0.01:
                _LOGGER.debug("Volume already at desired level %.2f for %s", level, entity_id)
                self._needs_restore[entity_id] = False
                continue
            
            _LOGGER.debug("Volume change needed for %s: %.2f → %.2f", 
                         entity_id, float(current_volume), level)
            
            # Set volume
            volume_tasks.append(
                self._set_volume_with_result(entity_id, level)
            )
        
        # Log summary of what we're going to do
        if volume_tasks:
            _LOGGER.debug("Setting volume for %d players (from current volumes: %s)", 
                         len(volume_tasks), 
                         ", ".join([f"{entity}: {vol:.2f}" for entity, vol in device_volumes.items()]))
            
            # Run volume tasks concurrently
            results = await asyncio.gather(*volume_tasks)
            
            # Mark which ones need restoration based on results
            successful_players = []
            for entity_id, success in results:
                if success:
                    self._needs_restore[entity_id] = True
                    successful_players.append(entity_id)
                else:
                    _LOGGER.warning("Failed to set volume for %s, will not restore later", entity_id)
                    
            _LOGGER.debug("Successfully set volume for %d players: %s", 
                         len(successful_players), ", ".join(successful_players) if successful_players else "none")
    
    async def _set_volume_with_result(self, entity_id: str, level: float) -> Tuple[str, bool]:
        """Set volume and return result tuple with entity_id and success status."""
        success = await set_media_player_volume(
            self.hass, 
            entity_id, 
            level
        )
        return (entity_id, success)

    async def restore(self) -> None:
        """Restore each media player to its original volume."""
        restore_tasks = []
        
        # Restore volumes for players that were changed
        for entity_id, original_volume in self._initial_volumes.items():
            # Only restore if we actually changed the volume
            if not self._needs_restore.get(entity_id, False):
                continue
                
            # Get current state and volume
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                continue
                
            current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if current_volume is None:
                continue
                
            # Only restore if current volume is different from original
            if abs(float(current_volume) - original_volume) > 0.01:
                _LOGGER.debug(
                    "Restoring volume for %s from %.2f to %.2f",
                    entity_id, float(current_volume), original_volume
                )
                restore_tasks.append(
                    set_media_player_volume(
                        self.hass, 
                        entity_id, 
                        original_volume
                    )
                )
        
        # Run all restoration tasks concurrently
        if restore_tasks:
            await asyncio.gather(*restore_tasks)


async def wait_for_duration(
    hass: HomeAssistant,
    tts_entity: str,
    timeout_ms: int = 30000
) -> int | None:
    """Wait for media_duration attribute."""
    start_time_ms = int(asyncio.get_event_loop().time() * 1000)
    
    while (int(asyncio.get_event_loop().time() * 1000) - start_time_ms) < timeout_ms:
        tts_state = hass.states.get(tts_entity)
        
        if tts_state and hasattr(tts_state, 'attributes'):
            media_duration_ms = tts_state.attributes.get('media_duration')
            
            if media_duration_ms is not None:
                _LOGGER.debug("TTS media duration: %d ms", media_duration_ms)
                return media_duration_ms
        
        # Wait 500ms before checking again
        await asyncio.sleep(0.5)
    
    _LOGGER.debug("Timeout waiting for media_duration from TTS entity")
    return None


async def _wait_tts_process(hass: HomeAssistant, tts_entity: str) -> Optional[int]:
    """Wait for TTS processing to complete and get duration."""
    start_time = time.monotonic()
    max_wait_time = 60.0  # Maximum wait time of 60s for long announcements
    duration_checks = 0 # Counter for logging purposes
    
    # Try to get the current engine_active state
    current_tts_state = hass.states.get(tts_entity)
    if current_tts_state is not None:
        initial_active = current_tts_state.attributes.get("engine_active", False)
        _LOGGER.debug("Initial TTS engine_active state: %s", initial_active)
        
        # Check if media_duration is already available (from a previous run)
        media_duration_ms = current_tts_state.attributes.get("media_duration")
        if media_duration_ms is not None and not initial_active:
            _LOGGER.debug("Media duration already available: %s ms (cached)", media_duration_ms)
            return media_duration_ms
    else:
        initial_active = False
        _LOGGER.warning("Could not get initial TTS state")
    
    # If engine is not yet active, wait for it to become active
    if not initial_active:
        tts_active = False
        for _ in range(20):  # 20 x 0.5s = 10s maximum wait
            tts_state = hass.states.get(tts_entity)
            if tts_state and tts_state.attributes.get("engine_active") is True:
                tts_active = True
                _LOGGER.debug("TTS engine is now active, waiting for processing to complete")
                break
            await asyncio.sleep(0.5)
            
        if not tts_active:
            _LOGGER.warning("TTS engine did not become active after 10s, using fallback timing")
            # Try to get media_duration anyway, in case it's set despite engine_active not updating
            tts_state = hass.states.get(tts_entity)
            if tts_state:
                media_duration_ms = tts_state.attributes.get("media_duration")
                if media_duration_ms is not None:
                    _LOGGER.debug("Retrieved media_duration despite engine_active issue: %s ms", media_duration_ms)
                    return media_duration_ms
            return None
    
    # Now wait for the engine to complete processing, or timeout
    processing_complete = False
    last_logged_media_duration = None
    
    while time.monotonic() - start_time < max_wait_time:
        tts_state = hass.states.get(tts_entity)
        if tts_state is None:
            await asyncio.sleep(0.5)
            continue
         
        # Check for media_duration even while processing is active
        # This helps get the duration as early as possible
        current_media_duration = tts_state.attributes.get("media_duration")
        
        # Only log when the media_duration changes to avoid log spam
        if current_media_duration is not None and current_media_duration != last_logged_media_duration:
            duration_checks += 1
            last_logged_media_duration = current_media_duration
            _LOGGER.debug("Media duration check #%d: %s ms (engine still active)", duration_checks, current_media_duration)
        
        # Check if processing is complete
        if tts_state.attributes.get("engine_active") is False:
            # Engine processing is complete
            processing_complete = True
            elapsed = time.monotonic() - start_time
            _LOGGER.debug("TTS processing completed after %.1f seconds", elapsed)
            
            # Get the final media duration
            media_duration_ms = tts_state.attributes.get("media_duration")
            if media_duration_ms is not None and media_duration_ms != last_logged_media_duration:
                _LOGGER.debug("Final media_duration: %s ms (engine inactive)", media_duration_ms)
            
            return media_duration_ms
        
        await asyncio.sleep(0.5)
    
    # If we timed out waiting for processing to complete, try to get media_duration anyway
    _LOGGER.warning("Timeout waiting for TTS processing to complete after %.1f seconds", time.monotonic() - start_time)
    final_tts_state = hass.states.get(tts_entity)
    if final_tts_state:
        media_duration_ms = final_tts_state.attributes.get("media_duration")
        if media_duration_ms is not None:
            _LOGGER.debug("Using media_duration despite timeout: %s ms", media_duration_ms)
            return media_duration_ms
    
    return None


async def monitor_players(
    hass: HomeAssistant,
    players: List[str],
    volume_restorer: VolumeRestorer,
    restore_enabled: bool = True,
    timeout_seconds: float = 120.0,
    media_duration_ms: Optional[int] = None,
    initially_playing_players: Optional[Set[str]] = None
) -> None:
    """Monitor players and restore volumes after playback."""
    _LOGGER.debug("Monitoring %d players for completion of playback", len(players))
    
    # Use the initially_playing_players override if provided, otherwise detect from current states
    # This ensures we handle the case where initially playing players are paused before this is called
    if initially_playing_players is None:
        initially_playing_players = set()
        for entity_id in players:
            state = hass.states.get(entity_id)
            if state and state.state == STATE_PLAYING:
                initially_playing_players.add(entity_id)
                _LOGGER.debug("Player %s was already in playing state before TTS - will use duration timer", entity_id)
    
    # Create a set of initially_idle_players that includes initially off players
    # These players will ONLY be restored based on state monitoring, not timers
    initially_idle_players = set([p for p in players if p not in initially_playing_players])
    
    # Initialize restored_players set
    restored_players = set()
        
    # Timer-based approach for initially playing players
    if initially_playing_players and media_duration_ms:
        _LOGGER.debug("%d initially playing players will use timer-based restoration", len(initially_playing_players))
        
        # Small buffer to ensure complete playback
        buffer_seconds = 0.5
        wait_time = (media_duration_ms / 1000) + buffer_seconds
        
        _LOGGER.debug("Using media_duration: %d ms for initially playing players only", media_duration_ms)
        _LOGGER.debug("Will wait exactly %.1f seconds for TTS on initially playing players (duration: %.1f sec + 0.5 sec buffer)", 
                    wait_time, media_duration_ms/1000)
        
        # Wait for the announcement to complete on initially playing players
        start_wait = time.monotonic()
        await asyncio.sleep(wait_time)
        actual_wait = time.monotonic() - start_wait
        
        # Now restore only the initially playing players
        for entity_id in initially_playing_players:
            if entity_id in players:  # Ensure player is still in the list
                _LOGGER.debug("Timer-based restoration for initially playing player %s after %.1f seconds", entity_id, actual_wait)
                
                # Restore volume if needed
                if restore_enabled:
                    if volume_restorer._needs_restore.get(entity_id, False):
                        original_volume = volume_restorer._initial_volumes.get(entity_id)
                        if original_volume is not None:
                            _LOGGER.debug("Restoring volume for initially playing player %s to %.2f", entity_id, original_volume)
                            await set_media_player_volume(hass, entity_id, original_volume)
                    else:
                        _LOGGER.debug("Initially playing player %s doesn't need volume restoration", entity_id)
                    
                    # Mark this player as restored
                    restored_players.add(entity_id)
        
        _LOGGER.debug("Timer-based restoration complete for initially playing players - restored %d players", 
                     len(restored_players & initially_playing_players))
    
    # State-based monitoring for initially off/idle players
    if initially_idle_players:    
        _LOGGER.debug("%d initially off/idle players will be monitored by state changes only", len(initially_idle_players))
    
    # State monitoring specific for initially idle/off players
    start_time = time.monotonic()
    playing_players = {}
    monitoring_players = set(initially_idle_players) - restored_players  # Only monitor initially idle/off players that aren't already restored
    
    # Wait for players to start playing the TTS message
    await asyncio.sleep(1.0)  # Give a moment for players to start
    
    # Check which initially idle/off players are now playing
    _LOGGER.debug("Monitoring %d initially idle/off players for state changes", len(monitoring_players))
    for entity_id in monitoring_players:
        state = hass.states.get(entity_id)
        if state and state.state == STATE_PLAYING:
            _LOGGER.debug("Initially idle/off player %s is now playing TTS", entity_id)
            playing_players[entity_id] = time.monotonic()
    
    # If no initially idle/off players started playing, check again after a delay
    if not playing_players and monitoring_players:
        _LOGGER.debug("No initially idle/off players detected as playing, waiting briefly")
        await asyncio.sleep(1.0)  # Brief wait for slow devices
        
        # Check one more time
        for entity_id in monitoring_players:
            state = hass.states.get(entity_id)
            if state and state.state == STATE_PLAYING:
                _LOGGER.debug("Initially idle/off player %s is now playing (delayed start)", entity_id)
                playing_players[entity_id] = time.monotonic()
    
    # Monitor remaining players until they finish or timeout
    while playing_players and time.monotonic() - start_time < timeout_seconds:
        # Wait a bit between checks
        await asyncio.sleep(0.5)
        
        # Check each player that is still playing
        for entity_id in list(playing_players.keys()):
            state = hass.states.get(entity_id)
            
            # Determine if this player has finished playing the TTS message
            is_finished = False
            
            # Check state changes for idle/off players
            if not state:
                is_finished = True
            elif hasattr(state, 'state') and get_speaker_status(state.state) == "inactive":
                is_finished = True
                _LOGGER.debug("Player %s changed state to %s - detected completion", entity_id, state.state)
            
            # If player has finished playing
            if is_finished:
                _LOGGER.debug("Player %s has finished playback", entity_id)
                
                # Restore this player's volume immediately if needed
                if restore_enabled and entity_id not in restored_players:
                    if volume_restorer._needs_restore.get(entity_id, False):
                        original_volume = volume_restorer._initial_volumes.get(entity_id)
                        if original_volume is not None:
                            _LOGGER.debug("Restoring volume for %s to %.2f", entity_id, original_volume)
                            await set_media_player_volume(hass, entity_id, original_volume)
                    else:
                        _LOGGER.debug("Player %s doesn't need volume restoration", entity_id)
                    
                    # Mark this player as restored
                    restored_players.add(entity_id)
                    _LOGGER.debug("Player %s has been restored", entity_id)
                
                # Remove from list of playing players
                del playing_players[entity_id]
    
    # Handle any players that timed out or are still playing (might be initially playing players)
    if playing_players:
        _LOGGER.warning("Timed out waiting for players to finish playback: %s", 
                       ", ".join(playing_players.keys()))
        
        # Try to restore these players anyway
        for entity_id in playing_players:
            if restore_enabled and entity_id not in restored_players:
                if volume_restorer._needs_restore.get(entity_id, False):
                    original_volume = volume_restorer._initial_volumes.get(entity_id)
                    if original_volume is not None:
                        _LOGGER.debug("Forcing volume restoration for timed-out player %s to %.2f", 
                                     entity_id, original_volume)
                        await set_media_player_volume(hass, entity_id, original_volume)
                
                restored_players.add(entity_id)
    
    # Log summary of what happened
    _LOGGER.debug("Monitoring complete - restored %d players", len(restored_players))
    return restored_players


async def prepare_players(
    hass: HomeAssistant,
    media_players: List[str],
    pause_enabled: bool = False,
    tts_volume: Optional[float] = None
) -> Tuple[List[str], VolumeRestorer, None]:
    """Prepare players for announcement."""
    # Filter for available players only
    available_players = []
    
    for entity_id in media_players:
        state = hass.states.get(entity_id)
        if state is not None and state.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
            available_players.append(entity_id)
            
            # Turn on the player immediately if it's off
            if state.state.lower() == "off":
                _LOGGER.debug("Player %s is initially off - turning on", entity_id)
                await call_media_player_service(hass, "turn_on", entity_id)
    
    if not available_players:
        _LOGGER.warning("No available media players")
        return [], None, None
    
    # Wait for players to wake up (brief wait for all players)
    await asyncio.sleep(1.0)  # Quick wait for players to wake up
    
    # Create volume and media state restorer
    volume_restorer = VolumeRestorer(hass, available_players)
    
    # Record media state if needed
    if pause_enabled:
        await volume_restorer.record_media_state()
        
        # Pause any playing media
        await volume_restorer.pause_playing_media()
    
    # Record initial volumes
    await volume_restorer.record_initial()
    
    # Handle volume setting for all players
    if tts_volume is not None:
        _LOGGER.debug("Setting volume to %.2f for %d players", float(tts_volume), len(available_players))
        
        # Set volume using standard method
        await volume_restorer.set_volume_if_needed(float(tts_volume))
        
        # Additional direct volume call for all players
        for entity_id in available_players:
            # Direct volume set call as backup method
            await hass.services.async_call(
                MP_DOMAIN,
                "volume_set",
                {
                    ATTR_ENTITY_ID: entity_id,
                    ATTR_MEDIA_VOLUME_LEVEL: float(tts_volume),
                },
                blocking=True,
            )
            
            # Mark all players for volume restoration
            volume_restorer._needs_restore[entity_id] = True
        
        # Short delay for volume changes
        _LOGGER.debug("Waiting 0.5 seconds for volume changes to take effect")
        await asyncio.sleep(0.5)
    
    return available_players, volume_restorer, None


async def cleanup_players(
    hass: HomeAssistant,
    volume_restorer: Optional[VolumeRestorer],
    pause_enabled: bool,
    players_already_restored: Optional[Set[str]] = None
) -> None:
    """Clean up after announcement."""
    # Only restore volumes for players that weren't already handled
    if volume_restorer and players_already_restored is not None:
        # Create a modified volume restorer that skips already restored players
        for entity_id in players_already_restored:
            # Mark as not needing restoration since it was already handled
            volume_restorer._needs_restore[entity_id] = False
            
        # Now restore only non-handled players
        await volume_restorer.restore()
    elif volume_restorer:
        # Original behavior if no tracking of restored players
        await volume_restorer.restore()
    
    # Resume media after cleanup is done
    if pause_enabled and volume_restorer:
        await asyncio.sleep(0.2)  # Minimal delay before resuming
        await volume_restorer.resume_media()


async def announce(
    hass: HomeAssistant,
    tts_entity: str,
    media_players: list[str],
    message: str,
    language: str = "en",
    options: dict[str, Any] | None = None,
    tts_volume: float | None = None,
    pause_playback: bool | None = None,
) -> None:
    """Play TTS announcement with volume control."""
    options = options or {}
    
    # Check config options
    restore_enabled = any(
        entry.options.get(CONF_VOLUME_RESTORE, False)
        for entry in hass.config_entries.async_entries(DOMAIN)
    ) or tts_volume is not None  # If tts_volume is specified, always enable restoration
    
    pause_enabled = pause_playback if pause_playback is not None else any(
        entry.options.get(CONF_PAUSE_PLAYBACK, False)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    
    if tts_volume is not None:
        _LOGGER.debug("Volume requested: %.2f - volume restoration will be enabled", float(tts_volume))
    
    try:
        # Track initial player states BEFORE any changes
        initially_off_players = set()
        initially_playing_players = set()
        
        for entity_id in media_players:
            state = hass.states.get(entity_id)
            if state:
                # Track player state (same handling for all player types)
                if state.state == "off":
                    _LOGGER.debug("Player %s is initially OFF", entity_id)
                    initially_off_players.add(entity_id)
                elif state.state == STATE_PLAYING:
                    _LOGGER.debug("Player %s is initially PLAYING", entity_id)
                    initially_playing_players.add(entity_id)
        
        # Prepare players
        available_players, volume_restorer, _ = await prepare_players(
            hass, 
            media_players, 
            pause_enabled=pause_enabled,
            tts_volume=tts_volume if restore_enabled else None
        )
        
        if not available_players:
            _LOGGER.warning("No available media players for announcement")
            return
            
        # Log info about the players we'll be using
        _LOGGER.debug("Playing TTS on %d speakers (%d initially off, %d initially playing)", 
                     len(available_players), len(initially_off_players), 
                     len(initially_playing_players & set(available_players)))
        
        # Get TTS entity state before TTS processing to check if media_duration is already available
        # This helps in the case where we're reusing a previously processed TTS message
        pre_tts_state = hass.states.get(tts_entity)
        pre_media_duration_ms = None
        if pre_tts_state and hasattr(pre_tts_state, 'attributes'):
            pre_media_duration_ms = pre_tts_state.attributes.get('media_duration')
            if pre_media_duration_ms is not None:
                _LOGGER.debug("Found existing media_duration: %d ms before TTS processing", pre_media_duration_ms)
        
        # Play TTS message
        _LOGGER.debug("Calling TTS service to speak message")         
        await hass.services.async_call(
            TTS_DOMAIN,
            "speak",
            {
                ATTR_ENTITY_ID: tts_entity,
                "message": message,
                "language": language,
                "options": options,
                "media_player_entity_id": available_players,
            },
            blocking=True,
        )
        
        # Wait for TTS processing to complete and get media duration
        # But only if we didn't already have a duration from before
        media_duration_ms = pre_media_duration_ms
        if media_duration_ms is None:
            _LOGGER.debug("Getting media duration from TTS entity after processing")
            media_duration_ms = await _wait_tts_process(hass, tts_entity)
        
        if media_duration_ms:
            _LOGGER.debug("Using media_duration: %d ms for playback timing", media_duration_ms)
        else:
            _LOGGER.warning("Could not retrieve media_duration, will use player state monitoring only")
        
        # Track which players will be restored in the monitor function
        # This prevents duplicate restoration in the cleanup function
        players_already_restored = await monitor_players(
            hass, 
            available_players, 
            volume_restorer,
            restore_enabled=restore_enabled,
            media_duration_ms=media_duration_ms,
            initially_playing_players=initially_playing_players  # Pass our saved list of initially playing players
        )
        
        # Pass the already restored players to cleanup to avoid duplicate restoration
        if players_already_restored:
            _LOGGER.debug("Players already restored: %s", players_already_restored)
            
        # Clean up after announcement (with awareness of already restored players)
        await cleanup_players(
            hass,
            volume_restorer,
            pause_enabled,
            players_already_restored
        )
            
        _LOGGER.debug("Announcement complete - restored %d players individually", 
                     len(players_already_restored) if players_already_restored else 0)
        
    except Exception as err:
        _LOGGER.error("Error during TTS announcement: %s", err)
        
        # Try to clean up on error
        try:
            # Handle cleanup in case variables exist
            if 'volume_restorer' in locals() and restore_enabled:
                _LOGGER.debug("Emergency volume restoration after error")
                await volume_restorer.restore()
            
            if 'volume_restorer' in locals() and pause_enabled:
                _LOGGER.debug("Emergency media resumption after error")
                await volume_restorer.resume_media()
                
        except Exception as restore_err:
            _LOGGER.error("Failed to restore state after error: %s", restore_err)
        
        raise