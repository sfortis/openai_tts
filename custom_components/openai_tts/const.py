"""
Constants for OpenAI TTS custom component
"""
from typing import Any

# Two of these live in Home Assistant's own const module with exactly
# the same values, so they are re-exported rather than redefined. Every
# reader keeps importing them from here.
#
# ``CONF_MODEL`` stays local on purpose. It is a much later addition to
# core than the other two, and the value is what gets persisted in
# config entry data, so an import that is missing on the oldest
# supported Home Assistant would be a hard failure at load time for a
# purely cosmetic gain.
from homeassistant.const import CONF_API_KEY as CONF_API_KEY
from homeassistant.const import CONF_URL as CONF_URL

DOMAIN = "openai_tts"
CONF_MODEL = "model"
CONF_VOICE = "voice"
CONF_SPEED = "speed"

# Whether the ``voice`` key is sent at all.
#
# Every OpenAI-compatible backend is expected to take a voice, but some
# do not: audio.cpp serving Chatterbox or VoxCPM2 rejects the request
# when the key is present, and rejects a null value too, so there was no
# way to suppress it. ``extra_payload`` cannot help, since it merges
# keys and cannot remove one.
#
# Per profile rather than per entry, because the same server can host
# one model that wants a voice and another that refuses it, and profiles
# are already per model here. Reported by @Arjenlodder in #71.
CONF_SEND_VOICE = "send_voice"

# Whether to read the response as it arrives. Off sends the request down
# the atomic path: one blocking fetch, validated, then handed over whole.
# It exists because some self-hosted backends answer a streamed read with
# audio that will not decode, while the same request read in one go is
# fine, and until now there was no way for anyone to say so: the preset
# flag this used to depend on is True in every preset and no user could
# reach it.
CONF_STREAM_AUDIO = "stream_audio"
DEFAULT_STREAM_AUDIO = True
DEFAULT_SEND_VOICE = True
CONF_PROVIDER = "provider"  # provider preset key, e.g. "openai", "mistral", "custom"
DEFAULT_URL = "https://api.openai.com/v1/audio/speech"

