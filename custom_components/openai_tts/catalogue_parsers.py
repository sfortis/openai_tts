"""Turn provider catalogue responses into selector options.

The transport lives in ``voice_listing``. The parsing lives here, apart
from it, because this module imports nothing from Home Assistant and can
therefore be tested on its own. Every parser returns ``None`` rather
than raising when a payload holds nothing usable, because a backend we
have never seen must degrade to a typed name, not break the config flow.
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def voice_options_from_payload(
    payload: Any, source_url: str = ""
) -> list[dict[str, str]] | None:
    """Turn a voice-listing response into selector options.

    Returns options of the form ``[{"value": <id>, "label": <name>},
    ...]`` or ``None`` when the payload holds nothing usable. Never
    raises: a backend we have never seen must degrade to the free-text
    voice field, not break the config flow.

    Response shapes seen in the wild:

    * Mistral:        ``{"items": [{"id": uuid, "name": "..."}], "total": N}``
    * OpenAI-style:   ``{"data":  [{"id": str,  "name": "..."}]}``
    * Kokoro-FastAPI: ``{"voices": ["af_bella", "am_adam", ...]}``
    * bare list:      ``["af_bella", ...]`` or ``[{"id": ...}, ...]``

    The bare-list shapes matter because several OpenAI-compatible
    self-hosted servers answer ``GET /v1/audio/voices`` with a
    top-level JSON array. Calling ``.get()`` on that raised
    ``AttributeError`` out of the config flow before this helper
    existed.
    """
    if isinstance(payload, list):
        items: Any = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("items")
            or payload.get("data")
            or payload.get("voices")
            or []
        )
    else:
        _LOGGER.debug(
            "Voice listing at %s returned an unsupported top-level type: %s",
            source_url, type(payload).__name__,
        )
        return None

    if not isinstance(items, list):
        _LOGGER.debug(
            "Voice listing at %s held a non-list voice collection: %s",
            source_url, type(items).__name__,
        )
        return None

    options: list[dict[str, str]] = []
    for v in items:
        if isinstance(v, str):
            # Plain string voice name (Kokoro-FastAPI). value == label
            # is fine because the slug is what the user reads in the
            # UI ("af_bella") and what the request needs.
            if v:
                options.append({"value": v, "label": v})
            continue
        if not isinstance(v, dict):
            continue
        voice_id = v.get("id") or v.get("voice_id") or v.get("value")
        if not voice_id:
            continue
        label = v.get("name") or v.get("label") or voice_id
        options.append({"value": str(voice_id), "label": str(label)})
    return options or None


def openrouter_catalogue_from_payload(
    payload: Any,
) -> dict[str, list[str]] | None:
    """Map each OpenRouter speech model id to the voices it accepts.

    The payload is the answer to ``GET /api/v1/models`` filtered with
    ``output_modalities=speech``. Each model carries its voices in
    ``supported_voices``, which is ``null`` for models that take a voice
    description or a cloned reference instead of a name. Those models
    are kept with an empty list, so they stay selectable and fall back
    to a typed voice.

    A model that does not list ``speech`` among its output modalities is
    skipped. The query parameter already filters them out, but a proxy
    or a future change on their side should not fill the picker with
    chat models.
    """
    if not isinstance(payload, dict):
        return None
    models = payload.get("data")
    if not isinstance(models, list):
        return None

    catalogue: dict[str, list[str]] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        architecture = model.get("architecture")
        outputs = (
            architecture.get("output_modalities")
            if isinstance(architecture, dict)
            else None
        )
        if isinstance(outputs, list) and "speech" not in outputs:
            continue
        voices = model.get("supported_voices")
        catalogue[model_id] = (
            [v for v in voices if isinstance(v, str) and v]
            if isinstance(voices, list)
            else []
        )
    return catalogue or None


def voice_options_for_model(
    catalogue: dict[str, list[str]] | None, model: str | None
) -> list[dict[str, str]] | None:
    """Selector options for ``model`` out of a per model catalogue.

    Returns ``None`` when the model is unknown or lists no voices, which
    the callers read as "let the user type one".
    """
    if not catalogue or not model:
        return None
    voices = catalogue.get(model)
    if not voices:
        return None
    return [{"value": voice, "label": voice} for voice in voices]


def voice_picker_allows_typing(options: list[dict[str, str]]) -> bool:
    """True when the voice dropdown can also accept a typed value.

    A backend whose voices are plain names (Kokoro-FastAPI, OpenRouter)
    shows each value as its own label, so turning on the selector's
    ``custom_value`` costs nothing, and it lets a user type what the
    list cannot hold, such as a Kokoro voice mix like
    ``am_michael(1)+am_eric(2)``. When a label differs from its value
    (Mistral shows "Paul - Sad" for a UUID), ``custom_value`` would
    make the frontend show the raw UUID once a voice is picked, so it
    stays off.
    """
    return bool(options) and all(
        opt.get("label") == opt.get("value") for opt in options
    )
