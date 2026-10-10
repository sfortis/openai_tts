"""Repairs helpers for the openai_tts integration.

Thin layer over ``homeassistant.helpers.issue_registry`` that gives the
rest of the integration:

* One place that owns the issue-id naming scheme, so we don't sprinkle
  ``f"{prefix}_{...}"`` strings across the codebase.
* A token-based API (``raise_repair`` / ``clear_repair``) so adding a
  new kind of repair is "define a token, write a matching translation
  key, call the helpers" - no per-issue boilerplate.
* Entry-level cleanup so update listeners can blanket-clear stale
  repairs on reload without knowing every issue type that exists.

Adding a new repair:

1. Define an ``ISSUE_*`` token below.
2. Add a matching translation block under ``issues.<token>`` in
   ``strings.json`` (and the ``translations/`` JSONs).
3. Call ``raise_repair(...)`` from wherever the failure is detected,
   and (optionally) wire ``clear_repairs_for_entry`` into the entry
   reload path so successful reconfigures clear stale issues.

Specialised wrappers like :func:`create_voice_deleted_issue` exist for
the cases that need extra context (placeholders, scope id rules); new
repairs that fit the generic pattern can call ``raise_repair``
directly.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import CONF_MODEL, CONF_PROFILE_NAME, CONF_URL, DOMAIN, is_openai_endpoint
from .ma_twins import find_twin, native_ids
from .model_retirement import DEPRECATIONS_URL, shutdown_date, warning_start

_LOGGER = logging.getLogger(__name__)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    """Required by HA's repairs platform loader.

    All issues we raise today are ``is_fixable=False`` (informational
    only), so HA never actually invokes this. The function exists so
    the module qualifies as a valid repairs platform - without it HA
    logs ``Invalid repairs platform`` at startup. ConfirmRepairFlow is
    a safe placeholder for any future fixable issue we add.
    """
    return ConfirmRepairFlow()

# --- Issue tokens ----------------------------------------------------------
#
# Each token is also the translation key under ``issues.<token>``.
# Keep them snake_case and stable: the registry persists issue ids
# across restarts, so renaming a token leaves stale issues hanging.
ISSUE_VOICE_DELETED = "voice_deleted"
ISSUE_MODEL_RETIREMENT = "model_retirement"
ISSUE_MUSIC_ASSISTANT_TWIN = "music_assistant_twin"

_MUSIC_ASSISTANT = "music_assistant"
_MEDIA_PLAYER = "media_player"


def _issue_id(token: str, scope_id: str) -> str:
    """Compose the canonical issue id for ``(token, scope)``.

    ``scope_id`` is the entity that the issue is "about" - usually a
    config entry or subentry id. Keeping it explicit means two
    different profiles with the same broken voice produce two
    distinct repairs the user can dismiss / fix independently.
    """
    return f"{token}_{scope_id}"


def raise_repair(
    hass: HomeAssistant,
    token: str,
    scope_id: str,
    *,
    translation_placeholders: dict[str, str] | None = None,
    severity: ir.IssueSeverity = ir.IssueSeverity.ERROR,
    is_fixable: bool = False,
    is_persistent: bool = True,
    learn_more_url: str | None = None,
) -> None:
    """Idempotently surface a Repairs panel issue.

    HA's issue registry deduplicates by ``(domain, issue_id)`` so this
    is safe to call on every retry of the same failure - the panel
    won't fill up with duplicates and the placeholders just refresh
    in place.

    A persistent issue survives a restart. Pass ``is_persistent=False``
    for an issue that is raised again whenever its cause recurs, so it
    disappears on its own once the cause is gone.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(token, scope_id),
        is_fixable=is_fixable,
        is_persistent=is_persistent,
        severity=severity,
        translation_key=token,
        translation_placeholders=translation_placeholders or {},
        learn_more_url=learn_more_url,
    )


def clear_repair(hass: HomeAssistant, token: str, scope_id: str) -> None:
    """Remove the repair for ``(token, scope)`` if it exists.

    No-op when the issue isn't present, so callers don't need to
    track which repairs are currently raised.
    """
    ir.async_delete_issue(hass, DOMAIN, _issue_id(token, scope_id))


def clear_repairs_for_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    tokens: Iterable[str] = (ISSUE_VOICE_DELETED,),
) -> None:
    """Wipe the listed repairs for every subentry under ``entry``.

    Used by the entry's update listener: after the user reconfigures a
    profile we don't know which subentry changed (HA only signals
    "the entry was updated"), so we clear every relevant repair under
    it. The next failed TTS call will recreate the issue if the
    underlying problem still exists.
    """
    for subentry_id in (getattr(entry, "subentries", None) or {}):
        for token in tokens:
            clear_repair(hass, token, subentry_id)


# --- Specialised wrappers --------------------------------------------------

def create_voice_deleted_issue(
    hass: HomeAssistant,
    parent_entry_id: str,
    subentry_id: str,
    profile_name: str,
    voice: str | None,
) -> None:
    """Surface a repair for a TTS agent whose voice was deleted upstream.

    Thin wrapper over :func:`raise_repair` that supplies the right
    token and the placeholders the translation expects. The
    ``parent_entry_id`` argument is unused today but kept in the
    signature so a future fix flow that opens the right config entry
    can be added without churning the call sites.
    """
    raise_repair(
        hass,
        ISSUE_VOICE_DELETED,
        subentry_id,
        translation_placeholders={
            "voice": voice or "?",
            "profile": profile_name,
        },
    )