# Provider presets shown in the parent-entry config flow as a single
# dropdown so a user picks "OpenAI" / "Mistral" / "Custom" instead of
# typing the right URL, voice-field name, and audio_format defaults
# from memory. Each entry is a recipe read by the config flow: the
# endpoint URL, the voice and model catalogues, the audio formats the
# backend accepts, and which fields to render. The engine reads none of
# it; request shaping there is provider-agnostic. Add new providers by
# extending this dict; nothing else in the codebase should need a
# branch on the provider key.
PROVIDER_OPENAI = "openai"
PROVIDER_MISTRAL = "mistral"
PROVIDER_GROQ = "groq"
PROVIDER_LEMONFOX = "lemonfox"
PROVIDER_KOKORO = "kokoro"
PROVIDER_CHATTERBOX = "chatterbox"
PROVIDER_CUSTOM = "custom"
DEFAULT_PROVIDER = PROVIDER_OPENAI

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    PROVIDER_OPENAI: {
        "label": "OpenAI",
        "url": DEFAULT_URL,
        "default_model": "gpt-4o-mini-tts",
        "default_format": "mp3",
        # voice catalog stays in VOICES below; OpenAI is the source of truth
        "voice_catalog": None,
        # Model catalog ``None`` means "fall back to ``MODELS``" (the
        # OpenAI list). Other presets set their own catalogue so a
        # Mistral subentry doesn't show ``tts-1`` in the dropdown.
        "model_catalog": None,
        "requires_api_key": True,
        # OpenAI supports every container we know about and chunked
        # streaming on all of them.
        "allowed_formats": None,
        "max_text_length": 4096,
        "supports_streaming": True,
        # OpenAI documents ``speed`` (0.25-4.0) on the speech endpoint.
        # ``extra_payload`` is suppressed because the request schema
        # is fixed - exposing the field would only invite users to
        # type things the API would reject anyway.
        "supports_speed": True,
        "supports_extra_payload": False,
        # The only provider that cannot be asked for its voices. Three
        # plausible paths were tried against the real API on 2026-08-22,
        # ``/v1/audio/voices``, ``/v1/voices`` and
        # ``/v1/audio/speech/voices``, and all three answered 404. The
        # catalogue is documentation only, which is why the static
        # tables above exist. Every other provider defaults to being
        # asked, because a fetch that fails degrades to a typed name.
        "supports_voice_listing": False,
    },
    PROVIDER_MISTRAL: {
        "label": "Mistral Voxtral",
        "url": "https://api.mistral.ai/v1/audio/speech",
        "default_model": "voxtral-mini-tts-latest",
        "model_catalog": ["voxtral-mini-tts-latest"],
        "default_format": "mp3",
        # Mistral has no built-in voice catalogue: every voice is
        # user-cloned via ``POST /v1/audio/voices`` from a sample.
        # Instead of shipping stale slugs we fetch the live list at
        # config-flow time (see ``supports_voice_listing`` below) and
        # fall back to free-text when the call fails or the account
        # has no cloned voices yet.
        "voice_catalog": None,
        "requires_api_key": True,
        # Voxtral accepts the same MP3/Opus/WAV/PCM/FLAC family OpenAI
        # does. Length cap left open: Mistral's documented max bounces
        # between releases and isn't strictly enforced, so we'd rather
        # surface their real error than reject a request locally.
        "allowed_formats": None,
        "max_text_length": None,
        "supports_streaming": True,
        # ``GET /v1/audio/voices`` returns ``{items, total}`` with
        # ``id`` (voice_id) + ``name`` per voice. Used by the config
        # flow to populate the voice dropdown dynamically.
        "supports_voice_listing": True,
        # Verified empirically (HTTP 422 ``extra_forbidden body.speed``
        # for any value other than 1.0): Voxtral hard-rejects the
        # ``speed`` field. Hide the slider so the user doesn't pick a
        # value that will fail at runtime. Extra payload is also off
        # because the request schema is fixed.
        "supports_speed": False,
        "supports_extra_payload": False,
    },
    PROVIDER_GROQ: {
        # Groq's hosted Orpheus v1 TTS endpoint, served via their
        # OpenAI-compatible audio router. Orpheus only emits WAV today
        # (response_format=wav), so the default audio_format is set
        # accordingly. The User-Agent header the engine sends
        # ("HomeAssistant-OpenAI-TTS") satisfies the 403/code 1010
        # anti-bot screen reported in issue #40. Users who want the
        # Arabic Saudi variant can reconfigure the model to
        # ``canopylabs/orpheus-arabic-saudi`` and pick an Arabic voice
        # (abdullah, fahad, sultan, lulwa, noura, aisha) via the
        # custom-voice free-text field.
        "label": "Groq (Orpheus TTS)",
        "url": "https://api.groq.com/openai/v1/audio/speech",
        "default_model": "canopylabs/orpheus-v1-english",
        "model_catalog": [
            "canopylabs/orpheus-v1-english",
            "canopylabs/orpheus-arabic-saudi",
        ],
        "default_format": "wav",
        # Orpheus v1 English voices. Users can still type a different
        # voice manually via the custom-voice free-text field (e.g. the
        # Arabic Saudi voices when paired with the Arabic model).
        "voice_catalog": [
            "autumn",
            "diana",
            "hannah",
            "austin",
            "daniel",
            "troy",
        ],
        "requires_api_key": True,
        # Orpheus only emits WAV. Asking for anything else is refused by
        # the Groq router with a plain message rather than an opaque
        # error: probed again on 2026-09-01, mp3, opus and flac each came
        # back 400 "response_format must be one of [wav]" while wav
        # returned a RIFF body, so the dropdown stays locked to wav.
        #
        # That also means Groq announcements never stream. WAV states its
        # length in a header written before any audio exists, so it takes
        # the atomic path no matter what ``supports_streaming`` says
        # below: that flag is about the provider speaking chunked HTTP,
        # not about the container being streamable.
        #
        # Length cap stays open; the Groq router does enforce a
        # per-request limit but it varies by model and we'd rather pass
        # through their error verbatim.
        "allowed_formats": ["wav"],
        "max_text_length": None,
        "supports_streaming": True,
        # Verified empirically: Orpheus accepts ``speed`` in the
        # request body but ignores it (3 calls at 0.5 / 1.0 / 2.0
        # returned identical byte counts). Hide the slider so users
        # don't think they're tuning something that's actually
        # silently dropped.
        "supports_speed": False,
        "supports_extra_payload": False,
    },
    PROVIDER_LEMONFOX: {
        # Lemonfox.ai hosts the open-source Kokoro-82M voice model
        # behind an OpenAI-compatible ``/v1/audio/speech`` endpoint -
        # cheaper alternative to OpenAI ($0.30/1M chars at the time
        # of writing) with broad multilingual coverage. The default
        # catalogue below lists the English voices Lemonfox documents
        # by short name. Users who want the full multilingual
        # Kokoro range (af_/am_/bf_/bm_/ef_/em_/ff_/jf_/jm_/zf_/zm_
        # prefixed slugs covering ES/FR/IT/PT/JA/ZH/HI etc.) can type
        # the full id via the custom-value path on the voice picker.
        "label": "Lemonfox.ai (Kokoro TTS)",
        "url": "https://api.lemonfox.ai/v1/audio/speech",
        # Lemonfox runs a single underlying model; ``tts-1`` is the
        # OpenAI-compatible alias they accept and the value is
        # otherwise ignored. Custom_value stays on so users on a
        # newer Lemonfox model alias don't need an integration update.
        "default_model": "tts-1",
        "model_catalog": ["tts-1"],
        "default_format": "mp3",
        # English short-name voices documented by Lemonfox. The
        # service also accepts the full Kokoro slugs (af_sarah,
        # bm_george, jf_alpha, ...) for non-English languages, but
        # short names work fine for the default English use case.
        "voice_catalog": [
            "heart", "bella", "sarah", "jessica", "river", "sky",
            "nicole", "aoede", "kore", "alloy",
            "adam", "echo", "michael", "eric", "liam", "onyx", "puck",
            "alice", "emma", "isabella", "lily",
            "daniel", "fable", "george", "lewis",
        ],
        "requires_api_key": True,
        # Lemonfox supports the same MP3/Opus/AAC/FLAC/WAV/PCM family
        # OpenAI does. Length cap left open: docs quote a per-request
        # limit but it varies; surface their real error if exceeded.
        "allowed_formats": None,
        "max_text_length": None,
        "supports_streaming": True,
        # Kokoro accepts ``speed`` (0.25-4.0) on the OpenAI-compatible
        # wrapper. Extra payload off because the schema is fixed.
        "supports_speed": True,
        "supports_extra_payload": False,
    },
    PROVIDER_KOKORO: {
        # Self-hosted shortcut for ``remsky/Kokoro-FastAPI``, the most
        # common docker deployment of the Kokoro-82M voice model.
        # Pre-fills the default ``localhost:8880`` URL so users with
        # the stock docker-compose setup don't have to type the
        # endpoint by hand. Issue #41 had multiple users (putt6359,
        # K-RAD) explicitly asking for Kokoro-FastAPI integration;
        # this preset is the zero-config path that closes that loop.
        "label": "Kokoro-FastAPI (self-hosted Kokoro)",
        "url": "http://localhost:8880/v1/audio/speech",
        # Kokoro-FastAPI maps ``tts-1`` / ``tts-1-hd`` / ``kokoro`` to
        # the same underlying model in ``openai_mappings.json``. We
        # default to ``kokoro`` as the canonical native name so the
        # config matches the upstream Quick Start examples verbatim.
        "default_model": "kokoro",
        "model_catalog": ["kokoro", "tts-1", "tts-1-hd"],
        "default_format": "mp3",
        # No static catalog: Kokoro-FastAPI exposes the live list at
        # ``GET /v1/audio/voices`` so we fetch the currently-installed
        # voicepacks (English ``af_/am_/bf_/bm_`` plus any locale
        # voicepacks the user has downloaded - JA / ZH / ES / FR /
        # IT / PT / HI). That way the dropdown matches reality and
        # the user never has to type a slug like ``af_bella``
        # themselves.
        "voice_catalog": None,
        # Default Kokoro-FastAPI docker compose runs without auth, so
        # the API key field stays optional. Users behind a reverse
        # proxy that adds Bearer auth can still fill it in.
        "requires_api_key": False,
        "allowed_formats": None,
        "max_text_length": None,
        # Kokoro-FastAPI advertises chunked streaming and accepts
        # ``speed`` on the OpenAI-compatible wrapper. Extra payload
        # off because the request schema is fixed.
        "supports_streaming": True,
        "supports_speed": True,
        "supports_extra_payload": False,
        # ``GET /v1/audio/voices`` returns ``{"voices": [...]}`` with
        # plain string voice names. Voice listing handler treats this
        # as authoritative and renders one option per name.
        "supports_voice_listing": True,
    },
    PROVIDER_CHATTERBOX: {
        # Chatterbox-TTS-Server (devnen). It serves an OpenAI compatible
        # endpoint next to its own ``/tts``, and that is the one to use:
        # ``/tts`` ignores ``output_format`` whenever ``stream`` is set and
        # answers in WAV, so on that endpoint progressive audio and a
        # compressed container are mutually exclusive. Sentence streaming
        # gets both, because each sentence is an ordinary request here.
        #
        # Measured against the server on 2026-09-03:
        #  - ``model`` is accepted and then ignored; every value answered
        #    200, including an empty string. The model is whatever the
        #    server's own config.yaml selected, so the catalogue below is
        #    a label rather than a choice.
        #  - ``response_format`` accepts mp3, opus and wav. flac, aac and
        #    pcm come back 422.
        #  - ``speed`` is honoured: 0.5 gave 7.0 s where 2.0 gave 1.8 s on
        #    the same sentence.
        #  - voices are published at ``/v1/audio/voices`` as file names
        #    such as ``Abigail.wav``.
        "label": "Chatterbox (self-hosted)",
        "url": "http://localhost:8004/v1/audio/speech",
        "default_model": "chatterbox",
        "model_catalog": ["chatterbox"],
        "default_format": "mp3",
        "voice_catalog": None,
        "requires_api_key": False,
        "allowed_formats": ["mp3", "opus", "wav"],
        "max_text_length": None,
        "supports_streaming": True,
        "supports_speed": True,
        "supports_extra_payload": True,
        "supports_voice_listing": True,
    },

    PROVIDER_CUSTOM: {
        # Catch-all preset for any OpenAI-compatible TTS endpoint we
        # don't have a dedicated preset for. Covers self-hosted servers
        # (speaches.ai, Alltalk V2, LocalAI, OpenedAI-Speech, pocket-tts,
        # Microsoft VibeVoice, vLLM-served Qwen3-TTS, ...) and any
        # third-party hosted proxy that exposes the OpenAI speech
        # contract. URL is empty so the user fills in their endpoint;
        # API key is optional because many self-hosted setups don't
        # gate the endpoint, while hosted proxies usually do.
        "label": "Custom / Self-hosted (any OpenAI-compatible endpoint)",
        "url": "",  # user enters
        "default_model": None,
        "model_catalog": None,
        "default_format": "mp3",
        "voice_catalog": None,
        "requires_api_key": False,
        # Backend capabilities are deployment-specific, so leave
        # every dial open and let the operator constrain via
        # ``extra_payload`` or by picking a different audio_format
        # if their server is picky.
        "allowed_formats": None,
        "max_text_length": None,
        "supports_streaming": True,
        "supports_speed": True,
        "supports_extra_payload": True,
    },
}


