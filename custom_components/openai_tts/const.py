"""
Constants for OpenAI TTS custom component
"""

DOMAIN = "openai_tts"
CONF_API_KEY = "api_key"
CONF_MODEL = "model"
CONF_VOICE = "voice"
CONF_SPEED = "speed"
CONF_URL = "url"
UNIQUE_ID = "unique_id"

# Engine selection
CONF_TTS_ENGINE = "tts_engine"
OPENAI_ENGINE = "openai"
KOKORO_FASTAPI_ENGINE = "kokoro_fastapi"
TTS_ENGINES = [OPENAI_ENGINE, KOKORO_FASTAPI_ENGINE]
DEFAULT_TTS_ENGINE = OPENAI_ENGINE

# Kokoro specific
CONF_KOKORO_URL = "kokoro_url"

MODELS = ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"] # Note: gpt-4o-mini-tts may be custom
VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]

CONF_CHIME_ENABLE = "chime"
CONF_CHIME_SOUND = "chime_sound"
CONF_NORMALIZE_AUDIO = "normalize_audio"
CONF_INSTRUCTIONS = "instructions"
