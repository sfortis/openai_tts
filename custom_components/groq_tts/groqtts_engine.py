"""
TTS Engine for Groq TTS.
"""
import json
import logging
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from asyncio import CancelledError

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

class AudioResponse:
    """A simple response wrapper with a 'content' attribute to hold audio bytes."""
    def __init__(self, content: bytes):
        self.content = content

class GroqTTSEngine:
    def __init__(self, api_key: str, voice: str, model: str, url: str):
        self._api_key = api_key
        self._voice = voice
        self._model = model
        self._url = url

    def get_tts(self, text: str, voice: str = None) -> AudioResponse:
        """Synchronous TTS request using urllib.request for Groq API."""
        if voice is None:
            voice = self._voice

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # Override User-Agent to match working curl request
        headers["User-Agent"] = "curl/8.7.1"

        data = {
            "model": self._model,
            "input": text,
            "voice": voice,
        }

        max_retries = 1
        attempt = 0
        while True:
            try:
                req = Request(
                    self._url,
                    data=json.dumps(data).encode("utf-8"),
                    headers=headers,
                    method="POST"
                )
                with urlopen(req, timeout=30) as response:
                    content = response.read()
                # Check if the response is JSON (error) instead of audio
                try:
                    decoded = content.decode("utf-8")
                    if decoded.startswith('{'):
                        error_json = json.loads(decoded)
                        if "error" in error_json:
                            _LOGGER.error("Groq API error: %s", error_json["error"].get("message", str(error_json["error"])) )
                            raise HomeAssistantError(f"Groq API error: {error_json['error'].get('message', str(error_json['error']))}")
                except Exception:
                    pass  # Not JSON, assume audio
                return AudioResponse(content)
            except CancelledError:
                _LOGGER.exception("TTS request cancelled")
                raise
            except (HTTPError, URLError) as net_err:
                status_code = getattr(net_err, 'code', None)
                error_body = None
                error_hint = None
                if hasattr(net_err, 'read'):
                    try:
                        error_content = net_err.read().decode('utf-8')
                        error_body = error_content
                        # Try to parse as JSON
                        try:
                            error_json = json.loads(error_content)
                            if "error" in error_json:
                                error_msg = error_json["error"].get("message", str(error_json["error"]))
                                _LOGGER.error("Groq API error (HTTP %s): %s", status_code, error_msg)
                            else:
                                _LOGGER.error("Groq API error (HTTP %s): %s", status_code, error_json)
                        except Exception:
                            # Not JSON, log raw error body
                            if "1010" in error_content:
                                error_hint = "(Groq error 1010: You may not have accepted the PlayAI TTS model terms in your Groq account. See https://console.groq.com/playground?model=playai-tts)"
                            _LOGGER.error("Groq API error (HTTP %s): %s %s", status_code, error_content, error_hint or "")
                    except Exception as e:
                        _LOGGER.error("Groq API error (HTTP %s): Unable to read error body: %s", status_code, e)
                else:
                    _LOGGER.error("Groq API error (HTTP %s): No error body available.", status_code)
                _LOGGER.exception("Network error in synchronous get_tts on attempt %d", attempt + 1)
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(1)
                    _LOGGER.debug("Retrying HTTP call (attempt %d)", attempt + 1)
                    continue
                else:
                    raise HomeAssistantError(f"Network error occurred while fetching TTS audio (HTTP {status_code}): {error_body}") from net_err
            except Exception as exc:
                _LOGGER.exception("Unknown error in synchronous get_tts on attempt %d", attempt + 1)
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(1)
                    _LOGGER.debug("Retrying HTTP call (attempt %d)", attempt + 1)
                    continue
                else:
                    raise HomeAssistantError("An unknown error occurred while fetching TTS audio") from exc

    def close(self):
        pass

    @staticmethod
    def get_supported_langs() -> list:
        # Update with Groq's supported languages if available
        return ["en"]
