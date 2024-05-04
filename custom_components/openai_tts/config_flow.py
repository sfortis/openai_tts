"""Config flow for OpenAI text-to-speech custom component."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
import logging

from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.selector import selector
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_API_KEY, CONF_MODEL, CONF_VOICE, CONF_SPEED, DOMAIN, MODELS, VOICES

_LOGGER = logging.getLogger(__name__)


async def validate_input(user_input: dict):
    """ Function to validate provided  data"""
    api_key_length = len(user_input['CONF_API_KEY'])
    if not (51 <= api_key_length <= 56):
        raise WrongAPIKey


class OpenAITTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow ."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""

        data_schema = {vol.Required(CONF_API_KEY): str,
                       vol.Optional(CONF_SPEED, default=1): int,
                       CONF_MODEL: selector({
                           "select": {
                               "options": MODELS,
                               "mode": "dropdown",
                               "sort": True,
                               "custom_value": False
                           }
                       }), CONF_VOICE: selector({
                            "select": {
                                "options": VOICES,
                                "mode": "dropdown",
                                "sort": True,
                                "custom_value": False
                            }
                       })
        }

        errors = {}

        if user_input is not None:
            try:
                self._async_abort_entries_match({CONF_VOICE: user_input[CONF_VOICE]})
                await validate_input(user_input)
                return self.async_create_entry(title="OpenAI TTS", data=user_input)
            except WrongAPIKey:
                _LOGGER.exception("Wrong or no API key provided.")
                errors[CONF_API_KEY] = "wrong_api_key"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unknown exception.")
                errors["base"] = "Unknown exception."

        return self.async_show_form(step_id="user", data_schema=vol.Schema(data_schema))


class WrongAPIKey(HomeAssistantError):
    """Error to indicate no or wrong API key."""
