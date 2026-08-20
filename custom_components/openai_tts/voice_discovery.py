"""Remote voice-list discovery for custom (non-OpenAI) TTS backends.

Several OpenAI-compatible self-hosted TTS servers (Kokoro-FastAPI,
openai-edge-tts, and others) expose a ``GET /v1/audio/voices`` endpoint
alongside the standard ``POST /v1/audio/speech`` endpoint, returning
either:

    {"voices": [{"id": "af_bella", "name": "Bella"}, ...]}   # current shape
    {"voices": ["af_bella", "af_sky", ...]}                  # legacy shape
    [{"id": "af_bella", "name": "Bella"}, ...]                # bare list
    ["af_bella", "af_sky", ...]                                # bare legacy list

This module fetches and normalises that list so both the config flow
(setup-time voice picker) and the TTS entity (``async_get_supported_voices``,
which feeds the Assist pipeline's voice dropdown) can use it. OpenAI's real
API has no such endpoint, so discovery is only ever attempted for
non-OpenAI URLs - callers are expected to gate on ``is_openai_endpoint``
before calling in here.

Failures (timeout, connection error, non-200, bad JSON) return ``None``
rather than raising, so every caller can fall back to free-text /  the
static OpenAI voice catalogue without special-casing exceptions.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# How long a successful discovery result stays valid before we hit the
# backend again. Keeps the config flow and entity setup snappy (no
# network round-trip on every form render) while still picking up voice
# packs added to a running container within a reasonable window.
_CACHE_TTL_SECONDS = 300

# hass.data[DOMAIN]["voice_cache"][speech_url] = (fetched_at, [(id, name), ...])
_CACHE_KEY = "voice_cache"


def _voices_url_from_speech_url(speech_url: str) -> str:
    """Derive the voices-list endpoint from the configured speech endpoint.

    Handles the common shapes users paste in:
        http://host:8880/v1/audio/speech       -> http://host:8880/v1/audio/voices
        http://host:8880/v1/audio/speech/       -> http://host:8880/v1/audio/voices
        http://host:8880/v1                     -> http://host:8880/v1/audio/voices
        http://host:8880                        -> http://host:8880/audio/voices
    """
    parsed = urlparse(speech_url)
    path = parsed.path.rstrip("/")

    if path.endswith("/audio/speech"):
        new_path = path[: -len("/speech")] + "/voices"
    elif path.endswith("/v1"):
        new_path = path + "/audio/voices"
    elif path == "":
        new_path = "/audio/voices"
    else:
        # Unknown shape - best effort: assume sibling of the last segment.
        new_path = path.rsplit("/", 1)[0] + "/audio/voices"

    return urljoin(f"{parsed.scheme}://{parsed.netloc}", new_path)


def _normalize_voice_entry(entry: Any) -> tuple[str, str] | None:
    """Normalize one entry from either response shape into (id, name)."""
    if isinstance(entry, str):
        voice_id = entry
        # "af_bella" -> "Bella (af)" - readable without a real display name.
        parts = voice_id.split("_", 1)
        name = parts[1].replace("_", " ").title() if len(parts) == 2 else voice_id
        return voice_id, name
    if isinstance(entry, dict):
        voice_id = entry.get("id") or entry.get("voice_id") or entry.get("value")
        if not voice_id:
            return None
        name = entry.get("name") or entry.get("label") or voice_id
        return str(voice_id), str(name)
    return None


def _normalize_payload(payload: Any) -> list[tuple[str, str]] | None:
    """Pull the voice list out of either the wrapped or bare response shape."""
    if isinstance(payload, dict):
        raw_list = payload.get("voices")
        if raw_list is None:
            return None
    elif isinstance(payload, list):
        raw_list = payload
    else:
        return None

    voices: list[tuple[str, str]] = []
    for entry in raw_list:
        normalized = _normalize_voice_entry(entry)
        if normalized is not None:
            voices.append(normalized)

    return voices or None


async def async_fetch_remote_voices(
    hass: HomeAssistant,
    speech_url: str,
    api_key: str | None = None,
    *,
    timeout: float = 5.0,
) -> list[tuple[str, str]] | None:
    """Fetch and normalize the voice list from a custom backend.

    Returns a list of ``(id, name)`` tuples, or ``None`` if discovery
    isn't supported / failed for any reason. Never raises.
    """
    voices_url = _voices_url_from_speech_url(speech_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            voices_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:
                _LOGGER.debug(
                    "Voice discovery: %s returned HTTP %s, backend likely "
                    "doesn't support voice listing",
                    voices_url,
                    response.status,
                )
                return None
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                _LOGGER.debug(
                    "Voice discovery: %s returned non-JSON body: %s",
                    voices_url,
                    err,
                )
                return None
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug(
            "Voice discovery: could not reach %s: %s", voices_url, err
        )
        return None

    voices = _normalize_payload(payload)
    if voices is None:
        _LOGGER.debug(
            "Voice discovery: %s returned an unrecognized payload shape",
            voices_url,
        )
    return voices


async def async_get_cached_remote_voices(
    hass: HomeAssistant,
    domain_data_root: dict,
    speech_url: str,
    api_key: str | None = None,
    *,
    force_refresh: bool = False,
) -> list[tuple[str, str]] | None:
    """Cached wrapper around ``async_fetch_remote_voices``.

    ``domain_data_root`` is the integration's ``hass.data[DOMAIN]`` dict -
    passed in explicitly rather than imported, so this module doesn't need
    to know about ``__init__.py``'s setup shape.
    """
    cache: dict[str, tuple[float, list[tuple[str, str]]]] = domain_data_root.setdefault(
        _CACHE_KEY, {}
    )

    if not force_refresh:
        cached = cache.get(speech_url)
        if cached is not None:
            fetched_at, voices = cached
            if time.monotonic() - fetched_at < _CACHE_TTL_SECONDS:
                return voices

    voices = await async_fetch_remote_voices(hass, speech_url, api_key)
    if voices is not None:
        cache[speech_url] = (time.monotonic(), voices)
    return voices
