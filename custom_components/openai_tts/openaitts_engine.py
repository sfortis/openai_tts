"""TTS Engine for OpenAI TTS with optional streaming support.

The engine provides two parallel call paths:

- ``get_tts()``: blocking, called via an executor by the legacy
  ``async_get_tts_audio()`` HA TTS contract. Always reads the full
  audio body inside the executor so the event loop never blocks on
  socket I/O.
- ``async_get_tts_stream()``: native async generator used by HA 2025.7+
  streaming TTS contract. Reuses HA's shared aiohttp session and
  retries pre-stream errors (connect resets, 5xx, true 429) once.

Both paths share a single ``_RequestBuilder`` for header/payload assembly
and a single ``_classify_http_error()`` for status-to-exception mapping,
so error handling stays consistent across them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import binascii
import base64
import time
from asyncio import CancelledError
from typing import AsyncGenerator, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .exceptions import (
    OpenAIAuthError,
    OpenAINetworkError,
    OpenAIQuotaExceededError,
    OpenAIRateLimitError,
    OpenAIServerError,
    OpenAITTSError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
STREAMING_TIMEOUT_SECONDS = 60
INITIAL_BUFFER_BYTES = 1024


def _classify_http_error(status: int, body_snippet: str = "") -> OpenAITTSError:
    """Map an HTTP status (and optional body) to a typed exception."""
    if status in (401, 403):
        return OpenAIAuthError(f"Authentication failed (HTTP {status})")
    if status == 402:
        return OpenAIQuotaExceededError(
            f"OpenAI account balance/quota exhausted (HTTP {status})"
        )
    if status == 429:
        # OpenAI returns 429 for BOTH true rate limits and out-of-credits.
        # The body's `insufficient_quota` marker disambiguates them.
        if "insufficient_quota" in body_snippet:
            return OpenAIQuotaExceededError(
                "OpenAI account quota exhausted (HTTP 429 insufficient_quota)"
            )
        return OpenAIRateLimitError(f"Rate limit hit (HTTP {status})")
    if status >= 500:
        return OpenAIServerError(f"OpenAI server error (HTTP {status})")
    return OpenAITTSError(f"OpenAI API error (HTTP {status})")


def _is_retryable(exc: BaseException) -> bool:
    """Auth/quota errors will fail again immediately, so don't waste a retry."""
    if isinstance(exc, (OpenAIAuthError, OpenAIQuotaExceededError)):
        return False
    if isinstance(exc, (OpenAIRateLimitError, OpenAIServerError, OpenAINetworkError)):
        return True
    if isinstance(exc, (URLError, aiohttp.ClientError)):
        return True
    return False


