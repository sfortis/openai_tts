"""
TTS Engine for OpenAI TTS.
"""
import asyncio
import threading
import logging
import aiohttp

_LOGGER = logging.getLogger(__name__)

class AudioResponse:
    """A simple response wrapper with a 'content' attribute to hold audio bytes."""
    def __init__(self, content: bytes):
        self.content = content

class OpenAITTSEngine:
    def __init__(self, api_key: str, voice: str, model: str, speed: float, url: str):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._speed = speed
        self._url = url

        # Create a dedicated event loop running in a background thread.
        self._loop = asyncio.new_event_loop()
        self._session = None
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()
        # Initialize the aiohttp session in the background event loop.
        asyncio.run_coroutine_threadsafe(self._init_session(), self._loop).result()

    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _init_session(self):
        # Create a persistent aiohttp session for reuse.
        self._session = aiohttp.ClientSession()

    async def _async_get_tts(self, text: str, speed: float, voice: str) -> AudioResponse:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = {
            "model": self._model,
            "input": text,
            "voice": voice,
            "response_format": "wav",
            "speed": speed,
            "stream": True
        }
        # Use separate timeouts for connecting and reading.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=25)
        async with self._session.post(self._url, headers=headers, json=data, timeout=timeout) as resp:
            resp.raise_for_status()
            audio_chunks = []
            # Optimize the chunk size to 4096 bytes.
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    audio_chunks.append(chunk)
            audio_data = b"".join(audio_chunks)
            return AudioResponse(audio_data)

    def get_tts(self, text: str, speed: float = None, voice: str = None) -> AudioResponse:
        """Synchronous wrapper that runs the asynchronous TTS request on a dedicated event loop.
           If 'speed' or 'voice' are provided, they override the stored values.
        """
        try:
            if speed is None:
                speed = self._speed
            if voice is None:
                voice = self._voice
            future = asyncio.run_coroutine_threadsafe(self._async_get_tts(text, speed, voice), self._loop)
            return future.result()
        except Exception as e:
            _LOGGER.error("Error in asynchronous get_tts: %s", e)
            raise e

    def close(self):
        """Clean up the aiohttp session and event loop on shutdown."""
        if self._session:
            asyncio.run_coroutine_threadsafe(self._session.close(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop())

    @staticmethod
    def get_supported_langs() -> list:
        return [
            "af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr", "cs", "da", "nl", "en",
            "et", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "is", "id", "it", "ja", "kn",
            "kk", "ko", "lv", "lt", "mk", "ms", "mr", "mi", "ne", "no", "fa", "pl", "pt", "ro",
            "ru", "sr", "sk", "sl", "es", "sw", "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi", "cy"
        ]
