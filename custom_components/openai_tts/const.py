"""
Constants for OpenAI TTS custom component
"""

DOMAIN = "openai_tts"
CONF_API_KEY = "api_key"
CONF_MODEL = "model"
CONF_VOICE = "voice"
CONF_SPEED = "speed"
CONF_URL = "url"
DEFAULT_URL = "https://api.openai.com/v1/audio/speech"
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

AUDIO_FORMATS = ["mp3", "opus", "aac", "flac", "wav", "pcm"]
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

# Toggle to pause/resume media playback
CONF_PAUSE_PLAYBACK = "pause_playback"

# Profile name for sub-entries
CONF_PROFILE_NAME = "profile_name"

# Key for storing message-to-duration cache in hass.data
MESSAGE_DURATIONS_KEY = "message_durations"