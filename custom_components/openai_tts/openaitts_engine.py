import requests

class OpenAITTSEngine:

    def __init__(self, api_key: str, voice: str, model: str, speed: int, url: str):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = url

    def get_tts(self, text: str):
        """ Makes request to OpenAI TTS engine to convert text into audio"""
        headers: dict = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data: dict = {
            "model": self._model,
            "input": text,
            "voice": self._voice,
            "response_format": "wav",
            "speed": self._speed
        }
        return requests.post(self._url, headers=headers, json=data)

    @staticmethod
    def get_supported_langs() -> list:
        """Returns list of supported languages. Note: the model determines the provides language automatically."""
        return ["af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "is", "id", "it", "ja", "kn", "kk", "ko", "lv", "lt", "mk", "ms", "mr", "mi", "ne", "no", "fa", "pl", "pt", "ro", "ru", "sr", "sk", "sl", "es", "sw", "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi", "cy"]
