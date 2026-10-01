"""How an announcement reaches each target, and the level it carries.

Home Assistant's ``tts.speak`` is a single ``play_media`` call with
``announce`` set and no ``extra``, so a volume override never reaches
the device. Two platforms read a level from ``extra`` when ``announce``
is set. Music Assistant reads ``announce_volume`` and Sonos reads
``volume``, both as a percentage. Cast spreads every ``extra`` key into
the receiver's app data, so a key it does not know can break the call.

The targets are therefore split into routes. Each route is one
``play_media`` call, and a route carries only the key its platform
reads. Targets on a route that carries a level announce on their own:
they duck, play at that level and restore themselves. Every other
target is left to the manual pause, volume and restore flow.

This module imports nothing from Home Assistant, so it can be tested on
its own.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

PLATFORM_MUSIC_ASSISTANT = "music_assistant"
PLATFORM_SONOS = "sonos"

# The key Home Assistant's Music Assistant integration reads from
# ``extra`` when ``announce`` is set.
EXTRA_ANNOUNCE_VOLUME = "announce_volume"

# The key Home Assistant's Sonos integration reads from ``extra`` when
# ``announce`` is set. It is passed to the speaker's audio clip command.
EXTRA_SONOS_VOLUME = "volume"

# Music Assistant clamps every announcement volume, an explicit one
# included, to a per player range. These are its defaults, from
# ``CONF_ENTRY_ANNOUNCE_VOLUME_MIN`` and ``CONF_ENTRY_ANNOUNCE_VOLUME_MAX``
# in the server's ``constants.py``. A player can be configured with a
# different range, which Home Assistant cannot see.
MA_DEFAULT_ANNOUNCE_MIN = 15
MA_DEFAULT_ANNOUNCE_MAX = 75


@dataclass(frozen=True)
class AnnounceRoute:
    """One ``play_media`` call and the targets it reaches.

    ``extra`` is empty on the managed route, the one the manual flow
    looks after. A route with a level in ``extra`` is native: its
    targets duck and restore themselves.

    ``returns_before_playback`` is set when the platform's ``play_media``
    hands the clip to the device and returns while it is still playing.
    Sonos does this. Music Assistant's call returns once the
    announcement has finished.
    """

    players: tuple[str, ...]
    extra: dict[str, Any] = field(default_factory=dict)
    returns_before_playback: bool = False

    @property
    def native(self) -> bool:
        """True when the route carries the level to the device itself."""
        return bool(self.extra)


def announce_volume_percent(level: float) -> int:
    """Convert a 0..1 level to the 1..100 percentage both platforms take.

    Zero is raised to one. Music Assistant's own action refuses zero,
    Sonos treats it as "no level given", and a silent announcement is
    better expressed by not sending one.
    """
    percent = round(float(level) * 100)
    return max(1, min(100, percent))


def outside_default_clamp(percent: int) -> bool:
    """True when Music Assistant's default range would change ``percent``."""
    return not MA_DEFAULT_ANNOUNCE_MIN <= percent <= MA_DEFAULT_ANNOUNCE_MAX


def plan_routes(
    players: Iterable[str],
    platform_of: Callable[[str], str | None],
    level: float | None,
) -> list[AnnounceRoute]:
    """Split ``players`` into the routes that reach them.

    Without a volume override every target shares one managed route,
    which is exactly what ``tts.speak`` would have sent. With an
    override, Music Assistant and Sonos targets get a native route
    each, and everything else stays on the managed route. The managed
    route comes first when there is one, and no route is empty.
    """
    players = list(players)
    if level is None:
        return [AnnounceRoute(tuple(players))] if players else []

    managed: list[str] = []
    music_assistant: list[str] = []
    sonos: list[str] = []
    for entity_id in players:
        platform = platform_of(entity_id)
        if platform == PLATFORM_MUSIC_ASSISTANT:
            music_assistant.append(entity_id)
        elif platform == PLATFORM_SONOS:
            sonos.append(entity_id)
        else:
            managed.append(entity_id)

    percent = announce_volume_percent(level)
    routes: list[AnnounceRoute] = []
    if managed:
        routes.append(AnnounceRoute(tuple(managed)))
    if music_assistant:
        routes.append(AnnounceRoute(
            tuple(music_assistant),
            extra={EXTRA_ANNOUNCE_VOLUME: percent},
        ))
    if sonos:
        routes.append(AnnounceRoute(
            tuple(sonos),
            extra={EXTRA_SONOS_VOLUME: percent},
            returns_before_playback=True,
        ))
    return routes
