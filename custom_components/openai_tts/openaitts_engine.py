"""
TTS Engine for OpenAI TTS.
"""
import json
import logging
import aiohttp
from asyncio import CancelledError

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

class OpenAITTSEngine:
    def __init__(self, api_key: str, voice: str, model: str, speed: float, url: str):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = url
        self._session = aiohttp.ClientSession()

    async def get_tts(self, text: str, speed: float = None, instructions: str = None, voice: str = None):
        """Asynchronous TTS request that streams audio chunks."""
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
            "response_format": "mp3",  # Assuming mp3 is still desired
            "speed": speed
        }
        if instructions is not None and self._model == "gpt-4o-mini-tts": # TODO: check if this model is correct
            data["instructions"] = instructions

        try:
            async with self._session.post(
                self._url,
                json=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30) # Overall timeout for the request
            ) as response:
                response.raise_for_status()  # Raise an exception for bad status codes
                async for chunk in response.content.iter_any():
                    if chunk:
                        yield chunk
        except CancelledError:
            _LOGGER.debug("TTS request cancelled")
            raise
        except aiohttp.ClientResponseError as net_err:
            # More specific error for HTTP issues if needed, e.g. response.status
            _LOGGER.error("Network error in get_tts: %s, status: %s", net_err.message, net_err.status)
            raise HomeAssistantError(f"Network error occurred while fetching TTS audio: {net_err.message}") from net_err
        except aiohttp.ClientError as net_err:
            _LOGGER.error("Network error in get_tts: %s", net_err)
            raise HomeAssistantError(f"Network error occurred while fetching TTS audio: {net_err}") from net_err
        except Exception as exc:
            _LOGGER.exception("Unknown error in get_tts")
            raise HomeAssistantError("An unknown error occurred while fetching TTS audio") from exc

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def get_supported_langs() -> list:
        return [
            "af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr", "cs", "da", "nl", "en",
            "et", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "is", "id", "it", "ja", "kn",
            "kk", "ko", "lv", "lt", "mk", "ms", "mr", "mi", "ne", "no", "fa", "pl", "pt", "ro",
            "ru", "sr", "sk", "sl", "es", "sw", "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi", "cy"
        ]
