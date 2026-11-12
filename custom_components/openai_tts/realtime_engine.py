"""Synthesis through the OpenAI Realtime API.

The engine offers the same two calls as ``OpenAITTSEngine``, so the
entity does not need to know which one it holds. Only pcm can be
produced, because the Realtime API has no compressed output format, and
the preset restricts the audio format to it.

Each request opens its own session and closes it after the reply.
Keeping a session open between announcements would save the handshake,
about a second, but a session keeps the conversation, so the next text
would be read with the previous ones as context.

The events themselves are built and read in ``realtime_protocol``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
from typing import AsyncGenerator, Optional

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .exceptions import (
    OpenAIAuthError,
    OpenAIInvalidResponseError,
    OpenAINetworkError,
    OpenAIQuotaExceededError,
    OpenAIRateLimitError,
    OpenAIServerError,
    OpenAITTSError,
)
from .openaitts_engine import (
    INITIAL_BUFFER_BYTES,
    MAX_RETRIES,
    RETRY_DELAY_SECONDS,
    AudioResponse,
    _classify_http_error,
    _is_retryable,
)
from .realtime_protocol import (
    ERROR_AUTH,
    ERROR_QUOTA,
    ERROR_RATE_LIMIT,
    ERROR_SERVER,
    build_request_events,
    clamp_speed,
    error_kind,
    parse_event,
    redact_keys,
    session_url,
    transcript_matches,
)

_LOGGER = logging.getLogger(__name__)

# The longest wait for the WebSocket handshake.
CONNECT_TIMEOUT_SECONDS = 15

# The longest wait from sending the request to the first audio. Without
# it, a server that kept sending other events would never time out.
FIRST_AUDIO_TIMEOUT_SECONDS = 30

# The longest wait for the next server event once audio flows. The reply
# arrives in deltas of at most 19 KB, generated about eight times faster
# than it plays, so a healthy session never comes close.
#
# There is no limit on the session as a whole. Home Assistant reads a
# stream at its own pace, and the server's events wait in the socket
# meanwhile, so a long announcement legitimately keeps its session open
# for as long as it takes to play.
RECEIVE_TIMEOUT_SECONDS = 30

# The blocking path has to receive the whole reply before it returns.
# Measured on 2026-11-11, 964 characters took 8.5 seconds, handshake
# included. This allows a base for the handshake and a slow start, plus
# one second for every 100 characters, about three times the measured
# rate: 71 seconds for the longest text the preset accepts.
_BLOCKING_BASE_SECONDS = 30
_BLOCKING_CHARS_PER_SECOND = 100

# How much of the text and the transcript a mismatch warning shows.
_LOG_TEXT_CHARS = 200


def _error_from_server(error: dict) -> OpenAITTSError:
    """Map a server error object to the integration's exception class."""
    kind = error_kind(error)
    if kind == ERROR_AUTH:
        # The message echoes the start of the key back, so it is left
        # out, as the speech engine does for HTTP 401.
        return OpenAIAuthError(
            "Realtime authentication failed. Check your API key."
        )
    message = redact_keys(
        str(error.get("message") or error.get("code") or "unknown error")
    )
    if kind == ERROR_QUOTA:
        return OpenAIQuotaExceededError(f"Realtime quota exhausted: {message}")
    if kind == ERROR_RATE_LIMIT:
        return OpenAIRateLimitError(f"Realtime rate limit hit: {message}")
    if kind == ERROR_SERVER:
        return OpenAIServerError(f"Realtime server error: {message}")
    return OpenAITTSError(f"Realtime request refused: {message}")