def preset_for(provider_key: str | None) -> dict[str, Any]:
    """Return the preset for ``provider_key`` (falls back to OpenAI).

    Tolerates both ``None`` and unknown keys so callers don't have to
    pre-validate.
    """
    if not provider_key:
        return PROVIDER_PRESETS[PROVIDER_OPENAI]
    return PROVIDER_PRESETS.get(provider_key, PROVIDER_PRESETS[PROVIDER_OPENAI])


def audio_format_options_for(preset: dict[str, Any]) -> list[dict[str, str]]:
    """Filter ``AUDIO_FORMAT_LABELS`` down to the preset's allowlist.

    ``allowed_formats=None`` means every format goes through (OpenAI,
    self-hosted). Used by the config flow so users can't pick e.g. mp3
    on Groq Orpheus, which only emits WAV.
    """
    allowed = preset.get("allowed_formats")
    if not allowed:
        return AUDIO_FORMAT_LABELS
    allowed_set = set(allowed)
    return [opt for opt in AUDIO_FORMAT_LABELS if opt["value"] in allowed_set]
UNIQUE_ID = "unique_id"

MODELS = ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"]
# All 13 OpenAI built-in voices. ``ballad``, ``verse``, ``marin`` and
# ``cedar`` are exclusive to ``gpt-4o-mini-tts``; the legacy ``tts-1`` /
# ``tts-1-hd`` models reject them. ``marin`` and ``cedar`` are OpenAI's
# recommended highest-quality voices.
VOICES = [
    "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable",
    "marin", "nova", "onyx", "sage", "shimmer", "verse",
]

