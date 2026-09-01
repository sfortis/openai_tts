"""
Utility functions for OpenAI TTS integration.
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple, Union

from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
)
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import StateType

from .const import AUDIO_FORMAT_ENCODER

_LOGGER = logging.getLogger(__name__)

# Loudness correction for speech.
#
# Two filters in series, and both are needed for different reasons.
#
# ``dynaudnorm`` replaced ``loudnorm=I=-16:TP=-1:LRA=5``. Measured
# against three engines on 2026-08-29, loudnorm made a short OpenAI clip
# six decibels QUIETER than it started (-24.25 LUFS in, -30.20 out): it
# is designed to run in two passes, and given only one it has not
# converged before a two second announcement ends. dynaudnorm corrects
# continuously instead, so it needs neither a second pass nor a complete
# file, which is also what lets normalisation run on a stream.
#
# ``acompressor`` in front of it answers a separate complaint: the clip
# was loud enough on average while individual words were still swallowed.
# Levelling the average does not lift a quiet syllable, and a fast
# compressor does. Measured on a thirteen second announcement, the
# quiet passages rose from -17.4 to -14.8 LUFS with the loudness range
# unchanged at 1.9 LU.
#
# The peak target is 0.85 rather than the filter's own 0.95 default.
# At 0.95 two of the three engines came out above -0.5 dBTP once the mp3
# encoder had added its overshoot, which clips. At 0.85 the worst of the
# three sits at -1.29 dBTP with the quiet passages only half a decibel
# lower, which is a good trade.
# Containers whose header states a total length that a streaming
# producer cannot know, so it writes a placeholder instead: wav writes
# 0xFFFFFFFF, flac writes zero samples. Decoders that honour the field
# then read the clip as hours long, or as empty. Re-encoding to a file
# is what fixes it, because ffmpeg can seek back and write the real
# figure once the audio has been written.
_REWRITE_HEADER_FORMATS = frozenset({"wav", "flac"})

LOUDNESS_FILTER = (
    "acompressor=threshold=-28dB:ratio=4:attack=3:release=60,"
    "dynaudnorm=p=0.85:m=20:f=40:g=5"
)


def resolve_ffmpeg_paths(hass: HomeAssistant) -> Tuple[str, str]:
    """Return the ffmpeg and ffprobe commands to use.

    Home Assistant lets the user configure where ffmpeg lives, through
    the ``ffmpeg`` integration, and installations that keep it outside
    the search path rely on that. Calling the bare names, as this module
    used to, ignored the setting.

    ffprobe has no equivalent setting, so it is taken from the same
    directory as ffmpeg. A bare ``ffmpeg`` means the search path is in
    use, so ``ffprobe`` is left bare too.
    """
    binary = "ffmpeg"
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        binary = get_ffmpeg_manager(hass).binary or "ffmpeg"
    except (ImportError, KeyError, RuntimeError, ValueError) as err:
        # ValueError is what get_ffmpeg_manager raises when the ffmpeg
        # integration has not been set up. The manifest declares it as a
        # dependency, so that should not happen, but a helper that
        # decides where a binary lives has no business raising.
        _LOGGER.debug("Falling back to ffmpeg on the search path: %s", err)

    directory = os.path.dirname(binary)
    if not directory:
        return binary, "ffprobe"
    # Same directory, plain name. Deriving the name from ffmpeg's own
    # would turn "ffmpeg-static" into "ffprobe-static", which is a
    # guess about someone else's file naming; "ffprobe" beside it is the
    # convention every distribution and static build follows.
    suffix = ".exe" if binary.lower().endswith(".exe") else ""
    return binary, os.path.join(directory, f"ffprobe{suffix}")


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
    return first not in (b"{", b"<", b"[")


def get_media_duration(file_path: str, ffprobe: str = "ffprobe") -> float:
    """Return the duration of a media file in seconds, or 0.0 on failure.

    One ffprobe invocation. There used to be two: the first looked for a
    ``tts_duration_ms`` tag this integration wrote into its own mp3
    output, and the second asked for the real duration when no tag was
    found. The tag was only ever written on the legacy TTS path, which
    Home Assistant stopped reaching once the entity implemented the
    streaming contract, so the first probe could never succeed and every
    measurement paid for two process spawns instead of one.

    Args:
        file_path: Path to the media file.
        ffprobe: The ffprobe command, from ``resolve_ffmpeg_paths``.
    """
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError) as err:
        _LOGGER.error("ffprobe failed for %s: %s", file_path, err)
        return 0.0
    duration_str = result.stdout.strip()
    if not duration_str:
        _LOGGER.error("ffprobe returned no duration for %s", file_path)
        return 0.0
    try:
        return float(duration_str)
    except ValueError:
        _LOGGER.error("ffprobe gave a non-numeric duration: %r", duration_str)
        return 0.0


def measure_audio_duration(
    audio_data: bytes, suffix: str = ".mp3", ffprobe: str = "ffprobe"
) -> float:
    """Return the duration of an in-memory clip in seconds, or 0.0.

    ffprobe needs a seekable input to read a container's real duration,
    so the bytes go to a temporary file first. Writing that file,
    probing it and deleting it are all blocking, which is why they live
    together in one synchronous function: the caller hands the whole
    sequence to an executor with a single call instead of leaving the
    write and the unlink on the event loop.

    Args:
        audio_data: The encoded clip.
        suffix: Extension for the temporary file. ffprobe identifies the
            format by probing the content, so this only affects the file
            name.
        ffprobe: The ffprobe command, from ``resolve_ffmpeg_paths``.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            tmp_file.write(audio_data)
            tmp_path = tmp_file.name
    except OSError as err:
        _LOGGER.error("Cannot write temporary file for duration probe: %s", err)
        if tmp_path is not None:
            _remove_quietly(tmp_path)
        return 0.0

    try:
        return get_media_duration(tmp_path, ffprobe)
    finally:
        _remove_quietly(tmp_path)


