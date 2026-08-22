"""Live voice catalogues from the provider's REST API.

OpenAI itself has no voices endpoint: three plausible paths were tried
against the real API on 2026-08-22 and all answered 404, so its
catalogue can only come from the static tables in ``const.py``. Every
other backend this integration talks to is different. Mistral clones
voices per account, Kokoro ships whatever voicepacks are installed, and
self-hosted servers vary, so for those the only correct list is the one
the backend reports.

Both the config flow, which fills the voice picker, and the TTS entity,
which answers Home Assistant's own voice dropdown, read the catalogue
from here so there is one transport and one set of response shapes to
maintain.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

_LOGGER = logging.getLogger(__name__)

# The listing endpoint sits beside the configured speech endpoint.
VOICES_PATH = "voices"

# How long a fetched catalogue is trusted before a reader asks for a
# fresh one. Voices change when someone clones or installs one, which is
# rare, so this is about eventual accuracy rather than being current to
# the second.
CATALOGUE_TTL_S = 1800.0


def voices_url_for(speech_url: str) -> str:
    """Return the voice-listing URL beside ``speech_url``.

    Derived rather than configured, so it follows whatever base path the
    user typed, be it api.mistral.ai/v1/audio/speech or a self-hosted
    equivalent.
    """
    return speech_url.rsplit("/", 1)[0] + f"/{VOICES_PATH}"


async def async_fetch_voice_options(
    hass: HomeAssistant, speech_url: str, api_key: str | None
) -> list[dict[str, str]] | None:
    """Fetch the catalogue, or return None if it cannot be had.

    Never raises. A backend that is unreachable, slow, or answering
    something unexpected has to degrade to a typed voice name rather
    than break the config flow or the entity.
    """
    voices_url = voices_url_for(speech_url)
    headers = {"User-Agent": "HomeAssistant-OpenAI-TTS"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        session = aiohttp_client.async_get_clientsession(hass)
        timeout = aiohttp.ClientTimeout(total=8)
        async with session.get(
            voices_url, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "Voice listing returned HTTP %s for %s",
                    resp.status, voices_url,
                )
                return None
            # ``content_type=None`` disables aiohttp's strict
            # application/json check: several self-hosted backends
            # serve the voice list as text/plain and would
            # otherwise raise ContentTypeError on a perfectly
            # valid JSON body.
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.debug("Voice listing fetch failed for %s: %s", voices_url, err)
        return None
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug(
            "Voice listing fetch raised for %s", voices_url, exc_info=True
        )
        return None

    return voice_options_from_payload(payload, voices_url)


def voice_options_from_payload(
    payload: Any, source_url: str = ""
) -> list[dict[str, str]] | None:
    """Turn a voice-listing response into selector options.

    Returns options of the form ``[{"value": <id>, "label": <name>},
    ...]`` or ``None`` when the payload holds nothing usable. Never
    raises: a backend we have never seen must degrade to the free-text
    voice field, not break the config flow.

    Response shapes seen in the wild:

    * Mistral:        ``{"items": [{"id": uuid, "name": "..."}], "total": N}``
    * OpenAI-style:   ``{"data":  [{"id": str,  "name": "..."}]}``
    * Kokoro-FastAPI: ``{"voices": ["af_bella", "am_adam", ...]}``
    * bare list:      ``["af_bella", ...]`` or ``[{"id": ...}, ...]``

    The bare-list shapes matter because several OpenAI-compatible
    self-hosted servers answer ``GET /v1/audio/voices`` with a
    top-level JSON array. Calling ``.get()`` on that raised
    ``AttributeError`` out of the config flow before this helper
    existed.
    """
    if isinstance(payload, list):
        items: Any = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("items")
            or payload.get("data")
            or payload.get("voices")
            or []
        )
    else:
        _LOGGER.debug(
            "Voice listing at %s returned an unsupported top-level type: %s",
            source_url, type(payload).__name__,
        )
        return None

    if not isinstance(items, list):
        _LOGGER.debug(
            "Voice listing at %s held a non-list voice collection: %s",
            source_url, type(items).__name__,
        )
        return None

    options: list[dict[str, str]] = []
    for v in items:
        if isinstance(v, str):
            # Plain string voice name (Kokoro-FastAPI). value == label
            # is fine because the slug is what the user reads in the
            # UI ("af_bella") and what the request needs.
            if v:
                options.append({"value": v, "label": v})
            continue
        if not isinstance(v, dict):
            continue
        voice_id = v.get("id") or v.get("voice_id") or v.get("value")
        if not voice_id:
            continue
        label = v.get("name") or v.get("label") or voice_id
        options.append({"value": str(voice_id), "label": str(label)})
    return options or None
