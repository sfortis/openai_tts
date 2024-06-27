"""Config flow for OpenAI text-to-speech custom component."""
from __future__ import annotations
from typing import Any
import voluptuous as vol
import logging

from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.selector import selector
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_API_KEY, CONF_MODEL, CONF_VOICE, CONF_SPEED, DOMAIN, MODELS, VOICES

_LOGGER = logging.getLogger(__name__)

class WrongAPIKey(HomeAssistantError):
    """Error to indicate no or wrong API key."""

async def validate_api_key(api_key: str):
    """Validate the API key format."""
    if api_key is None:
        raise WrongAPIKey("API key is required")
    if not (51 <= len(api_key) <= 70):
        raise WrongAPIKey("Invalid API key length")

async def validate_user_input(user_input: dict):
    """Validate user input fields."""
    await validate_api_key(user_input.get(CONF_API_KEY))
    if user_input.get(CONF_MODEL) is None:
        raise ValueError("Model is required")
    if user_input.get(CONF_VOICE) is None:
        raise ValueError("Voice is required")

class OpenAITTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenAI TTS."""
    VERSION = 1
    data_schema = vol.Schema({
        vol.Required(CONF_API_KEY): str,
        vol.Optional(CONF_SPEED, default=1.0): vol.Coerce(float),
        vol.Required(CONF_MODEL, default="tts-1"): selector({
            "select": {
                "options": MODELS,
                "mode": "dropdown",
                "sort": True,
                "custom_value": True
            }
        }),
        vol.Required(CONF_VOICE, default="shimmer"): selector({
            "select": {
                "options": VOICES,
                "mode": "dropdown",
                "sort": True,
                "custom_value": True
            }
        })
    })

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                await validate_user_input(user_input)
                await self.async_set_unique_id(f"{user_input[CONF_VOICE]}_{user_input[CONF_MODEL]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="OpenAI TTS", data=user_input)
            except data_entry_flow.AbortFlow:
                return self.async_abort(reason="already_configured")
            except HomeAssistantError as e:
                _LOGGER.exception(str(e))
                errors["api_key"] = "wrong_api_key"
            except ValueError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.exception(str(e))
                errors["base"] = "unknown_error"
        return self.async_show_form(step_id="user", data_schema=self.data_schema, errors=errors, description_placeholders=user_input)
