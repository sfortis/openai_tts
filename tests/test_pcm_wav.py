"""Tests for the WAV header's length field.

This module imports nothing from Home Assistant, so it can be tested on
a plain checkout with pytest alone.
"""
import asyncio
import struct
import sys
import wave
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from pcm_wav import PCM_SAMPLE_RATE, pcm_as_wav, wav_header

UNKNOWN = 0xFFFFFFFF


def _sizes(header: bytes) -> tuple[int, int]:
    """The RIFF size and the data chunk size a header declares."""
    return struct.unpack_from("<I", header, 4)[0], struct.unpack_from("<I", header, 40)[0]


def _collect(chunks) -> bytes:
    async def source():
        for chunk in chunks:
            yield chunk

    async def run():
        return b"".join([c async for c in pcm_as_wav(source())])

    return asyncio.run(run())


def test_stream_header_marks_the_length_unknown():
    """A stream's header goes out before the rest of the audio exists."""
    assert _sizes(wav_header()) == (UNKNOWN, UNKNOWN)


def test_strict_reader_gets_the_real_length():
    """Python's ``wave`` believes a known size, like Safari and iOS do."""
    audio = b"\x00\x00" * int(PCM_SAMPLE_RATE * 1.5)
    clip = wav_header(data_size=len(audio)) + audio
    with wave.open(BytesIO(clip)) as reader:
        assert reader.getnframes() == 36000


def test_empty_stream_writes_nothing():
    assert _collect([]) == b""
