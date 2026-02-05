"""
TTS Engine for OpenAI TTS with optional streaming support.
"""
import json
import logging
import time
import io
import asyncio
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from asyncio import CancelledError
from typing import Optional, Iterator, Callable, Union, AsyncGenerator
import aiohttp

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Chunk size for streaming (in bytes)
CHUNK_SIZE = 8192  # 8KB chunks for better network efficiency

class AudioResponse:
    """A simple response wrapper with a 'content' attribute to hold audio bytes."""
    def __init__(self, content: bytes):
        self.content = content

class StreamingAudioResponse:
    """A streaming response that collects audio chunks."""
    def __init__(self, response, on_first_chunk: Optional[Callable[[], None]] = None):
        self.response = response
        self._chunks = []
        self._first_chunk_callback = on_first_chunk
        self._first_chunk_received = False
        
    def read_all(self) -> bytes:
        """Read all chunks and return complete audio."""
        while True:
            chunk = self.response.read(CHUNK_SIZE)
            if not chunk:
                break
                
            # Call callback on first chunk
            if not self._first_chunk_received and self._first_chunk_callback:
                self._first_chunk_received = True
                self._first_chunk_callback()
                
            self._chunks.append(chunk)
        
        return b''.join(self._chunks)

class OpenAITTSEngine:
    def __init__(self, api_key: str, voice: str, model: str, speed: float, url: str):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = url

    def get_tts(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None,
        stream: bool = False,
        on_first_chunk: Optional[Callable[[], None]] = None
    ) -> Union[AudioResponse, StreamingAudioResponse]:
        """TTS request with optional streaming support.

        Args:
            text: Text to convert to speech
            speed: Speech speed (0.25-4.0)
            voice: Voice to use
            instructions: Optional instructions for the model
            extra_payload: JSON string with extra parameters to merge into the request
            stream: If True, returns StreamingAudioResponse for lower latency
            on_first_chunk: Callback when first chunk is received (streaming only)
        """
        if speed is None:
            speed = self._speed
        if voice is None:
            voice = self._voice
        if model is None:
            model = self._model

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "HomeAssistant-OpenAI-TTS"
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload: dict[str, object] = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        }
        # Include instructions if provided
        if instructions is not None:
            payload["instructions"] = instructions

        # Merge extra payload if provided (for custom backends)
        if extra_payload:
            try:
                extra = json.loads(extra_payload)
                if isinstance(extra, dict):
                    payload.update(extra)
                    _LOGGER.debug("Merged extra payload: %s", extra)
            except json.JSONDecodeError as e:
                _LOGGER.warning("Invalid extra_payload JSON, ignoring: %s", e)

        # Debug logging for payload
        _LOGGER.debug("TTS API payload: model=%s, voice=%s, speed=%s, instructions=%s, extra_keys=%s",
                     model, voice, speed, instructions,
                     [k for k in payload.keys() if k not in ("model", "input", "voice", "response_format", "speed", "instructions")])

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
                    # Return streaming response
                    resp = urlopen(req, timeout=30)
                    _LOGGER.debug("Using streaming mode for TTS")
                    return StreamingAudioResponse(resp, on_first_chunk)
                else:
                    # Return complete response (original behavior)
                    with urlopen(req, timeout=30) as resp:
                        return AudioResponse(resp.read())

            except CancelledError:
                _LOGGER.exception("TTS request cancelled")
                raise

            except (HTTPError, URLError) as net_err:
                _LOGGER.error("Network error fetching TTS audio (attempt %d): %s", attempt+1, net_err)
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(1)
                    continue
                raise HomeAssistantError("Network error fetching TTS audio") from net_err

            except Exception as exc:
                _LOGGER.error("Unknown error fetching TTS audio (attempt %d): %s", attempt+1, exc)
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(1)
                    continue
                raise HomeAssistantError("Unknown error fetching TTS audio") from exc

    async def async_get_tts_stream(
        self,
        text: str,
        response_format: str = "opus",
        speed: float | None = None,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
        extra_payload: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        """Stream TTS audio from OpenAI API.

        Args:
            text: Text to convert to speech
            response_format: Audio format (opus recommended for streaming)
            speed: Speech speed (0.25-4.0)
            voice: Voice to use
            model: Model to use
            instructions: Optional instructions for the model
            extra_payload: JSON string with extra parameters to merge into the request

        Yields:
            Audio data chunks as bytes
        """
        if speed is None:
            speed = self._speed
        if voice is None:
            voice = self._voice
        if model is None:
            model = self._model

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "HomeAssistant-OpenAI-TTS"
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": response_format,
            "speed": speed
        }

        # Include instructions if provided
        if instructions is not None:
            payload["instructions"] = instructions

        # Merge extra payload if provided (for custom backends)
        if extra_payload:
            try:
                extra = json.loads(extra_payload)
                if isinstance(extra, dict):
                    payload.update(extra)
                    _LOGGER.debug("Merged extra payload: %s", extra)
            except json.JSONDecodeError as e:
                _LOGGER.warning("Invalid extra_payload JSON, ignoring: %s", e)

        _LOGGER.debug("Streaming TTS API request: model=%s, voice=%s, speed=%s, format=%s",
                     model, voice, speed, response_format)

        # Create session that will live for the entire streaming duration
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    self._url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60)  # Increased timeout for streaming
                ) as response:
                    response.raise_for_status()

                    # Get content type to verify we're getting audio
                    content_type = response.headers.get('Content-Type', '')
                    _LOGGER.debug("Response content type: %s", content_type)

                    # Choose chunk size based on format
                    chunk_size = 4096 if response_format == "opus" else 8192

                    _LOGGER.debug("Starting to stream audio chunks (chunk_size=%d)", chunk_size)

                    # Collect all chunks first to debug
                    chunks_received = 0
                    total_bytes = 0
                    initial_buffer = []
                    initial_buffer_size = 0
                    min_initial_size = 1024  # Buffer at least 1KB before starting to yield

                    # Stream chunks as they arrive
                    try:
                        async for chunk in response.content.iter_chunked(chunk_size):
                            if chunk:
                                chunks_received += 1
                                total_bytes += len(chunk)

                                # Buffer initial chunks to ensure we have valid audio header
                                if initial_buffer_size < min_initial_size:
                                    initial_buffer.append(chunk)
                                    initial_buffer_size += len(chunk)
                                    # Only log initial buffering on first and when complete
                                    if chunks_received == 1:
                                        _LOGGER.debug("Buffering initial audio data...")

                                    # Once we have enough initial data, yield it all
                                    if initial_buffer_size >= min_initial_size:
                                        combined = b''.join(initial_buffer)
                                        _LOGGER.debug("Initial buffer complete: %d bytes, starting stream", len(combined))
                                        yield combined
                                        initial_buffer = []
                                else:
                                    # After initial buffer, only log every 50 chunks to reduce spam
                                    if chunks_received % 50 == 0:
                                        _LOGGER.debug("Streaming progress: %d chunks, %d total bytes",
                                                    chunks_received, total_bytes)
                                    yield chunk
                            else:
                                _LOGGER.debug("Received empty chunk, continuing...")
                    except asyncio.CancelledError:
                        _LOGGER.warning("Streaming cancelled after %d chunks (%d bytes)",
                                      chunks_received, total_bytes)
                        raise
                    except Exception as e:
                        _LOGGER.error("Error while iterating chunks: %s", e, exc_info=True)
                        raise

                    _LOGGER.debug("Finished streaming audio: %d chunks, %d total bytes",
                                chunks_received, total_bytes)

            except aiohttp.ClientError as e:
                _LOGGER.error("Network error during TTS streaming: %s", e)
                raise HomeAssistantError(f"Network error during TTS streaming: {e}") from e
            except asyncio.CancelledError:
                _LOGGER.warning("TTS streaming was cancelled")
                raise
            except Exception as e:
                _LOGGER.error("Unexpected error during TTS streaming: %s", e, exc_info=True)
                raise HomeAssistantError(f"Unexpected error during TTS streaming: {e}") from e

    def close(self):
        """Nothing to close."""
        pass

    @staticmethod
    def get_supported_langs() -> list[str]:
        return [
            "af","ar","hy","az","be","bs","bg","ca","zh","hr","cs","da","nl","en",
            "et","fi","fr","gl","de","el","he","hi","hu","is","id","it","ja","kn",
            "kk","ko","lv","lt","mk","ms","mr","mi","ne","no","fa","pl","pt","ro",
            "ru","sr","sk","sl","es","sw","sv","tl","ta","th","tr","uk","ur","vi","cy"
        ]