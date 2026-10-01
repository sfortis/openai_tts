"""Deliver an announcement to its targets, one ``play_media`` per route.

This replaces ``tts.speak``. In Home Assistant that service is a single
``play_media`` call with ``announce`` set, the media-source id from
``generate_media_source_id`` and no ``extra``. Making the call here
keeps it identical for the managed targets and lets a native route add
the level its platform reads. See ``announce_routes`` for how the
targets are split.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    SERVICE_PLAY_MEDIA,
    MediaType,
)
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.components.tts import generate_media_source_id
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .announce_routes import (
    EXTRA_ANNOUNCE_VOLUME,
    MA_DEFAULT_ANNOUNCE_MAX,
    MA_DEFAULT_ANNOUNCE_MIN,
    AnnounceRoute,
    outside_default_clamp,
)

_LOGGER = logging.getLogger(__name__)


def announcement_media_id(
    hass: HomeAssistant,
    tts_entity: str,
    message: str,
    language: str | None,
    options: dict[str, Any],
) -> str:
    """Build the media-source id every route plays.

    ``cache=True`` is the ``tts.speak`` default, so the audio comes from
    the same entity stream and lands in the same duration cache as it
    did through that service.
    """
    return generate_media_source_id(
        hass,
        message=message,
        engine=tts_entity,
        language=language,
        options=options,
        cache=True,
    )


async def play_route(
    hass: HomeAssistant, route: AnnounceRoute, media_id: str
) -> None:
    """Send one route its ``play_media`` call, exactly once.

    Engine retries already happen inside ``async_stream_tts_audio``,
    where they are safe because no audio has reached a speaker yet.
    Retrying here could replay audio that is already playing on one of
    the targets, so a failure is surfaced once instead.

    The call is blocking. How long that takes depends on the platform:
    Music Assistant returns when the announcement has finished, Sonos
    and Cast return once the device has the clip.
    """
    level = route.extra.get(EXTRA_ANNOUNCE_VOLUME)
    if level is not None and outside_default_clamp(level):
        _LOGGER.warning(
            "Music Assistant limits announcement volume to %d-%d%% by "
            "default, so the requested %d%% may be changed on %s. The "
            "range is set per player in Music Assistant, under the "
            "announcement settings.",
            MA_DEFAULT_ANNOUNCE_MIN, MA_DEFAULT_ANNOUNCE_MAX, level,
            ", ".join(route.players),
        )
    service_data: dict[str, Any] = {
        ATTR_ENTITY_ID: list(route.players),
        ATTR_MEDIA_CONTENT_ID: media_id,
        ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
        ATTR_MEDIA_ANNOUNCE: True,
    }
    if route.extra:
        service_data[ATTR_MEDIA_EXTRA] = dict(route.extra)
    await hass.services.async_call(
        MP_DOMAIN, SERVICE_PLAY_MEDIA, service_data, blocking=True,
    )
