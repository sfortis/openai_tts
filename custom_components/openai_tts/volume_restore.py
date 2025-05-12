# volume_restore.py
"""Helper to snapshot & restore media_player volumes and media state around a TTS announcement."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any

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
    SERVICE_MEDIA_SEEK,
    STATE_IDLE,
    STATE_PLAYING,
    MediaPlayerEntityFeature,
)
from homeassistant.components.tts import DOMAIN as TTS_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, STATE_UNAVAILABLE, STATE_PAUSED

from .const import DOMAIN, CONF_VOLUME_RESTORE, CONF_PAUSE_PLAYBACK

_LOGGER = logging.getLogger(__name__)


def get_media_duration_from_file(file_path: str) -> float:
    """Get the duration of a media file in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        duration_str = result.stdout.strip()
        return float(duration_str) if duration_str else 0.0
    except Exception as e:
        _LOGGER.error("Error getting media duration from file: %s", e)
        return 0.0


async def get_tts_audio_duration_from_url(hass: HomeAssistant, media_url: str) -> int | None:
    """Get the duration of TTS audio by downloading and parsing it with ffprobe."""
    try:
        # Extract the actual file path from the media URL if it's a local file
        if media_url.startswith("/api/tts_proxy/"):
            # This is a TTS proxy URL, we need to download it
            import aiohttp
            import tempfile
            
            full_url = f"{hass.config.internal_url}{media_url}"
            _LOGGER.debug("Downloading TTS from URL: %s", full_url)
            
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url) as response:
                    if response.status == 200:
                        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
                            content = await response.read()
                            tmp_file.write(content)
                            tmp_path = tmp_file.name
                        
                        # Get duration using ffprobe
                        duration = await hass.async_add_executor_job(get_media_duration_from_file, tmp_path)
                        
                        # Clean up temp file
                        try:
                            os.remove(tmp_path)
                        except:
                            pass
                        
                        return int(duration * 1000)  # Convert to milliseconds
        
        elif media_url.startswith("http"):
            # External URL, download and parse
            import aiohttp
            import tempfile
            
            async with aiohttp.ClientSession() as session:
                async with session.get(media_url) as response:
                    if response.status == 200:
                        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
                            content = await response.read()
                            tmp_file.write(content)
                            tmp_path = tmp_file.name
                        
                        duration = await hass.async_add_executor_job(get_media_duration_from_file, tmp_path)
                        
                        try:
                            os.remove(tmp_path)
                        except:
                            pass
                        
                        return int(duration * 1000)
        
        elif os.path.exists(media_url):
            # Local file path
            duration = await hass.async_add_executor_job(get_media_duration_from_file, media_url)
            return int(duration * 1000)
        
        return None
    except Exception as e:
        _LOGGER.error("Error getting TTS audio duration from URL: %s", e)
        return None


class MediaState:
    """Store media state for pause/resume functionality."""
    
    def __init__(self, entity_id: str, state: str, attributes: dict):
        """Initialize media state."""
        self.entity_id = entity_id
        self.state = state
        self.media_content_id = attributes.get(ATTR_MEDIA_CONTENT_ID)
        self.media_content_type = attributes.get(ATTR_MEDIA_CONTENT_TYPE)
        self.media_position = attributes.get(ATTR_MEDIA_POSITION)
        self.app_name = attributes.get(ATTR_APP_NAME)
        self.was_playing = state == STATE_PLAYING
        
        # Additional attributes for better playlist/queue support
        self.media_title = attributes.get("media_title")
        self.media_artist = attributes.get("media_artist")
        self.media_album = attributes.get("media_album_name")
        self.media_playlist = attributes.get("media_playlist")
        self.shuffle = attributes.get("shuffle", False)
        self.repeat = attributes.get("repeat", "off")
        
        # Try to extract Spotify context (playlist, album, etc)
        self.spotify_context = None
        if self.media_content_id and self.media_content_id.startswith("spotify:"):
            # Some integrations provide the context in attributes
            self.spotify_context = attributes.get("media_context_uri") or attributes.get("spotify_context")
        
    def should_resume(self) -> bool:
        """Check if media should be resumed."""
        return self.was_playing and self.media_content_id is not None


