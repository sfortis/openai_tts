"""Deliver audio to Home Assistant in a container its ffmpeg can read.

HA converts TTS audio with ``ffmpeg -f <extension>``. Two formats here
aren't ffmpeg input formats:

* ``pcm``: ffmpeg has no ``pcm`` input, so satellites got silence; a
  WAV header (a real input format) fixes that.
* ``opus``: ffmpeg writes ``opus`` but can't read it back. The audio is
  already Ogg Opus, which ffmpeg reads as ``ogg``.

A stream's WAV header ships before the length is known, so it's marked
unknown (0xFFFFFFFF); a known clip carries its real size, since a
placeholder there misreads a short clip as hours long (issue #68).
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
# Header bytes after the RIFF size field: "WAVE" + fmt chunk + data chunk header.
_RIFF_OVERHEAD = 36

# Extension to report when the format's own name isn't an ffmpeg input format.
_DELIVERY_FORMATS = {"pcm": "wav", "opus": "ogg"}


def wav_header(
    sample_rate: int = PCM_SAMPLE_RATE,
    channels: int = PCM_CHANNELS,
    sample_width: int = PCM_SAMPLE_WIDTH,
    *,
    data_size: int | None = None,
) -> bytes:
    """Return a 44-byte WAV header.

    Omit ``data_size`` to mark the length unknown, as on a stream. A size
    too large for the 32-bit fields is marked unknown too.
    """
    if data_size is None or not 0 <= data_size <= _UNKNOWN_SIZE - _RIFF_OVERHEAD:
        riff_size = data_chunk_size = _UNKNOWN_SIZE
    else:
        riff_size = _RIFF_OVERHEAD + data_size
        data_chunk_size = data_size
    block_align = channels * sample_width
    byte_rate = sample_rate * block_align
    return (
        b"RIFF"
        + struct.pack("<I", riff_size)
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
        + struct.pack("<I", data_chunk_size)
    )


def delivery_format(audio_format: str) -> str:
    """Return the extension Home Assistant is told about for ``audio_format``."""
    return _DELIVERY_FORMATS.get(audio_format, audio_format)


async def pcm_as_wav(
    chunks: AsyncIterable[bytes], data_size: int | None = None
) -> AsyncGenerator[bytes, None]:
    """Yield a WAV header, then the raw PCM chunks unchanged.

    ``data_size`` is forwarded to ``wav_header``.

    The header is sent only once the first chunk arrives, so a stream that
    fails before producing audio still fails without writing anything.
    """
    header_sent = False
    async for chunk in chunks:
        if not header_sent:
            yield wav_header(data_size=data_size)
            header_sent = True
        yield chunk
