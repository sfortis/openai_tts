"""
TTS Engine for OpenAI TTS.
"""
import json
import logging
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from asyncio import CancelledError

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

class AudioResponse:
    """A simple response wrapper with a 'content' attribute to hold audio bytes."""
    def __init__(self, content: bytes):
        self.content = content

class OpenAITTSEngine:
    def __init__(self, api_key: str, voice: str, model: str, speed: float, url: str):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = url

    def get_tts(self, text: str, speed: float = None, instructions: str = None, voice: str = None) -> AudioResponse:
        """Synchronous TTS request using urllib.request.
        If the API call fails, waits for 1 second and retries once.
        """
        if speed is None:
            speed = self._speed
        if voice is None:
            voice = self._voice

        headers = {"Content-Type": "application/json", "Autorisation": f"Bearer {self._api_key}"}

        data = {
            "model_id": self._model,
            "transcript": text,
            "voice": {
                "mode": id,
                "id": voice
            },
            "output_format": {
                "container": "mp3",
                "bit_rate": 128000,
                "sample_rate": 44100
            },
        }

        max_retries = 1
        attempt = 0
        while True:
            try:
                req = Request(
                    self._url,
                    data=json.dumps(data).encode("utf-8"),
                    headers=headers,
                    method="POST"
                )
                # Set a timeout of 30 seconds for the entire request.
                with urlopen(req, timeout=30) as response:
                    content = response.read()
                return AudioResponse(content)
            except CancelledError as ce:
                _LOGGER.exception("TTS request cancelled")
                raise  # Propagate cancellation.
            except (HTTPError, URLError) as net_err:
                _LOGGER.exception("Network error in synchronous get_tts on attempt %d", attempt + 1)
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(1)  # Wait for 1 second before retrying.
                    _LOGGER.debug("Retrying HTTP call (attempt %d)", attempt + 1)
                    continue
                else:
                    raise HomeAssistantError("Network error occurred while fetching TTS audio") from net_err
            except Exception as exc:
                _LOGGER.exception("Unknown error in synchronous get_tts on attempt %d", attempt + 1)
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(1)
                    _LOGGER.debug("Retrying HTTP call (attempt %d)", attempt + 1)
                    continue
                else:
                    raise HomeAssistantError("An unknown error occurred while fetching TTS audio") from exc

    def close(self):
        """Nothing to close in the synchronous version."""
        pass

    @staticmethod
    def get_supported_langs() -> list:
        return [
            "en", "fr", "de", "es", "pt", "zh", "ja", "hi", "it", "ko", "nl", "pl", "ru", "sv", "tr"
        ]
