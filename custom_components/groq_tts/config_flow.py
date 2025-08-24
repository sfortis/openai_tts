"""
Config flow for Groq TTS.
"""
from __future__ import annotations
from typing import Any
import os
import voluptuous as vol
import logging
import aiohttp
from urllib.parse import urlparse
import uuid

from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers.selector import selector

from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_VOICE,
    CONF_URL,
    DOMAIN,
    MODELS,
    VOICES,
    UNIQUE_ID,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
)

_LOGGER = logging.getLogger(__name__)

def generate_entry_id() -> str:
    return str(uuid.uuid4())

async def fetch_available(endpoint: str, api_key: str | None = None) -> list[str]:
    """Fetch list of items from Groq API endpoint returning JSON data."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(endpoint, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("data") or data
                    if isinstance(items, list):
                        names = []
                        for item in items:
                            if isinstance(item, dict):
                                name = item.get("id") or item.get("name")
                                if name:
                                    names.append(name)
                            elif isinstance(item, str):
                                names.append(item)
                        return sorted(names)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Error fetching %s: %s", endpoint, err)
    return []

async def get_dynamic_options(api_key: str | None) -> tuple[list[str], list[str]]:
    """Return a dynamic list of models and the built-in voices."""
    models_endpoint = "https://api.groq.com/openai/v1/models"
    models = await fetch_available(models_endpoint, api_key) or MODELS
    voices = VOICES
    return models, voices

async def validate_user_input(user_input: dict):
    if user_input.get(CONF_MODEL) is None:
        raise ValueError("Model is required")
    if user_input.get(CONF_VOICE) is None:
        raise ValueError("Voice is required")

def get_chime_options() -> list[dict[str, str]]:
    """
    Scans the "chime" folder (located in the same directory as this file)
    and returns a list of options for the dropdown selector.
    Each option is a dict with 'value' (the file name) and 'label' (the file name without extension).
    """
    chime_folder = os.path.join(os.path.dirname(__file__), "chime")
    try:
        files = os.listdir(chime_folder)
    except Exception as err:
        _LOGGER.error("Error listing chime folder: %s", err)
        files = []
    options = []
    for file in files:
        if file.lower().endswith(".mp3"):
            label = os.path.splitext(file)[0].title()  # e.g. "Signal1.mp3" -> "Signal1"
            options.append({"value": file, "label": label})
    options.sort(key=lambda x: x["label"])
    return options

class GroqTTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Groq TTS."""
    VERSION = 1
    data_schema = vol.Schema({
        vol.Optional(CONF_API_KEY): str,
        vol.Optional(CONF_URL, default="https://api.groq.com/openai/v1/audio/speech"): str,
        vol.Required(CONF_MODEL, default="playai-tts"): selector({
            "select": {
                "options": MODELS,
                "mode": "dropdown",
                "sort": True,
                "custom_value": True
            }
        }),
        vol.Required(CONF_VOICE, default="Arista-PlayAI"): selector({
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
        models, voices = await get_dynamic_options(user_input.get(CONF_API_KEY) if user_input else None)
        schema = vol.Schema({
            vol.Optional(CONF_API_KEY): str,
            vol.Optional(CONF_URL, default="https://api.groq.com/openai/v1/audio/speech"): str,
            vol.Required(CONF_MODEL, default="playai-tts"): selector({
                "select": {"options": models, "mode": "dropdown", "sort": True, "custom_value": True}
            }),
            vol.Required(CONF_VOICE, default="Arista-PlayAI"): selector({
                "select": {"options": voices, "mode": "dropdown", "sort": True, "custom_value": True}
            }),
        })
        if user_input is not None:
            try:
                await validate_user_input(user_input)
                entry_id = generate_entry_id()
                user_input[UNIQUE_ID] = entry_id
                await self.async_set_unique_id(entry_id)
                hostname = urlparse(user_input[CONF_URL]).hostname
                return self.async_create_entry(
                    title=f"Groq TTS ({hostname}, {user_input[CONF_MODEL]})",
                    data=user_input
                )
            except data_entry_flow.AbortFlow:
                return self.async_abort(reason="already_configured")
            except Exception as e:
                _LOGGER.exception(str(e))
                errors["base"] = "unknown_error"
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=user_input,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return GroqTTSOptionsFlow()

class GroqTTSOptionsFlow(OptionsFlow):
    """Handle options flow for Groq TTS."""
    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        chime_options = await self.hass.async_add_executor_job(get_chime_options)
        models, voices = await get_dynamic_options(
            self.config_entry.options.get(CONF_API_KEY, self.config_entry.data.get(CONF_API_KEY))
        )
        options_schema = vol.Schema({
            vol.Optional(
                CONF_API_KEY,
                default=self.config_entry.options.get(CONF_API_KEY, self.config_entry.data.get(CONF_API_KEY, ""))
            ): str,
            vol.Optional(
                CONF_URL,
                default=self.config_entry.options.get(CONF_URL, self.config_entry.data.get(CONF_URL, "https://api.groq.com/openai/v1/audio/speech"))
            ): str,
            vol.Optional(
                CONF_MODEL,
                default=self.config_entry.options.get(CONF_MODEL, self.config_entry.data.get(CONF_MODEL, "playai-tts"))
            ): selector({
                "select": {
                    "options": models,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True
                }
            }),
            vol.Optional(
                CONF_CHIME_ENABLE,
                default=self.config_entry.options.get(CONF_CHIME_ENABLE, self.config_entry.data.get(CONF_CHIME_ENABLE, False))
            ): selector({"boolean": {}}),
            vol.Optional(
                CONF_CHIME_SOUND,
                default=self.config_entry.options.get(CONF_CHIME_SOUND, self.config_entry.data.get(CONF_CHIME_SOUND, "threetone.mp3"))
            ): selector({
                "select": {
                    "options": chime_options
                }
            }),
            vol.Optional(
                CONF_VOICE,
                default=self.config_entry.options.get(CONF_VOICE, self.config_entry.data.get(CONF_VOICE, "Arista-PlayAI"))
            ): selector({
                "select": {
                    "options": voices,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True
                }
            }),
            vol.Optional(
                CONF_NORMALIZE_AUDIO,
                default=self.config_entry.options.get(CONF_NORMALIZE_AUDIO, self.config_entry.data.get(CONF_NORMALIZE_AUDIO, False))
            ): selector({"boolean": {}})
        })
        return self.async_show_form(step_id="init", data_schema=options_schema)
