"""
TTS Engine for Groq TTS.
"""
from __future__ import annotations
import json
import logging
import asyncio
from urllib.error import HTTPError, URLError

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from asyncio import CancelledError

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

class AudioResponse:
    """A simple response wrapper with a 'content' attribute to hold audio bytes."""
    def __init__(self, content: bytes):
        self.content = content

class GroqTTSEngine:
    def __init__(self, api_key: str, voice: str, model: str, url: str):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._url = url
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[tuple[str, str], bytes] = {}

    async def async_get_tts(self, hass, text: str, voice: str | None = None) -> AudioResponse:
        """Asynchronous TTS request using aiohttp for Groq API."""
        if voice is None:
            voice = self._voice

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers["User-Agent"] = "curl/8.7.1"

        data = {"model": self._model, "input": text, "voice": voice}

        cache_key = (voice, text)
        if cache_key in self._cache:
            _LOGGER.debug("Returning cached audio for %s", cache_key)
            return AudioResponse(self._cache[cache_key])

        max_retries = 1
        attempt = 0

        if self._session is None:
            self._session = async_get_clientsession(hass)
        session = self._session

        while True:
            try:
                async with session.post(self._url, json=data, headers=headers, timeout=30) as resp:
                    content = await resp.read()
                    if resp.headers.get("content-type", "").startswith("application/json"):
                        error_json = json.loads(content.decode("utf-8"))
                        if "error" in error_json:
                            msg = error_json["error"].get("message", str(error_json["error"]))
                            _LOGGER.error("Groq API error: %s", msg)
                            raise HomeAssistantError(f"Groq API error: {msg}")
                    self._cache[cache_key] = content
                    return AudioResponse(content)
            except CancelledError:
                _LOGGER.exception("TTS request cancelled")
                raise
            except (aiohttp.ClientError, HTTPError, URLError) as net_err:
                status_code = getattr(net_err, "status", None) or getattr(net_err, "code", None)
                error_body = getattr(net_err, "message", None)
                _LOGGER.error("Groq API network error: %s", net_err)
                error_hint = ""
                if error_body and "1010" in str(error_body):
                    error_hint = " (You may need to accept the PlayAI TTS model terms at https://console.groq.com/playground?model=playai-tts)"
                if attempt < max_retries:
                    attempt += 1
                    await asyncio.sleep(1)
                    _LOGGER.debug("Retrying HTTP call (attempt %d)", attempt + 1)
                    continue
                raise HomeAssistantError(
                    f"Network error occurred while fetching TTS audio (HTTP {status_code}): {error_body}{error_hint}"
                ) from net_err
            except Exception as exc:
                _LOGGER.exception("Unknown error in async_get_tts on attempt %d", attempt + 1)
                if attempt < max_retries:
                    attempt += 1
                    await asyncio.sleep(1)
                    _LOGGER.debug("Retrying HTTP call (attempt %d)", attempt + 1)
                    continue
                raise HomeAssistantError("An unknown error occurred while fetching TTS audio") from exc

    def close(self):
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    @staticmethod
    def get_supported_langs() -> list:
        """Return supported language codes for Groq TTS."""
        return [
            "ar",
            "de",
            "en",
            "es",
            "fr",
            "hi",
            "it",
            "ja",
            "ko",
            "pl",
            "pt",
            "ru",
            "tr",
            "zh",
        ]
