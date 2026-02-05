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
VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]

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

# Toggle to snapshot & restore volumes
CONF_VOLUME_RESTORE = "volume_restore"

# Toggle to pause/resume media playback
CONF_PAUSE_PLAYBACK = "pause_playback"

# Profile name for sub-entries
CONF_PROFILE_NAME = "profile_name"

# Key for storing message-to-duration cache in hass.data
MESSAGE_DURATIONS_KEY = "message_durations"