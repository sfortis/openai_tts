"""
Utility functions for OpenAI TTS integration.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import StateType
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    DOMAIN as MP_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def detect_audio_format(audio_data: bytes) -> str:
    """Detect audio format from magic bytes.

    Recognises mp3, wav, opus (Ogg), aac (ADTS) and flac. Returns "mp3"
    as a catch-all for byte sequences without a known signature -
    notably PCM, which is raw and has no header. Callers that need to
    distinguish PCM from real mp3 must pass an explicit format hint
    rather than relying on detection.
    """
    if len(audio_data) < 4:
        return "mp3"
    if audio_data[:4] == b'RIFF':
        return "wav"
    if audio_data[:4] == b'OggS':
        return "opus"
    if audio_data[:4] == b'fLaC':
        return "flac"
    if audio_data[:2] in (b'\xff\xf1', b'\xff\xf9'):
        return "aac"
    return "mp3"


# Magic-byte signatures used to verify that a TTS response actually
# contains audio of the expected format. Defends against the cache-poisoning
# class of bug (issue #64) where an HTTP 200 carries a JSON/HTML error body.
_MP3_MAGIC: Tuple[bytes, ...] = (
    b"ID3",                               # ID3v2 tag at start
    b"\xff\xfb", b"\xff\xfa", b"\xff\xf3",
    b"\xff\xf2", b"\xff\xfd", b"\xff\xfc",  # MPEG audio frame sync variants
)
_WAV_MAGIC: Tuple[bytes, ...] = (b"RIFF",)
_OPUS_MAGIC: Tuple[bytes, ...] = (b"OggS",)
_AAC_MAGIC: Tuple[bytes, ...] = (b"\xff\xf1", b"\xff\xf9")  # ADTS sync words
_FLAC_MAGIC: Tuple[bytes, ...] = (b"fLaC",)

# Default minimum byte count below which a TTS response is too short
# to plausibly contain real audio (a JSON `{"error":...}` is ~50 bytes).
_DEFAULT_MIN_AUDIO_BYTES = 256


def is_valid_audio(
    audio_data: Optional[bytes],
    expected_format: str = "mp3",
    min_size: int = _DEFAULT_MIN_AUDIO_BYTES,
) -> bool:
    """Return True if ``audio_data`` looks like real audio of ``expected_format``.

    Used as a last-line defense before handing audio to the Home Assistant
    TTS cache. If this returns False we MUST refuse to cache, otherwise the
    bad bytes will be served back to media players forever (issue #64).

    Args:
        audio_data: Raw bytes returned by the TTS backend.
        expected_format: ``"mp3"``, ``"wav"`` or ``"opus"``.
        min_size: Reject anything smaller than this many bytes. The default
            is generous enough to allow very short clips while still rejecting
            typical JSON / HTML error bodies.

    Returns:
        True only when both the size and the magic bytes look like the
        expected format.
    """
    if not audio_data or len(audio_data) < min_size:
        return False

    fmt = expected_format.lower()
    if fmt == "mp3":
        # ``\xff\xf1`` is shared between MP3 sync and AAC ADTS sync, so a
        # backend that auto-promotes mp3 → aac can land here too. Accept
        # both, plus the wav fallback covered already in is_valid_audio's
        # wav branch.
        return audio_data.startswith(_MP3_MAGIC) or audio_data.startswith(_WAV_MAGIC)
    if fmt == "wav":
        # Some backends return WAV when MP3 was requested; accept either.
        return audio_data.startswith(_WAV_MAGIC) or audio_data.startswith(_MP3_MAGIC)
    if fmt == "opus":
        return audio_data.startswith(_OPUS_MAGIC)
    if fmt == "aac":
        return audio_data.startswith(_AAC_MAGIC) or audio_data.startswith(_MP3_MAGIC)
    if fmt == "flac":
        return audio_data.startswith(_FLAC_MAGIC)
    if fmt == "pcm":
        # Raw PCM has no header; only reject obvious JSON/HTML error bodies.
        first = audio_data[:1]
        return first not in (b"{", b"<", b"[")

    # Unknown format: reject obvious text/JSON/HTML payloads.
    first = audio_data[:1]
    if first in (b"{", b"<", b"["):
        return False
    return True


def ensure_wav_chimes(chime_dir: str) -> None:
    """
    Ensure WAV versions of all MP3 chimes exist.
    Converts MP3 chimes to WAV if the WAV version doesn't exist.

    Args:
        chime_dir: Path to the chime directory
    """
    if not os.path.isdir(chime_dir):
        _LOGGER.warning("Chime directory not found: %s", chime_dir)
        return

    for filename in os.listdir(chime_dir):
        if filename.endswith(".mp3"):
            mp3_path = os.path.join(chime_dir, filename)
            wav_path = os.path.join(chime_dir, filename[:-4] + ".wav")

            if not os.path.exists(wav_path):
                _LOGGER.info("Converting chime to WAV: %s", filename)
                try:
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", mp3_path,
                        "-ac", "1",
                        "-ar", "24000",
                        wav_path
                    ]
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    _LOGGER.debug("Created WAV chime: %s", wav_path)
                except Exception as e:
                    _LOGGER.error("Failed to convert chime %s to WAV: %s", filename, e)


def ensure_chime_in_format(mp3_chime_path: str, target_format: str) -> str:
    """Return a chime file path matching ``target_format``, transcoding on demand.

    Sibling files are cached next to the source MP3 (``threetone.mp3`` →
    ``threetone.opus`` / ``.aac`` / ``.flac`` / ``.wav``). PCM uses ``.pcm``
    raw bytes. Falls back to the original mp3 when ffmpeg fails so the
    caller still has *some* chime to mix in.
    """
    if target_format == "mp3" or not mp3_chime_path:
        return mp3_chime_path
    base, _ = os.path.splitext(mp3_chime_path)
    target_path = f"{base}.{target_format}"
    if os.path.exists(target_path):
        return target_path

    from .const import AUDIO_FORMAT_ENCODER

    encoder = AUDIO_FORMAT_ENCODER.get(target_format)
    if encoder is None:
        _LOGGER.warning(
            "No encoder mapping for chime target format %s, using mp3 source",
            target_format,
        )
        return mp3_chime_path

    cmd = ["ffmpeg", "-y", "-i", mp3_chime_path, "-ac", "1", "-ar", "24000"]
    cmd.extend(encoder["codec_args"])
    cmd.extend(encoder["container_args"])
    cmd.append(target_path)
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _LOGGER.info("Transcoded chime to %s: %s", target_format, target_path)
        return target_path
    except Exception as exc:
        _LOGGER.warning(
            "Failed to transcode chime %s to %s (%s); falling back to mp3 source",
            mp3_chime_path, target_format, exc,
        )
        return mp3_chime_path


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
    concat_list_path: Optional[str] = None,
    tts_input_format: Optional[str] = None,
    output_format: str = "mp3",
) -> List[str]:
    """
    Build ffmpeg command for audio processing.

    Args:
        output_path: Path to output file
        input_paths: List of input file paths
        normalize_audio: Whether to apply audio normalization
        is_concat: Whether to use concat demuxer
        concat_list_path: Path to concat list file (only used if is_concat=True)
        tts_input_format: Explicit format hint for the LAST input path (the
            TTS audio). Only meaningful for headerless formats like ``pcm``;
            for everything else ffmpeg auto-detects from the file header
            and this argument is ignored. When set to ``pcm`` we tell
            ffmpeg the layout matches OpenAI's documented raw output
            (24kHz signed 16-bit little-endian mono).
    """
    from .const import AUDIO_FORMAT_ENCODER

    cmd = ["ffmpeg", "-y"]

    # Add inputs
    if is_concat and concat_list_path:
        cmd.extend(["-f", "concat", "-safe", "0", "-i", concat_list_path])
    else:
        last_idx = len(input_paths) - 1
        for idx, input_path in enumerate(input_paths):
            if idx == last_idx and tts_input_format == "pcm":
                cmd.extend([
                    "-f", "s16le", "-ar", "24000", "-ac", "1",
                ])
            cmd.extend(["-i", input_path])
    
    # Filter graph for chime+TTS mixing or single-input normalization.
    # Both streams are forced to a common PCM layout before concat so
    # the operation is codec-agnostic.
    if len(input_paths) > 1 and not is_concat:
        norm_step = ",loudnorm=I=-16:TP=-1:LRA=5" if normalize_audio else ""
        common = "aresample=24000:async=1,aformat=sample_fmts=fltp:channel_layouts=mono"
        cmd.extend([
            "-filter_complex",
            (
                f"[0:a]{common}[ch];"
                f"[1:a]{common}{norm_step}[tts];"
                "[ch][tts]concat=n=2:v=0:a=1[out]"
            ),
            "-map", "[out]",
        ])
    elif normalize_audio:
        cmd.extend(["-af", "loudnorm=I=-16:TP=-1:LRA=5"])

    # Output side: pick codec / muxer for the requested format. The
    # ``is_concat`` branch is the chime-only fast path (concat demuxer):
    # we use ``-c copy`` so the TTS payload is remuxed without a decode
    # /encode roundtrip, since the chime was pre-converted to match the
    # TTS codec via ``ensure_chime_in_format``.
    encoder = AUDIO_FORMAT_ENCODER.get(output_format, AUDIO_FORMAT_ENCODER["mp3"])
    if is_concat:
        cmd.extend(["-c", "copy"])
        cmd.extend(encoder["container_args"])
    else:
        cmd.extend(["-ac", "1", "-ar", "24000"])
        cmd.extend(encoder["codec_args"])
        cmd.extend(encoder["container_args"])
    cmd.append(output_path)

    return cmd

async def process_audio(
    hass: HomeAssistant,
    audio_content: bytes,
    output_path: Optional[str] = None,
    chime_enabled: bool = False,
    chime_path: Optional[str] = None,
    normalize_audio: bool = False,
    input_format: Optional[str] = None,
) -> Tuple[str, bytes, float]:
    """
    Process audio content with optional chime and normalization.

    Args:
        hass: HomeAssistant instance
        audio_content: Raw audio content bytes
        output_path: Optional output path
        chime_enabled: Whether to add chime
        chime_path: Path to chime file (MP3)
        normalize_audio: Whether to normalize audio
        input_format: Explicit format hint (mp3/wav/opus/aac/flac/pcm).
            Required for ``pcm`` since it has no header to auto-detect.
            Falls back to magic-byte detection when omitted.

    Returns:
        Tuple of (format, processed_audio, processing_time_ms). Output
        is always re-encoded to ``mp3`` so the result mixes cleanly
        with mp3 chimes and HA's ``preferred_format`` ffmpeg conversion
        can re-encode it to the user-selected delivery format.
    """
    import time

    start_time = time.monotonic()

    # Trust the caller's hint when given, fall back to magic-byte detection
    audio_format = input_format or detect_audio_format(audio_content)
    _LOGGER.debug("TTS audio format: %s (hint=%s)", audio_format, input_format)

    # When chime is enabled, transcode it on-demand into the same codec
    # the TTS came in as. Cheap (chime files are tiny) and unlocks the
    # ``-c copy`` fast path for chime-only requests, which skips the
    # decode/encode roundtrip on the much larger TTS payload.
    actual_chime_path = chime_path
    if chime_enabled and chime_path:
        actual_chime_path = await hass.async_add_executor_job(
            ensure_chime_in_format, chime_path, audio_format,
        )

    # Pick a temp-file suffix ffmpeg can use to auto-identify the input.
    # ``pcm`` has no header, so we strip the suffix and rely on the
    # explicit ``-f s16le`` flags injected via ``tts_input_format``.
    file_suffix = "" if audio_format == "pcm" else f".{audio_format}"

    def write_temp_file():
        with tempfile.NamedTemporaryFile(suffix=file_suffix, delete=False) as tts_file:
            tts_file.write(audio_content)
            return tts_file.name

    tts_path = await hass.async_add_executor_job(write_temp_file)
    
    try:
        # Determine final output path. ``caller_owns_output`` tracks whether
        # the path came from the caller; if so, we must NOT delete it during
        # cleanup (the caller is responsible for its own file).
        # Output path keeps the requested format extension so ffmpeg
        # picks the matching muxer automatically. PCM has no container,
        # so we still use ``.pcm`` and rely on the explicit ``-f s16le``
        # in the encoder's container_args.
        out_suffix = f".{audio_format}"
        final_output_path = output_path
        caller_owns_output = output_path is not None
        if not final_output_path:
            def create_temp_output():
                with tempfile.NamedTemporaryFile(suffix=out_suffix, delete=False) as out_file:
                    return out_file.name
            final_output_path = await hass.async_add_executor_job(create_temp_output)

        # Decide which ffmpeg pipeline to run:
        #   chime-only  → concat demuxer + ``-c copy`` (no TTS transcode)
        #   chime+norm  → filter_complex (loudnorm forces decode/encode)
        #   norm-only   → single-input loudnorm (decode/encode)
        #   neither     → caller already returned native bytes; we
        #                 shouldn't be here, but bail safely.
        if chime_enabled and actual_chime_path and not normalize_audio:
            def write_concat_list():
                with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as list_file:
                    list_file.write(f"file '{actual_chime_path}'\n")
                    list_file.write(f"file '{tts_path}'\n")
                    return list_file.name
            list_path = await hass.async_add_executor_job(write_concat_list)
            cmd = build_ffmpeg_command(
                final_output_path,
                [actual_chime_path, tts_path],
                normalize_audio=False,
                is_concat=True,
                concat_list_path=list_path,
                tts_input_format=audio_format,
                output_format=audio_format,
            )
        elif chime_enabled and actual_chime_path and normalize_audio:
            cmd = build_ffmpeg_command(
                final_output_path,
                [actual_chime_path, tts_path],
                normalize_audio=True,
                tts_input_format=audio_format,
                output_format=audio_format,
            )
        elif normalize_audio:
            cmd = build_ffmpeg_command(
                final_output_path,
                [tts_path],
                normalize_audio=True,
                tts_input_format=audio_format,
                output_format=audio_format,
            )
        else:
            # Caller invoked us with neither chime nor normalize; just
            # return the native bytes unchanged. Faster than a no-op
            # ffmpeg roundtrip and keeps the original encoder output.
            def read_original():
                with open(tts_path, "rb") as f:
                    return f.read()

            final_audio = await hass.async_add_executor_job(read_original)
            await hass.async_add_executor_job(os.remove, tts_path)
            total_time = (time.monotonic() - start_time) * 1000
            return audio_format, final_audio, total_time

        # Run ffmpeg command
        _LOGGER.debug("Executing ffmpeg command: %s", " ".join(cmd))

        try:
            _LOGGER.debug("Running ffmpeg in executor")
            await hass.async_add_executor_job(
                lambda: subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            )
        except Exception as exc:
            _LOGGER.error("Error executing ffmpeg: %s", exc)
            raise

        # Read the processed file
        def read_file():
            with open(final_output_path, "rb") as f:
                return f.read()

        final_audio = await hass.async_add_executor_job(read_file)
        
        # Final clean up of temporary files. We only own the output file
        # when the caller did not provide ``output_path``; otherwise the
        # caller is responsible for it (issue: previous code unconditionally
        # deleted it, which silently broke any caller that passed a path).
        def cleanup_files():
            try:
                os.remove(tts_path)
                if not caller_owns_output:
                    os.remove(final_output_path)
                if 'list_path' in locals():
                    os.remove(list_path)
            except Exception as e:
                _LOGGER.debug("Error cleaning up temporary files: %s", e)

        await hass.async_add_executor_job(cleanup_files)

        total_time = (time.monotonic() - start_time) * 1000
        return audio_format, final_audio, total_time

    except Exception as e:
        # Best-effort cleanup of any temp files we created during this call.
        def error_cleanup():
            try:
                os.remove(tts_path)
                if 'final_output_path' in locals() and not caller_owns_output:
                    os.remove(final_output_path)
                if 'list_path' in locals():
                    os.remove(list_path)
            except OSError:
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
) -> bool:
    """Fire-and-forget volume change.

    Earlier this helper used a sleep + verify + retry loop that
    routinely added ~1.2s of latency on speakers (notably JBL) that
    delay state-attribute updates. The verify-loop was not actually
    making playback any more reliable - 99% of the time the volume
    lands within 100ms regardless. The remaining 1% fails just as
    often after three retries as after one.

    We now issue ``volume_set`` blocking on the service call (so we
    know HA dispatched it) and return immediately. ``announce()``
    already includes a brief settle window before ``tts.speak`` runs,
    which is plenty for the device to apply the change.
    """
    state, attributes = await get_media_player_state(hass, entity_id)
    if state is None or attributes is None:
        _LOGGER.debug("Media player %s state not available", entity_id)
        return False

    current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
    if (
        current_volume is not None
        and abs(float(current_volume) - volume_level) < 0.01
    ):
        return True  # already at target

    if current_volume is not None:
        _LOGGER.debug(
            "Setting volume for %s from %.2f to %.2f",
            entity_id, float(current_volume), volume_level,
        )
    else:
        _LOGGER.debug(
            "Setting volume for %s to %.2f (current unknown)",
            entity_id, volume_level,
        )

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
        return True
    except Exception as err:
        _LOGGER.error("Failed to set volume for %s: %s", entity_id, err)
        return False

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

