"""
Constants for Groq TTS custom component
"""

DOMAIN = "groq_tts"
VERSION = "0.1"
CONF_API_KEY = "api_key"
CONF_MODEL = "model"
CONF_VOICE = "voice"
CONF_URL = "url"
UNIQUE_ID = "unique_id"

MODELS = ["playai-tts", "playai-tts-arabic"]
VOICES = [
    "Ahmad-PlayAI",
    "Amira-PlayAI",
    "Arista-PlayAI",
    "Atlas-PlayAI",
    "Basil-PlayAI",
    "Briggs-PlayAI",
    "Calum-PlayAI",
    "Celeste-PlayAI",
    "Cheyenne-PlayAI",
    "Chip-PlayAI",
    "Cillian-PlayAI",
    "Deedee-PlayAI",
    "Fritz-PlayAI",
    "Gail-PlayAI",
    "Indigo-PlayAI",
    "Khalid-PlayAI",
    "Mamaw-PlayAI",
    "Mason-PlayAI",
    "Mikail-PlayAI",
    "Mitch-PlayAI",
    "Nasser-PlayAI",
    "Quinn-PlayAI",
    "Thunder-PlayAI",
]

CONF_CHIME_ENABLE = "chime"
CONF_CHIME_SOUND = "chime_sound"
CONF_NORMALIZE_AUDIO = "normalize_audio"
CONF_CACHE_SIZE = "cache_size"
DEFAULT_CACHE_SIZE = 256
