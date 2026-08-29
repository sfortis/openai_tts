"""Loudness correction applied to a stream, without buffering it.

The atomic path hands ffmpeg a finished file, so it can use any filter
it likes. This module exists for the other path, where audio is
forwarded chunk by chunk as the provider produces it and there is no
finished file to hand anywhere.

An ffmpeg process is kept open for the life of the stream. Chunks are
written to its standard input by one task while another reads its
standard output, which is what keeps a large clip from deadlocking on a
full pipe buffer. The filter is the one defined in ``utils``, chosen
because it corrects continuously rather than computing a single offset
from the whole file, so it needs neither a second pass nor an end.

Only formats ffmpeg can read from a pipe without seeking are
supported. Callers ask with :func:`can_normalize_stream` first and fall
back to the atomic path when the answer is no.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterable

from .const import AUDIO_FORMAT_ENCODER

_LOGGER = logging.getLogger(__name__)

# How each format is described to ffmpeg on a pipe. A container whose
# header carries a length the writer cannot know in advance is not
# here: wav declares its size up front, and a streamed wav header
# claims a length that never matches, which ffmpeg reads as truncation.
_PIPE_ARGS: dict[str, list[str]] = {
    "mp3": ["-f", "mp3"],
    "pcm": ["-f", "s16le", "-ar", "24000", "-ac", "1"],
}

# Re-encoding is unavoidable once a filter is in the way, so the output
# has to be told what to aim for. Left to its own defaults ffmpeg
# rewrote a 128 kbps clip at 32 kbps, which is audible and was not
# something the user asked for. These are the same settings the atomic
# path uses, so a profile sounds the same whichever way it is produced.

# ffmpeg inspects the start of an input before it commits to a stream
# layout. On a file that costs nothing; on a stream arriving in real
# time it means waiting for audio that has not been spoken yet. Measured
# on a thirteen second clip fed at playback speed, the default probe
# held the first output byte back by 1039 ms, against 91 ms with these.
# Nothing is lost by shortening it here: the format and its parameters
# are stated explicitly on the command line, so there is nothing left to
# discover. Output bytes were identical either way.
_FAST_START = ["-probesize", "32", "-analyzeduration", "0"]

_READ_SIZE = 16384

# ffmpeg's diagnostics are drained continuously into this, and the tail
# is what gets reported. Draining matters more than the text: a pipe
# nobody reads fills up, and ffmpeg then blocks writing to it, which
# stops it reading its input and producing its output. Everything
# deadlocks over a message nobody wanted.
_STDERR_KEEP_BYTES = 4096

# How long to wait for ffmpeg to go away once it has been asked to.
_REAP_TIMEOUT_S = 5.0


def can_normalize_stream(audio_format: str) -> bool:
    """Whether this format can be corrected without buffering it."""
    return audio_format in _PIPE_ARGS


async def normalize_stream(
    source: AsyncIterable[bytes],
    audio_format: str,
    ffmpeg_bin: str,
    loudness_filter: str,
) -> AsyncGenerator[bytes, None]:
    """Yield ``source`` with its loudness corrected, still as a stream.

    Raises ``ValueError`` for a format that cannot be piped, and
    ``RuntimeError`` when ffmpeg exits non-zero. The caller should treat
    the latter like any other synthesis failure: what came out is
    incomplete and must not be cached.
    """
    if not can_normalize_stream(audio_format):
        raise ValueError(
            f"format {audio_format!r} cannot be filtered on a stream"
        )

    args = _PIPE_ARGS[audio_format]
    encoder = AUDIO_FORMAT_ENCODER[audio_format]
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin, "-hide_banner", "-loglevel", "error",
        *_FAST_START,
        *args, "-i", "pipe:0",
        "-af", loudness_filter,
        "-ac", "1", "-ar", "24000",
        *encoder["codec_args"],
        *args, "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    if stdin is None or stdout is None or stderr is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
        raise RuntimeError("ffmpeg was started without the pipes it needs")

    async def _feed() -> None:
        """Copy the source in, then close the pipe so ffmpeg finishes."""
        try:
            async for chunk in source:
                if not chunk:
                    continue
                stdin.write(chunk)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # ffmpeg went away early. Its exit code is what gets
            # reported, so there is nothing useful to add here.
            pass
        finally:
            # Close the provider's stream explicitly. Cancellation while
            # suspended in ``drain`` leaves the ``async for`` without
            # ever finalising it, so an HTTP response would stay open
            # until the garbage collector happened to notice.
            aclose = getattr(source, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
            with contextlib.suppress(
                BrokenPipeError, ConnectionResetError, RuntimeError
            ):
                stdin.close()
                await stdin.wait_closed()

    errors = bytearray()

    async def _drain_errors() -> None:
        """Keep ffmpeg's diagnostics moving, keeping only the tail."""
        while True:
            line = await stderr.readline()
            if not line:
                break
            errors.extend(line)
            if len(errors) > _STDERR_KEEP_BYTES:
                del errors[:-_STDERR_KEEP_BYTES]

    feeder = asyncio.create_task(_feed(), name="openai_tts loudness feeder")
    draining = asyncio.create_task(
        _drain_errors(), name="openai_tts loudness stderr"
    )
    try:
        while True:
            data = await stdout.read(_READ_SIZE)
            if not data:
                break
            yield data
        # Output is finished, so ffmpeg is on its way out. Both the
        # feeder and the exit code are checked here, in the normal flow,
        # rather than in the cleanup below: raising from cleanup would
        # replace whatever error actually stopped the stream, and would
        # also fire when the consumer simply walked away.
        await feeder
        await draining
        returncode = await proc.wait()
        if returncode != 0:
            detail = bytes(errors).decode(errors="replace").strip()
            raise RuntimeError(
                f"loudness filter failed (exit {returncode}): {detail[-300:]}"
            )
    finally:
        # Runs on success, on failure, and when the consumer abandons
        # the generator. It must never raise.
        # Kill first, then dispose of the helpers. Waiting on the feeder
        # while ffmpeg is wedged would mean waiting on ffmpeg, which is
        # exactly what cleanup must not do.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        for task in (feeder, draining):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if proc.returncode is None:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
