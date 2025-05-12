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

    def get_tts(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        instructions: str | None = None,
    ) -> AudioResponse:
        """Synchronous TTS request with optional instructions and retry logic."""
        if speed is None:
            speed = self._speed
        if voice is None:
            voice = self._voice

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload: dict[str, object] = {
            "model": self._model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        }
        # Include instructions if provided
        if instructions is not None:
            payload["instructions"] = instructions

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
