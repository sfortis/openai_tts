"""MP3 ID3 metadata helpers.

Embeds the TTS audio duration directly into the cached MP3 file as a custom
ID3 ``TXXX`` frame. Home Assistant's TTS cache only stores the raw bytes,
so without metadata embedded inside the file itself there is no way to
recover the original duration on a cache hit.
"""
from __future__ import annotations

import logging
import os
import tempfile

_LOGGER = logging.getLogger(__name__)

DURATION_METADATA_KEY = "tts_duration_ms"


def embed_duration_in_audio(audio_data: bytes, duration_ms: int) -> bytes:
    """Return ``audio_data`` with ``duration_ms`` written into an ID3 TXXX frame.

    Falls back to returning the original bytes unchanged if mutagen is not
    installed or the file cannot be parsed.
    """
    try:
        from mutagen.id3 import TXXX
        from mutagen.mp3 import MP3
    except ImportError:
        _LOGGER.warning("mutagen not available, skipping metadata embedding")
        return audio_data

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio_data)
        tmp_path = f.name

    try:
        try:
            audio = MP3(tmp_path)
        except Exception as e:
            _LOGGER.debug("Failed to open MP3 for metadata: %s", e)
            return audio_data

        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                # Tags may already exist in a non-ID3v2 form; skip silently.
                pass

        if audio.tags is not None:
            audio.tags.delall(f"TXXX:{DURATION_METADATA_KEY}")
            audio.tags.add(
                TXXX(encoding=3, desc=DURATION_METADATA_KEY, text=str(duration_ms))
            )
            audio.save()
            _LOGGER.debug("Embedded duration %d ms in audio metadata", duration_ms)

        with open(tmp_path, "rb") as f:
            return f.read()

    except Exception as e:
        _LOGGER.warning("Failed to embed metadata: %s", e)
        return audio_data
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
