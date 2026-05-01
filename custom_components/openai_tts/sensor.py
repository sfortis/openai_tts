"""API status sensor for the OpenAI TTS integration.

Exposes ``sensor.openai_tts_api_status`` per parent config entry. The state
is the discrete status code (``ok``, ``quota_exceeded``, ``auth_failed``,
``rate_limited``, ``server_error``, ``network_error``, ``unknown_error``),
which maps cleanly onto Home Assistant's ENUM device class.

The sensor's ``description`` attribute carries a short, user-readable
sentence so non-technical users can tell what's wrong at a glance without
having to know what each status code means.

Example automation:

    trigger:
      platform: state
      entity_id: sensor.openai_tts_api_status
      to: 'quota_exceeded'
    action:
      service: notify.persistent
      data:
        message: "OpenAI TTS: balance exhausted, please recharge"

This satisfies the original ask in issue #64 ("would it be possible to log
$0 balance in HA?").
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api_health import (
    ALL_STATUSES,
    API_STATUS_OK,
    OpenAITTSHealthTracker,
    STATUS_DESCRIPTIONS,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

HEALTH_TRACKER_KEY = "_health_tracker"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the API status sensor for ``entry``.

    Only parent entries get a sensor. Subentries (TTS profiles) inherit the
    parent's health state since the API key (billing scope) lives on the
    parent.
    """
    domain_data = hass.data.get(DOMAIN, {})
    tracker = domain_data.get(f"{entry.entry_id}{HEALTH_TRACKER_KEY}")
    if tracker is None:
        _LOGGER.debug(
            "No health tracker for %s; skipping API status sensor",
            entry.entry_id,
        )
        return
    async_add_entities([OpenAITTSAPIStatusSensor(tracker)])


class OpenAITTSAPIStatusSensor(
    CoordinatorEntity[OpenAITTSHealthTracker], SensorEntity
):
    """Reports the discrete OpenAI TTS API status as an ENUM sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ALL_STATUSES)
    _attr_translation_key = "api_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-check"

    def __init__(self, tracker: OpenAITTSHealthTracker) -> None:
        super().__init__(tracker)
        self._attr_unique_id = f"{tracker.entry_id}_api_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{tracker.entry_id}_health")},
            "name": "OpenAI TTS API",
            "manufacturer": "OpenAI",
            "model": "TTS Health Tracker",
            "entry_type": "service",
        }

    @property
    def native_value(self) -> str:
        """Return the current API status as the sensor's state."""
        return self.coordinator.data.get("status", API_STATUS_OK)

    @property
    def icon(self) -> str:
        """Cloud-check when healthy; cloud-alert otherwise."""
        return (
            "mdi:cloud-check"
            if self.coordinator.data.get("status") == API_STATUS_OK
            else "mdi:cloud-alert"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.coordinator.data.get("status", API_STATUS_OK)
        return {
            "description": STATUS_DESCRIPTIONS.get(status, status),
            "last_success_at": self.coordinator.data.get("last_success_at"),
            "last_error_at": self.coordinator.data.get("last_error_at"),
            "last_error_message": self.coordinator.data.get("last_error_message"),
        }
