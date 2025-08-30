"""
TTS Engine for Groq TTS.
"""
from __future__ import annotations
import json
import logging
import asyncio
from urllib.error import HTTPError, URLError
from collections import OrderedDict

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from asyncio import CancelledError

from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed
from .const import VERSION

_LOGGER = logging.getLogger(__name__)

class AudioResponse:
    """A simple response wrapper with a 'content' attribute to hold audio bytes."""
    def __init__(self, content: bytes):
        self.content = content

class GroqTTSEngine:
    def __init__(self, api_key: str, voice: str, model: str, url: str, cache_max: int | None = None):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._url = url
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_max = cache_max if cache_max is not None else 256

    async def async_get_tts(self, hass, text: str, voice: str | None = None) -> AudioResponse:
        """Asynchronous TTS request using aiohttp for Groq API."""
        if voice is None:
            voice = self._voice

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # Provide a clear, integration-specific user agent with version
        headers["User-Agent"] = f"homeassistant-groq-tts/{VERSION}"

        data = {"model": self._model, "input": text, "voice": voice}

        cache_key = (voice, text)
        if cache_key in self._cache:
            _LOGGER.debug("Returning cached audio for %s", cache_key)
            # Move key to end to mark as recently used
            content = self._cache.pop(cache_key)
            self._cache[cache_key] = content
            return AudioResponse(content)

        max_retries = 1
        attempt = 0

        if self._session is None:
            self._session = async_get_clientsession(hass)
        session = self._session

        while True:
            try:
                async with session.post(self._url, json=data, headers=headers, timeout=30) as resp:
                    content = await resp.read()
                    ctype = resp.headers.get("content-type", "")
                    # Treat non-2xx as errors; try to parse JSON body for details
                    if resp.status < 200 or resp.status >= 300:
                        if resp.status in (401, 403):
                            raise ConfigEntryAuthFailed("Authentication failed for Groq TTS API")
                        try:
                            if ctype.startswith("application/json"):
                                payload = json.loads(content.decode("utf-8", errors="ignore"))
                                detail = payload.get("error") or payload
                                raise HomeAssistantError(f"Groq API error (HTTP {resp.status}): {detail}")
                            raise HomeAssistantError(f"Groq API error (HTTP {resp.status})")
                        except HomeAssistantError:
                            raise
                        except Exception:
                            raise HomeAssistantError(f"Groq API error (HTTP {resp.status})")
                    # If JSON arrives on 2xx, check for embedded error structure
                    if ctype.startswith("application/json"):
                        try:
                            error_json = json.loads(content.decode("utf-8", errors="ignore"))
                        except Exception:
                            error_json = {}
                        if isinstance(error_json, dict) and "error" in error_json:
                            msg = error_json["error"].get("message", str(error_json["error"]))
                            _LOGGER.error("Groq API error: %s", msg)
                            raise HomeAssistantError(f"Groq API error: {msg}")
                        # Unexpected JSON with 2xx: treat as error if not explicitly successful
                        raise HomeAssistantError("Groq API returned JSON but no audio content")
                    # Guard against unexpected content types on 2xx
                    if not (ctype.startswith("audio/") or ctype.startswith("application/octet-stream")):
                        raise HomeAssistantError(f"Unexpected content-type from Groq API: {ctype}")
                    # Cache successful audio payloads with LRU eviction
                    self._cache[cache_key] = content
                    if len(self._cache) > self._cache_max:
                        self._cache.popitem(last=False)
                    return AudioResponse(content)
            except CancelledError:
                _LOGGER.debug("TTS request cancelled")
                raise
            except ConfigEntryAuthFailed:
                # Bubble up to trigger reauth flow
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
        # Use HA-managed session; do not close here to avoid impacting other integrations
        return None

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
