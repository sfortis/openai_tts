"""
TTS Engine for OpenAI TTS with optional streaming support.
"""
import json
import logging
import time
import io
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from asyncio import CancelledError
from typing import Optional, Iterator, Callable, Union

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
        stream: bool = False,
        on_first_chunk: Optional[Callable[[], None]] = None
    ) -> Union[AudioResponse, StreamingAudioResponse]:
        """TTS request with optional streaming support.
        
        Args:
            text: Text to convert to speech
            speed: Speech speed (0.25-4.0)
            voice: Voice to use
            instructions: Optional instructions for the model
            stream: If True, returns StreamingAudioResponse for lower latency
            on_first_chunk: Callback when first chunk is received (streaming only)
        """
        if speed is None:
            speed = self._speed
        if voice is None:
            voice = self._voice
        if model is None:
            model = self._model

        headers = {"Content-Type": "application/json"}
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
        
        # Debug logging for payload
        _LOGGER.debug("TTS API payload: model=%s, voice=%s, speed=%s, instructions=%s", 
                     model, voice, speed, instructions)

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