"""
Support for OpenAI TTS.
"""
import logging
import requests
import voluptuous as vol
from homeassistant.components.tts import CONF_LANG, PLATFORM_SCHEMA, Provider
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

CONF_API_KEY = 'api_key'
DEFAULT_LANG = 'en-US'
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
CONF_MODEL = 'model'
CONF_VOICE = 'voice'
CONF_SPEED = 'speed'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_API_KEY): cv.string,
    vol.Optional(CONF_LANG, default=DEFAULT_LANG): cv.string,
    vol.Optional(CONF_MODEL, default='tts-1'): cv.string,
    vol.Optional(CONF_VOICE, default='shimmer'): cv.string,
    vol.Optional(CONF_SPEED, default=1): cv.string,
})

def get_engine(hass, config, discovery_info=None):
    """Set up OpenAI TTS speech component."""
    api_key = config[CONF_API_KEY]
    language = config.get(CONF_LANG, DEFAULT_LANG)
    model = config.get(CONF_MODEL)
    voice = config.get(CONF_VOICE)
    speed = config.get(CONF_SPEED)
    return OpenAITTSProvider(hass, api_key, language, model, voice, speed)

class OpenAITTSProvider(Provider):
    """The OpenAI TTS API provider."""

    def __init__(self, hass, api_key, lang, model, voice, speed):
        """Initialize OpenAI TTS provider."""
        self.hass = hass
        self._api_key = api_key
        self._language = lang
        self._model = model
        self._voice = voice
        self._speed = speed

    @property
    def default_language(self):
        """Return the default language."""
        return self._language

    @property
    def supported_languages(self):
        """Return the list of supported languages."""
        # Ideally, this list should be dynamically fetched from OpenAI, if supported.
        return [self._language]

    def get_tts_audio(self, message, language, options=None):
        """Convert a given text to speech and return it as bytes."""
        # Define the headers, including the Authorization header with your API key
        headers = {
            'Authorization': f'Bearer {self._api_key}'
        }

        # Define the data payload, specifying the model, input text, voice, and response format
        data = {
            'model': self._model,  # Choose between 'tts-1' and 'tts-1-hd' based on your preference
            'voice': self._voice,  # Choose the desired voice
            'speed': self._speed,  # Voice speed
            'input': message,
            # Optional parameters can also be included, like 'speed' and 'response_format'
        }

        try:
            # Make the POST request to the correct endpoint for generating speech
            response = requests.post(OPENAI_TTS_URL, json=data, headers=headers)
            response.raise_for_status()  # Raises an HTTPError if the HTTP request returned an unsuccessful status code

            # The response should contain the audio file content
            return "mp3", response.content
        except requests.exceptions.HTTPError as http_err:
            _LOGGER.error("HTTP error from OpenAI: %s", http_err)
        except requests.exceptions.RequestException as req_err:
            _LOGGER.error("Request exception from OpenAI: %s", req_err)
        return None, None
