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

MODELS = ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"]
VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]

CONF_CHIME_ENABLE = "chime"
CONF_CHIME_SOUND = "chime_sound"
CONF_NORMALIZE_AUDIO = "normalize_audio"
CONF_INSTRUCTIONS = "instructions"

# Toggle to snapshot & restore volumes
CONF_VOLUME_RESTORE = "volume_restore"

# NEW: Toggle to pause/resume media playback
CONF_PAUSE_PLAYBACK = "pause_playback"
