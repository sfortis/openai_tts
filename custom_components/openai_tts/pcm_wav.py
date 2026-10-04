"""Deliver raw PCM to Home Assistant as streamable WAV.

This module imports nothing from Home Assistant, so it can be tested on
a plain checkout with pytest alone.

Some providers only speak raw PCM (OpenRouter's Gemini TTS refuses
anything else). Raw PCM has no header, so nothing downstream can tell
its sample rate, width or channel count. Home Assistant converts TTS
audio with ``ffmpeg -f <extension>``, and ffmpeg has no input format
called ``pcm``, so every conversion failed with "Unknown input format:
'pcm'" and Assist satellites (which always ask for WAV) stayed silent.

Putting a WAV header in front of the samples fixes that: ``wav`` is a
format ffmpeg reads, and the header carries the layout. The sizes are
set to 0xFFFFFFFF, the usual marker for a WAV stream of unknown length,
because the header goes out before the rest of the audio exists.
"""
from __future__ import annotations

import struct
from collections.abc import AsyncGenerator, AsyncIterable

# The layout OpenAI documents for ``response_format=pcm``, which the
# ffmpeg encoder settings in const.py also produce for post-processed audio.
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2  # bytes, signed 16-bit little-endian

_UNKNOWN_SIZE = 0xFFFFFFFF


def wav_header(
    sample_rate: int = PCM_SAMPLE_RATE,
    channels: int = PCM_CHANNELS,
    sample_width: int = PCM_SAMPLE_WIDTH,
) -> bytes:
    """Return a 44-byte PCM WAV header for a stream of unknown length."""
    block_align = channels * sample_width
    byte_rate = sample_rate * block_align
    return (
        b"RIFF"
        + struct.pack("<I", _UNKNOWN_SIZE)
        + b"WAVE"
        + b"fmt "
        + struct.pack(
            "<IHHIIHH",
            16,  # fmt chunk size
            1,  # audio format: integer PCM
            channels,
            sample_rate,
            byte_rate,
            block_align,
            sample_width * 8,
        )
        + b"data"
        + struct.pack("<I", _UNKNOWN_SIZE)
    )


def delivery_format(audio_format: str) -> str:
    """Return the container Home Assistant is told about for ``audio_format``."""
    return "wav" if audio_format == "pcm" else audio_format


async def pcm_as_wav(chunks: AsyncIterable[bytes]) -> AsyncGenerator[bytes, None]:
    """Yield a WAV header, then the raw PCM chunks unchanged.

    The header is sent only once the first chunk arrives, so a stream that
    fails before producing audio still fails without writing anything.
    """
    header_sent = False
    async for chunk in chunks:
        if not header_sent:
            yield wav_header()
            header_sent = True
        yield chunk