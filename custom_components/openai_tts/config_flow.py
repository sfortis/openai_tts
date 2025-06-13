"""
Config flow for OpenAI TTS.
"""
from __future__ import annotations
from typing import Any
import os
import voluptuous as vol
import logging
from urllib.parse import urlparse
import uuid

from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.helpers.selector import selector
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_VOICE,
    CONF_SPEED,
    CONF_URL,
    DOMAIN,
    MODELS,
    VOICES,
    UNIQUE_ID,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
    CONF_INSTRUCTIONS,
    # New constants
    CONF_TTS_ENGINE,
    OPENAI_ENGINE,
    KOKORO_FASTAPI_ENGINE,
    TTS_ENGINES,
    DEFAULT_TTS_ENGINE,
    CONF_KOKORO_URL,
)

_LOGGER = logging.getLogger(__name__)

# Removed class-level data_schema, it will be dynamic

def generate_entry_id() -> str:
    return str(uuid.uuid4())

async def validate_config_input(user_input: dict):
    """Validate common and engine-specific fields."""
    errors = {}
    # Common validations
    if not user_input.get(CONF_MODEL):
        errors[CONF_MODEL] = "model_required" # Assuming this key exists in strings.json or add it
    if not user_input.get(CONF_VOICE):
        errors[CONF_VOICE] = "voice_required" # Assuming this key exists in strings.json or add it

    # Engine specific validations
    engine_type = user_input.get(CONF_TTS_ENGINE)
    if engine_type == OPENAI_ENGINE:
        if not user_input.get(CONF_URL): # OpenAI URL has a default but user might clear it
            errors[CONF_URL] = "url_required_openai" # Add to strings.json
        # API key for OpenAI is generally good practice, but some proxies might not need it.
        # No hard error here, but could be a warning or different handling.
    elif engine_type == KOKORO_FASTAPI_ENGINE:
        if not user_input.get(CONF_KOKORO_URL):
            errors[CONF_KOKORO_URL] = "kokoro_url_required" # Add to strings.json

    if errors:
        # To make Voluptuous happy and show field-specific errors,
        # we should ideally integrate this into the schema validation step by step,
        # or use a different error reporting mechanism if just returning a dict of errors.
        # For now, raising a generic error if any specific field error is found.
        # A better way is to return errors and let async_show_form handle them.
        # This simple implementation will put error on "base".
        # Let's refine this to return the errors dict.
        pass # Errors will be returned and handled by async_show_form

    return errors # Return dict of errors

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

class OpenAITTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenAI TTS."""
    VERSION = 1
    # Connection class and data not needed for this version of config flow
    # CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL
    # data: dict[str, Any] = {} # To store data across steps if needed

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            # Store current input to repopulate form if errors occur
            # self.data.update(user_input) # If using self.data for multi-step

            # Validate common and engine-specific fields
            validation_errors = await validate_config_input(user_input)
            errors.update(validation_errors)

            if not errors:
                try:
                    entry_id = generate_entry_id()
                    # await self.async_set_unique_id(entry_id) # Deprecated, unique_id handled by data
                    user_input[UNIQUE_ID] = entry_id

                    title = "OpenAI TTS"
                    if user_input.get(CONF_TTS_ENGINE) == KOKORO_FASTAPI_ENGINE:
                        kokoro_url_parsed = urlparse(user_input.get(CONF_KOKORO_URL, ""))
                        title = f"Kokoro FastAPI TTS ({kokoro_url_parsed.hostname}, {user_input.get(CONF_MODEL)})"
                    else: # OpenAI or compatible
                        url_parsed = urlparse(user_input.get(CONF_URL, ""))
                        title = f"OpenAI TTS ({url_parsed.hostname}, {user_input.get(CONF_MODEL)})"

                    # Clean up data based on engine type
                    if user_input.get(CONF_TTS_ENGINE) == KOKORO_FASTAPI_ENGINE:
                        user_input.pop(CONF_API_KEY, None)
                        user_input.pop(CONF_URL, None)
                    else: # OpenAI
                        user_input.pop(CONF_KOKORO_URL, None)


                    return self.async_create_entry(title=title, data=user_input)
                except data_entry_flow.AbortFlow: # Should not happen if unique_id is always new
                    return self.async_abort(reason="already_configured")
                except Exception as e:
                    _LOGGER.exception("Unexpected error creating entry: %s", e)
                    errors["base"] = "unknown" # Use "unknown" from strings.json

        # Determine current engine for schema or default
        current_engine = user_input.get(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE) if user_input else DEFAULT_TTS_ENGINE

        # Build schema dynamically
        data_schema_user = {
            vol.Required(CONF_TTS_ENGINE, default=current_engine): selector({
                "select": {
                    "options": TTS_ENGINES,
                    "translation_key": "tts_engine" # Uses selector.<domain>.<translation_key>
                }
            }),
        }

        if current_engine == OPENAI_ENGINE:
            data_schema_user.update({
                vol.Optional(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(CONF_URL, default="https://api.openai.com/v1/audio/speech"): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            })
        elif current_engine == KOKORO_FASTAPI_ENGINE:
            data_schema_user.update({
                vol.Required(CONF_KOKORO_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            })

        data_schema_user.update({
            vol.Required(CONF_MODEL, default=user_input.get(CONF_MODEL, "tts-1") if user_input else "tts-1"): selector({
                "select": {
                    "options": MODELS, # Consider making this dynamic per engine in future
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                    "translation_key": "model"
                }
            }),
            vol.Required(CONF_VOICE, default=user_input.get(CONF_VOICE, "shimmer") if user_input else "shimmer"): selector({
                "select": {
                    "options": VOICES, # Consider making this dynamic per engine in future
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                    "translation_key": "voice"
                }
            }),
            vol.Optional(CONF_SPEED, default=user_input.get(CONF_SPEED, 1.0) if user_input else 1.0): selector({
                "number": {
                    "min": 0.25,
                    "max": 4.0,
                    "step": 0.05,
                    "mode": "slider"
                }
            }),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(data_schema_user),
            errors=errors,
            # description_placeholders can be used if needed, e.g. for API key hints
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return OpenAITTSOptionsFlow()

class OpenAITTSOptionsFlow(OptionsFlow):
    """Handle options flow for OpenAI TTS."""
    async def async_step_init(self, user_input: dict | None = None):
        """Handle options flow."""
        errors: dict[str, str] = {}
        # Determine the engine type from the main config entry
        engine_type = self.config_entry.data.get(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE)

        if user_input is not None:
            # Here you could add validation for options if needed
            # For example, ensure instructions are not too long, etc.
            # For now, just creating the entry with new options.
            return self.async_create_entry(title="", data=user_input)

        chime_options = await self.hass.async_add_executor_job(get_chime_options)

        # Get current values for defaults, from options or from data
        current_model = self.config_entry.options.get(CONF_MODEL, self.config_entry.data.get(CONF_MODEL, "tts-1"))
        current_voice = self.config_entry.options.get(CONF_VOICE, self.config_entry.data.get(CONF_VOICE, "shimmer"))
        current_speed = self.config_entry.options.get(CONF_SPEED, self.config_entry.data.get(CONF_SPEED, 1.0))
        current_instructions = self.config_entry.options.get(CONF_INSTRUCTIONS, self.config_entry.data.get(CONF_INSTRUCTIONS, "")) # Ensure string default
        current_chime_enable = self.config_entry.options.get(CONF_CHIME_ENABLE, self.config_entry.data.get(CONF_CHIME_ENABLE, False))
        current_chime_sound = self.config_entry.options.get(CONF_CHIME_SOUND, self.config_entry.data.get(CONF_CHIME_SOUND, "threetone.mp3"))
        current_normalize_audio = self.config_entry.options.get(CONF_NORMALIZE_AUDIO, self.config_entry.data.get(CONF_NORMALIZE_AUDIO, False))

        # TODO: Potentially make MODELS and VOICES dynamic based on engine_type
        # For now, using global MODELS and VOICES
        # Example:
        # available_models = KOKORO_MODELS if engine_type == KOKORO_FASTAPI_ENGINE else MODELS
        # available_voices = KOKORO_VOICES if engine_type == KOKORO_FASTAPI_ENGINE else VOICES
        available_models = MODELS
        available_voices = VOICES

        options_schema_dict = {
            vol.Optional(CONF_MODEL, default=current_model): selector({
                "select": {"options": available_models, "mode": "dropdown", "sort": True, "custom_value": True, "translation_key": "model"}
            }),
            vol.Optional(CONF_VOICE, default=current_voice): selector({
                "select": {"options": available_voices, "mode": "dropdown", "sort": True, "custom_value": True, "translation_key": "voice"}
            }),
            vol.Optional(CONF_SPEED, default=current_speed): selector({
                "number": {"min": 0.25, "max": 4.0, "step": 0.05, "mode": "slider"}
            }),
            vol.Optional(CONF_INSTRUCTIONS, default=current_instructions): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
            ),
            vol.Optional(CONF_CHIME_ENABLE, default=current_chime_enable): selector({"boolean": {}}),
            vol.Optional(CONF_CHIME_SOUND, default=current_chime_sound): selector({
                "select": {"options": chime_options}
            }),
            vol.Optional(CONF_NORMALIZE_AUDIO, default=current_normalize_audio): selector({"boolean": {}})
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(options_schema_dict),
            errors=errors
        )
