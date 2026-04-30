"""TTS Engine for OpenAI TTS with optional streaming support.

The engine provides two parallel call paths:

- ``get_tts()``: blocking, called via an executor by the legacy
  ``async_get_tts_audio()`` HA TTS contract.
- ``async_get_tts_stream()``: native async generator used by HA 2025.7+
  streaming TTS contract.

Both paths share a single ``_RequestBuilder`` for header/payload assembly
and a single ``_classify_http_error()`` for status-to-exception mapping,
so error handling stays consistent across them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from asyncio import CancelledError
from typing import AsyncGenerator, Callable, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import aiohttp

from .exceptions import (
    OpenAIAuthError,
    OpenAIQuotaExceededError,
    OpenAIRateLimitError,
    OpenAIServerError,
    OpenAITTSError,
)

_LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 8192
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
    if isinstance(exc, (OpenAIRateLimitError, OpenAIServerError)):
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

        if extra_payload:
            try:
                extra = json.loads(extra_payload)
                if isinstance(extra, dict):
                    payload.update(extra)
                    _LOGGER.debug("Merged extra payload keys: %s", list(extra.keys()))
            except json.JSONDecodeError as e:
                _LOGGER.warning("Invalid extra_payload JSON, ignoring: %s", e)

        return headers, payload


class AudioResponse:
    """Wraps a complete audio payload returned by ``get_tts(stream=False)``."""

    def __init__(self, content: bytes) -> None:
        self.content = content


class StreamingAudioResponse:
    """A streaming response that collects audio chunks lazily.

    Used when ``get_tts(stream=True)`` is called. The wrapped urllib
    response is read on demand by ``read_all()``.
    """

    def __init__(
        self,
        response,
        on_first_chunk: Optional[Callable[[], None]] = None,
    ) -> None:
        self.response = response
        self._chunks: list[bytes] = []
        self._first_chunk_callback = on_first_chunk
        self._first_chunk_received = False

    def read_all(self) -> bytes:
        """Read all chunks and return the complete audio bytes."""
        while True:
            chunk = self.response.read(CHUNK_SIZE)
            if not chunk:
                break
            if not self._first_chunk_received and self._first_chunk_callback:
                self._first_chunk_received = True
                self._first_chunk_callback()
            self._chunks.append(chunk)
        return b"".join(self._chunks)


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
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = url
        self._builder = _RequestBuilder(api_key, voice, model, speed)

    def get_tts(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None,
        stream: bool = False,
        on_first_chunk: Optional[Callable[[], None]] = None,
    ) -> Union[AudioResponse, StreamingAudioResponse]:
        """Blocking TTS request. Must be invoked via an executor.

        Raises:
            OpenAIAuthError: 401/403. Caller should trigger reauth.
            OpenAIQuotaExceededError: 402, or 429 with insufficient_quota.
            OpenAIRateLimitError: 429 due to true rate limiting.
            OpenAIServerError: 5xx (will retry once before raising).
            OpenAITTSError: other failures.
        """
        headers, payload = self._builder.build(
            text=text,
            response_format="mp3",
            speed=speed,
            voice=voice,
            model=model,
            instructions=instructions,
            extra_payload=extra_payload,
        )
        _LOGGER.debug(
            "TTS API request: model=%s, voice=%s, speed=%s, stream=%s",
            payload["model"], payload["voice"], payload["speed"], stream,
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
                if stream:
                    resp = urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS)
                    return StreamingAudioResponse(resp, on_first_chunk)
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
                    raise OpenAITTSError(
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
        response_format: str = "opus",
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream TTS audio from the OpenAI API.

        Error responses are classified BEFORE any chunk is yielded,
        so a failed request can never leak partial bytes into the HA
        TTS cache (see issue #64).
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
            "Streaming TTS API request: model=%s, voice=%s, speed=%s, format=%s",
            payload["model"], payload["voice"], payload["speed"], response_format,
        )

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    self._url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=STREAMING_TIMEOUT_SECONDS),
                ) as response:
                    if response.status >= 400:
                        body_snippet = ""
                        try:
                            body_snippet = (await response.content.read(2048)).decode(
                                "utf-8", errors="replace"
                            )
                        except Exception:
                            pass
                        raise _classify_http_error(response.status, body_snippet)

                    _LOGGER.debug(
                        "Response content type: %s",
                        response.headers.get("Content-Type", ""),
                    )

                    chunk_size = 4096 if response_format == "opus" else 8192

                    chunks_received = 0
                    total_bytes = 0
                    initial_buffer: list[bytes] = []
                    initial_buffer_size = 0

                    try:
                        async for chunk in response.content.iter_chunked(chunk_size):
                            if not chunk:
                                continue
                            chunks_received += 1
                            total_bytes += len(chunk)

                            if initial_buffer_size < INITIAL_BUFFER_BYTES:
                                initial_buffer.append(chunk)
                                initial_buffer_size += len(chunk)
                                if initial_buffer_size >= INITIAL_BUFFER_BYTES:
                                    yield b"".join(initial_buffer)
                                    initial_buffer = []
                            else:
                                if chunks_received % 50 == 0:
                                    _LOGGER.debug(
                                        "Streaming progress: %d chunks, %d bytes",
                                        chunks_received, total_bytes,
                                    )
                                yield chunk

                        # Flush any leftover initial buffer for very short clips
                        # whose total size never reached INITIAL_BUFFER_BYTES.
                        if initial_buffer:
                            yield b"".join(initial_buffer)

                    except asyncio.CancelledError:
                        _LOGGER.warning(
                            "Streaming cancelled after %d chunks (%d bytes)",
                            chunks_received, total_bytes,
                        )
                        raise

                    _LOGGER.debug(
                        "Finished streaming: %d chunks, %d total bytes",
                        chunks_received, total_bytes,
                    )

            except OpenAITTSError:
                raise
            except aiohttp.ClientError as e:
                _LOGGER.error("Network error during TTS streaming: %s", e)
                raise OpenAITTSError(
                    f"Network error during TTS streaming: {e}"
                ) from e
            except asyncio.CancelledError:
                _LOGGER.warning("TTS streaming was cancelled")
                raise
            except Exception as e:
                _LOGGER.error(
                    "Unexpected error during TTS streaming: %s", e, exc_info=True
                )
                raise OpenAITTSError(
                    f"Unexpected error during TTS streaming: {e}"
                ) from e

    def close(self) -> None:
        """Nothing persistent to close."""

    @staticmethod
    def get_supported_langs() -> list[str]:
        """Return list of supported language codes."""
        return [
            "af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr",
            "cs", "da", "nl", "en", "et", "fi", "fr", "gl", "de", "el",
            "he", "hi", "hu", "is", "id", "it", "ja", "kn", "kk", "ko",
            "lv", "lt", "mk", "ms", "mr", "mi", "ne", "no", "fa", "pl",
            "pt", "ro", "ru", "sr", "sk", "sl", "es", "sw", "sv", "tl",
            "ta", "th", "tr", "uk", "ur", "vi", "cy",
        ]