def _remove_quietly(path: str) -> None:
    """Delete a path, ignoring the case where it is already gone."""
    with contextlib.suppress(OSError):
        os.unlink(path)


def build_ffmpeg_command(
    output_path: str,
    input_paths: List[str],
    normalize_audio: bool = False,
    tts_input_format: Optional[str] = None,
    output_format: str = "mp3",
    ffmpeg: str = "ffmpeg",
) -> List[str]:
    """
    Build ffmpeg command for audio processing.

    Args:
        output_path: Path to output file
        input_paths: List of input file paths
        normalize_audio: Whether to apply audio normalization
        tts_input_format: Explicit format hint for the LAST input path (the
            TTS audio). Only meaningful for headerless formats like ``pcm``;
            for everything else ffmpeg auto-detects from the file header
            and this argument is ignored. When set to ``pcm`` we tell
            ffmpeg the layout matches OpenAI's documented raw output
            (24kHz signed 16-bit little-endian mono).
    """

    cmd = [ffmpeg, "-y"]

    # Add inputs
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
    if len(input_paths) > 1:
        norm_step = f",{LOUDNESS_FILTER}" if normalize_audio else ""
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
        cmd.extend(["-af", LOUDNESS_FILTER])

    # Output side: pick codec and muxer for the requested format. Always a
    # real re-encode. There used to be a ``-c copy`` remux path here for a
    # concat-demuxer input, but nothing has passed that input since the
    # chime pre-conversion it depended on was removed.
    encoder = AUDIO_FORMAT_ENCODER.get(output_format, AUDIO_FORMAT_ENCODER["mp3"])
    cmd.extend(["-ac", "1", "-ar", "24000"])
    cmd.extend(encoder["codec_args"])
    cmd.extend(encoder["container_args"])
    cmd.append(output_path)

    return cmd