# Per-model voice support. Used by config_flow to render only the
# voices that the chosen model can actually render, and by the service
# call layer to reject incompatible (model, voice) combinations early
# rather than letting OpenAI return an unhelpful 400.
_LEGACY_TTS_VOICES = [
    "alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer",
]
VOICES_BY_MODEL: dict[str, list[str]] = {
    "tts-1": _LEGACY_TTS_VOICES,
    "tts-1-hd": _LEGACY_TTS_VOICES,
    "gpt-4o-mini-tts": VOICES,  # 13 voices, supports all
}

# Models that accept the OpenAI ``instructions`` parameter on the
# speech endpoint. Today only ``gpt-4o-mini-tts``; ``tts-1`` /
# ``tts-1-hd`` ignore the field, and other providers (Mistral
# Voxtral, Groq Orpheus, ...) don't speak this dialect at all -
# Orpheus has its own bracketed vocal-direction syntax in the
# ``input`` text instead. The config flow uses this set to show
# the instructions input only on supported models so users don't
# fill in a field that the backend will silently drop.
INSTRUCTIONS_MODELS: frozenset[str] = frozenset({"gpt-4o-mini-tts"})


def model_supports_instructions(model: str | None) -> bool:
    """True when ``model`` accepts the ``instructions`` request field.

    Strict membership test, used by the config flow to decide whether
    to render the instructions input. Request building uses
    ``model_may_accept_instructions`` instead, which is deliberately
    more permissive about custom backends.
    """
    return bool(model) and model in INSTRUCTIONS_MODELS


