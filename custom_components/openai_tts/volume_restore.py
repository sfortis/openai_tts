# volume_restore.py
"""Helper to snapshot & restore media_player volumes and media state around a TTS announcement."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
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
    categorize_media_players as utils_categorize_media_players,
)

_LOGGER = logging.getLogger(__name__)


async def get_tts_audio_duration_from_url(hass: HomeAssistant, media_url: str) -> int | None:
    """Get the duration of TTS audio by downloading and parsing it with ffprobe."""
    try:
        # Handle TTS proxy URL
        if media_url.startswith("/api/tts_proxy/"):
            full_url = f"{hass.config.internal_url}{media_url}"
            return await _download_and_get_duration(hass, full_url)
        
        # Handle direct HTTP URLs
        elif media_url.startswith("http"):
            return await _download_and_get_duration(hass, media_url)
        
        # Handle local file paths
        elif os.path.exists(media_url):
            # This is running in an async context, so use executor
            duration = await hass.async_add_executor_job(get_media_duration, media_url)
            return int(duration * 1000)
        
        return None
    except Exception as e:
        _LOGGER.error("Error getting TTS audio duration from URL: %s", e)
        return None


async def _download_and_get_duration(hass: HomeAssistant, url: str) -> int | None:
    """Download audio from URL and get its duration."""
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
        self._was_off: Dict[str, bool] = {}
    
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
            
            # Record if device is off
            self._was_off[entity_id] = state.lower() == "off"

    async def record_media_state(self, media_player_type: Optional[str] = None) -> None:
        """Record the current media state for each player."""
        _LOGGER.debug("Recording media state for players")
        
        for entity_id in self.entity_ids:
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                continue
            
            # Filter by player type if specified
            if media_player_type and not self._is_player_type(entity_id, media_player_type):
                continue
                    
            # Store the media state
            self._media_states[entity_id] = MediaState(entity_id, state, attributes)
            
            media_state = self._media_states[entity_id]
            if state == STATE_PLAYING or state == STATE_PAUSED:
                _LOGGER.debug(
                    "Recorded %s media for %s: %s (position: %s)",
                    state, entity_id, media_state.media_content_id, media_state.media_position
                )

    async def pause_playing_media(self, media_player_type: Optional[str] = None) -> None:
        """Pause any currently playing media."""
        pause_tasks = []
        
        for entity_id, media_state in self._media_states.items():
            if (not media_player_type or self._is_player_type(entity_id, media_player_type)) and media_state.was_playing:
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

    async def resume_media(self, media_player_type: Optional[str] = None) -> None:
        """Resume previously playing media."""
        resume_tasks = []
        
        for entity_id, media_state in self._media_states.items():
            if (not media_player_type or self._is_player_type(entity_id, media_player_type)) and media_state.should_resume():
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
                
    def _is_player_type(self, entity_id: str, player_type: str) -> bool:
        """Check if a player matches a specified type."""
        if player_type.lower() == "sonos":
            return "sonos" in entity_id.lower()
        elif player_type.lower() == "cast":
            return any(keyword in entity_id.lower() for keyword in ["cast", "speaker", "display"])
        return False

    async def set_volume_if_needed(self, level: float) -> None:
        """Set media players to specified volume level."""
        _LOGGER.debug("Setting volume to %.2f for %d players", level, len(self.entity_ids))
        volume_tasks = []
        
        # Convert to float to ensure proper comparison
        level = float(level)
        
        for entity_id in self.entity_ids:
            is_cast = any(keyword in entity_id.lower() for keyword in ["cast", "speaker"])
            
            # Check current state and volume
            state, attributes = await get_media_player_state(self.hass, entity_id)
            if state is None or attributes is None:
                continue
                
            current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if current_volume is None:
                continue
                
            # Record initial volume if not already done
            if entity_id not in self._initial_volumes:
                self._initial_volumes[entity_id] = float(current_volume)
                self._was_off[entity_id] = state.lower() == "off"
            
            # Skip if already at target volume (with small tolerance)
            if abs(float(current_volume) - level) < 0.01:
                _LOGGER.debug("Volume already at desired level %.2f for %s", level, entity_id)
                self._needs_restore[entity_id] = False
                continue
            
            # Set volume
            volume_tasks.append(
                self._set_volume_with_result(entity_id, level, is_cast)
            )
        
        if volume_tasks:
            # Run volume tasks concurrently
            results = await asyncio.gather(*volume_tasks)
            
            # Mark which ones need restoration based on results
            for entity_id, success in results:
                if success:
                    self._needs_restore[entity_id] = True
    
    async def _set_volume_with_result(self, entity_id: str, level: float, is_cast: bool) -> Tuple[str, bool]:
        """Set volume and return result tuple with entity_id and success status."""
        success = await set_media_player_volume(
            self.hass, 
            entity_id, 
            level, 
            is_cast=is_cast
        )
        return (entity_id, success)

    async def restore(self) -> None:
        """Restore each media player to its original volume and state."""
        restore_tasks = []
        turn_off_tasks = []
        
        # Restore volumes for players that were changed
        for entity_id, original_volume in self._initial_volumes.items():
            # Only restore if we actually changed the volume
            if not self._needs_restore.get(entity_id, False):
                continue
                
            # Check if this is a Cast device
            is_cast = any(keyword in entity_id.lower() for keyword in ["cast", "speaker"])
            
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
                        original_volume,
                        is_cast=is_cast
                    )
                )
        
        # Turn off devices that were initially off
        for entity_id, was_off in self._was_off.items():
            if was_off:
                _LOGGER.debug("Turning off %s (was initially off)", entity_id)
                turn_off_tasks.append(
                    call_media_player_service(
                        self.hass,
                        "turn_off",
                        entity_id
                    )
                )
        
        # Run all restoration tasks concurrently
        if restore_tasks:
            await asyncio.gather(*restore_tasks)
            
        # Turn off devices after volume restoration
        if turn_off_tasks:
            await asyncio.gather(*turn_off_tasks)


async def wait_for_media_duration(
    hass: HomeAssistant,
    tts_entity: str,
    timeout_ms: int = 30000
) -> int | None:
    """Wait for the TTS entity to have a media_duration attribute."""
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


async def wait_for_media_players_complete(
    hass: HomeAssistant,
    media_players: list[str],
    timeout_ms: int = 30000,
    extra_wait_ms: int = 1000
) -> None:
    """Wait for media players to complete playback."""
    start_time_ms = int(asyncio.get_event_loop().time() * 1000)
    players_were_playing = set()
    
    _LOGGER.debug("Monitoring %d media players for playback completion", len(media_players))
    
    while (int(asyncio.get_event_loop().time() * 1000) - start_time_ms) < timeout_ms:
        all_finished = True
        
        for entity_id in media_players:
            state = hass.states.get(entity_id)
            if state is None:
                continue
            
            current_state = state.state
            
            # Track if player is/was playing
            if current_state == STATE_PLAYING:
                players_were_playing.add(entity_id)
                all_finished = False
            elif entity_id in players_were_playing and current_state in (STATE_IDLE, STATE_PAUSED, STATE_UNKNOWN, STATE_UNAVAILABLE):
                # Player that was playing has finished
                pass
            elif current_state == STATE_PLAYING:
                all_finished = False
        
        # If all players that were playing are now finished
        if all_finished and players_were_playing:
            _LOGGER.debug("All media players have finished playback")
            await asyncio.sleep(extra_wait_ms / 1000.0)
            return
        
        # If no players ever started playing but we've waited long enough
        if not players_were_playing and (int(asyncio.get_event_loop().time() * 1000) - start_time_ms) > 5000:
            _LOGGER.warning("No media players started playing after 5s")
            await asyncio.sleep(5.0)
            return
        
        # Check again in a moment
        await asyncio.sleep(0.5)
    
    _LOGGER.warning("Timeout waiting for playback completion")


async def group_sonos_speakers(hass: HomeAssistant, sonos_players: list[str]) -> str | None:
    """Group Sonos speakers for synchronized playback."""
    if len(sonos_players) <= 1:
        return None
    
    # Check if Sonos join service is available
    if not hass.services.has_service("sonos", "join"):
        _LOGGER.debug("Sonos join service not available")
        return None
    
    try:
        # Use the first speaker as the coordinator
        coordinator = sonos_players[0]
        others = sonos_players[1:]
        
        _LOGGER.debug("Grouping Sonos speakers - coordinator: %s, others: %s", coordinator, others)
        
        # Join all other speakers to the coordinator
        await hass.services.async_call(
            "sonos",
            "join",
            {
                ATTR_ENTITY_ID: others,
                "master": coordinator,
            },
            blocking=True,
        )
        
        # Give time for the group to form
        await asyncio.sleep(0.5)
        
        return coordinator
    except Exception as e:
        _LOGGER.warning("Failed to group Sonos speakers: %s", e)
        return None


async def ungroup_sonos_speakers(hass: HomeAssistant, sonos_players: list[str]) -> None:
    """Ungroup Sonos speakers after playback."""
    if len(sonos_players) <= 1:
        return
    
    # Check if Sonos unjoin service is available
    if not hass.services.has_service("sonos", "unjoin"):
        _LOGGER.debug("Sonos unjoin service not available")
        return
    
    try:
        _LOGGER.debug("Ungrouping %d Sonos speakers", len(sonos_players))
        
        # Separate each speaker
        for entity_id in sonos_players:
            await hass.services.async_call(
                "sonos",
                "unjoin",
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        
        # Give time for ungrouping to complete
        await asyncio.sleep(0.5)
    except Exception as e:
        _LOGGER.warning("Failed to ungroup Sonos speakers: %s", e)


async def categorize_media_players(
    hass: HomeAssistant,
    media_players: List[str]
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Categorize media players by type and availability."""
    categories = utils_categorize_media_players(hass, media_players)
    return (
        categories['available'],
        categories['sonos'],
        categories['cast'],
        categories['other']
    )


