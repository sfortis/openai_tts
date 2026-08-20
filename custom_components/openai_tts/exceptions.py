"""Custom exceptions for the OpenAI TTS integration.

These exceptions allow callers to react to specific failure modes
(e.g. trigger a reauth flow on auth errors, surface a quota-exceeded
state on 402, back off on 429) instead of treating every API failure
as a generic network error.
"""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class OpenAITTSError(HomeAssistantError):
    """Base class for all OpenAI TTS engine errors."""


class OpenAIAuthError(OpenAITTSError):
    """API key is invalid, revoked, or lacks the required permissions.

    Triggered by HTTP 401 / 403. Should escalate to a reauth flow.
    """


class OpenAIQuotaExceededError(OpenAITTSError):
    """The account has insufficient balance / quota to process the request.

    Triggered by HTTP 402, or by HTTP 429 whose body indicates
    `insufficient_quota` (OpenAI uses 429 for both rate limits and
    out-of-credits situations).
    """


class OpenAIRateLimitError(OpenAITTSError):
    """The request was rate-limited but the account itself is healthy.

    Triggered by HTTP 429 when the body does not indicate `insufficient_quota`.
    Callers may retry with backoff.
    """


class OpenAIServerError(OpenAITTSError):
    """OpenAI-side server error (HTTP 5xx). Safe to retry."""


class OpenAIInvalidResponseError(OpenAITTSError):
    """The HTTP request succeeded (2xx) but the body is not valid audio.

    Catches the cache-poisoning class of bugs where a misbehaving proxy
    or custom backend returns an error JSON / HTML page with status 200.
    """


class OpenAINetworkError(OpenAITTSError):
    """Could not even reach the API (DNS / TCP / TLS / connection refused).

    Distinct from ``OpenAITTSError`` so the health tracker can surface a
    ``network_error`` status separate from generic / unknown failures.
    Typical causes: custom backend offline, DNS misconfig, network down.
    """


class OpenAIVoiceDeletedError(OpenAITTSError):
    """The configured voice no longer exists on the provider.

    Raised when the API rejects a request because the ``voice`` field
    references an ID that has been deleted (Mistral) or otherwise made
    unavailable. The TTS layer catches this to surface a Home Assistant
    Repairs issue so the user gets a one-click path to reconfigure the
    profile with a current voice.
    """

    def __init__(self, message: str, voice: str | None = None) -> None:
        super().__init__(message)
        self.voice = voice
