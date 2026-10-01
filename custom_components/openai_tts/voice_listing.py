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
from here so there is one transport to maintain. The response shapes
are parsed in ``catalogue_parsers``.

Two sources exist, and a provider preset names its source in
``catalogue_source``. Most backends publish their voices beside the
speech endpoint, at ``/v1/audio/voices``. OpenRouter does not: it
answers 404 there, and publishes its speech models together with the
voices each one accepts at ``/api/v1/models``. Its voices therefore
depend on the model, which the first source knows nothing about.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

from .catalogue_parsers import (
    openrouter_catalogue_from_payload,
    voice_options_for_model,
    voice_options_from_payload,
)

_LOGGER = logging.getLogger(__name__)

# The values a preset may give ``catalogue_source``.
CATALOGUE_VOICES_ENDPOINT = "voices_endpoint"
CATALOGUE_OPENROUTER = "openrouter_models"

# The listing endpoint sits beside the configured speech endpoint.
VOICES_PATH = "voices"

# How long a fetched catalogue is trusted before a reader asks for a
# fresh one. Voices change when someone clones or installs one, which is
# rare, so this is about eventual accuracy rather than being current to
# the second.
CATALOGUE_TTL_S = 1800.0

# How many voices to ask for in a single request. Mistral paginates
# ``GET /v1/audio/voices`` and defaults to ten per page, so a
# parameterless fetch hid every voice past the first page from the
# picker. The endpoint documents ``limit``, ``offset`` and ``type``
# but states no maximum for any of them.
#
# A hundred is the ceiling reported on pull request #75, measured
# against a live account: the full catalogue came back at a hundred and
# anything above it answered HTTP 422. That was not re-verified here,
# because the endpoint answers 401 before it validates query
# parameters, so it cannot be probed without a key. Raising this value
# on an untested guess would cost every Mistral user their voice
# picker, since a rejected listing degrades to a free-text field.
#
# Backends that do not paginate declare no such parameter and drop it,
# so sending it costs them nothing.
VOICE_PAGE_LIMIT = 100


def voices_url_for(speech_url: str) -> str:
    """Return the voice-listing URL beside ``speech_url``.

    Derived rather than configured, so it follows whatever base path the
    user typed, be it api.mistral.ai/v1/audio/speech or a self-hosted
    equivalent.
    """
    return speech_url.rsplit("/", 1)[0] + f"/{VOICES_PATH}"


def openrouter_models_url_for(speech_url: str) -> str:
    """Return OpenRouter's model listing URL for ``speech_url``.

    ``https://openrouter.ai/api/v1/audio/speech`` becomes
    ``https://openrouter.ai/api/v1/models``.
    """
    base = speech_url.rstrip("/")
    if base.endswith("/audio/speech"):
        base = base[: -len("/audio/speech")]
    return f"{base}/models"


async def _async_get_json(
    hass: HomeAssistant,
    url: str,
    api_key: str | None,
    params: dict[str, Any] | None = None,
) -> Any | None:
    """GET ``url`` and return its JSON body, or ``None`` on any failure."""
    headers = {"User-Agent": "HomeAssistant-OpenAI-TTS"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        session = aiohttp_client.async_get_clientsession(hass)
        timeout = aiohttp.ClientTimeout(total=8)
        async with session.get(
            url, headers=headers, timeout=timeout, params=params
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "Catalogue listing returned HTTP %s for %s",
                    resp.status, url,
                )
                return None
            # ``content_type=None`` disables aiohttp's strict
            # application/json check: several self-hosted backends
            # serve the voice list as text/plain and would
            # otherwise raise ContentTypeError on a perfectly
            # valid JSON body.
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.debug("Catalogue fetch failed for %s: %s", url, err)
        return None
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("Catalogue fetch raised for %s", url, exc_info=True)
        return None


# OpenRouter answers for every model at once, and several readers want
# the same answer within seconds of each other: the model step and the
# voice step of one config flow, and every profile on one parent entry
# when Home Assistant starts. One fetch serves them all for a while.
_MODEL_CATALOGUE_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}


async def async_fetch_model_catalogue(
    hass: HomeAssistant,
    speech_url: str,
    api_key: str | None,
    source: str | None,
) -> dict[str, list[str]] | None:
    """Return ``{model id: [voices]}`` for this source, or ``None``.

    Only the OpenRouter source can list models. Every other source
    answers ``None``, and the caller keeps the preset's static list.
    Never raises.
    """
    if source != CATALOGUE_OPENROUTER:
        return None
    url = openrouter_models_url_for(speech_url)
    cached = _MODEL_CATALOGUE_CACHE.get(url)
    if cached is not None and time.monotonic() - cached[0] < CATALOGUE_TTL_S:
        return cached[1]
    payload = await _async_get_json(
        hass, url, api_key, params={"output_modalities": "speech"}
    )
    catalogue = (
        openrouter_catalogue_from_payload(payload) if payload is not None else None
    )
    if catalogue is None:
        # Keep an older answer rather than none at all. A listing that is
        # briefly unreachable should not empty the model picker.
        return cached[1] if cached is not None else None
    _MODEL_CATALOGUE_CACHE[url] = (time.monotonic(), catalogue)
    return catalogue


async def async_fetch_voice_options(
    hass: HomeAssistant,
    speech_url: str,
    api_key: str | None,
    *,
    model: str | None = None,
    source: str | None = None,
) -> list[dict[str, str]] | None:
    """Fetch the voice catalogue, or return None if it cannot be had.

    ``model`` matters only to a source whose voices depend on it, which
    today is OpenRouter. Never raises. A backend that is unreachable,
    slow, or answering something unexpected has to degrade to a typed
    voice name rather than break the config flow or the entity.
    """
    if source == CATALOGUE_OPENROUTER:
        catalogue = await async_fetch_model_catalogue(
            hass, speech_url, api_key, source
        )
        return voice_options_for_model(catalogue, model)

    voices_url = voices_url_for(speech_url)
    payload = await _async_get_json(
        hass, voices_url, api_key, params={"limit": VOICE_PAGE_LIMIT}
    )
    if payload is None:
        return None
    return voice_options_from_payload(payload, voices_url)
