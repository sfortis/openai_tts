# config_flow.py
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
import aiohttp

from homeassistant import data_entry_flow
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigSubentryFlow,
    OptionsFlow,
    ConfigEntry,
    ConfigFlowResult,
    SubentryFlowResult,
)
from homeassistant.helpers.selector import selector, TextSelector, TextSelectorConfig, TextSelectorType, TemplateSelector
from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed
from homeassistant.core import callback

from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_VOICE,
    CONF_SPEED,
    CONF_URL,
    DEFAULT_URL,
    DOMAIN,
    MODELS,
    VOICES,
    UNIQUE_ID,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
    CONF_INSTRUCTIONS,
    CONF_VOLUME_RESTORE,
    CONF_PAUSE_PLAYBACK,
    CONF_PROFILE_NAME,
)

SUBENTRY_TYPE_PROFILE = "profile"

_LOGGER = logging.getLogger(__name__)

# Custom exceptions for API validation
class InvalidAPIKey(HomeAssistantError):
    """Error to indicate invalid API key."""

class CannotConnect(HomeAssistantError):
    """Error to indicate connection failure."""

def generate_entry_id() -> str:
    return str(uuid.uuid4())

async def async_validate_api_key(api_key: str, url: str) -> bool:
    """Validate the API key by making a minimal test request.

    Args:
        api_key: The OpenAI API key to validate
        url: The API endpoint URL

    Returns:
        True if validation succeeds

    Raises:
        InvalidAPIKey: If the API key is invalid (401/403)
        CannotConnect: If unable to connect to the API
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # Make a minimal TTS request to validate the API key
    # Using minimal text to reduce cost
    payload = {
        "model": "tts-1",
        "input": ".",
        "voice": "alloy",
        "response_format": "mp3",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 401:
                    _LOGGER.error("API key validation failed: Unauthorized (401)")
                    raise InvalidAPIKey("Invalid API key")
                elif response.status == 403:
                    _LOGGER.error("API key validation failed: Forbidden (403)")
                    raise InvalidAPIKey("API key does not have required permissions")
                elif response.status >= 400:
                    _LOGGER.error("API validation failed with status %d", response.status)
                    raise CannotConnect(f"API returned status {response.status}")

                # Success - we got audio data back
                _LOGGER.debug("API key validation successful")
                return True

    except aiohttp.ClientError as err:
        _LOGGER.error("Connection error during API validation: %s", err)
        raise CannotConnect(f"Cannot connect to API: {err}") from err
    except TimeoutError as err:
        _LOGGER.error("Timeout during API validation")
        raise CannotConnect("Connection timed out") from err

async def validate_user_input(user_input: dict) -> None:
    """Validate user input for config flow."""
    api_url = user_input.get(CONF_URL, DEFAULT_URL)
    api_key = user_input.get(CONF_API_KEY)

    # API key is only required for the default OpenAI endpoint
    if api_url == DEFAULT_URL and not api_key:
        raise ValueError("API key is required for OpenAI API")

def get_chime_options() -> list[dict[str, str]]:
    """Scan chime folder and return dropdown options."""
    chime_folder = os.path.join(os.path.dirname(__file__), "chime")
    try:
        files = os.listdir(chime_folder)
    except Exception as err:
        _LOGGER.error("Error listing chime folder: %s", err)
        files = []
    opts: list[dict[str,str]] = []
    for file in files:
        if file.lower().endswith(".mp3"):
            opts.append({"value": file, "label": os.path.splitext(file)[0].title()})
    opts.sort(key=lambda x: x["label"])
    return opts

async def async_get_chime_options(hass) -> list[dict[str, str]]:
    """Scan chime folder and return dropdown options (async version)."""
    loop = hass.loop
    return await loop.run_in_executor(None, get_chime_options)

class OpenAITTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenAI TTS."""
    VERSION = 2
    MINOR_VERSION = 1  # Increment for subentry flow support
    
    data_schema = vol.Schema({
        vol.Optional(CONF_API_KEY, default=""): str,
        vol.Optional(CONF_URL, default=DEFAULT_URL): str,
    })

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await validate_user_input(user_input)

                api_key = user_input.get(CONF_API_KEY, "")
                api_url = user_input.get(CONF_URL, DEFAULT_URL)
                is_custom_endpoint = api_url != DEFAULT_URL

                # Check for duplicate API key (only if API key is provided)
                if api_key:
                    for entry in self._async_current_entries():
                        if entry.data.get(CONF_API_KEY) == api_key:
                            _LOGGER.error("An entry with this API key already exists: %s", entry.title)
                            errors["base"] = "duplicate_api_key"
                            return self.async_show_form(
                                step_id="user",
                                data_schema=self.data_schema,
                                errors=errors,
                            )

                # Validate API key by making a test request (only for default OpenAI endpoint)
                if api_key and not is_custom_endpoint:
                    await async_validate_api_key(api_key, api_url)

                # Generate unique ID
                import hashlib
                if api_key:
                    # Use API key hash for unique ID
                    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
                    unique_id = f"openai_tts_{api_key_hash}"
                else:
                    # Use URL hash for custom endpoints without API key
                    url_hash = hashlib.sha256(api_url.encode()).hexdigest()[:16]
                    unique_id = f"openai_tts_{url_hash}"

                user_input[UNIQUE_ID] = unique_id
                await self.async_set_unique_id(unique_id)
                hostname = urlparse(user_input[CONF_URL]).hostname
                return self.async_create_entry(
                    title=f"OpenAI TTS ({hostname})",
                    data=user_input,
                )
            except data_entry_flow.AbortFlow:
                return self.async_abort(reason="already_configured")
            except InvalidAPIKey:
                errors["base"] = "invalid_api_key"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except HomeAssistantError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except ValueError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"

        return self.async_show_form(
            step_id="user",
            data_schema=self.data_schema,
            errors=errors,
            description_placeholders=user_input,
        )

    # Options flow removed - all entries use reconfigure
    
    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the supported subentry types for this integration."""
        # Check if this is a legacy entry (has model/voice in data AND no subentries)
        has_model_voice = config_entry.data.get(CONF_MODEL) is not None or config_entry.data.get(CONF_VOICE) is not None
        has_subentries = hasattr(config_entry, 'subentries') and config_entry.subentries
        
        # Only modern parent entries (no model/voice in data OR has subentries) support subentries
        # Legacy entries (with model/voice but no subentries) do not support subentries
        is_legacy = has_model_voice and not has_subentries
        
        if is_legacy:
            return {}
        
        return {SUBENTRY_TYPE_PROFILE: OpenAITTSProfileSubentryFlow}
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OpenAITTSOptionsFlow(config_entry)
    
    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Return options flow support for this handler."""
        # Check if this is a legacy entry (has model/voice in data AND no subentries)
        has_model_voice = config_entry.data.get(CONF_MODEL) is not None or config_entry.data.get(CONF_VOICE) is not None
        has_subentries = hasattr(config_entry, 'subentries') and config_entry.subentries
        
        # Only legacy entries (with model/voice but no subentries) support options flow
        # Modern parent entries (with subentries) use reconfigure flow instead
        is_legacy = has_model_voice and not has_subentries
        
        return is_legacy

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauthorization flow triggered by auth failure."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id")
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reauthorization confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                api_key = user_input.get(CONF_API_KEY)
                api_url = self._reauth_entry.data.get(CONF_URL, "https://api.openai.com/v1/audio/speech")

                # Validate the new API key
                await async_validate_api_key(api_key, api_url)

                # Update the entry with new credentials
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={**self._reauth_entry.data, CONF_API_KEY: api_key},
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

            except InvalidAPIKey:
                errors["base"] = "invalid_api_key"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown_error"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): str,
            }),
            errors=errors,
            description_placeholders={
                "title": self._reauth_entry.title if self._reauth_entry else "OpenAI TTS"
            },
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of the parent entry."""
        errors: dict[str, str] = {}
        
        # Get the entry ID from context
        entry_id = self.context.get("entry_id")
        if not entry_id:
            return self.async_abort(reason="unknown_error")
        
        reconfigure_entry = self.hass.config_entries.async_get_entry(entry_id)
        if not reconfigure_entry:
            return self.async_abort(reason="unknown_error")
        
        if user_input is not None:
            try:
                await validate_user_input(user_input)
                
                # Check for duplicate API key (exclude current entry)
                api_key = user_input.get(CONF_API_KEY)
                for entry in self._async_current_entries():
                    if entry.entry_id != reconfigure_entry.entry_id and entry.data.get(CONF_API_KEY) == api_key:
                        _LOGGER.error("An entry with this API key already exists: %s", entry.title)
                        errors["base"] = "duplicate_api_key"
                        break
                
                if not errors:
                    # Update the entry using the recommended helper
                    from urllib.parse import urlparse
                    hostname = urlparse(user_input[CONF_URL]).hostname
                    
                    # Ensure unique_id doesn't change
                    await self.async_set_unique_id(reconfigure_entry.unique_id)
                    self._abort_if_unique_id_mismatch()
                    
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data_updates=user_input,
                        title=f"OpenAI TTS ({hostname})"
                    )
                    
            except HomeAssistantError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except ValueError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"
        
        # Show the form with current values as defaults
        current_data = reconfigure_entry.data
        schema = vol.Schema({
            vol.Required(CONF_API_KEY, default=current_data.get(CONF_API_KEY, "")): str,
            vol.Optional(CONF_URL, default=current_data.get(CONF_URL, "https://api.openai.com/v1/audio/speech")): str,
        })
        
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )


class OpenAITTSProfileSubentryFlow(ConfigSubentryFlow):
    """Handle a subentry flow for OpenAI TTS profiles."""
    
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle initialization with data (for migration)."""
        # This is called when flow is initiated with data directly
        if user_input is not None:
            # Direct creation from migration
            return self.async_create_subentry(
                data=user_input,
                title=user_input.get(CONF_PROFILE_NAME, "Default")
            )
        # Otherwise proceed to user step
        return await self.async_step_user()
    
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle the initial step for profile creation."""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            try:
                # Validate profile name is unique
                profile_name = user_input.get(CONF_PROFILE_NAME, "")
                if not profile_name:
                    raise ValueError("Profile name is required")
                
                # Check if profile name already exists in subentries
                parent_entry = self._get_entry()
                # Get all config entries and filter for subentries of this parent
                all_entries = self.hass.config_entries.async_entries(DOMAIN)
                for entry in all_entries:
                    if (hasattr(entry, 'parent_entry_id') and 
                        entry.parent_entry_id == parent_entry.entry_id and
                        entry.data.get(CONF_PROFILE_NAME) == profile_name):
                        raise ValueError("Profile name already exists")
                
                # Map string keys to constants
                mapped_input = {}
                key_mapping = {
                    "chime": CONF_CHIME_ENABLE,
                    "chime_sound": CONF_CHIME_SOUND,
                    "normalize_audio": CONF_NORMALIZE_AUDIO,
                    "instructions": CONF_INSTRUCTIONS,
                }
                
                for key, value in user_input.items():
                    mapped_key = key_mapping.get(key, key)
                    # Handle empty instructions - convert to None
                    if key == "instructions" and value == "":
                        mapped_input[mapped_key] = None
                    else:
                        mapped_input[mapped_key] = value
                
                # Create unique ID for this profile
                entry_id = generate_entry_id()
                mapped_input[UNIQUE_ID] = entry_id
                
                # Don't call async_set_unique_id in subentry flows
                # Subentry unique IDs are handled differently than main entry unique IDs
                
                # Create the subentry
                return self.async_create_entry(
                    title=profile_name,
                    data=mapped_input,
                )
                
            except ValueError as e:
                _LOGGER.exception(str(e))
                errors["base"] = str(e)
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"
        
        # Get chime options
        chime_opts = await async_get_chime_options(self.hass)
        
        # Schema for profile creation
        profile_schema = vol.Schema({
            vol.Required(CONF_PROFILE_NAME): str,
            vol.Required(CONF_MODEL, default="tts-1"): selector({
                "select": {
                    "options": MODELS,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            }),
            vol.Required(CONF_VOICE, default="shimmer"): selector({
                "select": {
                    "options": VOICES,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            }),
            vol.Optional(
                "instructions",
                description={
                    "suggested_value": ""
                },
            ): TemplateSelector(),
            vol.Optional(CONF_SPEED, default=1.0): selector({
                "number": {"min": 0.25, "max": 4.0, "step": 0.05, "mode": "slider"}
            }),
            vol.Optional("chime", default=False): selector({"boolean": {}}),
            vol.Optional("chime_sound", default="threetone.mp3"): selector({
                "select": {"options": chime_opts}
            }),
            vol.Optional("normalize_audio", default=False): selector({"boolean": {}}),
        })
        
        return self.async_show_form(
            step_id="user",
            data_schema=profile_schema,
            errors=errors,
        )
    
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Handle reconfiguration of a profile."""
        errors: dict[str, str] = {}
        
        try:
            subentry = self._get_reconfigure_subentry()
        except Exception as e:
            _LOGGER.error("Failed to get reconfigure subentry: %s", e)
            return self.async_abort(reason="subentry_not_found")
        
        if not subentry:
            _LOGGER.error("Reconfigure subentry is None")
            return self.async_abort(reason="subentry_not_found")
        
        # Log subentry info - subentries have limited attributes in the config flow context
        subentry_info = f"{subentry.title} (profile: {subentry.data.get(CONF_PROFILE_NAME, 'unknown')})"
        _LOGGER.debug("Reconfiguring subentry: %s", subentry_info)
        
        if user_input is not None:
            try:
                # Map string keys to constants
                key_mapping = {
                    "chime": CONF_CHIME_ENABLE,
                    "chime_sound": CONF_CHIME_SOUND,
                    "normalize_audio": CONF_NORMALIZE_AUDIO,
                    "instructions": CONF_INSTRUCTIONS,
                }
                
                mapped_input = {}
                for key, value in user_input.items():
                    mapped_key = key_mapping.get(key, key)
                    # Handle empty instructions - convert to None
                    if key == "instructions" and value == "":
                        mapped_input[mapped_key] = None
                    else:
                        mapped_input[mapped_key] = value
                
                # Keep the original profile name and unique ID
                updated_data = {**subentry.data, **mapped_input}
                
                # Log the update
                entry_id = getattr(subentry, 'entry_id', getattr(subentry, 'subentry_id', 'unknown'))
                _LOGGER.info("Updating subentry %s with data: %s", entry_id, updated_data)
                
                # Update the subentry
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    data=updated_data,
                )
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"
        
        # Get existing data for defaults
        existing_data = subentry.data
        
        # Get chime options
        chime_opts = await async_get_chime_options(self.hass)
        
        # Schema for profile reconfiguration (without profile name)
        reconfigure_schema = vol.Schema({
            vol.Required(CONF_MODEL, default=existing_data.get(CONF_MODEL, "tts-1")): selector({
                "select": {
                    "options": MODELS,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            }),
            vol.Required(CONF_VOICE, default=existing_data.get(CONF_VOICE, "shimmer")): selector({
                "select": {
                    "options": VOICES,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            }),
            vol.Optional(
                "instructions",
                description={
                    "suggested_value": existing_data.get(CONF_INSTRUCTIONS) or ""
                },
            ): TemplateSelector(),
            vol.Optional(CONF_SPEED, default=existing_data.get(CONF_SPEED, 1.0)): selector({
                "number": {"min": 0.25, "max": 4.0, "step": 0.05, "mode": "slider"}
            }),
            vol.Optional("chime", default=existing_data.get(CONF_CHIME_ENABLE, False)): selector({"boolean": {}}),
            vol.Optional("chime_sound", default=existing_data.get(CONF_CHIME_SOUND, "threetone.mp3")): selector({
                "select": {"options": chime_opts}
            }),
            vol.Optional("normalize_audio", default=existing_data.get(CONF_NORMALIZE_AUDIO, False)): selector({"boolean": {}}),
        })
        
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=reconfigure_schema,
            errors=errors,
        )


class OpenAITTSOptionsFlow(OptionsFlow):
    """Handle options flow for OpenAI TTS."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        # Check if this is a profile (subentry) or main entry
        is_profile = hasattr(self._config_entry, 'subentry_type') and self._config_entry.subentry_type == SUBENTRY_TYPE_PROFILE
        
        # Check if this is a legacy entry (has model/voice in data)
        is_legacy = self._config_entry.data.get(CONF_MODEL) is not None or self._config_entry.data.get(CONF_VOICE) is not None
        
        # Modern parent entries and subentries should not have options flow
        if not is_legacy or is_profile:
            _LOGGER.warning("Options flow accessed for non-legacy entry %s, aborting", self._config_entry.entry_id)
            return self.async_abort(reason="not_supported")
        
        _LOGGER.debug("OptionsFlow init - is_profile: %s, is_legacy: %s, entry_id: %s", 
                     is_profile, is_legacy, self._config_entry.entry_id)
        _LOGGER.debug("Current options: %s", self._config_entry.options)
        _LOGGER.debug("Current data: %s", {k: v for k, v in self._config_entry.data.items() if k != CONF_API_KEY})
        
        if user_input is not None:
            # Map string keys to constants
            key_mapping = {
                "model": CONF_MODEL,
                "voice": CONF_VOICE,
                "speed": CONF_SPEED,
                "instructions": CONF_INSTRUCTIONS,
                "chime": CONF_CHIME_ENABLE,
                "chime_sound": CONF_CHIME_SOUND,
                "normalize_audio": CONF_NORMALIZE_AUDIO,
                "volume_restore": CONF_VOLUME_RESTORE,
                "pause_playback": CONF_PAUSE_PLAYBACK,
            }
            
            # Process the input to handle empty strings and map keys
            processed_data = {}
            for key, value in user_input.items():
                mapped_key = key_mapping.get(key, key)
                
                # Convert empty strings to None for instructions field
                if key == "instructions":
                    # If instructions is empty or contains only whitespace, set to None
                    if value is None or (isinstance(value, str) and value.strip() == ""):
                        processed_data[mapped_key] = None
                        _LOGGER.debug("Setting instructions to None (empty/whitespace value)")
                    else:
                        processed_data[mapped_key] = value.strip() if isinstance(value, str) else value
                        _LOGGER.debug("Setting instructions to: %s", processed_data[mapped_key])
                else:
                    processed_data[mapped_key] = value
            
            _LOGGER.info("Saving options for entry %s: %s", self._config_entry.entry_id, processed_data)
            _LOGGER.debug("Processed options data: %s", processed_data)
            return self.async_create_entry(title="", data=processed_data)

        chime_opts = await async_get_chime_options(self.hass)
        
        # Get current instructions value
        current_instructions = self._config_entry.options.get(CONF_INSTRUCTIONS, self._config_entry.data.get(CONF_INSTRUCTIONS, ""))
        
        _LOGGER.debug("Current instructions value: %s", current_instructions)
        
        # Build schema based on whether this is a profile or main entry
        schema_dict = {}
        
        # Check if this is a legacy entry (has model/voice in data)
        is_legacy = self._config_entry.data.get(CONF_MODEL) is not None or self._config_entry.data.get(CONF_VOICE) is not None
        
        # If this is a profile or legacy entry, include voice, model, and speed options
        if is_profile or is_legacy:
            schema_dict[vol.Optional(
                "model",
                default=self._config_entry.options.get(CONF_MODEL, self._config_entry.data.get(CONF_MODEL, "tts-1")),
            )] = selector({
                "select": {
                    "options": MODELS,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            })
            
            schema_dict[vol.Optional(
                "voice",
                default=self._config_entry.options.get(CONF_VOICE, self._config_entry.data.get(CONF_VOICE, "shimmer")),
            )] = selector({
                "select": {
                    "options": VOICES,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            })
            
            # Instructions field - multiline text
            schema_dict[vol.Optional(
                "instructions",  # Multiline text field
                description={
                    "suggested_value": current_instructions if current_instructions else ""
                },
            )] = selector({
                "text": {
                    "multiline": True,
                    "type": "text"
                }
            })
            
            schema_dict[vol.Optional(
                "speed",
                default=self._config_entry.options.get(CONF_SPEED, self._config_entry.data.get(CONF_SPEED, 1.0)),
            )] = selector({
                "number": {"min": 0.25, "max": 4.0, "step": 0.05, "mode": "slider"}
            })
        
        # Only show TTS-specific options for legacy entries and profiles
        if is_profile or is_legacy:
            # These options only make sense for entries that create TTS entities
            schema_dict[vol.Optional(
                "chime",  # Use strings directly here, not constants
                default=self._config_entry.options.get(CONF_CHIME_ENABLE, self._config_entry.data.get(CONF_CHIME_ENABLE, False)),
            )] = selector({"boolean": {}})

            schema_dict[vol.Optional(
                "chime_sound",  # Use strings directly
                default=self._config_entry.options.get(CONF_CHIME_SOUND, self._config_entry.data.get(CONF_CHIME_SOUND, "threetone.mp3")),
            )] = selector({"select": {"options": chime_opts}})

            schema_dict[vol.Optional(
                "normalize_audio",  # Use strings directly
                default=self._config_entry.options.get(CONF_NORMALIZE_AUDIO, self._config_entry.data.get(CONF_NORMALIZE_AUDIO, False)),
            )] = selector({"boolean": {}})

            # Instructions fields moved above after voice

            schema_dict[vol.Optional(
                "volume_restore",  # Use strings directly
                default=self._config_entry.options.get(CONF_VOLUME_RESTORE, self._config_entry.data.get(CONF_VOLUME_RESTORE, False)),
            )] = selector({"boolean": {}})
            
            # Use string directly for pause_playback
            schema_dict[vol.Optional(
                "pause_playback",  # Must match exactly with translation key
                default=self._config_entry.options.get(CONF_PAUSE_PLAYBACK, self._config_entry.data.get(CONF_PAUSE_PLAYBACK, False)),
            )] = selector({"boolean": {}})
        
        options_schema = vol.Schema(schema_dict)

        return self.async_show_form(step_id="init", data_schema=options_schema)


__all__ = ["OpenAITTSConfigFlow", "OpenAITTSProfileSubentryFlow"]