class VolumeRestorer:
    """Handle volume restoration for media players."""
    
    def __init__(self, hass: HomeAssistant, entity_ids: list[str]):
        """Initialize the volume restorer."""
        self.hass = hass
        self.entity_ids = entity_ids
        self._initial: dict[str, float] = {}
        self._needs_restore: dict[str, bool] = {}  # Track which players need restoration
        self._media_states: dict[str, MediaState] = {}  # Track media states for pause/resume

    async def record_initial(self) -> None:
        """Record the initial volume for each media player."""
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                _LOGGER.warning("Media player %s not found", entity_id)
                continue
            
            volume = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if volume is not None:
                self._initial[entity_id] = float(volume)
                self._needs_restore[entity_id] = False  # Initially assume no restore needed
                _LOGGER.debug("Recorded volume %.2f for %s", volume, entity_id)

    async def record_media_state(self) -> None:
        """Record the current media state for each player (only Sonos)."""
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            
            # Only record state for Sonos devices
            is_sonos = ("sonos" in entity_id.lower() or 
                       state.attributes.get("platform") == "sonos")
            
            if not is_sonos:
                continue
                
            self._media_states[entity_id] = MediaState(
                entity_id, 
                state.state, 
                state.attributes
            )
            
            media_state = self._media_states[entity_id]
            
            if state.state == STATE_PLAYING:
                _LOGGER.debug(
                    "Recorded playing media for Sonos %s: %s (position: %s, context: %s, playlist: %s)",
                    entity_id,
                    media_state.media_content_id,
                    media_state.media_position,
                    media_state.spotify_context,
                    media_state.media_playlist
                )

    async def pause_playing_media(self) -> None:
        """Pause any currently playing media (only Sonos)."""
        for entity_id, media_state in self._media_states.items():
            if media_state.was_playing:
                _LOGGER.debug("Pausing media on Sonos %s", entity_id)
                await self.hass.services.async_call(
                    MP_DOMAIN,
                    SERVICE_MEDIA_PAUSE,
                    {ATTR_ENTITY_ID: entity_id},
                    blocking=True,
                )
                # Give the player time to pause
                await asyncio.sleep(0.5)

    async def resume_media(self) -> None:
        """Resume previously playing media (Sonos devices only)."""
        for entity_id, media_state in self._media_states.items():
            if not media_state.should_resume():
                continue
                
            _LOGGER.debug(
                "Resuming media on Sonos %s: %s (app: %s)", 
                entity_id, 
                media_state.media_content_id,
                media_state.app_name
            )
            
            try:
                # For Sonos, simply use media_play to resume
                await self.hass.services.async_call(
                    MP_DOMAIN,
                    SERVICE_MEDIA_PLAY,
                    {ATTR_ENTITY_ID: entity_id},
                    blocking=True,
                )
                
                _LOGGER.debug("Successfully resumed media on %s", entity_id)
                
            except Exception as err:
                _LOGGER.warning("Failed to resume media on %s: %s", entity_id, err)

    async def set_volume_if_needed(self, level: float) -> None:
        """Set media players to specified volume level only if they're not already at that level."""
        for entity_id in self.entity_ids:
            if entity_id not in self._initial:
                continue
            
            # Get current volume
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
                
            current_volume = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            if current_volume is None:
                continue
            
            # Check if volume adjustment is needed (with small tolerance for float comparison)
            if abs(float(current_volume) - level) > 0.01:
                _LOGGER.debug(
                    "Changing volume for %s from %.2f to %.2f",
                    entity_id, current_volume, level
                )
                
                await self.hass.services.async_call(
                    MP_DOMAIN,
                    SERVICE_VOLUME_SET,
                    {
                        ATTR_ENTITY_ID: entity_id,
                        ATTR_MEDIA_VOLUME_LEVEL: level,
                    },
                    blocking=True,
                )
                self._needs_restore[entity_id] = True
            else:
                _LOGGER.debug(
                    "Volume for %s already at desired level %.2f, skipping adjustment",
                    entity_id, level
                )
                self._needs_restore[entity_id] = False

    async def restore(self) -> None:
        """Restore each media player to its original volume, but only if we changed it."""
        for entity_id, original_volume in self._initial.items():
            # Only restore if we actually changed the volume
            if self._needs_restore.get(entity_id, False):
                await self.hass.services.async_call(
                    MP_DOMAIN,
                    SERVICE_VOLUME_SET,
                    {
                        ATTR_ENTITY_ID: entity_id,
                        ATTR_MEDIA_VOLUME_LEVEL: original_volume,
                    },
                    blocking=True,
                )
                _LOGGER.debug("Restored volume to %.2f for %s", original_volume, entity_id)
            else:
                _LOGGER.debug(
                    "Skipping volume restore for %s (was not changed)",
                    entity_id
                )


