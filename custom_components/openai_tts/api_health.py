"""Tracks the OpenAI TTS API health for the binary sensor.

Single source of truth for "is the API currently healthy?". The TTS engine
calls :meth:`OpenAITTSHealthTracker.record_success` after every successful
request and :meth:`record_error` after every failure; the binary sensor
subscribes to ``async_add_listener`` and re-renders on every state change.

State is intentionally NOT persisted across restarts: on startup we don't
know whether the cloud is healthy until the first request, so the natural
default is "ok" and we let the first call confirm it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Discrete API health values. ``ok`` is the only "problem-free" state.
API_STATUS_OK = "ok"
API_STATUS_QUOTA_EXCEEDED = "quota_exceeded"
API_STATUS_AUTH_FAILED = "auth_failed"
API_STATUS_RATE_LIMITED = "rate_limited"
API_STATUS_SERVER_ERROR = "server_error"
API_STATUS_NETWORK_ERROR = "network_error"
API_STATUS_UNKNOWN_ERROR = "unknown_error"

# All statuses that the sensor can publish (used as ENUM options).
ALL_STATUSES: tuple[str, ...] = (
    API_STATUS_OK,
    API_STATUS_QUOTA_EXCEEDED,
    API_STATUS_AUTH_FAILED,
    API_STATUS_RATE_LIMITED,
    API_STATUS_SERVER_ERROR,
    API_STATUS_NETWORK_ERROR,
    API_STATUS_UNKNOWN_ERROR,
)

# Human-readable description shown to non-technical users via the
# ``description`` attribute. Kept short - the state itself carries the
# machine-readable value, this is just for the dashboard glance.
STATUS_DESCRIPTIONS: dict[str, str] = {
    API_STATUS_OK:
        "All systems operational",
    API_STATUS_QUOTA_EXCEEDED:
        "OpenAI account balance/quota exhausted - recharge required",
    API_STATUS_AUTH_FAILED:
        "API key is invalid or expired - reauthorization required",
    API_STATUS_RATE_LIMITED:
        "Rate limited - too many requests, automatically retrying",
    API_STATUS_SERVER_ERROR:
        "OpenAI service error - try again later",
    API_STATUS_NETWORK_ERROR:
        "Cannot reach the TTS endpoint - check internet/DNS or custom backend",
    API_STATUS_UNKNOWN_ERROR:
        "Unexpected error - check logs for details",
}

# Map exception class names to status. Decoupled from ``exceptions.py`` so
# this module doesn't need to import (or grow alongside) the exception
# hierarchy - just match by class name.
ERROR_NAME_TO_STATUS = {
    "OpenAIAuthError": API_STATUS_AUTH_FAILED,
    "OpenAIQuotaExceededError": API_STATUS_QUOTA_EXCEEDED,
    "OpenAIRateLimitError": API_STATUS_RATE_LIMITED,
    "OpenAIServerError": API_STATUS_SERVER_ERROR,
    "OpenAINetworkError": API_STATUS_NETWORK_ERROR,
}


class OpenAITTSHealthTracker(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator-shaped tracker for API health.

    We use ``DataUpdateCoordinator`` because the binary sensor's
    ``CoordinatorEntity`` machinery already wires up listeners and
    ``available`` for free; nothing else here actually polls.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_health_{entry.entry_id}",
            update_interval=None,
        )
        self._entry = entry
        self.data = {
            "status": API_STATUS_OK,
            "last_success_at": None,
            "last_error_at": None,
            "last_error_message": None,
        }

    @property
    def entry_id(self) -> str:
        return self._entry.entry_id

    @property
    def status(self) -> str:
        return self.data.get("status", API_STATUS_OK)

    @property
    def is_problem(self) -> bool:
        """True when the sensor should report an active problem."""
        return self.status != API_STATUS_OK

    def record_success(self) -> None:
        """Note a successful TTS call. Clears any prior unhealthy status."""
        previous = self.status
        self.data = {
            **self.data,
            "status": API_STATUS_OK,
            "last_success_at": datetime.now(timezone.utc).isoformat(),
        }
        if previous != API_STATUS_OK:
            _LOGGER.info("OpenAI TTS API health recovered (%s -> ok)", previous)
        self.async_set_updated_data(self.data)

    def record_error(
        self, error: BaseException, message: Optional[str] = None
    ) -> None:
        """Map ``error`` to a status code and surface it to the sensor."""
        new_status = ERROR_NAME_TO_STATUS.get(
            type(error).__name__, API_STATUS_UNKNOWN_ERROR
        )
        self.data = {
            **self.data,
            "status": new_status,
            "last_error_at": datetime.now(timezone.utc).isoformat(),
            "last_error_message": message or str(error),
        }
        _LOGGER.debug(
            "Recorded API error: status=%s message=%s",
            new_status, self.data["last_error_message"],
        )
        self.async_set_updated_data(self.data)
