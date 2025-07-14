"""
Utility functions for OpenAI TTS integration.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

import asyncio
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import StateType
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    DOMAIN as MP_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

def get_media_duration(file_path: str) -> float:
    """
    Get the duration of a media file in seconds.
    First tries to read from metadata, then falls back to ffprobe.
    
    Args:
        file_path: Path to the media file
        
    Returns:
        Duration in seconds as float
    """
    try:
        # First try to get duration from metadata
        cmd_metadata = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            file_path
        ]
        result = subprocess.run(cmd_metadata, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        
        if result.stdout:
            import json
            data = json.loads(result.stdout)
            # Check for our custom metadata
            if "format" in data and "tags" in data["format"]:
                tags = data["format"]["tags"]
                # Look for our duration metadata
                for key, value in tags.items():
                    if "tts_duration_ms" in key:
                        _LOGGER.debug("Found duration in metadata: %s ms", value)
                        return float(value) / 1000.0
        
        # Fallback to standard duration detection
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
        _LOGGER.error("Error getting media duration: %s", e)
        return 0.0

async def safe_execute(func: Callable, *args, log_prefix: str = "", **kwargs) -> Any:
    """
    Execute a function safely with standardized error handling.
    
    Args:
        func: Function to execute
        log_prefix: Prefix for error log messages
        *args: Arguments to pass to function
        **kwargs: Keyword arguments to pass to function
        
    Returns:
        Result of the function
        
    Raises:
        HomeAssistantError: On any error to standardize exception handling
    """
    try:
        return await func(*args, **kwargs) if asyncio_function(func) else func(*args, **kwargs)
    except Exception as err:
        error_msg = f"{log_prefix} error: {err}"
        _LOGGER.error(error_msg)
        raise HomeAssistantError(error_msg) from err

def asyncio_function(func: Callable) -> bool:
    """
    Check if a function is a coroutine function.
    
    Args:
        func: Function to check
        
    Returns:
        True if coroutine function, False otherwise
    """
    return hasattr(func, "__await__") or hasattr(func, "__aenter__")

def build_ffmpeg_command(
    output_path: str,
    input_paths: List[str],
    normalize_audio: bool = False,
    is_concat: bool = False,
    concat_list_path: Optional[str] = None
) -> List[str]:
    """
    Build ffmpeg command for audio processing.
    
    Args:
        output_path: Path to output file
        input_paths: List of input file paths
        normalize_audio: Whether to apply audio normalization
        is_concat: Whether to use concat demuxer
        concat_list_path: Path to concat list file (only used if is_concat=True)
        
    Returns:
        List of command parts for subprocess.run
    """
    cmd = ["ffmpeg", "-y"]
    
    # Add inputs
    if is_concat and concat_list_path:
        cmd.extend(["-f", "concat", "-safe", "0", "-i", concat_list_path])
    else:
        for input_path in input_paths:
            cmd.extend(["-i", input_path])
    
    # Add filters
    if normalize_audio:
        if len(input_paths) > 1:
            # Complex filter for multiple inputs with normalization
            cmd.extend([
                "-filter_complex", 
                "[1:a]loudnorm=I=-16:TP=-1:LRA=5[tts_norm]; [0:a][tts_norm]concat=n=2:v=0:a=1[out]",
                "-map", "[out]"
            ])
        else:
            # Simple normalization filter for single input
            cmd.extend(["-af", "loudnorm=I=-16:TP=-1:LRA=5"])
    
    # Add output parameters (same for all cases)
    cmd.extend([
        "-ac", "1",
        "-ar", "24000",
        "-b:a", "128k",
        "-preset", "superfast",
        "-threads", "4",
        output_path
    ])
    
    return cmd

async def process_audio(
    hass: HomeAssistant,
    audio_content: bytes,
    output_path: Optional[str] = None,
    chime_enabled: bool = False,
    chime_path: Optional[str] = None,
    normalize_audio: bool = False
) -> Tuple[str, bytes, float]:
    """
    Process audio content with optional chime and normalization.
    
    Args:
        hass: HomeAssistant instance
        audio_content: Raw audio content bytes
        output_path: Optional output path
        chime_enabled: Whether to add chime
        chime_path: Path to chime file
        normalize_audio: Whether to normalize audio
        
    Returns:
        Tuple of (format, processed_audio, processing_time_ms)
    """
    import time
    
    start_time = time.monotonic()
    ffmpeg_start_time = None
    ffmpeg_time = 0
    
    # Create a temporary file for TTS audio
    def write_temp_file():
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tts_file:
            tts_file.write(audio_content)
            return tts_file.name
    
    tts_path = await hass.async_add_executor_job(write_temp_file)
    
    try:
        # Determine final output path
        final_output_path = output_path
        if not final_output_path:
            def create_temp_output():
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as out_file:
                    return out_file.name
            final_output_path = await hass.async_add_executor_job(create_temp_output)
        
        # Process based on options
        if chime_enabled and chime_path:
            if normalize_audio:
                # Chime + normalization
                cmd = build_ffmpeg_command(
                    final_output_path,
                    [chime_path, tts_path],
                    normalize_audio=True
                )
            else:
                # Chime only (using concat demuxer)
                def write_concat_list():
                    with tempfile.NamedTemporaryFile(mode="w", delete=False) as list_file:
                        list_file.write(f"file '{chime_path}'\n")
                        list_file.write(f"file '{tts_path}'\n")
                        return list_file.name
                list_path = await hass.async_add_executor_job(write_concat_list)
                
                cmd = build_ffmpeg_command(
                    final_output_path,
                    [chime_path, tts_path],  # Still need this for command structure
                    normalize_audio=False,
                    is_concat=True,
                    concat_list_path=list_path
                )
        
        elif normalize_audio:
            # Normalization only
            cmd = build_ffmpeg_command(
                final_output_path,
                [tts_path],
                normalize_audio=True
            )
        
        else:
            # No processing needed, just read the file
            def read_original():
                with open(tts_path, "rb") as f:
                    return f.read()
            
            final_audio = await hass.async_add_executor_job(read_original)
            
            # Get duration
            duration = await hass.async_add_executor_job(get_media_duration, tts_path)
            
            # Clean up and return
            await hass.async_add_executor_job(os.remove, tts_path)
            
            total_time = (time.monotonic() - start_time) * 1000
            return "mp3", final_audio, total_time
        
        # Run ffmpeg command
        _LOGGER.debug("Executing ffmpeg command: %s", " ".join(cmd))
        ffmpeg_start_time = time.monotonic()
        
        # When using asyncio.run, we need to simplify execution to avoid event loop conflicts
        # Just run synchronously since this whole function is being wrapped in asyncio.run()
        try:
            _LOGGER.debug("Running ffmpeg in executor")
            await hass.async_add_executor_job(
                lambda: subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            )
        except Exception as exc:
            _LOGGER.error("Error executing ffmpeg: %s", exc)
            raise
            
        ffmpeg_time = (time.monotonic() - ffmpeg_start_time) * 1000
        
        # Read the processed file
        def read_file():
            with open(final_output_path, "rb") as f:
                return f.read()
        
        final_audio = await hass.async_add_executor_job(read_file)
        
        # Get duration from processed file
        duration = await hass.async_add_executor_job(get_media_duration, final_output_path)
        
        # Final clean up of temporary files
        def cleanup_files():
            try:
                os.remove(tts_path)
                os.remove(final_output_path)
                
                # Remove concat list file if it was created
                if chime_enabled and not normalize_audio and 'list_path' in locals():
                    os.remove(list_path)
            except Exception as e:
                _LOGGER.debug("Error cleaning up temporary files: %s", e)
        
        await hass.async_add_executor_job(cleanup_files)
        
        total_time = (time.monotonic() - start_time) * 1000
        return "mp3", final_audio, total_time
    
    except Exception as e:
        # Clean up in case of error
        def error_cleanup():
            try:
                os.remove(tts_path)
                if 'final_output_path' in locals():
                    os.remove(final_output_path)
                if 'list_path' in locals():
                    os.remove(list_path)
            except:
                pass
        
        await hass.async_add_executor_job(error_cleanup)
        
        _LOGGER.error("Error processing audio: %s", e)
        raise HomeAssistantError(f"Error processing audio: {e}") from e

def check_ffmpeg_installed() -> bool:
    """
    Check if ffmpeg is installed and available.
    
    Returns:
        True if ffmpeg is available, False otherwise
    """
    try:
        subprocess.run(
            ["ffmpeg", "-version"], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            check=True
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False

def normalize_entity_ids(entity_ids: Union[str, List[str]]) -> List[str]:
    """
    Normalize entity IDs to always be a list.
    
    Args:
        entity_ids: Entity ID or list of entity IDs
        
    Returns:
        List of entity IDs
    """
    if not entity_ids:
        return []
    
    if isinstance(entity_ids, str):
        return [entity_ids]
    
    return entity_ids

async def get_media_player_state(
    hass: HomeAssistant, 
    entity_id: str
) -> Tuple[Optional[StateType], Optional[Dict]]:
    """
    Get media player state and attributes if available.
    
    Args:
        hass: Home Assistant instance
        entity_id: Entity ID to get state for
        
    Returns:
        Tuple of (state, attributes) or (None, None) if unavailable
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
        return None, None
    return state.state, state.attributes

