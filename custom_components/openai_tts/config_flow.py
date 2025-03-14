"""
Config flow for OpenAI TTS.
"""
from __future__ import annotations
from typing import Any
import voluptuous as vol
import logging
from urllib.parse import urlparse
import uuid

from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers.selector import selector
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_API_KEY, CONF_MODEL, CONF_VOICE, CONF_SPEED, CONF_URL, DOMAIN, MODELS, VOICES, UNIQUE_ID

_LOGGER = logging.getLogger(__name__)

def generate_entry_id() -> str:
    return str(uuid.uuid4())

async def validate_user_input(user_input: dict):
    if user_input.get(CONF_MODEL) is None:
        raise ValueError("Model is required")
    if user_input.get(CONF_VOICE) is None:
        raise ValueError("Voice is required")

class OpenAITTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenAI TTS."""
    VERSION = 1
    data_schema = vol.Schema({
        vol.Optional(CONF_API_KEY): str,
        vol.Optional(CONF_URL, default="https://api.openai.com/v1/audio/speech"): str,
        vol.Optional(CONF_SPEED, default=1.0): selector({
            "number": {
                "min": 0.25,
                "max": 4.0,
                "step": 0.05,
                "mode": "slider"
            }
        }),
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
        errors = {}
        if user_input is not None:
            try:
                await validate_user_input(user_input)
                # Generate a random unique id so multiple integrations can be added.
                entry_id = generate_entry_id()
                user_input[UNIQUE_ID] = entry_id
                await self.async_set_unique_id(entry_id)
                hostname = urlparse(user_input[CONF_URL]).hostname
                return self.async_create_entry(
                    title=f"OpenAI TTS ({hostname}, {user_input[CONF_MODEL]})",
                    data=user_input
                )
            except data_entry_flow.AbortFlow:
                return self.async_abort(reason="already_configured")
            except HomeAssistantError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except ValueError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except Exception as e:
                _LOGGER.exception(str(e))
                errors["base"] = "unknown_error"
        return self.async_show_form(
            step_id="user",
            data_schema=self.data_schema,
            errors=errors,
            description_placeholders=user_input
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return OpenAITTSOptionsFlow()

class OpenAITTSOptionsFlow(OptionsFlow):
    """Handle options flow for OpenAI TTS."""
    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options_schema = vol.Schema({
            vol.Optional(
                "chime",
                default=self.config_entry.options.get("chime", self.config_entry.data.get("chime", False))
            ): selector({"boolean": {}}),
            vol.Optional(
                CONF_SPEED,
                default=self.config_entry.options.get(CONF_SPEED, self.config_entry.data.get(CONF_SPEED, 1.0))
            ): selector({
                "number": {
                    "min": 0.25,
                    "max": 4.0,
                    "step": 0.05,
                    "mode": "slider"
                }
            }),
            vol.Optional(
                CONF_VOICE,
                default=self.config_entry.options.get(CONF_VOICE, self.config_entry.data.get(CONF_VOICE, "shimmer"))
            ): selector({
                "select": {
                    "options": VOICES,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True
                }
            })
        })
        return self.async_show_form(step_id="init", data_schema=options_schema)