def warn_music_assistant_twins(
    hass: HomeAssistant, entity_ids: Iterable[str]
) -> None:
    """Warn about targets that Music Assistant also plays to.

    ``entity_ids`` are the targets the manual flow is about to pause or
    set a volume on. A target whose device matches a Music Assistant
    player gets a warning that names the Music Assistant entity to use
    instead. The announcement goes around Music Assistant: a pause
    reaches it only through the speaker and stops the speaker's whole
    group, and on Cast the announcement replaces Music Assistant's
    receiver app, so the speaker stays silent afterwards while Music
    Assistant still reports it playing. See ``ma_twins``.

    The issue is not persistent. It is raised on the first announcement
    to the target after each start, and an issue that already exists is
    left alone, so the log carries one warning per target and an issue
    the user ignored stays ignored. Once the automations no longer use
    the target, the issue is gone after the next restart.
    """
    targets = list(entity_ids)
    if not targets:
        return
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    music_assistant: dict[str, list[str]] = {}
    for ma_entry in hass.config_entries.async_entries(_MUSIC_ASSISTANT):
        for entity in er.async_entries_for_config_entry(entities, ma_entry.entry_id):
            if entity.domain != _MEDIA_PLAYER or entity.device_id is None:
                continue
            device = devices.async_get(entity.device_id)
            if device is None:
                continue
            music_assistant[entity.entity_id] = [
                value
                for domain, *values in device.identifiers
                if domain == _MUSIC_ASSISTANT
                for value in values
            ]
    if not music_assistant:
        return

    for entity_id in targets:
        entry = entities.async_get(entity_id)
        if (
            entry is None
            or entry.platform == _MUSIC_ASSISTANT
            or entry.device_id is None
        ):
            continue
        device = devices.async_get(entry.device_id)
        if device is None:
            continue
        twin = find_twin(
            native_ids(device.identifiers, device.connections), music_assistant
        )
        if twin is None:
            continue
        # A non-persistent issue stays in the registry after a restart,
        # inactive, so that an ignored one stays ignored. Only an active
        # issue means this start has already warned about the target.
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, _issue_id(ISSUE_MUSIC_ASSISTANT_TWIN, entity_id)
        )
        if issue is not None and issue.active:
            continue
        _LOGGER.warning(
            "%s is the same speaker as the Music Assistant entity %s. "
            "An announcement on it goes around Music Assistant, which "
            "either stops the speaker's group or leaves the speaker "
            "silent afterwards. Target %s instead",
            entity_id, twin, twin,
        )
        raise_repair(
            hass,
            ISSUE_MUSIC_ASSISTANT_TWIN,
            entity_id,
            translation_placeholders={"entity": entity_id, "ma_entity": twin},
            severity=ir.IssueSeverity.WARNING,
            is_persistent=False,
        )


def sync_model_retirement_issues(hass: HomeAssistant) -> date | None:
    """Warn about every profile whose OpenAI model is about to retire.

    The issues are derived from the configuration and today's date, so
    this runs on every entry setup and rebuilds them for all entries of
    the domain. Home Assistant reloads an entry after any change to its
    profiles, which means a profile that moved to another model or was
    deleted loses its warning at the next setup without anyone tracking
    it.

    Returns the earliest day after today on which another warning
    starts, or None, so that the caller can come back on that day.
    """
    today = dt_util.now().date()
    upcoming: date | None = None
    wanted: dict[str, dict[str, str]] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        on_openai = is_openai_endpoint(entry.data.get(CONF_URL))
        for subentry_id, subentry in (getattr(entry, "subentries", None) or {}).items():
            model = subentry.data.get(CONF_MODEL)
            start = warning_start(model, on_openai)
            if start is None:
                continue
            if start > today:
                upcoming = start if upcoming is None else min(upcoming, start)
                continue
            when = shutdown_date(model, on_openai)
            wanted[subentry_id] = {
                "profile": subentry.data.get(CONF_PROFILE_NAME) or subentry.title,
                "model": model,
                "date": when.isoformat(),
            }

    prefix = _issue_id(ISSUE_MODEL_RETIREMENT, "")
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if (
            domain == DOMAIN
            and issue_id.startswith(prefix)
            and issue_id[len(prefix):] not in wanted
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    for subentry_id, placeholders in wanted.items():
        raise_repair(
            hass,
            ISSUE_MODEL_RETIREMENT,
            subentry_id,
            translation_placeholders=placeholders,
            severity=ir.IssueSeverity.WARNING,
            learn_more_url=DEPRECATIONS_URL,
        )
    return upcoming


def async_track_model_retirement(hass: HomeAssistant) -> Callable[[], None]:
    """Sync the retirement warnings now and again when the next one starts.

    A warning is due on a calendar day, and Home Assistant may run for
    weeks without a restart, so a check at setup alone would miss it.
    The returned callable cancels the pending timer, for the entry's
    unload. Every entry keeps its own timer, which is harmless because
    each one rebuilds the same set of issues.
    """
    cancel: Callable[[], None] | None = None

    @callback
    def _sync(_now=None) -> None:
        nonlocal cancel
        upcoming = sync_model_retirement_issues(hass)
        if upcoming is not None:
            _LOGGER.debug("Next model retirement check on %s", upcoming)
        cancel = (
            async_track_point_in_time(
                hass, _sync, dt_util.start_of_local_day(upcoming)
            )
            if upcoming is not None
            else None
        )

    _sync()

    @callback
    def _cancel() -> None:
        if cancel is not None:
            cancel()

    return _cancel