async def process_audio(
    hass: HomeAssistant,
    audio_content: bytes,
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
        chime_enabled: Whether to add chime
        chime_path: Path to chime file (MP3)
        normalize_audio: Whether to normalize audio
        input_format: Explicit format hint (mp3/wav/opus/aac/flac/pcm).
            Required for ``pcm`` since it has no header to auto-detect.
            Falls back to magic-byte detection when omitted.

    Returns:
        Tuple of (format, processed_audio, processing_time_ms). Output is
        encoded in the format the caller asked for, via
        ``output_format``, so a profile set to wav or opus keeps that
        format through post-processing. HA's ``preferred_format`` ffmpeg
        layer still sits downstream if the media_player needs another.
    """
    import time

    start_time = time.monotonic()
    ffmpeg, _ = resolve_ffmpeg_paths(hass)

    # Trust the caller's hint when given, fall back to magic-byte detection
    audio_format = input_format or detect_audio_format(audio_content)
    _LOGGER.debug("TTS audio format: %s (hint=%s)", audio_format, input_format)

    # The chime path is passed straight through to ffmpeg. The
    # ``filter_complex`` graph in ``build_ffmpeg_command`` runs an
    # ``aresample`` + ``aformat`` step on every input before concat, so
    # any sample rate / channel layout / codec combination on the chime
    # is normalised against the TTS audio in a single re-encode pass.
    actual_chime_path = chime_path if chime_enabled else None

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
        # Always a temp file: no caller passes an output path, so this
        # function owns the file it creates and removes it below.
        def create_temp_output() -> str:
            with tempfile.NamedTemporaryFile(
                suffix=f".{audio_format}", delete=False
            ) as tmp:
                return tmp.name

        final_output_path = await hass.async_add_executor_job(create_temp_output)

        # Decide which ffmpeg pipeline to run:
        #   chime (any)  → filter_complex re-encode of both streams. The
        #                  concat-demuxer + ``-c copy`` shortcut produced
        #                  byte-valid mp3s that miniaudio (HomePod /
        #                  Apple TV via pyatv) refused to decode because
        #                  the Xing/Info header from the chime no longer
        #                  matched the combined frame layout. A single
        #                  fresh encode keeps the bitstream consistent.
        #   norm-only    → single-input loudnorm (decode/encode)
        #   neither      → a plain re-encode, which is not a no-op for
        #                  the formats that declare a total length in
        #                  their header. A provider streaming wav or
        #                  flac cannot know that length and writes a
        #                  placeholder; writing to a file here lets
        #                  ffmpeg seek back and put the real one in.
        #                  For every other format the caller returns
        #                  the native bytes and never reaches this.
        if chime_enabled and actual_chime_path:
            cmd = build_ffmpeg_command(
                final_output_path,
                [actual_chime_path, tts_path],
                normalize_audio=normalize_audio,
                tts_input_format=audio_format,
                output_format=audio_format,
                ffmpeg=ffmpeg,
            )
        elif normalize_audio:
            cmd = build_ffmpeg_command(
                final_output_path,
                [tts_path],
                normalize_audio=True,
                tts_input_format=audio_format,
                output_format=audio_format,
                ffmpeg=ffmpeg,
            )
        elif audio_format in _REWRITE_HEADER_FORMATS:
            cmd = build_ffmpeg_command(
                final_output_path,
                [tts_path],
                normalize_audio=False,
                tts_input_format=audio_format,
                output_format=audio_format,
                ffmpeg=ffmpeg,
            )
        else:
            # Neither chime nor normalize, and a format that carries no
            # total length to correct: return the native bytes. Faster
            # than a no-op ffmpeg roundtrip and it keeps the original
            # encoder output byte for byte.
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
            # ASYNC221 reads the ``subprocess.run`` lexically and takes it
            # for a blocking call on the event loop. It is not: the lambda
            # is handed to the executor, which is the pattern the rule
            # exists to ask for.
            await hass.async_add_executor_job(
                lambda: subprocess.run(  # noqa: ASYNC221
                    cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
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
        # Remove the temp file we created; the
        # caller is responsible for it (issue: previous code unconditionally
        # deleted it, which silently broke any caller that passed a path).
        def cleanup_files():
            try:
                os.remove(tts_path)
                os.remove(final_output_path)
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
                if 'final_output_path' in locals():
                    os.remove(final_output_path)
            except OSError:
                pass

        await hass.async_add_executor_job(error_cleanup)

        _LOGGER.error("Error processing audio: %s", e)
        raise HomeAssistantError(f"Error processing audio: {e}") from e

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

async def set_media_player_volume(
    hass: HomeAssistant,
    entity_id: str,
    volume_level: float,
    force: bool = False,
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

    ``force`` skips the "already at target" shortcut. That shortcut
    trusts the reported level, and reported levels are not always
    current: a DLNA renderer whose event subscription never reached
    Home Assistant kept reporting the level it had before the
    announcement while the speaker itself sat at the announcement
    volume, so the restore decided there was nothing to do and left it
    there. A caller that knows it changed the volume passes ``force``
    and the shortcut is not consulted.
    """
    state, attributes = await get_media_player_state(hass, entity_id)
    if state is None or attributes is None:
        _LOGGER.debug("Media player %s state not available", entity_id)
        return False

    current_volume = attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
    if (
        not force
        and current_volume is not None
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

