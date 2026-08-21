"""Sentence-level pipelining for streaming TTS.

Home Assistant hands a TTS entity an async generator of text rather than
a finished string, so that synthesis can start on the first sentence
while a conversation agent is still writing the rest. Draining that
generator first, which is what this integration did until now, throws
that away: nothing reaches the provider until the agent has finished,
and the user hears silence for as long as the agent takes to think.

``POST /v1/audio/speech`` takes ``input`` as one complete string and has
no way to append to an open request, so the only way to start early is
to split the text and issue more than one request. This module does the
splitting and stitches the resulting audio back into a single stream.

The approach follows what Home Assistant's own integrations do. Nabu
Casa Cloud (in ``hass_nabucasa.voice.process_tts_stream``) and ElevenLabs
both sit behind request-per-string APIs and both solve it the same way:
detect sentence boundaries with the ``sentence_stream`` package, run a
producer task that collects sentences while a consumer synthesises them,
and synthesise the first sentence alone so the first audio arrives as
early as possible.

Two things here are deliberately unlike ElevenLabs. It concatenates
whole MP3 responses back to back, which leaves several file headers
inside one stream; issue #64 showed a HomePod decoder rejecting a stream
over a single stray metadata frame, so that is a risk not worth taking.
Instead this module follows Nabu Casa and only handles container formats
it can join cleanly: raw PCM, which has no header at all, and WAV, whose
header is emitted once and stripped from every later response.

The caller supplies synthesis as a callback, so this module needs to
know nothing about the engine, options, caching or error handling.
"""
from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Callable
from typing import Any

from sentence_stream import SentenceBoundaryDetector

_LOGGER = logging.getLogger(__name__)

# Audio formats this module can join into one continuous stream.
#
# ``pcm`` is raw signed 16-bit samples with no container, so responses
# concatenate as-is.
#
# ``wav`` carries a RIFF header stating the frame count, and decoders
# honour it, so the header is rewritten once to say "length unknown".
# See ``_split_wav_header``.
#
# ``mp3`` is a bare sequence of MPEG frames, which is why appending one
# response to another works: measured on three real OpenAI responses,
# the joined file reads as exactly the sum of its parts. Metadata is the
# only hazard, and ``_strip_mp3_metadata`` removes it from every
# response after the first.
#
# ``opus`` and ``flac`` declare their total sample count up front and
# stop there, so a second response is silently ignored. ``aac`` loses
# samples at the seam. Those three are excluded, and the caller falls
# back to a single request for them.
PIPELINEABLE_FORMATS: frozenset[str] = frozenset({"pcm", "wav", "mp3"})

# How many sentences to put in each successive request.
#
# The first sentence goes alone so speech starts as early as possible.
# While it plays there is time to batch the next few, and after that
# every sentence already completed is sent together. This keeps requests
# fragmented only where latency is actually visible to the listener, and
# is the same schedule Nabu Casa and ElevenLabs settled on.
_SENTENCE_SCHEDULE: tuple[int, ...] = (1, 3)

# How long to wait, before the very first request, to see whether the
# text stream is already finished.
#
# The boundary detector needs to see text past a full stop before it
# will call it a sentence end, so a complete string yields its last
# sentence only from ``finish()``. Without this grace period the choice
# between "split it" and "send it whole" would come down to which
# coroutine the event loop happened to run first. That is a real
# correctness issue, not a tidiness one: ``openai_tts.say`` must keep
# issuing exactly one request. Paid once, and only when text is still
# arriving.
_COMPLETION_GRACE_S = 0.05

# A synthesis callback: given a piece of text, yield its audio bytes.
SynthesizeFn = Callable[[str], AsyncGenerator[bytes, None]]

# Somewhere to park the producer task. Passing the scheduler in keeps
# this module free of any Home Assistant import.
CreateTaskFn = Callable[[Awaitable[None], str], object]


