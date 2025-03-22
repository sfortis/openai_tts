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

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        data = {
            "model": self._model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed
        }
        if instructions is not None and self._model == "gpt-4o-mini-tts":
            data["instructions"] = instructions

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
            "af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr", "cs", "da", "nl", "en",
            "et", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "is", "id", "it", "ja", "kn",
            "kk", "ko", "lv", "lt", "mk", "ms", "mr", "mi", "ne", "no", "fa", "pl", "pt", "ro",
            "ru", "sr", "sk", "sl", "es", "sw", "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi", "cy"
        ]