async def wait_for_media_duration(
    hass: HomeAssistant,
    tts_entity: str,
    timeout_ms: int = 30000  # 30 seconds for duration check
) -> int | None:
    """Wait for the TTS entity to have a media_duration attribute.
    
    Returns the media duration in milliseconds.
    """
    start_time_ms = int(asyncio.get_event_loop().time() * 1000)
    
    while (int(asyncio.get_event_loop().time() * 1000) - start_time_ms) < timeout_ms:
        tts_state = hass.states.get(tts_entity)
        
        if tts_state and hasattr(tts_state, 'attributes'):
            # Check if TTS is still processing (engine_active)
            engine_active = tts_state.attributes.get('engine_active', False)
            media_duration_ms = tts_state.attributes.get('media_duration')  # Already in milliseconds
            
            _LOGGER.debug(
                "Checking TTS entity: engine_active=%s, media_duration=%s ms",
                engine_active, media_duration_ms
            )
            
            # If we have a duration, return it immediately (don't wait for engine)
            if media_duration_ms is not None:
                _LOGGER.debug("TTS media duration: %d ms", media_duration_ms)
                return media_duration_ms
        
        # Wait 500ms before checking again
        await asyncio.sleep(0.5)
    
    _LOGGER.debug("Timeout waiting for media_duration from TTS entity")
    return None


async def get_media_duration_from_players(
    hass: HomeAssistant,
    media_players: list[str],
    timeout_ms: int = 10000
) -> tuple[int | None, str | None]:
    """Get media duration and URL from media players after TTS starts playing."""
    start_time_ms = int(asyncio.get_event_loop().time() * 1000)
    
    # Filter out unavailable players before checking
    available_players = []
    for entity_id in media_players:
        state = hass.states.get(entity_id)
        if state and state.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
            available_players.append(entity_id)
    
    if not available_players:
        _LOGGER.warning("No available media players to get duration from")
        return None, None
    
    while (int(asyncio.get_event_loop().time() * 1000) - start_time_ms) < timeout_ms:
        for entity_id in available_players:
            state = hass.states.get(entity_id)
            if state and state.state == STATE_PLAYING:
                # Get the media content ID (URL)
                media_content_id = state.attributes.get("media_content_id")
                if media_content_id:
                    _LOGGER.debug("Found media URL from player %s: %s", entity_id, media_content_id)
                    
                    # Parse the audio file to get exact duration
                    duration_ms = await get_tts_audio_duration_from_url(hass, media_content_id)
                    if duration_ms:
                        return duration_ms, media_content_id
        
        # Wait before checking again
        await asyncio.sleep(0.5)
    
    _LOGGER.debug("Timeout waiting for media URL from players")
    return None, None


