"""Find the Music Assistant entity of a speaker targeted by another one.

A speaker that Music Assistant plays to usually appears in Home
Assistant twice: once as a Music Assistant entity and once from its own
integration, such as ESPHome, Cast or Sonos. When an announcement pauses
the second entity, Music Assistant only hears about it through the
speaker, and on a grouped speaker it stops the whole group. Targeting
the Music Assistant entity avoids that, so the integration warns when
it can tell the two entities apart.

Home Assistant records no link between the two devices. Music
Assistant's device carries one identifier, its own player id, and that
id embeds the speaker's native id in some form:

* Sonos: the same ``RINCON_...`` id the Sonos integration uses.
* Cast: the same uuid, written with dashes.
* ESPHome over Sendspin: the MAC address without separators, behind a
  short prefix.

Comparing the ids with every separator removed covers all three. The
format of a Music Assistant player id is not a documented interface, so
a failed match only means that no warning is given.

This module imports nothing from Home Assistant, so it can be tested on
its own.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# The shortest native id that is compared. A MAC address without
# separators is twelve characters. Shorter values, such as an IP
# address or a small serial number, could occur inside an unrelated
# player id by chance.
MIN_ID_LENGTH = 12

_SEPARATORS = re.compile(r"[^0-9a-z]")


def normalise_id(value: object) -> str:
    """Return ``value`` in lower case with everything but letters and digits removed."""
    return _SEPARATORS.sub("", str(value).lower())


def native_ids(
    identifiers: Iterable[Iterable[object]],
    connections: Iterable[Iterable[object]],
) -> set[str]:
    """Collect the comparable ids of a device from its registry entry.

    ``identifiers`` and ``connections`` are the device registry's sets
    of tuples. The first element of each tuple names the domain or the
    connection type and is not part of the id. Some integrations store
    tuples with more than two elements, so every element after the
    first is taken.
    """
    found: set[str] = set()
    for pair in (*identifiers, *connections):
        for value in list(pair)[1:]:
            normalised = normalise_id(value)
            if len(normalised) >= MIN_ID_LENGTH:
                found.add(normalised)
    return found


def find_twin(
    native: Iterable[str],
    music_assistant: Mapping[str, Iterable[str]],
) -> str | None:
    """Return the Music Assistant entity whose player id contains a native id.

    ``native`` holds the normalised ids of the targeted device, as
    returned by ``native_ids``. ``music_assistant`` maps each Music
    Assistant ``media_player`` entity to the player ids of its device.
    Entities are tried in sorted order, so the result does not depend on
    the order the registry returned them in.
    """
    wanted = [value for value in native if len(value) >= MIN_ID_LENGTH]
    if not wanted:
        return None
    for entity_id in sorted(music_assistant):
        for player_id in music_assistant[entity_id]:
            normalised = normalise_id(player_id)
            if any(value in normalised for value in wanted):
                return entity_id
    return None
