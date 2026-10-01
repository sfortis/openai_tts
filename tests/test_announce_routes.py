"""Tests for how announcement targets are split into routes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openai_tts"))

from announce_routes import (
    EXTRA_ANNOUNCE_VOLUME,
    EXTRA_SONOS_VOLUME,
    AnnounceRoute,
    announce_volume_percent,
    outside_default_clamp,
    plan_routes,
)

PLATFORMS = {
    "media_player.kitchen_cast": "cast",
    "media_player.mancave": "music_assistant",
    "media_player.lyrat": "music_assistant",
    "media_player.move": "sonos",
    "media_player.unregistered": None,
}


@pytest.mark.parametrize(
    ("level", "percent"),
    [
        (0.35, 35),
        (0.3, 30),
        (0.555, 56),
        (1.0, 100),
        # Zero is refused by Music Assistant's own action.
        (0.0, 1),
        (0.004, 1),
        (1.5, 100),
    ],
)
def test_announce_volume_percent(level, percent):
    assert announce_volume_percent(level) == percent


@pytest.mark.parametrize(
    ("percent", "outside"),
    [(14, True), (15, False), (50, False), (75, False), (76, True), (90, True)],
)
def test_outside_default_clamp(percent, outside):
    assert outside_default_clamp(percent) is outside


def _plan(players, level):
    return plan_routes(players, PLATFORMS.get, level)


def test_without_override_every_target_shares_the_managed_route():
    routes = _plan(list(PLATFORMS), None)
    assert routes == [AnnounceRoute(tuple(PLATFORMS))]
    assert not routes[0].native


def test_no_targets_gives_no_routes():
    assert _plan([], None) == []
    assert _plan([], 0.4) == []


def test_override_splits_by_platform_with_the_managed_route_first():
    routes = _plan(list(PLATFORMS), 0.35)
    assert routes == [
        AnnounceRoute(("media_player.kitchen_cast", "media_player.unregistered")),
        AnnounceRoute(
            ("media_player.mancave", "media_player.lyrat"),
            extra={EXTRA_ANNOUNCE_VOLUME: 35},
        ),
        AnnounceRoute(
            ("media_player.move",),
            extra={EXTRA_SONOS_VOLUME: 35},
            returns_before_playback=True,
        ),
    ]
    assert [route.native for route in routes] == [False, True, True]


def test_override_on_native_targets_only_has_no_managed_route():
    routes = _plan(["media_player.move", "media_player.mancave"], 0.5)
    assert all(route.native for route in routes)
    assert {p for route in routes for p in route.players} == {
        "media_player.move", "media_player.mancave",
    }


def test_managed_route_never_carries_extra():
    # Cast spreads every extra key into the receiver's app data, so a
    # key it does not know must never reach it.
    (route,) = _plan(["media_player.kitchen_cast"], 0.6)
    assert route.extra == {}


def test_zero_override_still_sends_a_level():
    routes = _plan(["media_player.move"], 0.0)
    assert routes[0].extra == {EXTRA_SONOS_VOLUME: 1}