async def wait_for_media_players_complete(
    hass: HomeAssistant,
    media_players: list[str],
    timeout_ms: int = 30000,
    extra_wait_ms: int = 1000,
) -> None:
    """Wait for media players to complete playback by checking their state."""
    start_time_ms = int(asyncio.get_event_loop().time() * 1000)
    players_were_playing = set()
    
    _LOGGER.debug("Starting to monitor media players for completion: %s", media_players)
    
    while (int(asyncio.get_event_loop().time() * 1000) - start_time_ms) < timeout_ms:
        all_finished = True
        
        for entity_id in media_players:
            state = hass.states.get(entity_id)
            
            if state is None:
                _LOGGER.warning("Media player %s not found", entity_id)
                continue
            
            # Check current state
            current_state = state.state
            _LOGGER.debug("Media player %s state: %s", entity_id, current_state)
            
            # Track if the player is/was playing
            if current_state == STATE_PLAYING:
                players_were_playing.add(entity_id)
                all_finished = False
            
            # Check if a player that was playing is now idle or paused
            elif entity_id in players_were_playing and current_state in (STATE_IDLE, STATE_PAUSED, STATE_UNKNOWN, STATE_UNAVAILABLE):
                _LOGGER.debug("Media player %s finished playing (now %s)", entity_id, current_state)
                # Player finished, but keep checking others
            
            # If player is still playing, we're not done
            elif current_state == STATE_PLAYING:
                all_finished = False
        
        # If all players that were playing are now idle/paused, we're done
        if all_finished and players_were_playing:
            _LOGGER.debug("All media players have finished playback")
            # Add a small extra wait for audio processing
            await asyncio.sleep(extra_wait_ms / 1000.0)
            return
        
        # If no players ever started playing but we've waited a reasonable time
        if not players_were_playing and (int(asyncio.get_event_loop().time() * 1000) - start_time_ms) > 5000:
            _LOGGER.warning("No media players started playing after 5s - TTS may have failed")
            # Use a default duration for failed TTS
            await asyncio.sleep(5.0)
            return
        
        # Wait before checking again
        await asyncio.sleep(0.5)
    
    _LOGGER.warning("Timeout waiting for media players to complete playback")


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
    """Ungroup Sonos speakers."""
    if not hass.services.has_service("sonos", "unjoin"):
        return
    
    try:
        await hass.services.async_call(
            "sonos",
            "unjoin",
            {ATTR_ENTITY_ID: sonos_players},
            blocking=True,
        )
    except Exception as e:
        _LOGGER.warning("Failed to ungroup Sonos speakers: %s", e)


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
    """Play a TTS announcement with volume management and optional media pause/resume (Sonos only)."""
    
    if options is None:
        options = {}
    
    # Check if volume restore is enabled
    restore_enabled = any(
        entry.options.get(CONF_VOLUME_RESTORE, False)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    
    # Check if pause playback is enabled
    # Service call parameter overrides global setting
    if pause_playback is not None:
        pause_enabled = pause_playback
    else:
        pause_enabled = any(
            entry.options.get(CONF_PAUSE_PLAYBACK, False)
            for entry in hass.config_entries.async_entries(DOMAIN)
        )
    
    # Filter out unavailable media players
    available_media_players = []
    sonos_players = []
    
    for entity_id in media_players:
        state = hass.states.get(entity_id)
        if state is not None and state.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
            available_media_players.append(entity_id)
            # Check if it's a Sonos device
            if "sonos" in entity_id.lower() or state.attributes.get("platform") == "sonos":
                sonos_players.append(entity_id)
        else:
            _LOGGER.warning("Media player %s is not available, skipping", entity_id)
    
    if not available_media_players:
        _LOGGER.warning("No media players are available")
        return
    
    _LOGGER.debug("Available players: %s, Sonos players: %s", available_media_players, sonos_players)
    
    # Create restorer instance
    restorer = VolumeRestorer(hass, available_media_players)
    
    try:
        # Record current volumes
        await restorer.record_initial()
        
        # Only pause/resume for Sonos devices
        if pause_enabled and sonos_players:
            await restorer.record_media_state()
            await restorer.pause_playing_media()
        
        # Set announcement volume only if needed and restore is enabled
        if restore_enabled and tts_volume is not None:
            await restorer.set_volume_if_needed(tts_volume)
            
            # Give media players time to process volume change
            if any(restorer._needs_restore.values()):
                await asyncio.sleep(0.5)
        
        # Group Sonos speakers if multiple are present for better sync
        grouped_coordinator = None
        sonos_to_use = sonos_players
        if len(sonos_players) > 1:
            grouped_coordinator = await group_sonos_speakers(hass, sonos_players)
            if grouped_coordinator:
                sonos_to_use = [grouped_coordinator]
                _LOGGER.debug("Using Sonos group coordinator: %s", grouped_coordinator)
        
        # Regular TTS call
        tts_data = {
            ATTR_ENTITY_ID: tts_entity,
            "message": message,
            "language": language,
            "options": options,
            "media_player_entity_id": available_media_players,
        }
        
        _LOGGER.debug("Playing TTS on all speakers: %s", available_media_players)
        
        await hass.services.async_call(
            TTS_DOMAIN,
            "speak",
            tts_data,
            blocking=True,
        )
        
        # Get media duration - try multiple methods
        media_duration_ms = None
        
        # Method 1: Wait for media players to start and get duration from audio file
        _LOGGER.debug("Waiting for TTS to start playing to get media URL")
        await asyncio.sleep(1.5)  # Give TTS time to start
        
        media_duration_ms, media_url = await get_media_duration_from_players(hass, available_media_players)
        
        if media_duration_ms:
            _LOGGER.debug("Got exact duration by parsing audio file: %s ms", media_duration_ms)
        else:
            # Method 2: Try to get from TTS entity (shorter timeout for cached files)
            _LOGGER.debug("Checking TTS entity for duration")
            media_duration_ms = await wait_for_media_duration(hass, tts_entity, timeout_ms=3000)
            
            if media_duration_ms:
                _LOGGER.debug("Got duration from TTS entity: %s ms", media_duration_ms)
            else:
                # Method 3: Monitor player states as fallback
                _LOGGER.debug("No duration available, will monitor player states")
                await wait_for_media_players_complete(
                    hass, 
                    available_media_players,
                    timeout_ms=30000,
                    extra_wait_ms=1000
                )
                return  # Return early since we already waited
        
        # Wait for playback to complete based on exact duration
        if media_duration_ms:
            wait_time_ms = media_duration_ms + 1500  # Add buffer
            _LOGGER.debug("Waiting %d ms for TTS playback to complete (exact duration)", wait_time_ms)
            await asyncio.sleep(wait_time_ms / 1000.0)
        
        # Restore original volumes
        if restore_enabled:
            await restorer.restore()
        
        # Ungroup Sonos speakers if we grouped them
        if grouped_coordinator:
            await ungroup_sonos_speakers(hass, sonos_players)
        
        # Resume media only for Sonos devices
        if pause_enabled and sonos_players:
            # Give a short delay before resuming
            await asyncio.sleep(0.5)
            await restorer.resume_media()
        
    except Exception as err:
        _LOGGER.error("Error during TTS announcement: %s", err)
        # Try to restore volumes and resume media even if TTS failed
        try:
            if restore_enabled:
                await restorer.restore()
            if grouped_coordinator:
                await ungroup_sonos_speakers(hass, sonos_players)
            if pause_enabled and sonos_players:
                await restorer.resume_media()
        except Exception as restore_err:
            _LOGGER.error("Failed to restore state: %s", restore_err)
        raise