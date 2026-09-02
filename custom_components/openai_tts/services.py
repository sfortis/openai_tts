"""The ``openai_tts.say`` action.

Registered once from ``async_setup`` rather than per config entry. It
used to be registered by whichever entry happened to be set up first and
removed again when the last one unloaded, so reloading a single entry
took the action away and put it back, and an automation firing inside
that window was told the service did not exist.

The handler needs no entry of its own: everything it works with is
addressed by entity id and resolved through the registries when the call
arrives.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.components.tts import DOMAIN as TTS_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.service import async_register_admin_service

from .api_validation import async_validate_api_key
from .const import (
    CONF_API_KEY,
    CONF_CHIME_ENABLE,
    CONF_CHIME_SOUND,
    CONF_EXTRA_PAYLOAD,
    CONF_MODEL,
    CONF_NORMALIZE_AUDIO,
    CONF_PROVIDER,
    CONF_URL,
    CONF_VOICE,
    DEFAULT_URL,
    DOMAIN,
    is_openai_endpoint,
    migrating_flag,
    preset_for,
    voices_for_model,
)
from .exceptions import OpenAIAuthError
from .utils import normalize_entity_ids
from .volume_restore import announce

_LOGGER = logging.getLogger(__name__)

SERVICE_NAME = "say"


SERVICE_SET_API_KEY = "set_api_key"

# Either way of naming the entry is accepted. ``config_entry_id`` is the
# accurate one, since the key belongs to the parent entry and not to a
# profile; ``tts_entity`` is offered because that is what every other
# call in this integration is targeted with.
# Exactly one way of naming the entry, not one or both. Accepting both
# meant the entry id quietly won and the entity was never looked at, so a
# caller could believe it was addressing one entry while writing to
# another. The key itself has to be non-blank: with validation turned
# off, an empty string would otherwise overwrite a working credential.
SET_API_KEY_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("config_entry_id"): vol.All(
                cv.string, vol.Strip, vol.Length(min=1)
            ),
            vol.Optional("tts_entity"): cv.entity_domain(TTS_DOMAIN),
            vol.Required("api_key"): vol.All(cv.string, vol.Strip, vol.Length(min=1)),
            vol.Optional("validate", default=True): cv.boolean,
        }
    ),
    cv.has_at_least_one_key("config_entry_id", "tts_entity"),
    cv.has_at_most_one_key("config_entry_id", "tts_entity"),
)

# Service Schema
SAY_SCHEMA = vol.Schema(
    {
        vol.Required("tts_entity"): cv.entity_id,
        vol.Required("message"): cv.string,
        vol.Optional("language", default="en"): cv.string,
        vol.Optional("voice"): cv.string,  # Allow any voice name for custom backends
        vol.Optional("speed"): vol.All(vol.Coerce(float), vol.Range(min=0.25, max=4.0)),
        vol.Optional("instructions"): cv.string,
        vol.Optional(CONF_EXTRA_PAYLOAD): cv.string,  # JSON string for custom backend parameters
        # No default= for chime/normalize_audio: voluptuous would inject the
        # default into the validated dict and the handler's "key in data"
        # probe (used to fall back to the TTS profile's defaults) would
        # always see the key as "explicitly provided", silently shadowing
        # profile-level chime / normalize settings.
        vol.Optional("chime"): cv.boolean,
        vol.Optional("chime_sound"): cv.string,
        vol.Optional("normalize_audio"): cv.boolean,
        vol.Optional("volume"): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
        # ``announce`` is the modern flag (default True) that lets each
        # speaker decide via ``MEDIA_ANNOUNCE`` capability detection.
        # ``pause_playback`` is kept as a legacy alias - the engine
        # maps it onto ``announce`` when the new field is missing.
        vol.Optional("announce"): cv.boolean,
        vol.Optional("pause_playback"): cv.boolean,
        vol.Optional("entity_id"): cv.entity_ids,  # For direct entity targeting
        vol.Optional("device_id"): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string])),  # For device targeting
        vol.Optional("area_id"): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string]))     # For area targeting
    }, extra=vol.ALLOW_EXTRA
)


def _validate_voice_compatibility(
    hass: HomeAssistant, tts_entity: str, voice_override: str | None
) -> None:
    """Reject the call early if the voice can't render on the entity's model.

    No silent overrides: when the user picks a voice the configured
    model doesn't support (e.g. ``marin`` on ``tts-1``) we surface a
    HomeAssistantError naming the problem and listing valid voices.
    The user keeps full control over which model their profile uses -
    auto-promoting under the hood would change the API call without
    their knowledge and complicate cost / model-version reasoning.

    Skipped for non-OpenAI endpoints (custom backends decide their own
    voice catalogues; we have no way to know what works there) and
    for unknown / custom model strings (likewise opaque).
    """
    entity_reg = er.async_get(hass)
    entity_entry = entity_reg.async_get(tts_entity)
    if entity_entry is None or entity_entry.config_entry_id is None:
        return
    parent = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
    if parent is None or not is_openai_endpoint(parent.data.get(CONF_URL)):
        return

    model: str | None = None
    if entity_entry.config_subentry_id and getattr(parent, "subentries", None):
        sub = parent.subentries.get(entity_entry.config_subentry_id)
        if sub is not None:
            model = sub.data.get(CONF_MODEL)
    if model is None:
        model = parent.data.get(CONF_MODEL) or parent.options.get(CONF_MODEL)
    if model is None:
        return

    from .const import VOICES_BY_MODEL
    if model not in VOICES_BY_MODEL:
        return  # custom / unknown model - can't validate

    voice = voice_override
    if voice is None:
        if entity_entry.config_subentry_id and getattr(parent, "subentries", None):
            sub = parent.subentries.get(entity_entry.config_subentry_id)
            if sub is not None:
                voice = sub.data.get(CONF_VOICE)
        if voice is None:
            voice = parent.data.get(CONF_VOICE) or parent.options.get(CONF_VOICE)
    if not voice:
        return

    allowed = voices_for_model(model)
    if voice in allowed:
        return
    raise HomeAssistantError(
        f"Voice '{voice}' is not supported by model '{model}'. "
        f"Compatible voices: {', '.join(sorted(allowed))}. "
        f"Switch the profile to 'gpt-4o-mini-tts' to use this voice."
    )


def _get_entities_from_target(
    hass: HomeAssistant, 
    target: dict | None
) -> list[str]:
    """
    Extract entity IDs from service target more efficiently.
    
    Args:
        hass: Home Assistant instance
        target: Service call target dictionary
        
    Returns:
        List of entity IDs
    """
    if not target:
        return []
    
    _LOGGER.debug("Target: %s", target)
    entities = []
    
    # Handle direct entity_ids - normalize to always work with lists.
    # Only accept media_player entities here; passing e.g. a light or
    # input_boolean as a direct target would otherwise sail through to
    # tts.speak and blow up deep inside HA's TTS layer.
    if entity_ids := target.get("entity_id"):
        for entity_id in normalize_entity_ids(entity_ids):
            if entity_id.startswith(f"{MP_DOMAIN}."):
                entities.append(entity_id)
            else:
                _LOGGER.warning(
                    "Ignoring non-media_player target %s (only media_player entities are valid)",
                    entity_id,
                )
        _LOGGER.debug("Added entity_ids from target: %s", entities)

    # Get entity registry only once if needed
    entity_reg = None
    device_reg = None

    if any(key in target for key in ["area_id", "device_id"]):
        entity_reg = er.async_get(hass)
        device_reg = dr.async_get(hass)
    
    # Handle area_ids
    if area_ids := target.get("area_id"):
        # Normalize to always work with lists
        area_ids = normalize_entity_ids(area_ids)
        _LOGGER.debug("Processing area_ids: %s", area_ids)
        
        if entity_reg:
            # First, get all device IDs in these areas
            area_device_ids = set()
            
            # Find devices in these areas
            if device_reg:
                for device in device_reg.devices.values():
                    if device.area_id in area_ids:
                        area_device_ids.add(device.id)
                _LOGGER.debug("Found devices in areas: %s", area_device_ids)
            
            # Get all media player entities for devices in these areas
            for entry in entity_reg.entities.values():
                # Check if entity is directly in area
                if (entry.area_id in area_ids and 
                    entry.domain == MP_DOMAIN and 
                    entry.entity_id not in entities):
                    entities.append(entry.entity_id)
                    _LOGGER.debug("Added entity %s from area %s", entry.entity_id, entry.area_id)
                
                # Also check if entity's device is in area
                elif (entry.device_id in area_device_ids and
                      entry.domain == MP_DOMAIN and
                      entry.entity_id not in entities):
                    entities.append(entry.entity_id)
                    _LOGGER.debug("Added entity %s from device %s in area", entry.entity_id, entry.device_id)
    
    # Handle device_ids
    if device_ids := target.get("device_id"):
        # Normalize to always work with lists
        device_ids = normalize_entity_ids(device_ids)
        _LOGGER.debug("Processing device_ids: %s", device_ids)
        
        if entity_reg:
            # Get all media player entities for specified devices
            for entry in entity_reg.entities.values():
                if (entry.device_id in device_ids and 
                    entry.domain == MP_DOMAIN and 
                    entry.entity_id not in entities):
                    entities.append(entry.entity_id)
                    _LOGGER.debug("Added entity %s from device %s", entry.entity_id, entry.device_id)
    
    _LOGGER.debug("Final entities from target: %s", entities)
    return entities


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the action. Called once, from ``async_setup``."""

    async def _do_say(call: ServiceCall) -> None:
        """Inner body of the say service. Raises on any failure;
        ``_handle_say`` decides whether to surface the exception to
        the caller or convert it to a response payload.
        """
        data = call.data

        # Debug logging
        _LOGGER.debug("Service call data: %s", data)
        _LOGGER.debug("Service call target: %s", getattr(call, 'target', None))

        # Extract media players from target and data
        media_players = []

        # Combine target from both places (call.target attribute and data)
        target_data = {}

        # First check call.target attribute (preferred way)
        if hasattr(call, "target") and call.target:
            # Convert call.target to dict if it's not already
            target_data = dict(call.target) if not isinstance(call.target, dict) else call.target
            _LOGGER.debug("Processing target from call.target: %s", target_data)

        # Also check data for targeting parameters
        for target_key in ["entity_id", "device_id", "area_id"]:
            if target_key in data:
                target_data[target_key] = data[target_key]
                _LOGGER.debug("Found %s in data: %s", target_key, data[target_key])

        # Extract entities using our helper
        if target_data:
            media_players = _get_entities_from_target(hass, target_data)
            _LOGGER.debug("Media players from target data: %s", media_players)

        # Validate TTS entity
        tts_entity = data["tts_entity"]
        tts_state = hass.states.get(tts_entity)
        if not tts_state:
            raise ValueError(f"TTS entity {tts_entity} not found")

        # Get TTS entity's default options from its config
        # Look up the entity to get its default_options property
        entity_defaults = {}
        entity_reg = er.async_get(hass)
        entity_entry = entity_reg.async_get(tts_entity)
        if entity_entry and entity_entry.config_subentry_id:
            # This is a subentry-based entity - find the parent and subentry
            for entry in hass.config_entries.async_entries(DOMAIN):
                if hasattr(entry, 'subentries') and entry.subentries:
                    for subentry_id, subentry in entry.subentries.items():
                        if subentry_id == entity_entry.config_subentry_id:
                            entity_defaults = {
                                "chime": subentry.data.get(CONF_CHIME_ENABLE, False),
                                "chime_sound": subentry.data.get(CONF_CHIME_SOUND, "threetone.mp3"),
                                "normalize_audio": subentry.data.get(CONF_NORMALIZE_AUDIO, True),
                            }
                            _LOGGER.debug("Found entity defaults from subentry: %s", entity_defaults)
                            break
        elif entity_entry and entity_entry.config_entry_id:
            # Legacy entry - get from config entry options
            config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
            if config_entry:
                entity_defaults = {
                    "chime": config_entry.options.get(CONF_CHIME_ENABLE, config_entry.data.get(CONF_CHIME_ENABLE, False)),
                    "chime_sound": config_entry.options.get(CONF_CHIME_SOUND, config_entry.data.get(CONF_CHIME_SOUND, "threetone.mp3")),
                    "normalize_audio": config_entry.options.get(CONF_NORMALIZE_AUDIO, config_entry.data.get(CONF_NORMALIZE_AUDIO, True)),
                }
                _LOGGER.debug("Found entity defaults from config entry: %s", entity_defaults)

        # Get service data - use entity defaults for options not explicitly set
        message = data["message"]
        language = data.get("language", "en")

        # Reject overlength input before we hit the network, but
        # only when we actually know which preset the entry uses
        # (i.e. it has ``CONF_PROVIDER`` set). Legacy entries from
        # before the wizard don't carry the key and may point at a
        # custom backend with a different cap; defaulting them to
        # OpenAI's 4096 would silently regress >4096-char messages
        # that worked in 3.7. Better to let the upstream surface
        # its real error in those cases.
        parent_entry_id = entity_entry.config_entry_id if entity_entry else None
        parent_entry = (
            hass.config_entries.async_get_entry(parent_entry_id)
            if parent_entry_id else None
        )
        provider_key = parent_entry.data.get(CONF_PROVIDER) if parent_entry else None
        if provider_key:
            preset = preset_for(provider_key)
            max_len = preset.get("max_text_length")
            if max_len is not None and len(message) > max_len:
                raise ValueError(
                    f"Message length {len(message)} exceeds the "
                    f"{preset['label']} provider limit of {max_len} characters"
                )

        # For chime/normalize_audio: use service call value if provided, else entity default
        # Note: data.get("chime") returns None if not in call, False if explicitly set to False
        chime_value = data.get("chime") if "chime" in data else entity_defaults.get("chime", False)
        normalize_value = data.get("normalize_audio") if "normalize_audio" in data else entity_defaults.get("normalize_audio", True)
        chime_sound_value = data.get("chime_sound") if "chime_sound" in data else entity_defaults.get("chime_sound")

        options = {
            "voice": data.get("voice"),
            "speed": data.get("speed"),
            "instructions": data.get("instructions"),
            CONF_EXTRA_PAYLOAD: data.get(CONF_EXTRA_PAYLOAD),
            "chime": chime_value,
            "chime_sound": chime_sound_value,
            "normalize_audio": normalize_value,
        }

        # Remove None values
        options = {k: v for k, v in options.items() if v is not None}

        # Reject incompatible (model, voice) combos early so the
        # user sees an actionable error rather than an opaque
        # OpenAI 400. No silent model promotion - profile config
        # is the source of truth.
        _validate_voice_compatibility(hass, tts_entity, options.get("voice"))

        tts_volume = data.get("volume")
        # ``announce`` is the new explicit flag; ``pause_playback``
        # is the legacy alias kept for existing automations. When
        # both are absent, ``announce()`` falls back to the
        # profile-level config (with its own legacy alias chain).
        announce_mode = data.get("announce")
        pause_playback = data.get("pause_playback")

        _LOGGER.debug(
            "Calling announce with: tts_entity=%s, media_players=%s, message=%s, "
            "announce=%s, pause_playback=%s",
            tts_entity, media_players, message, announce_mode, pause_playback,
        )

        # Call the orchestrator. ``announce`` raises HomeAssistantError
        # on speak / engine failures; ``_do_say`` lets it propagate
        # so the outer handler can decide whether to surface as a
        # response payload or re-raise.
        await announce(
            hass,
            tts_entity=tts_entity,
            media_players=media_players,
            message=message,
            language=language,
            options=options,
            tts_volume=tts_volume,
            pause_playback=pause_playback,
            announce=announce_mode,
        )

    async def _handle_say(call: ServiceCall) -> dict[str, Any] | None:
        """Handle the say service call.

        Returns a small status dict when invoked with
        ``response_variable`` so automations / scripts can branch on
        success vs failure (e.g. notify on a failed announcement).
        Without ``return_response`` the service is fire-and-forget
        and returns ``None`` for backwards compatibility with
        existing automations.
        """
        try:
            await _do_say(call)
        except Exception as err:
            if call.return_response:
                return {"success": False, "error": str(err)}
            raise

        if call.return_response:
            return {"success": True}
        return None

    async def _handle_set_api_key(call: ServiceCall) -> dict[str, Any]:
        """Store a new API key on an entry and let the reload pick it up.

        Written for providers that hand out keys with a short life, where
        the alternative is a person opening the settings once a month. The
        key goes into the entry's own data, which is where Home Assistant
        keeps credentials; putting it in an ``input_text`` instead would
        publish it through the states API and the recorder.
        """
        data = call.data
        entry_id = data.get("config_entry_id")
        tts_entity = data.get("tts_entity")
        if not entry_id:
            entity_entry = er.async_get(hass).async_get(tts_entity)
            if entity_entry is None or not entity_entry.config_entry_id:
                raise ServiceValidationError(
                    f"TTS entity {tts_entity} not found"
                )
            entry_id = entity_entry.config_entry_id

        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"{entry_id} is not an OpenAI TTS entry"
            )

        api_key = data["api_key"]
        url = entry.data.get(CONF_URL, DEFAULT_URL)

        # The unchanged case is settled before anything is sent anywhere.
        # This action is built to be called on a schedule, so the common
        # run is one where the key has not moved, and validating it would
        # bill the provider for a request that changes nothing.
        if entry.data.get(CONF_API_KEY) == api_key:
            _LOGGER.debug("API key for %s is unchanged", entry.title)
            return {"success": True, "changed": False}

        if data["validate"]:
            # A key that does not work is worth refusing here. The caller
            # is an automation, so nobody is watching, and the failure
            # would otherwise surface at the next announcement.
            #
            # The two ways this can fail are kept apart on purpose. Only
            # 401 and 403 mean the endpoint looked at the key and said no.
            # Anything else, a timeout, a 5xx, or a 400 because the probe
            # asks for tts-1 and alloy and a self-hosted backend has never
            # heard of either, says nothing about the key. Reporting that
            # as "rejected" would accuse a perfectly good key, and this
            # action exists for exactly those custom endpoints.
            try:
                await async_validate_api_key(hass, api_key, url)
            except OpenAIAuthError as err:
                _LOGGER.error(
                    "%s rejected the new API key for %s: %s",
                    url, entry.title, err,
                )
                raise HomeAssistantError(
                    f"{url} rejected this key: {err}"
                ) from err
            except Exception as err:
                _LOGGER.error(
                    "Could not check the new API key for %s against %s, "
                    "keeping the old one: %s",
                    entry.title, url, err,
                )
                raise HomeAssistantError(
                    f"The key could not be checked against {url} ({err}). "
                    "The old key is still in place. Call this action with "
                    "validate: false to store it without checking."
                ) from err

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_API_KEY: api_key}
        )
        # Writing the entry is synchronous; the reload that rebuilds the
        # engine with the new key is not. The entry's update listener
        # schedules it, and that listener declines to reload at all while
        # Home Assistant is still starting, which is a real possibility
        # for an action meant to be driven by automations. So the reply
        # says what was actually done, stored, and separately whether the
        # running engine has picked it up yet.
        # Three things have to hold for the entry to reload. The listener
        # that does it is registered in ``async_setup_entry`` and torn
        # down on unload, so an entry that is disabled or failed to set
        # up has no listener at all and nothing will pick the key up.
        active = (
            entry.state is ConfigEntryState.LOADED
            and hass.is_running
            and not hass.data.get(DOMAIN, {}).get(
                migrating_flag(entry.entry_id)
            )
        )
        if active:
            _LOGGER.info("Stored a new API key for %s, reloading", entry.title)
        else:
            _LOGGER.warning(
                "Stored a new API key for %s, but the entry will not "
                "reload right now: it is %s, Home Assistant running is "
                "%s, migrating is %s. The key takes effect the next time "
                "the entry is loaded.",
                entry.title, entry.state, hass.is_running,
                bool(hass.data.get(DOMAIN, {}).get(
                    migrating_flag(entry.entry_id)
                )),
            )
        return {"success": True, "changed": True, "reloading": active}

    # Admin only, unlike ``say``. This one rewrites a credential, so the
    # bar is not "may call actions" but "administers this install".
    # ``async_register_admin_service`` is Home Assistant's own wrapper for
    # exactly that and refuses the call for anyone else.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_API_KEY,
        _handle_set_api_key,
        schema=SET_API_KEY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    # Register service. ``SupportsResponse.OPTIONAL`` keeps existing
    # fire-and-forget callers working AND lets newer automations use
    # ``response_variable`` to capture a {success, error} dict for
    # error-handling branches (notify on TTS failure etc.).
    hass.services.async_register(
        DOMAIN,
        SERVICE_NAME,
        _handle_say,
        schema=SAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.info("OpenAI TTS service 'say' registered successfully")