def get_speaker_status(state: Optional[str]) -> str:
    """
    Get speaker status based on state.
    
    Args:
        state: Speaker state
        
    Returns:
        "inactive" if state is "off" or "idle" or "paused", "active" otherwise
    """
    # Hardcode state values instead of importing constants to avoid import issues
    if not state:
        return "inactive"
    
    state_lower = state.lower()
    
    # Check for the three inactive states
    if state_lower == "idle" or state_lower == "off" or state_lower == "paused":
        return "inactive"
        
    return "active"

async def set_media_player_volume(
    hass: HomeAssistant, 
    entity_id: str, 
    volume_level: float,
    retries: int = 3,
    retry_delay: float = 0.7
) -> bool:
    """
    Set volume for a media player.
    
    Args:
        hass: Home Assistant instance
        entity_id: Entity ID to set volume for
        volume_level: Volume level to set (0.0-1.0)
        retries: Number of retries
        retry_delay: Delay between retries
        
    Returns:
        Whether volume was successfully set
    """
    # Skip if entity is not available
    state, attributes = await get_media_player_state(hass, entity_id)
    if state is None or attributes is None:
        _LOGGER.debug("Media player %s state not available", entity_id)
        return False
    
    # Skip if entity doesn't have a volume level attribute
    current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
    if current_volume is None:
        # For Google speakers, they might not report volume when off
        # Try to set volume anyway and let the device handle it
        _LOGGER.debug("Media player %s has no volume attribute (state: %s), attempting to set volume anyway", 
                      entity_id, state)
        # Don't return False here - continue with volume setting
    
    # Skip if already at target volume (with small tolerance)
    if current_volume is not None and abs(float(current_volume) - volume_level) < 0.01:
        _LOGGER.debug("Volume already at desired level %.2f for %s", volume_level, entity_id)
        return True
    
    # Set volume
    if current_volume is not None:
        _LOGGER.debug("Setting volume for %s from %.2f to %.2f", entity_id, float(current_volume), volume_level)
    else:
        _LOGGER.debug("Setting volume for %s to %.2f (current volume unknown)", entity_id, volume_level)
    
    for attempt in range(1, retries + 1):
        try:
            await hass.services.async_call(
                MP_DOMAIN,
                "volume_set",
                {
                    ATTR_ENTITY_ID: entity_id,
                    ATTR_MEDIA_VOLUME_LEVEL: volume_level,
                },
                blocking=True,
            )
            
            # Brief wait for volume change
            await asyncio.sleep(0.3)
            
            # Verify volume
            new_state, new_attributes = await get_media_player_state(hass, entity_id)
            if new_state is not None and new_attributes is not None:
                new_volume = new_attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
                if new_volume is not None:
                    # Tolerance for volume verification
                    tolerance = 0.1
                    
                    if abs(float(new_volume) - volume_level) < tolerance:
                        _LOGGER.debug(
                            "Successfully set volume for %s to %.2f (actual: %.2f)",
                            entity_id, volume_level, float(new_volume)
                        )
                        return True
                    else:
                        _LOGGER.debug(
                            "Volume not set correctly for %s: target=%.2f, actual=%.2f (difference: %.2f)",
                            entity_id, volume_level, float(new_volume), abs(float(new_volume) - volume_level)
                        )
            
            if attempt < retries:
                # Shorter retry delay
                delay = 0.3
                _LOGGER.debug("Volume change not effective yet, retrying %d/%d after %.1f seconds", 
                             attempt, retries, delay)
                await asyncio.sleep(delay)
            
        except Exception as err:
            _LOGGER.error("Failed to set volume for %s: %s", entity_id, err)
            if attempt < retries:
                await asyncio.sleep(0.3)
    
    # Even if we couldn't verify the volume was set, return True
    # Sometimes devices update their state but don't report it back immediately
    _LOGGER.warning("Could not verify volume was set for %s, continuing anyway", entity_id)
    return True