async def prepare_players_for_announcement(
    hass: HomeAssistant,
    media_players: List[str],
    pause_enabled: bool = False,
    tts_volume: Optional[float] = None
) -> Tuple[List[str], VolumeRestorer, Optional[str]]:
    """Prepare media players for an announcement."""
    # Categorize players
    available_players, sonos_players, cast_players, other_players = await categorize_media_players(
        hass, media_players
    )
    
    if not available_players:
        _LOGGER.warning("No available media players")
        return [], None, None
    
    # Create volume and media state restorer
    volume_restorer = VolumeRestorer(hass, available_players)
    
    # Process in parallel where possible
    setup_tasks = []
    
    # Record media state and pause if needed
    if pause_enabled and sonos_players:
        setup_tasks.append(volume_restorer.record_media_state(media_player_type="sonos"))
    
    # Turn on any Cast devices that are off
    turn_on_tasks = []
    for entity_id in cast_players:
        state = hass.states.get(entity_id)
        if state and state.state.lower() == "off":
            _LOGGER.debug("Turning on Cast device %s", entity_id)
            turn_on_tasks.append(
                call_media_player_service(
                    hass,
                    "turn_on",
                    entity_id
                )
            )
    
    if turn_on_tasks:
        setup_tasks.append(asyncio.gather(*turn_on_tasks))
    
    # Run initial setup tasks concurrently
    if setup_tasks:
        await asyncio.gather(*setup_tasks)
        
        # Wait a bit for Cast devices to initialize
        if turn_on_tasks:
            await asyncio.sleep(2.0)
    
    # Pause media if needed (after setup)
    if pause_enabled and sonos_players:
        await volume_restorer.pause_playing_media(media_player_type="sonos")
    
    # Group Sonos speakers if multiple present
    grouped_sonos = None
    if len(sonos_players) > 1:
        grouped_sonos = await group_sonos_speakers(hass, sonos_players)
    
    # Record initial volumes
    await volume_restorer.record_initial()
    
    # Set announcement volume if needed
    if tts_volume is not None:
        await volume_restorer.set_volume_if_needed(float(tts_volume))
    
    return available_players, volume_restorer, grouped_sonos


