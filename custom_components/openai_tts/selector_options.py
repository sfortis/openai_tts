"""Helpers for building option lists that Home Assistant selectors accept.

This module deliberately imports nothing from Home Assistant. The rules
it encodes are pure data handling, and keeping them here means they can
be unit tested without a Home Assistant installation, which is what
would have caught the defect described below.

A ``select`` selector takes either a list of plain strings or a list of
``{"value": ..., "label": ...}`` mappings, and the two cannot be mixed.
The chime picker builds mappings, so a saved value appended to it as a
bare string produced a list the selector refused, and the profile form
stopped opening at all. The comparison that guarded the append was
itself wrong in the same way: a string is never equal to a mapping, so
the append always happened.
"""
from __future__ import annotations

from typing import Any


def option_values(options: list[Any]) -> list[str]:
    """Return the submitted value of each option, whatever its shape."""
    return [
        opt["value"] if isinstance(opt, dict) else opt
        for opt in options
    ]


def ensure_selectable(
    options: list[Any],
    value: str | None,
    *,
    label_suffix: str = "saved",
) -> list[Any]:
    """Return ``options`` with ``value`` guaranteed to be selectable.

    A form whose default is not among its own options opens on something
    the option list does not contain, and submitting it rewrites the
    stored value to whichever option the frontend falls back to. That is
    how a saved voice, model, audio format or chime gets replaced by a
    value the user never chose.

    The appended entry copies the shape of the existing options, so a
    list of mappings stays a list of mappings and a list of strings stays
    a list of strings. ``label_suffix`` marks the added entry in the
    picker, for example ``gone.mp3 (saved)``.
    """
    if not value:
        return list(options)

    result = list(options)
    if value in option_values(result):
        return result

    uses_mappings = any(isinstance(opt, dict) for opt in result) or not result
    if uses_mappings:
        result.append({"value": value, "label": f"{value} ({label_suffix})"})
    else:
        result.append(value)
    return result