def get_cascaded_config_value(
    options: Dict[str, Any], 
    data: Dict[str, Any], 
    service_data: Dict[str, Any],
    key: str, 
    default: Any = None
) -> Any:
    """
    Get a configuration value with proper cascade priority:
    service_data > options > data > default
    
    Args:
        options: Component options
        data: Component data
        service_data: Service call data
        key: Key to retrieve
        default: Default value if not found
        
    Returns:
        The value with proper priority
    """
    return service_data.get(
        key, 
        options.get(
            key, 
            data.get(key, default)
        )
    )

async def call_media_player_service(
    hass: HomeAssistant,
    service: str,
    entity_id: Union[str, List[str]],
    extra_data: Optional[Dict[str, Any]] = None,
    blocking: bool = True
) -> None:
    """
    Call a media player service with standardized error handling.
    
    Args:
        hass: Home Assistant instance
        service: Service to call
        entity_id: Entity ID or list of entity IDs
        extra_data: Additional service data
        blocking: Whether to wait for service completion
    """
    service_data = {ATTR_ENTITY_ID: entity_id}
    
    if extra_data:
        service_data.update(extra_data)
    
    try:
        await hass.services.async_call(
            MP_DOMAIN,
            service,
            service_data,
            blocking=blocking,
        )
    except Exception as err:
        entity_ids = normalize_entity_ids(entity_id)
        _LOGGER.error("Failed to call %s for %s: %s", service, ", ".join(entity_ids), err)

