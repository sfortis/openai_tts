"""Shutdown dates that OpenAI has announced for its speech models.

This module imports nothing from Home Assistant, so the rules can be
unit tested without a Home Assistant installation.

On 2026-10-01 OpenAI announced that ``tts-1``, ``tts-1-hd`` and every
snapshot of ``gpt-4o-mini-tts`` stop working on 2027-01-06. Its
recommended replacement, ``gpt-realtime-2.1-mini``, runs on the Realtime
API, which this integration does not use, and no replacement model was
announced for the speech endpoint.

A date applies only to OpenAI's own endpoint. Many self-hosted servers,
Kokoro-FastAPI among them, accept ``tts-1`` as an alias for their own
model, and OpenAI retiring the name changes nothing for them.
"""
from __future__ import annotations

from datetime import date

DEPRECATIONS_URL = "https://developers.openai.com/api/docs/deprecations"

_SHUTDOWN_DATES: dict[str, date] = {
    "tts-1": date(2027, 1, 6),
    "tts-1-hd": date(2027, 1, 6),
    "gpt-4o-mini-tts": date(2027, 1, 6),
}


def shutdown_date(model: str | None, on_openai: bool) -> date | None:
    """Return the date ``model`` stops working, or None when it does not.

    A snapshot carries the base name followed by a dash, such as
    ``gpt-4o-mini-tts-2025-03-20`` or ``tts-1-1106``, and shares the date
    of its base model.
    """
    if not on_openai or not model:
        return None
    name = model.strip().lower()
    for base, when in _SHUTDOWN_DATES.items():
        if name == base or name.startswith(f"{base}-"):
            return when
    return None
