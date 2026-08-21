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
import base64
import json
import logging
import time
from asyncio import CancelledError
from typing import AsyncGenerator, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import model_may_accept_instructions
from .exceptions import (
    OpenAIAuthError,
    OpenAINetworkError,
    OpenAIQuotaExceededError,
    OpenAIRateLimitError,
    OpenAIServerError,
    OpenAITTSError,
    OpenAIVoiceDeletedError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
STREAMING_TIMEOUT_SECONDS = 60
INITIAL_BUFFER_BYTES = 1024

# Retry knobs for transient failures (5xx, 429, network blips). Auth /
# quota / voice-deleted errors are skipped by ``_is_retryable`` so this
# only applies to errors that have a real chance of clearing on the
# next attempt. ``MAX_RETRIES`` is the number of *retries* on top of
# the initial attempt, so the total request count is bounded at
# ``1 + MAX_RETRIES`` (2 by default).
#
# Deliberately kept at a single retry. The per-attempt timeout is what
# dominates the worst case: with ``STREAMING_TIMEOUT_SECONDS`` at 60 a
# hanging provider already costs 2 x 60 + 1 = 121 s, and Home
# Assistant's own TTS / service timeouts fire well before that. A
# second retry only pushes the user-visible failure further out
# without ever getting a chance to succeed. Flat (non-exponential) by
# design - the user is waiting on the speaker, exponential backoff
# would just turn dropped audio into longer silence.
MAX_RETRIES = 1
RETRY_DELAY_SECONDS = 1


_VOICE_DELETED_PATTERNS = (
    # Free-text fallback for backends that don't expose a structured
    # error code. Each entry is matched case-insensitively against
    # the body. Kept narrow so generic 400s like
    # "voice argument is required" don't false-positive.
    "voice does not exist",
    "voice has been deleted",
    "voice_not_found",
    "voice_deleted",
    "invalid voice",
    "unknown voice",
    # Groq's response when the voice slug is unknown / typo'd.
    "voice must be",
    "voice should be",
)

# Structured error codes / types that mean "this voice can't be used".
# Matched against ``error.code`` and ``error.type`` from the JSON body.
# Mistral uses ``type=invalid_voice`` (numeric ``code=1902``); the OpenAI
# pattern is ``code=invalid_voice`` / ``code=voice_not_found``.
_VOICE_DELETED_JSON_TOKENS = frozenset({
    "invalid_voice",
    "voice_not_found",
    "voice_deleted",
    "voice_does_not_exist",
})


def _voice_deletion_via_json(body_snippet: str) -> bool:
    """Return True when the JSON envelope flags a voice-level error.

    Looks at the typed fields first because they're stable across
    provider releases - free-form messages can change wording from
    one model rev to the next, codes generally don't.
    """
    body_snippet = body_snippet.strip()
    if not body_snippet or not body_snippet.startswith("{"):
        return False
    try:
        envelope = json.loads(body_snippet)
    except (json.JSONDecodeError, ValueError):
        return False
    # OpenAI-style nests under ``error``; Mistral keeps the fields at
    # the top level. Try both.
    candidates = []
    if isinstance(envelope, dict):
        candidates.append(envelope)
        nested = envelope.get("error")
        if isinstance(nested, dict):
            candidates.append(nested)
    for cand in candidates:
        type_ = str(cand.get("type", "")).lower()
        code = str(cand.get("code", "")).lower()
        if type_ in _VOICE_DELETED_JSON_TOKENS:
            return True
        if code in _VOICE_DELETED_JSON_TOKENS:
            return True
    return False


def _looks_like_voice_deletion(body_snippet: str) -> bool:
    """Detect a 4xx body that says 'this voice no longer exists'.

    Two-tier check: structured JSON tokens first (Mistral
    ``type=invalid_voice``, OpenAI ``code=invalid_voice``), free-text
    pattern matching second (Groq, custom backends without a code).
    The JSON tier is the authoritative source whenever it's present
    so a future wording change in the message text doesn't silently
    break detection.
    """
    if not body_snippet:
        return False
    if _voice_deletion_via_json(body_snippet):
        return True
    haystack = body_snippet.lower()
    return any(p in haystack for p in _VOICE_DELETED_PATTERNS)


def _classify_http_error(
    status: int, body_snippet: str = "", voice: str | None = None
) -> OpenAITTSError:
    """Map an HTTP status (and optional body) to a typed exception.

    The body snippet is appended to the message for 4xx responses so the
    user (or issue tracker) can see what the upstream server actually
    complained about. We trim to 200 chars to keep logs readable; the
    full body remains visible at DEBUG via the existing read-2048 path.

    ``voice`` lets the engine pass the voice slug it was trying to use
    so a downstream Repairs issue can name the missing voice.
    """
    detail = f" - {body_snippet[:200]}" if body_snippet and 400 <= status < 500 else ""
    if status in (401, 403):
        return OpenAIAuthError(
            f"TTS authentication failed (HTTP {status}). Check your API key.{detail}"
        )
    if status == 402:
        return OpenAIQuotaExceededError(
            f"TTS account balance/quota exhausted (HTTP {status}).{detail}"
        )
    if status == 429:
        # OpenAI returns 429 for BOTH true rate limits and out-of-credits.
        # The body's `insufficient_quota` marker disambiguates them.
        if "insufficient_quota" in body_snippet:
            return OpenAIQuotaExceededError(
                "TTS account quota exhausted (HTTP 429 insufficient_quota). "
                "Top up your account."
            )
        return OpenAIRateLimitError(
            f"TTS rate limit hit (HTTP {status}). Slow down requests.{detail}"
        )
    if status >= 500:
        return OpenAIServerError(
            f"TTS provider server error (HTTP {status}). Usually temporary; "
            "the integration retries once before giving up."
        )
    # 4xx that mentions the voice is gone: surface a dedicated error
    # so the TTS layer can raise a Repairs issue instead of letting
    # this look like a generic bad request.
    if 400 <= status < 500 and _looks_like_voice_deletion(body_snippet):
        return OpenAIVoiceDeletedError(
            f"Configured voice {voice or '?'} no longer exists on the "
            f"provider (HTTP {status}).{detail}",
            voice=voice,
        )
    return OpenAITTSError(
        f"TTS provider rejected the request (HTTP {status}).{detail}"
    )


def _is_retryable(exc: BaseException) -> bool:
    """Auth/quota errors will fail again immediately, so don't waste a retry."""
    if isinstance(exc, (OpenAIAuthError, OpenAIQuotaExceededError)):
        return False
    if isinstance(exc, (OpenAIRateLimitError, OpenAIServerError, OpenAINetworkError)):
        return True
    if isinstance(exc, (URLError, aiohttp.ClientError)):
        return True
    return False


def _decode_json_audio_blob(body: bytes) -> bytes:
    """Pull base64 audio out of a JSON-wrapped speech response.

    Mistral Voxtral and similar OpenAI-compatible providers return a
    JSON envelope with the audio under ``audio_data`` (or ``audio`` /
    ``data``) instead of raw bytes. We unwrap and base64-decode in one
    place so both the sync ``get_tts`` and the async streaming path can
    reuse it. Raises ``OpenAITTSError`` when the body is JSON but the
    audio field is missing or unreadable.
    """
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as exc:
        preview = body[:120].decode("utf-8", errors="replace")
        raise OpenAITTSError(
            f"Could not parse JSON audio response: {exc}. Body starts with: {preview!r}"
        ) from exc

    if not isinstance(envelope, dict):
        raise OpenAITTSError(
            f"JSON audio response was not an object: {type(envelope).__name__}"
        )

    for key in ("audio_data", "audio", "data"):
        value = envelope.get(key)
        if isinstance(value, str) and value:
            try:
                return base64.b64decode(value)
            except (ValueError, base64.binascii.Error) as exc:
                raise OpenAITTSError(
                    f"Could not base64-decode {key!r} in audio response: {exc}"
                ) from exc

    snippet = json.dumps(envelope)[:200]
    raise OpenAITTSError(
        "JSON audio response did not include a recognised audio field "
        f"(audio_data / audio / data). Body: {snippet}"
    )


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
        }
        # Only send ``speed`` when the user has actually changed it from
        # the API default of 1.0. Some compatible backends (Mistral
        # Voxtral) reject any unrecognised body field with HTTP 422
        # ``extra_forbidden``, which would otherwise block requests
        # that are still using the default 1.0.
        effective_speed = speed if speed is not None else self._default_speed
        if effective_speed is not None and abs(float(effective_speed) - 1.0) > 1e-9:
            payload["speed"] = effective_speed
        # Keep ``instructions`` out of the body when the model is a known
        # OpenAI one that rejects it. The profile may still hold a value
        # from when it was pointed at ``gpt-4o-mini-tts``; that value is
        # preserved on purpose, but sending it to ``tts-1`` fails the
        # whole request. Custom backends under unrecognised model names
        # still get it.
        if instructions is not None:
            effective_model = payload["model"]
            if model_may_accept_instructions(
                effective_model if isinstance(effective_model, str) else None
            ):
                payload["instructions"] = instructions
            else:
                _LOGGER.debug(
                    "Dropping instructions for model %s, which does not "
                    "accept the field", effective_model,
                )

        if extra_payload:
            # Be lenient with whitespace and the common ```json fenced
            # code-block wrapping, then try to parse. If the payload is
            # still invalid we log a single actionable warning and drop
            # the payload rather than fail the whole request - blocking
            # otherwise-working TTS over a malformed optional field is a
            # worse user experience than skipping it (issue #65).
            cleaned = extra_payload.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.lstrip("`")
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.rstrip("`").strip()
            try:
                extra = json.loads(cleaned)
            except json.JSONDecodeError as e:
                _LOGGER.warning(
                    "Ignoring invalid extra_payload JSON (%s). "
                    "Expected a JSON object like {\"temperature\": 0.7}. "
                    "Received: %r",
                    e, extra_payload[:120],
                )
                extra = None
            if extra is not None and not isinstance(extra, dict):
                _LOGGER.warning(
                    "Ignoring extra_payload: must be a JSON object, "
                    "got %s. Received: %r",
                    type(extra).__name__, extra_payload[:120],
                )
                extra = None
            if extra:
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
            payload["model"], payload["voice"], payload.get("speed", 1.0),
        )

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
                    body = resp.read()
                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" in content_type.lower():
                        body = _decode_json_audio_blob(body)
                    return AudioResponse(body)

            except CancelledError:
                _LOGGER.debug("TTS request cancelled")
                raise

            except HTTPError as http_err:
                body_snippet = ""
                try:
                    body_snippet = http_err.read(2048).decode("utf-8", errors="replace")
                except Exception:
                    pass
                classified = _classify_http_error(
                    http_err.code, body_snippet, voice=voice
                )
                _LOGGER.error(
                    "OpenAI TTS HTTP %s on attempt %d/%d: %s",
                    http_err.code, attempt + 1, MAX_RETRIES + 1, classified,
                )
                if not _is_retryable(classified) or attempt >= MAX_RETRIES:
                    raise classified from http_err
                attempt += 1
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            except URLError as net_err:
                _LOGGER.error(
                    "Network error fetching TTS audio (attempt %d/%d): %s",
                    attempt + 1, MAX_RETRIES + 1, net_err,
                )
                if attempt >= MAX_RETRIES:
                    raise OpenAINetworkError(
                        f"Network error fetching TTS audio: {net_err}"
                    ) from net_err
                attempt += 1
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            except OpenAITTSError:
                raise

            except Exception as exc:
                _LOGGER.error(
                    "Unknown error fetching TTS audio (attempt %d/%d): %s",
                    attempt + 1, MAX_RETRIES + 1, exc,
                )
                if attempt >= MAX_RETRIES:
                    raise OpenAITTSError(
                        f"Unknown error fetching TTS audio: {exc}"
                    ) from exc
                attempt += 1
                time.sleep(RETRY_DELAY_SECONDS)

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
        TTS proxy and Chromecast handle most reliably; opus has known
        receiver-side compatibility issues on older cast hardware. The
        entity always passes the profile's configured format explicitly,
        so this default only applies when the engine is called directly.

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
            payload["model"], payload["voice"], payload.get("speed", 1.0),
            response_format,
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

        chunk_size = 4096 if response_format == "opus" else 8192

        try:
            response = await self._open_stream_with_retries(
                session, payload, headers, MAX_RETRIES
            )
            try:
                content_type = response.headers.get("Content-Type", "")
                _LOGGER.debug("Response content type: %s", content_type)

                # OpenAI returns raw audio bytes; some compatible providers
                # (Mistral Voxtral, etc.) wrap a base64 audio blob in a JSON
                # envelope. Detect by Content-Type and yield decoded bytes
                # in one go - the audio is small enough that this does not
                # need a streaming decoder, and HA's pipeline buffers a
                # 1KB header window upstream anyway.
                if "application/json" in content_type.lower():
                    audio_bytes = await self._decode_json_audio_response(response)
                    if audio_bytes:
                        yield audio_bytes
                    return

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
            finally:
                # Always release the response - covers cancellation and
                # any exception during chunk iteration. aiohttp leaves
                # the underlying socket connected to the pool only if
                # release() runs.
                response.release()
        finally:
            if owns_session:
                await session.close()

    @staticmethod
    async def _decode_json_audio_response(
        response: aiohttp.ClientResponse,
    ) -> bytes:
        """Async wrapper that reads the body and delegates the unwrap."""
        body = await response.read()
        return _decode_json_audio_blob(body)

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
                    "Network error opening TTS stream (attempt %d/%d): %s",
                    attempt + 1, max_retries + 1, e,
                )
                if attempt >= max_retries:
                    raise OpenAINetworkError(
                        f"Network error opening TTS stream: {e}"
                    ) from e
                attempt += 1
                await asyncio.sleep(RETRY_DELAY_SECONDS)
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
            classified = _classify_http_error(
                response.status, body_snippet, voice=payload.get("voice"),
            )
            if not _is_retryable(classified) or attempt >= max_retries:
                raise classified
            _LOGGER.warning(
                "TTS stream HTTP %s on attempt %d/%d (retryable): %s",
                response.status, attempt + 1, max_retries + 1, classified,
            )
            attempt += 1
            await asyncio.sleep(RETRY_DELAY_SECONDS)