def model_may_accept_instructions(model: str | None) -> bool:
    """True unless ``model`` is a known OpenAI model that rejects instructions.

    Used when building the request body, where the question is not "may
    the user edit this field" but "will sending it break the call".

    A profile can hold an ``instructions`` value that its current model
    does not accept, because the value is kept when the user switches
    models rather than silently discarded. Sending it to ``tts-1`` or
    ``tts-1-hd`` makes OpenAI reject the request, so it is dropped for
    those.

    Unknown model names are treated as capable. A custom backend served
    under an arbitrary slug may well accept instructions, and guessing
    otherwise would make the field unusable for exactly the deployments
    that need it most.
    """
    if not model:
        return True
    if model in INSTRUCTIONS_MODELS:
        return True
    return model not in MODELS


def voices_for_model(model: str | None) -> list[str]:
    """Return the supported voices for ``model``.

    Falls back to the full ``VOICES`` list for unknown / custom backend
    models so we don't accidentally restrict choice for users targeting
    Chatterbox / TTS Web UI / etc.
    """
    if not model:
        return VOICES
    return VOICES_BY_MODEL.get(model, VOICES)


# Human-readable suffixes shown in voice pickers so users can spot at
# a glance which voices need ``gpt-4o-mini-tts`` and which two are
# OpenAI's "best quality" recommendation.
_VOICE_DESCRIPTIONS: dict[str, str] = {
    "alloy": "Neutral",
    "ash": "Calm",
    "ballad": "Warm · gpt-4o-mini-tts only",
    "cedar": "Recommended · gpt-4o-mini-tts only",
    "coral": "Friendly",
    "echo": "Smooth",
    "fable": "Expressive",
    "marin": "Recommended · gpt-4o-mini-tts only",
    "nova": "Energetic",
    "onyx": "Authoritative",
    "sage": "Thoughtful",
    "shimmer": "Gentle",
    "verse": "Versatile · gpt-4o-mini-tts only",
}