async def cleanup_after_announcement(
    hass: HomeAssistant,
    volume_restorer: Optional[VolumeRestorer],
    sonos_players: Optional[List[str]],
    grouped_sonos: Optional[str],
    pause_enabled: bool
) -> None:
    """Clean up after an announcement."""
    cleanup_tasks = []
    
    # Restore original volumes
    if volume_restorer:
        cleanup_tasks.append(volume_restorer.restore())
    
    # Ungroup Sonos speakers if needed
    if sonos_players and grouped_sonos:
        cleanup_tasks.append(ungroup_sonos_speakers(hass, sonos_players))
    
    # Run cleanup tasks concurrently
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks)
    
    # Resume media after cleanup is done
    if pause_enabled and sonos_players and volume_restorer:
        await asyncio.sleep(0.5)  # Small delay before resuming
        await volume_restorer.resume_media(media_player_type="sonos")


async def announce_with_volume_restore(
    hass: HomeAssistant,
    tts_entity: str,
    media_players: list[str],
    message: str,
    language: str = "en",
    options: dict[str, Any] | None = None,
    tts_volume: float | None = None,
    pause_playback: bool | None = None,
) -> None:
    """Play a TTS announcement with reliable volume restoration."""
    options = options or {}
    
    # Check config options
    restore_enabled = any(
        entry.options.get(CONF_VOLUME_RESTORE, False)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    
    pause_enabled = pause_playback if pause_playback is not None else any(
        entry.options.get(CONF_PAUSE_PLAYBACK, False)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    
    try:
        # Prepare players
        available_players, volume_restorer, grouped_sonos = await prepare_players_for_announcement(
            hass, 
            media_players, 
            pause_enabled=pause_enabled,
            tts_volume=tts_volume if restore_enabled else None
        )
        
        if not available_players:
            return
        
        # Play TTS message
        _LOGGER.debug("Playing TTS on %d speakers", len(available_players))
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
        
        # Wait for playback to complete
        media_duration_ms = await wait_for_media_duration(hass, tts_entity)
        
        if media_duration_ms:
            # Calculate wait time with buffer for multiple players
            buffer_ms = 2000 + (len(available_players) * 500)
            wait_time_ms = media_duration_ms + buffer_ms
            _LOGGER.debug("Waiting %.1f seconds for TTS playback", wait_time_ms / 1000.0)
            await asyncio.sleep(wait_time_ms / 1000.0)
        else:
            # If we couldn't get duration, wait for players to complete
            await wait_for_media_players_complete(hass, available_players)
        
        # Clean up
        await cleanup_after_announcement(
            hass,
            volume_restorer if restore_enabled else None,
            sonos_players=[p for p in available_players if "sonos" in p.lower()],
            grouped_sonos=grouped_sonos,
            pause_enabled=pause_enabled
        )
        
    except Exception as err:
        _LOGGER.error("Error during TTS announcement: %s", err)
        
        # Try to clean up on error
        try:
            # Handle cleanup in case variables exist
            if 'volume_restorer' in locals() and restore_enabled:
                await volume_restorer.restore()
            
            if 'grouped_sonos' in locals() and grouped_sonos:
                sonos_players = [p for p in media_players if "sonos" in p.lower()]
                await ungroup_sonos_speakers(hass, sonos_players)
            
            if 'volume_restorer' in locals() and pause_enabled:
                sonos_players = [p for p in media_players if "sonos" in p.lower()]
                if sonos_players:
                    await volume_restorer.resume_media(media_player_type="sonos")
                
        except Exception as restore_err:
            _LOGGER.error("Failed to restore state after error: %s", restore_err)
        
        raise