class _RequestBuilder:
    """Assembles HTTP headers + JSON payload for an OpenAI TTS request.

    Lives in its own class so the sync and async engine paths can share
    the same defaults-merge and ``extra_payload`` logic without drifting.
    """

    def __init__(
        self,
        api_key: str,
        default_voice: str,
        default_model: str,
        default_speed: float,
    ) -> None:
        self._api_key = api_key
        self._default_voice = default_voice
        self._default_model = default_model
        self._default_speed = default_speed

    def build(
        self,
        text: str,
        response_format: str,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        instructions: Optional[str] = None,
        extra_payload: Optional[str] = None,
        stream: bool = False,
    ) -> tuple[dict[str, str], dict[str, object]]:
        """Return (headers, payload) for an OpenAI TTS request."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "HomeAssistant-OpenAI-TTS",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload: dict[str, object] = {
            "model": model or self._default_model,
            "input": text,
            "voice": voice or self._default_voice,
            "response_format": response_format,
            "speed": speed if speed is not None else self._default_speed,
        }
        if instructions is not None:
            payload["instructions"] = instructions
            
        if stream:
            payload["stream_format"] = "sse"
            headers["Accept"] = "text/event-stream"

        if extra_payload:
            try:
                extra = json.loads(extra_payload)
            except json.JSONDecodeError as e:
                # Surface configuration errors as typed exceptions so the
                # caller sees a real failure (HomeAssistantError + sensor
                # update) instead of a quiet warning that the user might
                # never notice. Silent fallback hid bugs in custom-backend
                # configs where the user thought their params were sent.
                raise OpenAITTSError(
                    f"Invalid extra_payload JSON: {e}"
                ) from e
            if not isinstance(extra, dict):
                raise OpenAITTSError(
                    "extra_payload must be a JSON object, "
                    f"got {type(extra).__name__}"
                )
            payload.update(extra)
            _LOGGER.debug("Merged extra payload keys: %s", list(extra.keys()))

        return headers, payload


class AudioResponse:
    """Wraps a complete audio payload returned by ``get_tts()``.

    Kept as a thin wrapper rather than returning raw ``bytes`` so callers
    can dispatch on a stable shape (``response.content``) regardless of
    whether the engine grows alternative response variants in the future.
    """

    def __init__(self, content: bytes) -> None:
        self.content = content


class OpenAITTSEngine:
    """OpenAI TTS API client.

    Raises typed ``OpenAITTSError`` subclasses (see ``exceptions.py``)
    on every failure so callers can handle each mode (auth / quota /
    rate-limit / server / unknown) distinctly.
    """

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
        self._builder = _RequestBuilder(api_key, voice, model, speed)

    async def _iter_sse_audio(
        self,
        response: aiohttp.ClientResponse,
    ) -> AsyncGenerator[bytes, None]:
        """Parse an OpenAI SSE speech stream and yield decoded audio bytes.

        HTTP chunk boundaries are deliberately ignored. aiohttp may return
        arbitrary fragments of an SSE event, so this method buffers until a
        complete SSE event is received.

        Expected events:

            data: {"type":"speech.audio.delta","audio":"<base64>"}

            data: {"type":"speech.audio.done",...}

            data: [DONE]

        Only ``speech.audio.delta`` events produce output bytes.
        ``[DONE]`` terminates the stream.
        """

        buffer = bytearray()
        saw_done = False

        async for raw_chunk in response.content.iter_any():
            if not raw_chunk:
                continue

            buffer.extend(raw_chunk)

            while True:
                # SSE permits CRLF or LF line endings. Look for either
                # representation of the blank line separating events.
                separator = buffer.find(b"\r\n\r\n")

                if separator >= 0:
                    event_bytes = bytes(buffer[:separator])
                    del buffer[:separator + 4]
                else:
                    separator = buffer.find(b"\n\n")

                    if separator < 0:
                        break

                    event_bytes = bytes(buffer[:separator])
                    del buffer[:separator + 2]

                # Normalize CRLF so the event parser only needs to deal
                # with one line-ending convention.
                event_bytes = event_bytes.replace(b"\r\n", b"\n")

                data_lines: list[bytes] = []

                for line in event_bytes.split(b"\n"):
                    # Ignore SSE comments / empty lines.
                    if not line or line.startswith(b":"):
                        continue

                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].lstrip())

                if not data_lines:
                    continue

                # SSE allows multiple data: lines in one event. The SSE
                # protocol joins them with a newline.
                data = b"\n".join(data_lines).strip()

                if data == b"[DONE]":
                    saw_done = True
                    return

                try:
                    event = json.loads(data)
                except json.JSONDecodeError as err:
                    raise OpenAITTSError(
                        f"Invalid SSE event from TTS server: {data[:500]!r}"
                    ) from err

                event_type = event.get("type")

                if event_type == "speech.audio.delta":
                    audio_b64 = event.get("audio")

                    if not audio_b64:
                        _LOGGER.debug(
                            "Received speech.audio.delta without audio data"
                        )
                        continue

                    try:
                        audio_bytes = base64.b64decode(
                            audio_b64,
                            validate=True,
                        )
                    except (ValueError, binascii.Error) as err:
                        raise OpenAITTSError(
                            "Invalid base64 audio in SSE response"
                        ) from err

                    if audio_bytes:
                        yield audio_bytes

                elif event_type == "speech.audio.done":
                    # Do NOT terminate here.
                    #
                    # OpenAI-style SSE streams can subsequently send
                    # `data: [DONE]`, which is the actual stream terminator.
                    _LOGGER.debug(
                        "Received speech.audio.done; waiting for [DONE]"
                    )
                    continue

                elif event_type in {
                    "error",
                    "speech.error",
                    "response.error",
                }:
                    error = event.get("error", event)

                    raise OpenAITTSError(
                        f"TTS streaming API error: {error}"
                    )

                else:
                    # Unknown events are deliberately ignored. This keeps
                    # the integration forward-compatible with additional
                    # OpenAI streaming event types.
                    _LOGGER.debug(
                        "Ignoring SSE event type: %s",
                        event_type,
                    )

        # If the HTTP connection closes without [DONE], the stream was
        # incomplete. Do not silently report successful completion.
        if not saw_done:
            raise OpenAITTSError(
                "TTS SSE stream ended before receiving data: [DONE]"
            )

    def get_tts(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None,
        response_format: str = "mp3",
    ) -> AudioResponse:
        """Blocking TTS request. Must be invoked via an executor.

        Always reads the full body INSIDE the executor (no lazy/streaming
        variant). The previous lazy ``StreamingAudioResponse`` would have
        the executor open the socket but defer reads to the event-loop
        caller, which then blocked the loop on socket I/O - exactly the
        thing run_in_executor is supposed to prevent.

        Raises:
            OpenAIAuthError: 401/403. Caller should trigger reauth.
            OpenAIQuotaExceededError: 402, or 429 with insufficient_quota.
            OpenAIRateLimitError: 429 due to true rate limiting.
            OpenAIServerError: 5xx (will retry once before raising).
            OpenAITTSError: other failures.
        """
        headers, payload = self._builder.build(
            text=text,
            response_format=response_format,
            speed=speed,
            voice=voice,
            model=model,
            instructions=instructions,
            extra_payload=extra_payload,
        )
        _LOGGER.debug(
            "TTS API request: model=%s, voice=%s, speed=%s",
            payload["model"], payload["voice"], payload["speed"],
        )

        max_retries = 1
        attempt = 0
        while True:
            try:
                req = Request(
                    self._url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:
                    return AudioResponse(resp.read())

            except CancelledError:
                _LOGGER.debug("TTS request cancelled")
                raise

            except HTTPError as http_err:
                body_snippet = ""
                try:
                    body_snippet = http_err.read(2048).decode("utf-8", errors="replace")
                except Exception:
                    pass
                classified = _classify_http_error(http_err.code, body_snippet)
                _LOGGER.error(
                    "OpenAI TTS HTTP %s on attempt %d: %s",
                    http_err.code, attempt + 1, classified,
                )
                if not _is_retryable(classified) or attempt >= max_retries:
                    raise classified from http_err
                attempt += 1
                time.sleep(1)
                continue

            except URLError as net_err:
                _LOGGER.error(
                    "Network error fetching TTS audio (attempt %d): %s",
                    attempt + 1, net_err,
                )
                if attempt >= max_retries:
                    raise OpenAINetworkError(
                        f"Network error fetching TTS audio: {net_err}"
                    ) from net_err
                attempt += 1
                time.sleep(1)
                continue

            except OpenAITTSError:
                raise

            except Exception as exc:
                _LOGGER.error(
                    "Unknown error fetching TTS audio (attempt %d): %s",
                    attempt + 1, exc,
                )
                if attempt >= max_retries:
                    raise OpenAITTSError(
                        f"Unknown error fetching TTS audio: {exc}"
                    ) from exc
                attempt += 1
                time.sleep(1)

    async def async_get_tts_stream(
        self,
        text: str,
        response_format: str = "mp3",
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream TTS audio from the OpenAI API.

        ``response_format`` defaults to ``mp3`` because that's what HA's
        TTS proxy + Chromecast handle most reliably. Opus has known
        receiver-side compatibility issues on older cast hardware,
        which is why the entity always passes ``mp3`` explicitly - this
        default just makes the engine safe to call directly.

        Error responses are classified BEFORE any chunk is yielded,
        so a failed request can never leak partial bytes into the HA
        TTS cache (see issue #64).
        
        ##NEW##
        The backend is requested with ``stream_format=sse``. HTTP transport
        chunks are not treated as audio boundaries; the SSE stream is parsed
        until ``data: [DONE]``. Each ``speech.audio.delta`` event is base64
        decoded and yielded as raw audio bytes to Home Assistant.
        
        """
        headers, payload = self._builder.build(
            text=text,
            response_format=response_format,
            speed=speed,
            voice=voice,
            model=model,
            instructions=instructions,
            extra_payload=extra_payload,
            stream=True,
        )
        _LOGGER.debug(
            "Streaming TTS API request: model=%s, voice=%s, speed=%s, format=%s",
            payload["model"], payload["voice"], payload["speed"], response_format,
        )

        # Reuse HA's shared aiohttp session so we get connection
        # pooling and DNS reuse across calls. Spinning up a new
        # ClientSession per request (the old behaviour) defeated both
        # and added per-call setup cost. Falling back to a one-shot
        # session keeps the engine usable from contexts without an HA
        # instance (tests, eventually CLI), even though that's slower.
        if self._hass is not None:
            session = async_get_clientsession(self._hass)
            owns_session = False
        else:
            session = aiohttp.ClientSession()
            owns_session = True

        max_retries = 1

        try:
            response = await self._open_stream_with_retries(
                session, payload, headers, max_retries
            )
            try:
                _LOGGER.debug(
                    "Response content type: %s",
                    response.headers.get("Content-Type", ""),
                )

                audio_chunks_received = 0
                total_audio_bytes = 0

                try:
                    async for audio_chunk in self._iter_sse_audio(response):
                        if not audio_chunk:
                            continue
                        audio_chunks_received += 1
                        total_audio_bytes += len(audio_chunk)
                        yield audio_chunk

                except asyncio.CancelledError:
                    _LOGGER.warning(
                        "Streaming cancelled after %d audio chunks (%d bytes)",
                        audio_chunks_received,
                        total_audio_bytes,
                    )
                    raise
                    
                _LOGGER.debug(
                    "Finished streaming: %d audio chunks, %d total audio bytes",
                    audio_chunks_received,
                    total_audio_bytes,
                )
            finally:
                # Always release the response - covers cancellation and
                # any exception during chunk iteration. aiohttp leaves
                # the underlying socket connected to the pool only if
                # release() runs.
                response.release()
        finally:
            if owns_session:
                await session.close()

    async def _open_stream_with_retries(
        self,
        session: aiohttp.ClientSession,
        payload: dict,
        headers: dict[str, str],
        max_retries: int,
    ) -> aiohttp.ClientResponse:
        """POST and return the open response, retrying transient pre-stream errors.

        Retries cover errors that happen BEFORE the first audio byte is
        observed: connection resets, true 5xx, true rate-limits. Once
        chunk iteration starts we cannot retry (HA is already consuming
        bytes), so this guard is the only place to absorb a flapping
        network or a momentarily unhappy backend.

        Auth/quota errors are NOT retried - they will fail again
        identically and waking the speaker twice helps no one.
        """
        attempt = 0
        while True:
            try:
                response = await session.post(
                    self._url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=STREAMING_TIMEOUT_SECONDS),
                )
            except asyncio.CancelledError:
                _LOGGER.warning("TTS streaming connect was cancelled")
                raise
            except aiohttp.ClientError as e:
                _LOGGER.error(
                    "Network error opening TTS stream (attempt %d): %s",
                    attempt + 1, e,
                )
                if attempt >= max_retries:
                    raise OpenAINetworkError(
                        f"Network error opening TTS stream: {e}"
                    ) from e
                attempt += 1
                await asyncio.sleep(1)
                continue

            if response.status < 400:
                return response

            body_snippet = ""
            try:
                body_snippet = (await response.content.read(2048)).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                pass
            response.release()
            classified = _classify_http_error(response.status, body_snippet)
            if not _is_retryable(classified) or attempt >= max_retries:
                raise classified
            _LOGGER.warning(
                "TTS stream HTTP %s on attempt %d (retryable): %s",
                response.status, attempt + 1, classified,
            )
            attempt += 1
            await asyncio.sleep(1)