def voice_options(voices: list[str]) -> list[dict[str, str]]:
    """Return ``{label, value}`` options for ``voices`` (preserves order).

    Used by config_flow to render the voice picker with the same
    descriptive labels as the services.yaml dropdown.
    """
    return [
        {
            "value": v,
            "label": (
                f"{v.capitalize()} ({_VOICE_DESCRIPTIONS[v]})"
                if v in _VOICE_DESCRIPTIONS
                else v.capitalize()
            ),
        }
        for v in voices
    ]


def is_openai_endpoint(url: str | None) -> bool:
    """True when ``url`` points at OpenAI's official TTS endpoint.

    Used to decide whether the voice picker should be a fixed dropdown
    (OpenAI - finite voice catalogue) or a free-text input (custom
    backends with arbitrary voice IDs).
    """
    if not url:
        return True  # default endpoint is OpenAI
    return "api.openai.com" in url.lower()

# Supported languages (OpenAI TTS auto-detects from text, this list is for HA UI)
# Based on OpenAI Whisper model language support
SUPPORTED_LANGUAGES = [
    "af",  # Afrikaans
    "ar",  # Arabic
    "bg",  # Bulgarian
    "bn",  # Bengali
    "bs",  # Bosnian
    "ca",  # Catalan
    "cs",  # Czech
    "cy",  # Welsh
    "da",  # Danish
    "de",  # German
    "el",  # Greek
    "en",  # English
    "es",  # Spanish
    "et",  # Estonian
    "fa",  # Persian
    "fi",  # Finnish
    "fr",  # French
    "gl",  # Galician
    "he",  # Hebrew
    "hi",  # Hindi
    "hr",  # Croatian
    "hu",  # Hungarian
    "id",  # Indonesian
    "is",  # Icelandic
    "it",  # Italian
    "ja",  # Japanese
    "kk",  # Kazakh
    "ko",  # Korean
    "lt",  # Lithuanian
    "lv",  # Latvian
    "mk",  # Macedonian
    "ml",  # Malayalam
    "mr",  # Marathi
    "ms",  # Malay
    "nb",  # Norwegian Bokmål
    "nl",  # Dutch
    "pl",  # Polish
    "pt",  # Portuguese
    "ro",  # Romanian
    "ru",  # Russian
    "sk",  # Slovak
    "sl",  # Slovenian
    "sr",  # Serbian
    "sv",  # Swedish
    "sw",  # Swahili
    "ta",  # Tamil
    "te",  # Telugu
    "th",  # Thai
    "tl",  # Tagalog
    "tr",  # Turkish
    "uk",  # Ukrainian
    "ur",  # Urdu
    "vi",  # Vietnamese
    "zh",  # Chinese
]

CONF_CHIME_ENABLE = "chime"
CONF_CHIME_SOUND = "chime_sound"
CONF_NORMALIZE_AUDIO = "normalize_audio"
CONF_INSTRUCTIONS = "instructions"
CONF_EXTRA_PAYLOAD = "extra_payload"  # JSON string for custom TTS backend parameters
CONF_AUDIO_FORMAT = "audio_format"   # mp3 (default) / wav / opus, for custom backends

AUDIO_FORMAT_LABELS: list[dict[str, str]] = [
    {"value": "mp3", "label": "MP3 (default, broad compatibility)"},
    {"value": "opus", "label": "Opus (low-latency streaming)"},
    {"value": "aac", "label": "AAC (mobile / iOS / Android)"},
    {"value": "flac", "label": "FLAC (lossless)"},
    {"value": "wav", "label": "WAV (uncompressed, low decode overhead)"},
    {"value": "pcm", "label": "PCM (raw 24kHz 16-bit, no header)"},
]
DEFAULT_AUDIO_FORMAT = "mp3"