class OpenAIRealtimeEngine:
    """Reads a text aloud through one Realtime session per request."""

    def __init__(
        self,
        api_key: str,
        voice: str,
        model: str,
        speed: float,
        url: str,
        hass: Optional[HomeAssistant] = None,
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = url
        self._hass = hass

    @staticmethod
    def blocking_timeout_s(text: str) -> float:
        """How long the blocking path may take to synthesise ``text``."""
        return _BLOCKING_BASE_SECONDS + len(text) / _BLOCKING_CHARS_PER_SECOND

    def get_tts(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None,
        response_format: str = "pcm",
        send_voice: bool = True,
    ) -> AudioResponse:
        """Blocking request, called by the entity from an executor thread.

        The session runs on Home Assistant's event loop, and this thread
        waits for its result. ``extra_payload`` and ``send_voice`` are
        accepted for the shared signature and have no meaning here.
        """
        if self._hass is None:
            raise OpenAITTSError("The Realtime engine needs Home Assistant")
        future = asyncio.run_coroutine_threadsafe(
            self._async_collect(text, response_format, speed, voice, model, instructions),
            self._hass.loop,
        )
        try:
            audio = future.result(timeout=self.blocking_timeout_s(text))
        except concurrent.futures.TimeoutError as err:
            future.cancel()
            raise OpenAINetworkError("Realtime request timed out") from err
        return AudioResponse(audio)

    async def _async_collect(
        self,
        text: str,
        response_format: str,
        speed: float | None,
        voice: str | None,
        model: str | None,
        instructions: str | None,
    ) -> bytes:
        chunks = [
            chunk
            async for chunk in self.async_get_tts_stream(
                text,
                response_format=response_format,
                speed=speed,
                voice=voice,
                model=model,
                instructions=instructions,
            )
        ]
        return b"".join(chunks)

    async def async_get_tts_stream(
        self,
        text: str,
        response_format: str = "pcm",
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None,
        send_voice: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Yield the pcm of ``text`` as the server sends it.

        A failure before the first audio is retried once when it is
        transient, the same rule the speech engine applies. Nothing is
        yielded until ``INITIAL_BUFFER_BYTES`` have arrived, so the
        entity's first chunk check sees real audio.
        """
        if response_format != "pcm":
            raise OpenAITTSError(
                f"The Realtime API produces pcm only, not {response_format}"
            )
        model = model or self._model
        voice = voice or self._voice
        speed = self._speed if speed is None else speed
        sent_speed = clamp_speed(speed)
        if sent_speed != speed:
            _LOGGER.debug(
                "Speed %s is outside what the Realtime API accepts, sending %s",
                speed, sent_speed,
            )
        events = build_request_events(text, voice, sent_speed, instructions)
        _LOGGER.debug(
            "Realtime request: model=%s, voice=%s, speed=%s", model, voice, sent_speed
        )

        attempt = 0
        while True:
            yielded = False
            try:
                # Closed explicitly, so that a consumer that stops early
                # also closes the socket at once, rather than when the
                # abandoned generator is finalised. The server keeps
                # generating, and billing, until the socket closes.
                async with contextlib.aclosing(
                    self._run_session(model, events, text)
                ) as chunks:
                    async for chunk in chunks:
                        yielded = True
                        yield chunk
                return
            except OpenAITTSError as err:
                if yielded or not _is_retryable(err) or attempt >= MAX_RETRIES:
                    raise
                _LOGGER.warning(
                    "Realtime request failed on attempt %d/%d (retryable): %s",
                    attempt + 1, MAX_RETRIES + 1, err,
                )
                attempt += 1
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    async def _run_session(
        self, model: str, events: list[dict], text: str
    ) -> AsyncGenerator[bytes, None]:
        """Open one session, send ``events`` and yield the audio."""
        if self._hass is not None:
            session = async_get_clientsession(self._hass)
            owns_session = False
        else:
            session = aiohttp.ClientSession()
            owns_session = True

        try:
            try:
                ws = await asyncio.wait_for(
                    session.ws_connect(
                        session_url(self._url, model),
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    ),
                    CONNECT_TIMEOUT_SECONDS,
                )
            except aiohttp.WSServerHandshakeError as err:
                raise _classify_http_error(err.status, str(err.message)) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise OpenAINetworkError(
                    f"Cannot open the Realtime session: {err}"
                ) from err

            async with ws:
                for event in events:
                    await ws.send_str(json.dumps(event))
                async for chunk in self._read_reply(ws, text):
                    yield chunk
        finally:
            if owns_session:
                await session.close()

    async def _read_reply(
        self, ws: aiohttp.ClientWebSocketResponse, text: str
    ) -> AsyncGenerator[bytes, None]:
        """Yield the audio of the reply, then check its transcript."""
        buffered: list[bytes] = []
        buffered_size = 0
        flushed = False
        transcript: list[str] = []
        total = 0
        loop = asyncio.get_running_loop()
        first_audio_deadline = loop.time() + FIRST_AUDIO_TIMEOUT_SECONDS

        while True:
            timeout = RECEIVE_TIMEOUT_SECONDS
            if total == 0:
                timeout = min(timeout, first_audio_deadline - loop.time())
                if timeout <= 0:
                    raise OpenAINetworkError("The Realtime reply did not start in time")
            try:
                message = await ws.receive(timeout=timeout)
            except TimeoutError as err:
                raise OpenAINetworkError("The Realtime server stopped answering") from err
            if message.type != aiohttp.WSMsgType.TEXT:
                raise OpenAINetworkError(
                    f"The Realtime session closed before the reply ended ({message.type.name})"
                )
            try:
                event = parse_event(json.loads(message.data))
            except (ValueError, TypeError) as err:
                raise OpenAIInvalidResponseError(
                    f"Unreadable Realtime event: {err}"
                ) from err

            if event.kind == "error":
                raise _error_from_server(event.error or {})
            if event.kind == "transcript":
                transcript.append(event.text)
                continue
            if event.kind == "audio":
                total += len(event.audio)
                if flushed:
                    yield event.audio
                    continue
                buffered.append(event.audio)
                buffered_size += len(event.audio)
                if buffered_size >= INITIAL_BUFFER_BYTES:
                    flushed = True
                    yield b"".join(buffered)
                    buffered = []
                continue
            if event.kind == "done":
                break

        if buffered:
            yield b"".join(buffered)
        if total == 0:
            raise OpenAIInvalidResponseError("The Realtime reply had no audio")

        spoken = "".join(transcript)
        if not transcript_matches(text, spoken):
            # The warning leaves the words out, because an announcement
            # can carry private content and warnings reach the log
            # without debug logging. The words follow at debug level.
            _LOGGER.warning(
                "The Realtime model did not read the text word for word: "
                "%d words in the text, %d spoken. Turn on debug logging "
                "to see both",
                len(text.split()), len(spoken.split()),
            )
            _LOGGER.debug(
                "Text: %r. Spoken: %r",
                text[:_LOG_TEXT_CHARS], spoken[:_LOG_TEXT_CHARS],
            )
        _LOGGER.debug("Realtime reply complete: %d bytes of pcm", total)