_MP3_BITRATES_V1_L3 = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)
_MP3_BITRATES_V2_L3 = (
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0,
)
_MP3_SAMPLE_RATES = {
    3: (44100, 48000, 32000),   # MPEG 1
    2: (22050, 24000, 16000),   # MPEG 2
    0: (11025, 12000, 8000),    # MPEG 2.5
}


def _mpeg_frame_length(header: bytes) -> int | None:
    """Byte length of the MPEG Layer III frame starting at ``header``.

    Returns None when the four bytes are not a usable frame header. Only
    Layer III is handled, which is the only layer any TTS backend emits.
    """
    if len(header) < 4:
        return None
    if header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
        return None  # missing frame sync

    version_bits = (header[1] >> 3) & 0x03
    layer_bits = (header[1] >> 1) & 0x03
    if layer_bits != 0x01:
        return None  # not Layer III

    bitrate_index = (header[2] >> 4) & 0x0F
    rate_index = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01

    rates = _MP3_SAMPLE_RATES.get(version_bits)
    if rates is None or rate_index > 2:
        return None
    sample_rate = rates[rate_index]

    table = _MP3_BITRATES_V1_L3 if version_bits == 3 else _MP3_BITRATES_V2_L3
    bitrate = table[bitrate_index] * 1000
    if not bitrate or not sample_rate:
        return None  # free-format or reserved

    # MPEG 1 fits 1152 samples per frame, MPEG 2 and 2.5 fit 576.
    coefficient = 144 if version_bits == 3 else 72
    return (coefficient * bitrate // sample_rate) + padding


def _strip_mp3_metadata(data: bytes) -> bytes:
    """Remove container metadata so ``data`` is pure audio frames.

    Appending whole MP3 responses works because the format is just a run
    of frames, but each response may be wrapped in metadata that has no
    business appearing mid-stream:

    * An ID3v2 tag at the front. ffmpeg writes one, so any backend that
      re-encodes through ffmpeg produces them.
    * A Xing or Info frame as the first frame, holding the duration and
      seek table for the response it came with. Left in place it
      describes the wrong audio, and it was a stray frame of exactly
      this kind that broke decoding on a HomePod in issue #64.
    * An ID3v1 trailer in the last 128 bytes.

    OpenAI's own responses carry none of these, verified by inspection:
    they start directly with a frame sync. This is here for the
    self-hosted backends where that is not true.
    """
    start = 0
    end = len(data)

    if data[:3] == b"ID3" and len(data) >= 10:
        # Syncsafe integer: seven bits per byte.
        size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        start = min(size + 10, end)

    if end - start >= 128 and data[end - 128:end - 125] == b"TAG":
        end -= 128

    # A Xing/Info marker lives inside the first frame, a little past its
    # header. Skip the whole frame when one is there.
    first = data[start:start + 4]
    frame_len = _mpeg_frame_length(first)
    if frame_len and start + frame_len <= end:
        frame = data[start:start + frame_len]
        if b"Xing" in frame[:64] or b"Info" in frame[:64]:
            start += frame_len

    return data[start:end]


def _make_wav_header(rate: int, width: int, channels: int) -> bytes:
    """Build a WAV header advertising an unknown length.

    ``wave`` writes ``nframes = 0`` when nothing has been written to it,
    which is what a streamed WAV needs: the total length is not known
    when the header goes out. Players treat it as "read until the
    stream ends".
    """
    with io.BytesIO() as buf:
        writer = wave.open(buf, "wb")
        with writer:
            writer.setframerate(rate)
            writer.setsampwidth(width)
            writer.setnchannels(channels)
        return buf.getvalue()


def _split_wav_header(wav_bytes: bytes) -> tuple[bytes, bytes]:
    """Split one WAV response into (streamable header, raw frames)."""
    with io.BytesIO(wav_bytes) as buf:
        reader = wave.open(buf, "rb")
        with reader:
            return (
                _make_wav_header(
                    rate=reader.getframerate(),
                    width=reader.getsampwidth(),
                    channels=reader.getnchannels(),
                ),
                reader.readframes(reader.getnframes()),
            )


class _SentenceCollector:
    """Turns a text stream into sentences, in the background.

    The generator is consumed by a task of its own so that synthesis of
    an earlier sentence overlaps with the agent still producing later
    ones. ``ready`` is set whenever sentences become available and once
    more when the stream ends, so a waiting consumer always wakes up.
    ``finished`` is set only at the very end, which lets the consumer
    ask "is more text still coming?" without racing the producer.
    """

    def __init__(self, message_gen: AsyncIterable[str]) -> None:
        self._message_gen = message_gen
        self._detector = SentenceBoundaryDetector()
        self._sentences: list[str] = []
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.complete = False
        self.error: BaseException | None = None
        # The text exactly as it arrived. Rejoining detected sentences
        # would normalise whitespace, and the duration cache key is
        # built from the original message, so only the untouched string
        # hashes to the same value.
        self.raw_text = ""

    async def run(self) -> None:
        """Collect sentences until the text stream is exhausted."""
        try:
            async for chunk in self._message_gen:
                self.raw_text += chunk
                # Chunks arrive on token boundaries, not sentence ones,
                # so a chunk may hold several sentences or half of one.
                added = False
                for sentence in self._detector.add_chunk(chunk):
                    if sentence.strip():
                        self._sentences.append(sentence)
                        added = True
                if added:
                    self.ready.set()

            if trailing := self._detector.finish():
                if trailing.strip():
                    self._sentences.append(trailing)
        except asyncio.CancelledError:
            raise
        except BaseException as err:  # noqa: BLE001 - surfaced to consumer
            # Store rather than raise: this runs detached, so the
            # consumer is the only place that can report it.
            self.error = err
            _LOGGER.error("Text stream failed: %s", err, exc_info=True)
        finally:
            self.complete = True
            self.ready.set()
            self.finished.set()

    def take(self) -> list[str]:
        """Return the sentences collected so far and clear the buffer."""
        taken = self._sentences[:]
        self._sentences.clear()
        return taken


async def pipelined_audio_stream(
    message_gen: AsyncIterable[str],
    synthesize: SynthesizeFn,
    audio_format: str,
    create_task: CreateTaskFn,
    stats: dict[str, Any] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield one continuous audio stream, synthesising sentence by sentence.

    ``synthesize`` is called once per batch of sentences and must yield
    the audio bytes for the text it is given. It is responsible for
    validation, error classification and anything else engine-specific.

    When the whole text turns out to be available before the first
    request goes out, this collapses to a single ``synthesize`` call for
    the complete text. That is the normal case for ``openai_tts.say``,
    where Home Assistant wraps a plain string in a one-chunk generator,
    and it keeps that path byte-identical to not pipelining at all.

    ``audio_format`` must be in :data:`PIPELINEABLE_FORMATS`; the caller
    checks that before choosing this path.

    ``stats``, when given, is filled in as the run proceeds:
    ``requests`` counts synthesis calls, ``single_request`` says whether
    the whole text went out in one, and ``raw_text`` carries the text
    exactly as it arrived in that case. Callers need those to decide
    whether the emitted audio is a complete clip they can measure and
    cache, or several joined pieces whose total was never known in one
    place.
    """
    if audio_format not in PIPELINEABLE_FORMATS:
        raise ValueError(f"format {audio_format!r} cannot be pipelined")

    collector = _SentenceCollector(message_gen)
    producer = create_task(collector.run(), "openai_tts sentence collector")

    wav_header_sent = False
    schedule = list(_SENTENCE_SCHEDULE)
    first_batch = True
    requests = 0
    if stats is not None:
        stats["requests"] = 0
        stats["single_request"] = False

    async def _passthrough(text: str) -> AsyncGenerator[bytes, None]:
        """Forward one response untouched, as the only response.

        Safe only when nothing follows, and worth it because it keeps the
        response arriving chunk by chunk instead of being buffered.

        Buffering is what the joined path has to do, for two different
        reasons. A WAV header states the frame count and decoders honour
        it, so ffmpeg given a header claiming one second stops after one
        second and ignores whatever was appended; the header has to be
        rewritten, which needs the whole response. An MP3 response may
        be wrapped in metadata that has to be found and removed. Neither
        applies when there is only one response.
        """
        nonlocal wav_header_sent
        wav_header_sent = True
        async for chunk in synthesize(text):
            yield chunk

    async def _emit_joined(text: str) -> AsyncGenerator[bytes, None]:
        """Run one synthesis request and yield audio that can be joined."""
        nonlocal wav_header_sent
        if audio_format == "mp3":
            # Frames append cleanly, but any container metadata around
            # them must not land mid-stream, and finding it needs the
            # whole response. Bounded by one sentence.
            parts = [chunk async for chunk in synthesize(text)]
            yield _strip_mp3_metadata(b"".join(parts))
            return
        if audio_format == "wav":
            # The frame count has to be rewritten to "unknown", which
            # means parsing the header, which means having the whole
            # response. Buffering is bounded by one sentence rather than
            # the whole reply, and only the first sentence is on the
            # critical path for perceived latency.
            parts = [chunk async for chunk in synthesize(text)]
            header, frames = _split_wav_header(b"".join(parts))
            if not wav_header_sent:
                wav_header_sent = True
                yield header
            yield frames
            return
        # PCM has no header, so bytes can go out as they arrive.
        async for chunk in synthesize(text):
            yield chunk

    try:
        while True:
            await collector.ready.wait()

            if first_batch and not collector.complete:
                # Settle the "was the text complete all along?" question
                # before splitting anything. See _COMPLETION_GRACE_S.
                try:
                    await asyncio.wait_for(
                        collector.finished.wait(), _COMPLETION_GRACE_S
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    pass

            if not collector.complete:
                # Only clear while more text may still arrive, otherwise
                # the next wait would block forever.
                collector.ready.clear()

            pending = collector.take()
            if not pending:
                if collector.complete:
                    break
                continue

            if first_batch and collector.complete:
                # The entire text was ready before we sent anything, so
                # there is nothing to overlap with. One request for the
                # lot gives the provider full context and avoids any
                # seam in the audio.
                first_batch = False
                text = " ".join(pending).strip()
                if text:
                    requests += 1
                    if stats is not None:
                        stats["requests"] = requests
                        stats["single_request"] = True
                        stats["raw_text"] = collector.raw_text
                    _LOGGER.debug(
                        "Text was complete up front; single request for "
                        "%d sentence(s)", len(pending),
                    )
                    async for chunk in _passthrough(text):
                        yield chunk
                continue

            first_batch = False
            while pending:
                if schedule:
                    take = schedule.pop(0)
                    batch = pending[:take]
                    pending = pending[take:]
                else:
                    batch = pending
                    pending = []

                text = " ".join(batch).strip()
                if not text:
                    continue
                requests += 1
                if stats is not None:
                    stats["requests"] = requests
                    stats["single_request"] = False
                _LOGGER.debug(
                    "Synthesising batch of %d sentence(s) while text "
                    "continues to arrive", len(batch),
                )
                async for chunk in _emit_joined(text):
                    yield chunk

        if collector.error is not None:
            raise collector.error

        if audio_format == "wav" and not wav_header_sent:
            # Nothing was synthesised at all. Emit a bare header so the
            # consumer gets a valid, empty WAV rather than zero bytes,
            # which downstream ffmpeg treats as corrupt.
            _LOGGER.warning("Text stream produced no sentences")
            yield _make_wav_header(rate=24000, width=2, channels=1)

        _LOGGER.info(
            "Pipelined stream complete: %d synthesis request(s)", requests
        )
    finally:
        cancel = getattr(producer, "cancel", None)
        if callable(cancel):
            cancel()