# Maps each user-selectable audio format to the ffmpeg codec / muxer flags
# used when we emit that format. ``container_args`` is appended after the
# filter graph; ``codec_args`` carries the encoder-specific switches.
# PCM is special-cased: it has no container, so we force the s16le muxer
# at 24kHz mono to match OpenAI's documented raw output layout.
AUDIO_FORMAT_ENCODER: dict[str, dict[str, list[str]]] = {
    "mp3":  {"codec_args": ["-c:a", "libmp3lame", "-b:a", "128k"], "container_args": []},
    "opus": {"codec_args": ["-c:a", "libopus", "-b:a", "96k"],     "container_args": []},
    "aac":  {"codec_args": ["-c:a", "aac", "-b:a", "128k"],        "container_args": []},
    "flac": {"codec_args": ["-c:a", "flac"],                       "container_args": []},
    "wav":  {"codec_args": ["-c:a", "pcm_s16le"],                  "container_args": []},
    "pcm":  {"codec_args": ["-c:a", "pcm_s16le"],
             "container_args": ["-f", "s16le", "-ar", "24000", "-ac", "1"]},
}

# Toggle to snapshot & restore volumes
CONF_VOLUME_RESTORE = "volume_restore"

# Legacy "always pause the current media before speaking" toggle.
# Predates announcement mode and keeps its original meaning: when the
# user has switched it on, playing targets are paused before the speak
# regardless of what announcement mode decides. Default off.
#
# Kept as its own key on purpose. Reusing it to carry announcement
# mode would have silently redefined a setting users had already saved,
# turning "don't pause my music" into "manage my music".
CONF_PAUSE_PLAYBACK = "pause_playback"


def migrating_flag(entry_id: str) -> str:
    """Key under ``hass.data[DOMAIN]`` that marks an entry mid-migration.

    Two places have to agree on it: the migration sets it, and anything
    that would otherwise reload the entry has to leave it alone until the
    migration takes its own reload.
    """
    return f"{entry_id}_migrating"

# Announcement-mode toggle, the modern setting. Mirrors the
# ``announce`` field on the ``openai_tts.say`` service. Semantics on
# True (the default):
#   * Speakers exposing ``MediaPlayerEntityFeature.MEDIA_ANNOUNCE``
#     (Sonos, Music Assistant, newer Cast) get a native announcement
#     - the device ducks under the speak and auto-resumes; we don't
#     touch their volume.
#   * Speakers without that capability go through manual pause +
#     speak + resume so the user doesn't lose the music on Cast /
#     Bluetooth.
# On False the integration is hands-off: the speaker handles the
# incoming media however it normally does (Cast replaces, BT
# overlays, Sonos still ducks at firmware level).
#
# Entries that predate this key are migrated in ``__init__.py``: a
# saved ``pause_playback`` value carries over, so someone who had
# explicitly opted out of media management keeps that behaviour.
CONF_ANNOUNCE_MODE = "announce_mode"
DEFAULT_ANNOUNCE_MODE = True

# Sentence-level streaming pipelining.
#
# When on, and the text arrives gradually from a streaming conversation
# agent, synthesis starts on the first completed sentence instead of
# waiting for the agent to finish writing. This only affects the voice
# assistant path: ``openai_tts.say`` passes a finished string, which
# Home Assistant wraps in a single-chunk generator, so it keeps issuing
# exactly one request.
#
# Off by default. It only helps when the conversation agent is slow
# enough for the wait to be audible, and it has a real cost: each
# sentence is a separate request, and OpenAI has no way to carry
# prosody across requests (no equivalent of ElevenLabs'
# ``previous_request_ids``), so the tone can shift mid-reply.
#
# Restricted at runtime to the audio formats whose responses can be
# joined into one stream: mp3, wav and pcm. opus and flac declare their
# total length up front and would cut everything after the first
# sentence, and aac loses samples at the seam. See
# ``streaming.PIPELINEABLE_FORMATS``.
CONF_STREAM_PIPELINING = "stream_pipelining"

# Profile name for sub-entries
CONF_PROFILE_NAME = "profile_name"

# Key for storing message-to-duration cache in hass.data
MESSAGE_DURATIONS_KEY = "message_durations"