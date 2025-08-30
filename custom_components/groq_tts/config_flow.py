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
import hashlib

from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers.selector import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
    CONF_CACHE_SIZE,
    DEFAULT_CACHE_SIZE,
)

_LOGGER = logging.getLogger(__name__)

def generate_entry_id() -> str:
    return str(uuid.uuid4())

async def fetch_available(hass, endpoint: str, api_key: str | None = None) -> list[str]:
    """Fetch list of items from Groq API endpoint returning JSON data."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        session = async_get_clientsession(hass)
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

async def get_dynamic_options(hass, api_key: str | None) -> tuple[list[str], list[str]]:
    """Return a dynamic list of models and the built-in voices."""
    models_endpoint = "https://api.groq.com/openai/v1/models"
    models = await fetch_available(hass, models_endpoint, api_key) or MODELS
    voices = VOICES
    return models, voices

async def validate_user_input(user_input: dict):
    if user_input.get(CONF_MODEL) is None:
        raise ValueError("Model is required")
    if user_input.get(CONF_VOICE) is None:
        raise ValueError("Voice is required")
    url = user_input.get(CONF_URL)
    if not url:
        raise ValueError("URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid URL")

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
        models, voices = await get_dynamic_options(self.hass, user_input.get(CONF_API_KEY) if user_input else None)
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
                # Create a deterministic unique_id from URL + model to avoid duplicates
                url_value = user_input[CONF_URL]
                model_value = user_input[CONF_MODEL]
                uid_hash = hashlib.sha1(f"{url_value}|{model_value}".encode("utf-8")).hexdigest()
                unique_id = f"groq_tts_{uid_hash}"
                await self.async_set_unique_id(unique_id)
                # Abort if already configured
                self._abort_if_unique_id_configured()
                # Store unique id in data for backward-compat device identifiers
                user_input[UNIQUE_ID] = unique_id
                hostname = urlparse(url_value).hostname
                return self.async_create_entry(
                    title=f"Groq TTS ({hostname}, {user_input[CONF_MODEL]})",
                    data=user_input
                )
            except data_entry_flow.AbortFlow:
                return self.async_abort(reason="already_configured")
            except ValueError as e:
                msg = str(e)
                if "Invalid URL" in msg:
                    errors[CONF_URL] = "invalid_url"
                elif "URL is required" in msg:
                    errors[CONF_URL] = "required"
                elif "Model is required" in msg:
                    errors[CONF_MODEL] = "required"
                elif "Voice is required" in msg:
                    errors[CONF_VOICE] = "required"
                else:
                    errors["base"] = "unknown_error"
            except Exception as e:
                _LOGGER.exception("Unexpected error in config flow: %s", e)
                errors["base"] = "unknown_error"
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return GroqTTSOptionsFlow()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> data_entry_flow.FlowResult:
        """Handle reauthentication when credentials are invalid."""
        # Store the entry we're reauthenticating for use in confirm step
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input.get(CONF_API_KEY)
            if not api_key:
                errors[CONF_API_KEY] = "required"
            else:
                # Update only the API key, preserve other data
                reauth_entry = getattr(self, "_reauth_entry", None)
                if reauth_entry is None:
                    return self.async_abort(reason="unknown")
                new_data = dict(reauth_entry.data)
                new_data[CONF_API_KEY] = api_key
                # Abort current flow, update & reload the entry with new credentials
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates=new_data,
                    reason="reauth_successful",
                )

        schema = vol.Schema({vol.Required(CONF_API_KEY): str})
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

class GroqTTSOptionsFlow(OptionsFlow):
    """Handle options flow for Groq TTS."""
    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        chime_options = await self.hass.async_add_executor_job(get_chime_options)
        models, voices = await get_dynamic_options(
            self.hass,
            self.config_entry.options.get(CONF_API_KEY, self.config_entry.data.get(CONF_API_KEY)),
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
            ,
            vol.Optional(
                CONF_CACHE_SIZE,
                default=self.config_entry.options.get(CONF_CACHE_SIZE, DEFAULT_CACHE_SIZE)
            ): selector({
                "number": {
                    "min": 0,
                    "max": 4096,
                    "mode": "box",
                    "step": 1
                }
            })
        })
        return self.async_show_form(step_id="init", data_schema=options_schema)
