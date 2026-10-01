"""Check whether an API key is accepted by a speech endpoint.

This lives on its own because two very different callers need it. The
config flow asks before it writes an entry, and the ``set_api_key``
action asks before it rotates a key on an entry that already exists.
Runtime code should not have to import the config flow, which is a user
interface module and free to change its steps and selectors.

The probe is a speech request with an empty text. Every provider
checks the key before it looks at the body, so a bad key is refused with
401 or 403, and a good one gets as far as the body and is refused for
the empty text with 400 or 422. Nothing is synthesised and nothing is
billed. Measured on 2026-10-01: OpenAI answers a good key with 400
"String should have at least 1 character" and a bad one with 401, and
OpenRouter answers 400 "Model tts-1 does not exist" and 401.

The failures are reported with the integration's own exception classes
rather than a second set defined next to the caller. ``OpenAIAuthError``
means the key was refused. Any other ``OpenAITTSError`` means the answer
says nothing about the key, such as a timeout, a 5xx, a 429 or a 404
from a wrong address, and the caller decides what that is worth.
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

# A request that passes authentication and then fails on its body, so
# that no audio is ever produced. The model and voice only make the body
# look like a speech request; a provider that knows neither still
# refuses it after the key check.
_PROBE_PAYLOAD = {
    "model": "tts-1",
    "input": "",
    "voice": "alloy",
    "response_format": "mp3",
}

# Answers that mean the request got past authentication. 2xx covers a
# server that accepts an empty text, 400 and 422 the usual refusal of it.
_KEY_ACCEPTED_STATUSES = frozenset({400, 422})

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
            if (
                not 200 <= response.status < 300
                and response.status not in _KEY_ACCEPTED_STATUSES
            ):
                # A 404 from a wrong address, a 429, or a redirect
                # aiohttp did not follow: none of these says whether the
                # key is good, so none of them is taken as acceptance.
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


async def async_ensure_key_not_rejected(
    hass: HomeAssistant, api_key: str, url: str
) -> None:
    """Raise ``OpenAIAuthError`` if ``url`` refuses ``api_key``.

    For the setup forms, where someone is looking. Only a refused key
    stops the form. When the check cannot tell, because the endpoint is
    slow, rate limited or answers with something unexpected, the key is
    kept and the reason is logged: refusing a working key over a timeout
    would leave the user unable to finish setup, and a key that really
    is bad raises re-authentication at its first use.
    """
    try:
        await async_validate_api_key(hass, api_key, url)
    except OpenAIAuthError:
        raise
    except OpenAITTSError as err:
        _LOGGER.warning(
            "Could not check the API key against %s, keeping it: %s",
            url, err,
        )
