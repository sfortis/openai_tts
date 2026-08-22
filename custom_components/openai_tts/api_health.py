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

# Statuses that guarantee the next API call fails for a reason the user
# has to fix outside Home Assistant: a dead key, an empty balance.
BLOCKING_STATUSES: frozenset[str] = frozenset(
    {API_STATUS_AUTH_FAILED, API_STATUS_QUOTA_EXCEEDED}
)

# How long a blocking status suppresses calls before one is let through.
#
# Without a window the block is permanent: only a success clears the
# status, and no success is possible while every call is blocked, so the
# only way out is reloading the config entry. That is defensible for a
# bad API key, where the fix is a reconfigure and reloads anyway, but
# wrong for an exhausted balance: the user tops up at the provider and
# changes nothing here, so Home Assistant has to find out by trying.
#
# Ten minutes keeps the cost low. A blocked speaker wakes at most once
# per window, and the failure path restores its volume immediately.
BLOCKING_RETRY_AFTER_S = 600.0

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
            # Passed explicitly rather than left to the ContextVar the
            # base class falls back on, which only works while the
            # coordinator is built inside the entry's own setup.
            config_entry=entry,
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

    def blocks_requests(
        self, retry_after_s: float = BLOCKING_RETRY_AFTER_S
    ) -> bool:
        """True when a request should be refused without being attempted.

        A blocking status stops being blocking once ``retry_after_s`` has
        passed since the failure was recorded, which lets a single call
        through to discover that the problem is gone. If it still fails,
        ``record_error`` stamps a fresh timestamp and the window starts
        again.
        """
        if self.status not in BLOCKING_STATUSES:
            return False
        raw = self.data.get("last_error_at")
        if not raw:
            # Status without a timestamp: nothing to age out, stay blocked.
            return True
        try:
            failed_at = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return True
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - failed_at).total_seconds()
        if elapsed < retry_after_s:
            return True
        _LOGGER.info(
            "API status %s is %.0fs old; letting one request through to "
            "check whether it has been resolved", self.status, elapsed,
        )
        return False

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


# Each parent config entry carries its own tracker as runtime data. The
# tracker used to live in ``hass.data`` under a key built from the entry
# id, alongside a single ``main_entry`` slot that every parent entry
# overwrote, so with two providers configured that slot named whichever
# one was set up last.
type OpenAITTSConfigEntry = ConfigEntry[OpenAITTSHealthTracker]


def health_tracker_for(
    entry: ConfigEntry | None,
) -> Optional[OpenAITTSHealthTracker]:
    """Return the tracker an entry carries, or None.

    ``runtime_data`` is unset until the entry finishes setting up, and
    subentries never carry one, so callers that reach an entry through
    the registry need this rather than a bare attribute read.
    """
    if entry is None:
        return None
    tracker = getattr(entry, "runtime_data", None)
    return tracker if isinstance(tracker, OpenAITTSHealthTracker) else None
