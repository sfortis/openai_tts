"""Check whether an API key is accepted by a speech endpoint.

This lives on its own because two very different callers need it. The
config flow asks before it writes an entry, and the ``set_api_key``
action asks before it rotates a key on an entry that already exists.
Runtime code should not have to import the config flow, which is a user
interface module and free to change its steps and selectors.

The failures are reported with the integration's own exception classes
rather than a second set defined next to the caller. Only 401 and 403
mean the endpoint looked at the key and refused it; everything else,
including a 400 because a self-hosted backend has never heard of the
model this probe asks for, says nothing about the key itself.
"""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .exceptions import (
    OpenAIAuthError,
    OpenAINetworkError,
    OpenAIServerError,
    OpenAITTSError,
)

_LOGGER = logging.getLogger(__name__)

# The smallest request that still exercises authentication. A single full
# stop keeps the bill and the wait to the minimum on providers that
# charge by the character.
_PROBE_PAYLOAD = {
    "model": "tts-1",
    "input": ".",
    "voice": "alloy",
    "response_format": "mp3",
}

_TIMEOUT_S = 10


async def async_validate_api_key(
    hass: HomeAssistant, api_key: str, url: str
) -> bool:
    """Return True when ``url`` accepts ``api_key``.

    Raises ``OpenAIAuthError`` when the endpoint rejected the key, and
    one of the other ``OpenAITTSError`` subclasses when the answer says
    nothing about the key: the caller has to keep those apart, because
    refusing a rotation over a timeout would be wrong.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json=_PROBE_PAYLOAD,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
        ) as response:
            if response.status in (401, 403):
                _LOGGER.error(
                    "API key validation failed with HTTP %d", response.status
                )
                raise OpenAIAuthError(
                    "The endpoint refused this key"
                    if response.status == 401
                    else "This key lacks the required permissions"
                )
            if response.status >= 500:
                _LOGGER.error(
                    "API validation could not complete, HTTP %d",
                    response.status,
                )
                raise OpenAIServerError(f"API returned status {response.status}")
            if not 200 <= response.status < 300:
                # Everything that is neither an auth answer nor a server
                # error lands here, and none of it says the key is bad. A
                # backend that has never heard of the model this probe
                # asks for answers 400, and a redirect aiohttp did not
                # follow answers 3xx. Only a 2xx is taken as acceptance:
                # falling through on anything else would call a key good
                # without the endpoint ever having said so.
                _LOGGER.error(
                    "API validation could not complete, HTTP %d",
                    response.status,
                )
                raise OpenAITTSError(f"API returned status {response.status}")

            _LOGGER.debug("API key validation successful")
            return True

    except TimeoutError as err:
        _LOGGER.error("Timeout during API validation")
        raise OpenAINetworkError("Connection timed out") from err
    except aiohttp.ClientError as err:
        _LOGGER.error("Connection error during API validation: %s", err)
        raise OpenAINetworkError(f"Cannot connect to API: {err}") from err
