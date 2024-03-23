import requests

from .const import URL


class OpenAITTSEngine:

    def __init__(self, api_key: str, voice: str, model: str, speed: int):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = URL

    def get_tts(self, text: str):
        """ Makes request to OpenAI TTS engine to convert text into audio"""
        headers: dict = {"Authorization": f"Bearer {self._api_key}"}
        data: dict = {"model": self._model, "input": text, "voice": self._voice, "speed": self._speed}
        return requests.post(self._url, headers=headers, json=data)

    @staticmethod
    def get_supported_langs() -> list:
        """Returns list of supported languages. Note: the model determines the provides language automatically."""
        return ["af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "is", "id", "it", "ja", "kn", "kk", "ko", "lv", "lt", "mk", "ms", "mr", "mi", "ne", "no", "fa", "pl", "pt", "ro", "ru", "sr", "sk", "sl", "es", "sw", "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi", "cy"]


