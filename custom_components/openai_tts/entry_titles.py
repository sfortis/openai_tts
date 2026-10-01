"""Build and read back the title of a parent config entry.

This module imports nothing from Home Assistant, so the rules can be
unit tested without a Home Assistant installation.

A title starts with the preset's short ``title_name``, not its
``label``. The label is written for the provider dropdown, and as a
title it produced names such as "Custom / Self-hosted (any
OpenAI-compatible endpoint) (192.168.1.5)". An account name the user
types follows after " - ". When the typed name only repeats the
provider, it is dropped, so that "OpenRouter" typed on the OpenRouter
preset gives "OpenRouter" and not "OpenRouter - OpenRouter".

Presets marked ``self_hosted`` say so in the title. Presets marked
``title_shows_host`` add the hostname when there is no account name,
because one user can run several servers of the same kind. Cloud
presets have a fixed host, so the hostname would add nothing.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SELF_HOSTED = "self-hosted"


def _repeats_provider(account_name: str, preset: dict[str, Any]) -> bool:
    """Return True when the account name only names the provider again."""
    typed = account_name.casefold()
    return typed in {
        preset["title_name"].casefold(),
        preset["label"].casefold(),
    }


def entry_title(
    preset: dict[str, Any],
    account_name: str | None,
    hostname: str | None,
) -> str:
    """Return the entry title for a preset, account name and hostname."""
    name = (account_name or "").strip()
    if name and _repeats_provider(name, preset):
        name = ""

    details: list[str] = []
    if preset.get("self_hosted"):
        details.append(SELF_HOSTED)
    if not name and preset.get("title_shows_host") and hostname:
        details.append(hostname)

    title = preset["title_name"]
    if details:
        title = f"{title} ({', '.join(details)})"
    if name:
        title = f"{title} - {name}"
    return title


def account_name_from_title(
    title: str, presets: Iterable[dict[str, Any]]
) -> str:
    """Return the account name part of a title, or "" when it has none.

    Titles written before the short names existed start with the
    preset label, so both forms are recognised. The longest prefix is
    tried first, so that a label which starts with a short name, such
    as "OpenRouter" and a hypothetical "OpenRouter Plus", cannot cut a
    title in the wrong place.
    """
    prefixes: set[str] = set()
    for preset in presets:
        prefixes.add(f"{preset['label']} - ")
        head = entry_title(preset, None, None)
        prefixes.add(f"{head} - ")
    for prefix in sorted(prefixes, key=len, reverse=True):
        if title.startswith(prefix):
            return title[len(prefix):]
    return ""
