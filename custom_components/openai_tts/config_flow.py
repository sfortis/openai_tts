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
from homeassistant.helpers.selector import selector, TemplateSelector
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import callback

from .streaming import PIPELINEABLE_FORMATS
from .voice_listing import async_fetch_voice_options
from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_VOICE,
    CONF_SPEED,
    CONF_URL,
    CONF_PROVIDER,
    DEFAULT_URL,
    DEFAULT_PROVIDER,
    DOMAIN,
    MODELS,
    PROVIDER_PRESETS,
    PROVIDER_CUSTOM,
    PROVIDER_OPENAI,
    audio_format_options_for,
    model_supports_instructions,
    is_openai_endpoint,
    voice_options,
    voices_for_model,
    UNIQUE_ID,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_NORMALIZE_AUDIO,
    CONF_INSTRUCTIONS,
    CONF_EXTRA_PAYLOAD,
    CONF_AUDIO_FORMAT,
    DEFAULT_AUDIO_FORMAT,
    CONF_VOLUME_RESTORE,
    CONF_ANNOUNCE_MODE,
    CONF_PAUSE_PLAYBACK,
    DEFAULT_ANNOUNCE_MODE,
    CONF_PROFILE_NAME,
    CONF_STREAM_PIPELINING,
)

SUBENTRY_TYPE_PROFILE = "profile"


class _PipeliningConflict(Exception):
    """Internal signal: the submitted combination cannot stream.

    Raised and caught inside the same handler purely to jump out of the
    build-and-save block without falling into its ``except Exception``,
    which would relabel this as an unknown error.
    """


def _pipelining_conflict(user_input: dict[str, Any]) -> str | None:
    """Return an error key when pipelining is on but cannot take effect.

    Settings that silently defeat streaming are refused here rather
    than ignored at playback time, because quietly dropping something
    the user asked for tells them nothing and leaves them believing
    streaming is broken.

    A chime needs the finished audio before anything can be sent. The
    excluded formats cannot have two responses joined. And loudness
    correction, which streams in general, needs a format it can read
    from a pipe, which rules out the one container whose header states
    a length before the length is known.
    """
    if not user_input.get("stream_pipelining"):
        return None
    if user_input.get("chime"):
        return "pipelining_needs_no_chime"
    audio_format = user_input.get("audio_format")
    if audio_format and audio_format not in PIPELINEABLE_FORMATS:
        return "pipelining_needs_joinable_format"
    return None



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

def validate_user_input(user_input: dict) -> str | None:
    """Return an error translation key for bad input, or None if it is fine.

    Returns a key rather than raising, so the caller can put it straight
    into ``errors`` where the frontend will translate it. Raising a
    ``ValueError`` here meant the message ended up in the error slot as
    untranslatable prose.
    """
    api_url = user_input.get(CONF_URL, DEFAULT_URL)
    api_key = user_input.get(CONF_API_KEY)

    # API key is only required for the default OpenAI endpoint
    if api_url == DEFAULT_URL and not api_key:
        return "api_key_required"
    return None

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
    return await hass.async_add_executor_job(get_chime_options)

class OpenAITTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenAI TTS."""
    VERSION = 2
    MINOR_VERSION = 1  # Increment for subentry flow support

    def __init__(self) -> None:
        super().__init__()
        # Carries the chosen provider preset across steps so
        # ``async_step_credentials`` can pre-fill the URL and decide
        # whether the API key is required.
        self._provider_key: str = DEFAULT_PROVIDER

    @staticmethod
    def _provider_options() -> list[dict[str, str]]:
        """Build the provider dropdown options from ``PROVIDER_PRESETS``."""
        return [
            {"value": key, "label": preset["label"]}
            for key, preset in PROVIDER_PRESETS.items()
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick the TTS provider preset.

        Only collects the provider key. URL / API key / display name come
        in the follow-up ``credentials`` step, where we already know the
        preset and can pre-fill defaults so the user only types what's
        actually unique to them (the API key in most cases).
        """
        if user_input is not None:
            self._provider_key = user_input.get(
                CONF_PROVIDER, DEFAULT_PROVIDER
            )
            return await self.async_step_credentials()

        schema = vol.Schema({
            vol.Required(CONF_PROVIDER, default=DEFAULT_PROVIDER): selector({
                "select": {
                    "options": self._provider_options(),
                    "mode": "dropdown",
                }
            }),
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: collect credentials for the chosen provider preset.

        URL is pre-filled from the preset (editable for ``custom``).
        For presets where ``requires_api_key`` is True the field is
        marked required; otherwise it's optional (custom endpoints can
        legitimately run without one).
        """
        preset = PROVIDER_PRESETS.get(self._provider_key, PROVIDER_PRESETS[PROVIDER_CUSTOM])
        errors: dict[str, str] = {}

        if user_input is not None:
            # Carry the provider key into the saved entry so reconfigure
            # / future migrations know which preset created this entry.
            user_input[CONF_PROVIDER] = self._provider_key
            try:
                if invalid := validate_user_input(user_input):
                    return self.async_show_form(
                        step_id="credentials",
                        data_schema=self._credentials_schema(preset, user_input),
                        errors={"base": invalid},
                        description_placeholders={"provider": preset["label"]},
                    )

                api_key = user_input.get(CONF_API_KEY, "")
                api_url = (user_input.get(CONF_URL) or "").strip()
                # Self-hosted / Custom presets ship with an empty
                # default URL because we don't know the user's LAN
                # endpoint. If they submitted the form unchanged we'd
                # save an entry that posts to "" at runtime - reject
                # explicitly here so the failure surfaces in the flow,
                # not in the first TTS call. Hosted presets (OpenAI /
                # Mistral / Groq) ship with a working URL so the
                # branch below is hit only when the user blanks it.
                if not api_url:
                    errors["base"] = "url_required"
                    return self.async_show_form(
                        step_id="credentials",
                        data_schema=self._credentials_schema(preset, user_input),
                        errors=errors,
                        description_placeholders={"provider": preset["label"]},
                    )
                user_input[CONF_URL] = api_url
                is_custom_endpoint = api_url != DEFAULT_URL

                # Check for duplicate API key (only if API key is provided)
                if api_key:
                    for entry in self._async_current_entries():
                        if entry.data.get(CONF_API_KEY) == api_key:
                            _LOGGER.error("An entry with this API key already exists: %s", entry.title)
                            errors["base"] = "duplicate_api_key"
                            return self.async_show_form(
                                step_id="credentials",
                                data_schema=self._credentials_schema(preset, user_input),
                                errors=errors,
                                description_placeholders={"provider": preset["label"]},
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
                # Catches the custom-endpoint-without-API-key duplicate case
                # that the explicit duplicate_api_key check above can't see
                # (no API key to compare).
                self._abort_if_unique_id_configured()
                hostname = urlparse(user_input[CONF_URL]).hostname
                # Title uses the provider label as the prefix when a
                # known preset was picked, falls back to "OpenAI TTS"
                # for custom / unknown providers. The optional account
                # name still suffixes the title so multi-account setups
                # ("OpenAI - Personal" / "OpenAI - Work") keep working.
                provider_label = preset["label"]
                custom_name = (user_input.get("name") or "").strip()
                if custom_name:
                    title = f"{provider_label} - {custom_name}"
                else:
                    title = f"{provider_label} ({hostname})"
                return self.async_create_entry(
                    title=title,
                    data=user_input,
                )
            except data_entry_flow.AbortFlow:
                return self.async_abort(reason="already_configured")
            except InvalidAPIKey:
                errors["base"] = "invalid_api_key"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except HomeAssistantError:
                # ``errors`` values are translation keys, not prose, so
                # the exception text goes to the log and the user gets a
                # key the frontend can look up.
                _LOGGER.exception("Unexpected Home Assistant error")
                errors["base"] = "unknown_error"
            except ValueError:
                _LOGGER.exception("Invalid value submitted")
                errors["base"] = "unknown_error"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"

        return self.async_show_form(
            step_id="credentials",
            data_schema=self._credentials_schema(preset, user_input),
            errors=errors,
            description_placeholders={"provider": preset["label"]},
        )

    @staticmethod
    def _credentials_schema(
        preset: dict[str, Any], user_input: dict[str, Any] | None
    ) -> vol.Schema:
        """Build the step-2 schema with preset-driven defaults."""
        url_default = (user_input or {}).get(CONF_URL) or preset.get("url") or ""
        api_key_default = (user_input or {}).get(CONF_API_KEY) or ""
        name_default = (user_input or {}).get("name") or ""

        api_key_field: Any
        if preset.get("requires_api_key"):
            api_key_field = vol.Required(CONF_API_KEY, default=api_key_default)
        else:
            api_key_field = vol.Optional(CONF_API_KEY, default=api_key_default)

        # Hosted presets (OpenAI / Mistral / Groq) ship with a working
        # URL pre-filled, so the field stays Optional - the user only
        # needs to override it for unusual reverse-proxy setups. For
        # presets that ship with an empty URL (Self-hosted / Custom)
        # the field is Required so the form refuses to submit blank.
        if preset.get("url"):
            url_field: Any = vol.Optional(CONF_URL, default=url_default)
        else:
            url_field = vol.Required(CONF_URL, default=url_default)

        return vol.Schema({
            vol.Optional("name", default=name_default): str,
            api_key_field: str,
            url_field: str,
        })

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
                # Ensure cleared optional fields are explicitly empty
                if CONF_API_KEY not in user_input:
                    user_input[CONF_API_KEY] = ""
                if CONF_URL not in user_input:
                    user_input[CONF_URL] = DEFAULT_URL

                if invalid := validate_user_input(user_input):
                    errors["base"] = invalid

                api_key = user_input.get(CONF_API_KEY, "")
                api_url = user_input.get(CONF_URL, DEFAULT_URL)
                is_custom_endpoint = api_url != DEFAULT_URL

                # Check for duplicate API key (exclude current entry)
                if api_key:
                    for entry in self._async_current_entries():
                        if entry.entry_id != reconfigure_entry.entry_id and entry.data.get(CONF_API_KEY) == api_key:
                            _LOGGER.error("An entry with this API key already exists: %s", entry.title)
                            errors["base"] = "duplicate_api_key"
                            break

                # Validate the new API key the same way initial setup does,
                # so reconfigure can't quietly save an invalid key that
                # would only fail at runtime.
                if not errors and api_key and not is_custom_endpoint:
                    await async_validate_api_key(api_key, api_url)

                if not errors:
                    # Update the entry using the recommended helper
                    from urllib.parse import urlparse
                    hostname = urlparse(api_url).hostname

                    # Ensure unique_id doesn't change
                    await self.async_set_unique_id(reconfigure_entry.unique_id)
                    self._abort_if_unique_id_mismatch()

                    # Title prefix follows the entry's preset label
                    # (Mistral, Groq, ...) instead of always saying
                    # "OpenAI TTS", which made non-OpenAI entries
                    # read as "OpenAI TTS - Mistral".
                    provider_key = reconfigure_entry.data.get(CONF_PROVIDER)
                    preset = PROVIDER_PRESETS.get(
                        provider_key, PROVIDER_PRESETS[PROVIDER_OPENAI]
                    ) if provider_key else PROVIDER_PRESETS[PROVIDER_OPENAI]
                    title_prefix = preset["label"]

                    custom_name = (user_input.get("name") or "").strip()
                    if custom_name:
                        new_title = f"{title_prefix} - {custom_name}"
                    else:
                        new_title = f"{title_prefix} ({hostname})"
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data_updates=user_input,
                        title=new_title,
                    )

            except InvalidAPIKey:
                errors["base"] = "invalid_api_key"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except HomeAssistantError:
                # ``errors`` values are translation keys, not prose, so
                # the exception text goes to the log and the user gets a
                # key the frontend can look up.
                _LOGGER.exception("Unexpected Home Assistant error")
                errors["base"] = "unknown_error"
            except ValueError:
                _LOGGER.exception("Invalid value submitted")
                errors["base"] = "unknown_error"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"
        
        # Show the form with current values as suggested (not default)
        # Using suggested_value allows users to clear these fields.
        # Pre-fill the name field by reverse-extracting it from the
        # current title - keeps the disambiguation editable. Strip
        # whichever known preset label currently fronts the title so
        # both "OpenAI TTS - Foo" and "Mistral Voxtral - Foo" yield
        # ``current_name == "Foo"``.
        current_data = reconfigure_entry.data
        current_title = reconfigure_entry.title or ""
        current_name = ""
        for _preset in PROVIDER_PRESETS.values():
            prefix = f"{_preset['label']} - "
            if current_title.startswith(prefix):
                current_name = current_title[len(prefix):]
                break
        schema = vol.Schema({
            vol.Optional("name", description={"suggested_value": current_name}): str,
            vol.Optional(CONF_API_KEY, description={"suggested_value": current_data.get(CONF_API_KEY, "")}): str,
            vol.Optional(CONF_URL, description={"suggested_value": current_data.get(CONF_URL, DEFAULT_URL)}): str,
        })
        
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )


class OpenAITTSProfileSubentryFlow(ConfigSubentryFlow):
    """Handle a subentry flow for OpenAI TTS profiles."""

    # Carries selections from step 1 (profile name + model) into step 2
    # (voice + audio options) so we can render the voice picker against
    # the chosen model. Reconfigure also reuses ``_step1_model``.
    _step1_profile_name: str = ""
    _step1_model: str = "tts-1"
    _reconfigure_subentry: Any = None

    def _parent_preset(self) -> dict[str, Any]:
        """Return the provider preset attached to the parent entry.

        Entries created before the provider wizard have no
        ``CONF_PROVIDER`` key. For those we infer the preset from the
        endpoint URL rather than assuming OpenAI: an entry pointing at
        a self-hosted backend must land on the Custom preset, whose
        capability flags leave ``extra_payload`` and free-text voices
        enabled. Assuming OpenAI there hid ``extra_payload`` from the
        form, and the reconfigure save path then cleared the stored
        value.
        """
        parent_entry = self._get_entry()
        if parent_entry is None:
            return PROVIDER_PRESETS[PROVIDER_OPENAI]

        provider_key = parent_entry.data.get(CONF_PROVIDER)
        if provider_key:
            preset = PROVIDER_PRESETS.get(provider_key)
            if preset is not None:
                return preset

        # No stored provider (or an unknown one): fall back on the URL.
        if is_openai_endpoint(parent_entry.data.get(CONF_URL)):
            return PROVIDER_PRESETS[PROVIDER_OPENAI]
        return PROVIDER_PRESETS[PROVIDER_CUSTOM]

    async def _fetch_remote_voices(self) -> list[dict[str, str]] | None:
        """Pull the live voice catalogue from the provider's REST API.

        Returns selector options of the form ``[{"value": <id>,
        "label": <name>}, ...]`` so the dropdown shows human-readable
        names while submitting the voice ID the TTS request needs.

        Mistral is the canonical case: every voice is user-cloned via
        ``POST /v1/audio/voices`` so a static catalogue would always
        be wrong. We hit ``GET /v1/audio/voices`` (sibling of the
        configured speech endpoint) and translate
        ``{"items": [{"id":..,"name":..}], "total": N}``.

        Returns ``None`` on any failure, empty result, or when the
        preset doesn't advertise voice listing - the caller falls
        back to a free-text input so the flow never gets stuck on a
        Mistral account that hasn't cloned any voices yet.
        """
        # Default is to try. A backend that has no listing endpoint
        # answers 404 and the caller falls back to a typed voice name,
        # which is a better trade than never asking the self-hosted
        # servers that most need asking. Only OpenAI opts out, because
        # it is known to have no such endpoint.
        preset = self._parent_preset()
        if preset.get("supports_voice_listing", True) is False:
            return None

        parent_entry = self._get_entry()
        if not parent_entry:
            return None
        speech_url = parent_entry.data.get(CONF_URL)
        if not speech_url:
            return None
        return await async_fetch_voice_options(
            self.hass, speech_url, parent_entry.data.get(CONF_API_KEY)
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Step 1 of profile creation: profile name + model.

        Splitting the flow into two steps lets us render the voice
        picker in step 2 with options filtered by the model the user
        just chose - guided UX, no chance of picking a voice the model
        rejects (e.g. ``marin`` on ``tts-1``).
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                profile_name = user_input.get(CONF_PROFILE_NAME, "")
                # Reject duplicates here so the user sees the error
                # before investing in step 2.
                parent_entry = self._get_entry()
                existing_subentries = getattr(parent_entry, "subentries", {}) or {}
                duplicate = any(
                    sub.data.get(CONF_PROFILE_NAME) == profile_name
                    for sub in existing_subentries.values()
                )
                if not profile_name:
                    errors["base"] = "profile_name_required"
                elif duplicate:
                    errors["base"] = "already_exists"
                else:
                    # Stash step-1 selections on the flow so step 2 can
                    # read them and show the right voice list.
                    self._step1_profile_name = profile_name
                    self._step1_model = user_input.get(CONF_MODEL, "tts-1")
                    return await self.async_step_voice_audio()

            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"

        preset = self._parent_preset()
        # Non-OpenAI presets (Mistral, Groq, self-hosted, custom) default
        # to the model name baked into the preset so users don't have to
        # remember the exact slug. OpenAI / unknown presets keep "tts-1".
        default_model = preset.get("default_model") or "tts-1"
        # Model catalogue mirrors voice_catalog: presets that know
        # their model list (Mistral, Groq) provide it, otherwise the
        # OpenAI default ``MODELS`` is used. ``custom_value`` is on so
        # self-hosted users can still type their own model name.
        model_options = preset.get("model_catalog") or MODELS
        step1_schema = vol.Schema({
            vol.Required(CONF_PROFILE_NAME): str,
            vol.Required(CONF_MODEL, default=default_model): selector({
                "select": {
                    "options": model_options,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            }),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=step1_schema,
            errors=errors,
        )

    async def async_step_voice_audio(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 2 of profile creation: voice + audio options.

        Renders the voice picker with options filtered by the model
        chosen in step 1. For non-OpenAI endpoints (custom backends
        like Chatterbox), falls back to a free-text voice input
        because we have no idea what the backend's voice catalogue
        looks like.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                key_mapping = {
                    "chime": CONF_CHIME_ENABLE,
                    "chime_sound": CONF_CHIME_SOUND,
                    "normalize_audio": CONF_NORMALIZE_AUDIO,
                    "instructions": CONF_INSTRUCTIONS,
                    "extra_payload": CONF_EXTRA_PAYLOAD,
                    "audio_format": CONF_AUDIO_FORMAT,
                    "stream_pipelining": CONF_STREAM_PIPELINING,
                }
                mapped_input: dict[str, Any] = {
                    CONF_PROFILE_NAME: self._step1_profile_name,
                    CONF_MODEL: self._step1_model,
                }
                for key, value in user_input.items():
                    mapped_key = key_mapping.get(key, key)
                    if key in ("instructions", "extra_payload") and value == "":
                        mapped_input[mapped_key] = None
                    else:
                        mapped_input[mapped_key] = value

                if conflict := _pipelining_conflict(user_input):
                    errors["base"] = conflict
                    raise _PipeliningConflict

                mapped_input[UNIQUE_ID] = generate_entry_id()
                return self.async_create_entry(
                    title=self._step1_profile_name,
                    data=mapped_input,
                )
            except _PipeliningConflict:
                pass
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"

        chime_opts = await async_get_chime_options(self.hass)
        parent_entry = self._get_entry()
        endpoint_url = parent_entry.data.get(CONF_URL) if parent_entry else None
        is_openai = is_openai_endpoint(endpoint_url)
        preset = self._parent_preset()
        preset_voices = preset.get("voice_catalog")
        # Try a live fetch when the preset advertises ``supports_voice_listing``
        # (Mistral). Falls through to the free-text branch when the
        # backend returns 0 voices, errors out, or the preset doesn't
        # support listing - so a brand-new Mistral account never gets
        # stuck on an empty dropdown.
        remote_voices: list[dict[str, str]] | None = None
        if not is_openai and not preset_voices:
            remote_voices = await self._fetch_remote_voices()
        allowed_voices = voices_for_model(self._step1_model)
        default_voice = "shimmer" if "shimmer" in allowed_voices else allowed_voices[0]

        if is_openai:
            # ``custom_value`` is OFF on OpenAI endpoints so the picker
            # cannot accept a voice the model rejects. Custom backends
            # below get a free-text input instead.
            voice_field: Any = selector({
                "select": {
                    "options": voice_options(allowed_voices),
                    "mode": "dropdown",
                    "sort": False,
                    "custom_value": False,
                }
            })
        elif preset_voices:
            # Known non-OpenAI preset (Groq Orpheus, ...) - render the
            # preset's static catalogue as a dropdown but keep
            # ``custom_value`` ON so users can still type a name the
            # catalogue is missing (e.g. an Arabic Saudi voice when
            # the user switched the Groq model to
            # ``canopylabs/orpheus-arabic-saudi``).
            voice_field = selector({
                "select": {
                    "options": list(preset_voices),
                    "mode": "dropdown",
                    "sort": False,
                    "custom_value": True,
                }
            })
            default_voice = preset_voices[0]
        elif remote_voices:
            # Live catalogue from the provider (Mistral's user-cloned
            # voices). Submit the voice ID, show the user-given name.
            # ``custom_value`` is OFF so the HA frontend renders the
            # ``label`` ("Paul - Sad") in the selected state; with it
            # ON the picker shows the raw UUID after a click.
            voice_field = selector({
                "select": {
                    "options": remote_voices,
                    "mode": "dropdown",
                    "sort": False,
                    "custom_value": False,
                }
            })
            default_voice = remote_voices[0]["value"]
        else:
            # Self-hosted / custom backend with no published catalogue,
            # or Mistral account with no cloned voices yet - let the
            # user type whatever the backend understands.
            voice_field = selector({"text": {}})

        step2_fields: dict[Any, Any] = {
            vol.Required(CONF_VOICE, default=default_voice): voice_field,
        }
        # ``speed`` is OpenAI-style (0.25-4.0). Mistral hard-rejects
        # any non-default value with HTTP 422 ``extra_forbidden``;
        # Groq Orpheus accepts the field but silently ignores it.
        # Either way the slider is misleading on those backends, so
        # the preset gates it.
        if preset.get("supports_speed", True):
            step2_fields[vol.Optional(CONF_SPEED, default=1.0)] = selector({
                "number": {"min": 0.25, "max": 4.0, "step": 0.05, "mode": "slider"}
            })
        # ``instructions`` only goes on the wire for models that
        # actually accept it (gpt-4o-mini-tts today). Hiding the field
        # for the others keeps the form honest - and stops users
        # filling in styling text the backend will silently ignore.
        if model_supports_instructions(self._step1_model):
            step2_fields[
                vol.Optional("instructions", description={"suggested_value": ""})
            ] = TemplateSelector()
        step2_fields.update({
            vol.Optional("chime", default=False): selector({"boolean": {}}),
            vol.Optional("chime_sound", default="threetone.mp3"): selector({
                "select": {"options": chime_opts}
            }),
            vol.Optional("normalize_audio", default=True): selector({"boolean": {}}),
        })
        # ``extra_payload`` is the value-add of self-hosted / custom
        # presets (e.g. Chatterbox ``seed``, TTS Web UI speaker_id).
        # Hosted providers have a fixed schema and would only reject
        # the extra fields, so the preset gates it.
        if preset.get("supports_extra_payload", False):
            step2_fields[
                vol.Optional("extra_payload", description={"suggested_value": ""})
            ] = TemplateSelector()
        # ``audio_format`` is always surfaced. OpenAI handles all values
        # natively, so users can switch to wav/opus without breaking the
        # request. For custom backends (issue #61: pocket-tts) it's the
        # only way to negotiate around servers that reject mp3. The
        # preset's ``default_format`` wins when the backend is picky
        # (e.g. Groq Orpheus only emits WAV) and the dropdown is
        # filtered to the preset's allowed list so users can't save a
        # combo the backend will reject.
        default_audio_format = preset.get("default_format") or DEFAULT_AUDIO_FORMAT
        step2_fields[
            vol.Optional("audio_format", default=default_audio_format)
        ] = selector({
            "select": {
                "options": audio_format_options_for(preset),
                "mode": "dropdown",
                "sort": False,
            }
        })
        # Sentence pipelining. Offered on every streaming-capable preset,
        # because the format it depends on is chosen on this same form
        # and so cannot gate a field rendered alongside it. An
        # incompatible combination is rejected on submit by
        # ``_pipelining_conflict`` rather than being silently accepted.
        # The runtime fallback in ``tts.py`` still exists for profiles
        # saved before that validation was added.
        if preset.get("supports_streaming", True):
            step2_fields[
                vol.Optional("stream_pipelining", default=False)
            ] = selector({"boolean": {}})
        step2_schema = vol.Schema(step2_fields)

        return self.async_show_form(
            step_id="voice_audio",
            data_schema=step2_schema,
            errors=errors,
            description_placeholders={"model": self._step1_model},
        )
    
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Reconfigure step 1: pick the model.

        Mirrors create flow: model first (so step 2 can filter the
        voice picker by it), audio settings second. Lets the user
        switch from tts-1 → gpt-4o-mini-tts and immediately see the
        marin/cedar/ballad/verse voices that the new model unlocks.
        """
        errors: dict[str, str] = {}

        try:
            subentry = self._get_reconfigure_subentry()
        except Exception as e:
            _LOGGER.error("Failed to get reconfigure subentry: %s", e)
            return self.async_abort(reason="subentry_not_found")

        if not subentry:
            _LOGGER.error("Reconfigure subentry is None")
            return self.async_abort(reason="subentry_not_found")

        self._reconfigure_subentry = subentry
        existing_data = subentry.data
        _LOGGER.debug(
            "Reconfiguring subentry: %s (profile: %s)",
            subentry.title,
            existing_data.get(CONF_PROFILE_NAME, "unknown"),
        )

        if user_input is not None:
            try:
                self._step1_model = user_input.get(CONF_MODEL, "tts-1")
                return await self.async_step_reconfigure_voice()
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"

        existing_model = existing_data.get(CONF_MODEL, "tts-1")
        preset = self._parent_preset()
        model_options = list(preset.get("model_catalog") or MODELS)
        # Make sure the saved model is selectable so an entry that
        # was created before the preset's catalogue existed (or
        # whose model was renamed upstream) still loads cleanly.
        if existing_model and existing_model not in model_options:
            model_options.append(existing_model)
        step1_schema = vol.Schema({
            vol.Required(CONF_MODEL, default=existing_model): selector({
                "select": {
                    "options": model_options,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            }),
        })

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=step1_schema,
            errors=errors,
        )

    async def async_step_reconfigure_voice(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure step 2: voice + audio settings.

        Voice picker is filtered by the model chosen in step 1, so
        the user only sees voices the model can actually render.
        """
        errors: dict[str, str] = {}
        subentry = self._reconfigure_subentry
        existing_data = subentry.data

        if user_input is not None:
            try:
                key_mapping = {
                    "chime": CONF_CHIME_ENABLE,
                    "chime_sound": CONF_CHIME_SOUND,
                    "normalize_audio": CONF_NORMALIZE_AUDIO,
                    "instructions": CONF_INSTRUCTIONS,
                    "extra_payload": CONF_EXTRA_PAYLOAD,
                    "audio_format": CONF_AUDIO_FORMAT,
                    "stream_pipelining": CONF_STREAM_PIPELINING,
                }
                mapped_input: dict[str, Any] = {CONF_MODEL: self._step1_model}
                for key, value in user_input.items():
                    mapped_key = key_mapping.get(key, key)
                    if key in ("instructions", "extra_payload") and value == "":
                        mapped_input[mapped_key] = None
                    else:
                        mapped_input[mapped_key] = value
                # Empty optional text fields aren't submitted by HA, so
                # explicitly clear them to drop any stale value on the
                # subentry.
                #
                # Only clear the fields this form actually rendered.
                # ``instructions`` is gated on the model and
                # ``extra_payload`` on the provider preset, so a field
                # the user never saw is absent from ``user_input`` for
                # a completely different reason than "the user emptied
                # it". Clearing those wiped a stored value on every
                # unrelated reconfigure - the profile keeps it instead.
                clearable: list[tuple[str, str]] = []
                if model_supports_instructions(self._step1_model):
                    clearable.append(("instructions", CONF_INSTRUCTIONS))
                if self._parent_preset().get("supports_extra_payload", False):
                    clearable.append(("extra_payload", CONF_EXTRA_PAYLOAD))
                for field, const in clearable:
                    if field not in user_input:
                        mapped_input[const] = None

                if conflict := _pipelining_conflict(user_input):
                    errors["base"] = conflict
                    raise _PipeliningConflict

                updated_data = {**existing_data, **mapped_input}
                entry_id = getattr(subentry, "entry_id", getattr(subentry, "subentry_id", "unknown"))
                _LOGGER.info("Updating subentry %s with data: %s", entry_id, updated_data)
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    data=updated_data,
                )
            except _PipeliningConflict:
                pass
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown_error"

        chime_opts = await async_get_chime_options(self.hass)
        parent_entry = self._get_entry()
        endpoint_url = parent_entry.data.get(CONF_URL) if parent_entry else None
        is_openai = is_openai_endpoint(endpoint_url)
        preset = self._parent_preset()
        preset_voices = preset.get("voice_catalog")
        remote_voices: list[dict[str, str]] | None = None
        if not is_openai and not preset_voices:
            remote_voices = await self._fetch_remote_voices()
        allowed_voices = voices_for_model(self._step1_model)
        existing_voice = existing_data.get(CONF_VOICE, "shimmer")
        # For OpenAI endpoints, fall back to a compatible voice when
        # the saved one isn't in the model's allowed list. For custom
        # endpoints the field is a free-text input and the saved voice
        # is whatever the backend understands (Mistral slugs, custom
        # cloned voice ids, etc.); don't second-guess it.
        if is_openai:
            default_voice = (
                existing_voice if existing_voice in allowed_voices
                else allowed_voices[0]
            )
        else:
            default_voice = existing_voice

        if is_openai:
            # See note in async_step_voice_audio: lock the picker so
            # users can't save an incompatible voice.
            voice_field: Any = selector({
                "select": {
                    "options": voice_options(allowed_voices),
                    "mode": "dropdown",
                    "sort": False,
                    "custom_value": False,
                }
            })
        elif preset_voices:
            # Known preset (Groq Orpheus, ...) - dropdown of catalogue
            # voices but custom_value stays ON so an existing free-text
            # voice (older entries, Arabic Saudi voices on Groq, ...)
            # still loads cleanly.
            voice_field = selector({
                "select": {
                    "options": list(preset_voices),
                    "mode": "dropdown",
                    "sort": False,
                    "custom_value": True,
                }
            })
        elif remote_voices:
            # Live catalogue from the provider (Mistral). The saved
            # value is appended when missing so an existing entry
            # whose voice was deleted upstream still loads instead of
            # tripping the schema; user can then pick a current one.
            # ``custom_value`` stays OFF (frontend shows the label in
            # the selected state) - the appended ``(saved)`` row
            # already covers the "voice deleted upstream" edge case
            # without needing free-text input.
            options = list(remote_voices)
            if not any(opt["value"] == existing_voice for opt in options):
                options.append({
                    "value": existing_voice,
                    "label": f"{existing_voice} (saved)",
                })
            voice_field = selector({
                "select": {
                    "options": options,
                    "mode": "dropdown",
                    "sort": False,
                    "custom_value": False,
                }
            })
        else:
            voice_field = selector({"text": {}})

        step2_fields: dict[Any, Any] = {
            vol.Required(CONF_VOICE, default=default_voice): voice_field,
        }
        # ``speed`` and ``extra_payload`` are gated by the preset
        # capability flags (see notes in async_step_voice_audio).
        # Saved values on profiles that switched to a non-supporting
        # provider remain in the entry data but stop being rendered;
        # harmless on the request side because the engine drops
        # ``speed=1.0`` from the payload anyway.
        if preset.get("supports_speed", True):
            step2_fields[
                vol.Optional(CONF_SPEED, default=existing_data.get(CONF_SPEED, 1.0))
            ] = selector({
                "number": {"min": 0.25, "max": 4.0, "step": 0.05, "mode": "slider"}
            })
        if model_supports_instructions(self._step1_model):
            step2_fields[
                vol.Optional(
                    "instructions",
                    description={
                        "suggested_value": existing_data.get(CONF_INSTRUCTIONS) or ""
                    },
                )
            ] = TemplateSelector()
        step2_fields.update({
            vol.Optional("chime", default=existing_data.get(CONF_CHIME_ENABLE, False)): selector({"boolean": {}}),
            vol.Optional("chime_sound", default=existing_data.get(CONF_CHIME_SOUND, "threetone.mp3")): selector({
                "select": {"options": chime_opts}
            }),
            vol.Optional("normalize_audio", default=existing_data.get(CONF_NORMALIZE_AUDIO, True)): selector({"boolean": {}}),
        })
        if preset.get("supports_extra_payload", False):
            step2_fields[
                vol.Optional(
                    "extra_payload",
                    description={
                        "suggested_value": existing_data.get(CONF_EXTRA_PAYLOAD) or ""
                    },
                )
            ] = TemplateSelector()
        # Always surface audio_format in reconfigure too (see create-flow note).
        # The dropdown is filtered to the preset's ``allowed_formats``
        # for the same reason as in the create flow. The saved value is
        # forced into the list so an older entry with a now-disallowed
        # format (e.g. Groq+mp3 from a pre-Orpheus build) still loads and
        # the user can pick a valid one.
        format_options = list(audio_format_options_for(preset))
        existing_format = existing_data.get(CONF_AUDIO_FORMAT, DEFAULT_AUDIO_FORMAT)
        if not any(opt["value"] == existing_format for opt in format_options):
            format_options.append({
                "value": existing_format,
                "label": f"{existing_format} (saved value)",
            })
        step2_fields[
            vol.Optional(
                "audio_format",
                default=existing_format,
            )
        ] = selector({
            "select": {
                "options": format_options,
                "mode": "dropdown",
                "sort": False,
            }
        })
        if preset.get("supports_streaming", True):
            step2_fields[
                vol.Optional(
                    "stream_pipelining",
                    default=existing_data.get(CONF_STREAM_PIPELINING, False),
                )
            ] = selector({"boolean": {}})
        step2_schema = vol.Schema(step2_fields)

        return self.async_show_form(
            step_id="reconfigure_voice",
            data_schema=step2_schema,
            errors=errors,
            description_placeholders={"model": self._step1_model},
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
                "announce_mode": CONF_ANNOUNCE_MODE,
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
            current_model = self._config_entry.options.get(
                CONF_MODEL, self._config_entry.data.get(CONF_MODEL, "tts-1")
            )
            schema_dict[vol.Optional(
                "model",
                default=current_model,
            )] = selector({
                "select": {
                    "options": MODELS,
                    "mode": "dropdown",
                    "sort": True,
                    "custom_value": True,
                }
            })

            # Voice picker filtered by the current model and locked to
            # that set so legacy entries can't save marin/cedar/etc on
            # a tts-1 profile and hit a runtime API failure.
            allowed_legacy_voices = voices_for_model(current_model)
            current_voice = self._config_entry.options.get(
                CONF_VOICE, self._config_entry.data.get(CONF_VOICE, "shimmer")
            )
            voice_default = (
                current_voice
                if current_voice in allowed_legacy_voices
                else allowed_legacy_voices[0]
            )
            schema_dict[vol.Optional(
                "voice",
                default=voice_default,
            )] = selector({
                "select": {
                    "options": voice_options(allowed_legacy_voices),
                    "mode": "dropdown",
                    "sort": False,
                    "custom_value": False,
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
                default=self._config_entry.options.get(CONF_NORMALIZE_AUDIO, self._config_entry.data.get(CONF_NORMALIZE_AUDIO, True)),
            )] = selector({"boolean": {}})

            # Instructions fields moved above after voice

            schema_dict[vol.Optional(
                "volume_restore",  # Use strings directly
                default=self._config_entry.options.get(CONF_VOLUME_RESTORE, self._config_entry.data.get(CONF_VOLUME_RESTORE, False)),
            )] = selector({"boolean": {}})
            
            # Announcement mode: the modern toggle. Defaults to on, but
            # an entry that already carried a ``pause_playback`` choice
            # was seeded from it during setup, so the value shown here
            # is the user's own preference rather than a blanket True.
            schema_dict[vol.Optional(
                "announce_mode",  # Must match exactly with translation key
                default=self._config_entry.options.get(
                    CONF_ANNOUNCE_MODE,
                    self._config_entry.data.get(
                        CONF_ANNOUNCE_MODE, DEFAULT_ANNOUNCE_MODE
                    ),
                ),
            )] = selector({"boolean": {}})

            # Legacy force-pause toggle, kept with its original
            # meaning. It only adds pausing on top of whatever
            # announcement mode decides.
            schema_dict[vol.Optional(
                "pause_playback",  # Must match exactly with translation key
                default=self._config_entry.options.get(CONF_PAUSE_PLAYBACK, self._config_entry.data.get(CONF_PAUSE_PLAYBACK, False)),
            )] = selector({"boolean": {}})
        
        options_schema = vol.Schema(schema_dict)

        return self.async_show_form(step_id="init", data_schema=options_schema)


__all__ = ["OpenAITTSConfigFlow", "OpenAITTSProfileSubentryFlow"]