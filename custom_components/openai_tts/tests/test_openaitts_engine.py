import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.exceptions import HomeAssistantError

# Adjust the import path according to your project structure
from custom_components.openai_tts.openaitts_engine import OpenAITTSEngine
from custom_components.openai_tts.const import (
    KOKORO_FASTAPI_ENGINE, # Assuming this is defined
    OPENAI_ENGINE
)

class TestOpenAITTSEngine(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.api_key = "test_api_key"
        self.openai_voice = "alloy"
        self.openai_model = "tts-1"
        self.openai_speed = 1.0
        self.openai_url = "https://api.openai.com/v1/audio/speech"

        self.kokoro_voice = "kokoro_voice"
        self.kokoro_model = "kokoro_model"
        self.kokoro_speed = 1.2
        self.kokoro_url = "http://localhost:8002/tts"

    async def test_init_openai_engine(self):
        """Test engine initialization with OpenAI configuration."""
        engine = OpenAITTSEngine(
            api_key=self.api_key,
            voice=self.openai_voice,
            model=self.openai_model,
            speed=self.openai_speed,
            url=self.openai_url
        )
        self.assertIsNotNone(engine._session)
        self.assertEqual(engine._api_key, self.api_key)
        self.assertEqual(engine._url, self.openai_url)
        await engine.close()

    async def test_init_kokoro_engine(self):
        """Test engine initialization with Kokoro FastAPI configuration."""
        engine = OpenAITTSEngine(
            api_key=None, # Kokoro might not use an API key
            voice=self.kokoro_voice,
            model=self.kokoro_model,
            speed=self.kokoro_speed,
            url=self.kokoro_url
        )
        self.assertIsNotNone(engine._session)
        self.assertIsNone(engine._api_key)
        self.assertEqual(engine._url, self.kokoro_url)
        await engine.close()

    @patch("aiohttp.ClientSession")
    async def test_kokoro_configuration_request(self, MockClientSession):
        """Test that Kokoro engine makes requests to the correct URL without API key if not provided."""
        mock_session_instance = MockClientSession.return_value
        mock_post_response = AsyncMock()
        mock_post_response.status = 200
        mock_post_response.content.iter_any = AsyncMock(return_value=[b"chunk1", b"chunk2"])
        mock_session_instance.post = AsyncMock(return_value=mock_post_response)

        engine = OpenAITTSEngine(
            api_key=None,  # No API key for Kokoro
            voice=self.kokoro_voice,
            model=self.kokoro_model,
            speed=self.kokoro_speed,
            url=self.kokoro_url
        )

        text_to_speak = "Hello Kokoro"
        async for _ in engine.get_tts(text_to_speak):
            pass

        mock_session_instance.post.assert_called_once()
        args, kwargs = mock_session_instance.post.call_args
        self.assertEqual(args[0], self.kokoro_url)
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["json"]["input"], text_to_speak)
        self.assertEqual(kwargs["json"]["voice"], self.kokoro_voice)
        await engine.close()

    @patch("aiohttp.ClientSession")
    async def test_openai_configuration_request_with_key(self, MockClientSession):
        """Test that OpenAI engine makes requests to the correct URL with API key."""
        mock_session_instance = MockClientSession.return_value
        mock_post_response = AsyncMock()
        mock_post_response.status = 200
        mock_post_response.content.iter_any = AsyncMock(return_value=[b"chunk1", b"chunk2"])
        mock_session_instance.post = AsyncMock(return_value=mock_post_response)

        engine = OpenAITTSEngine(
            api_key=self.api_key,
            voice=self.openai_voice,
            model=self.openai_model,
            speed=self.openai_speed,
            url=self.openai_url
        )

        text_to_speak = "Hello OpenAI"
        async for _ in engine.get_tts(text_to_speak):
            pass

        mock_session_instance.post.assert_called_once()
        args, kwargs = mock_session_instance.post.call_args
        self.assertEqual(args[0], self.openai_url)
        self.assertIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {self.api_key}")
        self.assertEqual(kwargs["json"]["input"], text_to_speak)
        await engine.close()


    @patch("aiohttp.ClientSession")
    async def test_streaming_success(self, MockClientSession):
        """Test successful streaming of audio chunks."""
        mock_session_instance = MockClientSession.return_value
        mock_post_response = AsyncMock()
        mock_post_response.status = 200
        # Simulate iter_any() behavior
        async def dummy_iter_any():
            yield b"stream_chunk_1"
            yield b"stream_chunk_2"
        mock_post_response.content.iter_any = dummy_iter_any
        mock_session_instance.post = AsyncMock(return_value=mock_post_response)

        engine = OpenAITTSEngine(self.api_key, self.openai_voice, self.openai_model, self.openai_speed, self.openai_url)

        collected_chunks = []
        async for chunk in engine.get_tts("test streaming"):
            collected_chunks.append(chunk)

        self.assertEqual(collected_chunks, [b"stream_chunk_1", b"stream_chunk_2"])
        await engine.close()

    @patch("aiohttp.ClientSession")
    async def test_api_error_handling_streaming(self, MockClientSession):
        """Test API error (ClientResponseError) handling during streaming."""
        mock_session_instance = MockClientSession.return_value
        mock_session_instance.post = AsyncMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=MagicMock(),
                status=400,
                message="Test API Error"
            )
        )

        engine = OpenAITTSEngine(self.api_key, self.openai_voice, self.openai_model, self.openai_speed, self.openai_url)

        with self.assertRaises(HomeAssistantError) as context:
            async for _ in engine.get_tts("test api error"):
                pass
        self.assertIn("Network error occurred while fetching TTS audio: Test API Error", str(context.exception))
        await engine.close()

    @patch("aiohttp.ClientSession")
    async def test_network_error_handling_streaming(self, MockClientSession):
        """Test general network error (ClientError) handling during streaming."""
        mock_session_instance = MockClientSession.return_value
        mock_session_instance.post = AsyncMock(side_effect=aiohttp.ClientError("Test Network Connection Error"))

        engine = OpenAITTSEngine(self.api_key, self.openai_voice, self.openai_model, self.openai_speed, self.openai_url)

        with self.assertRaises(HomeAssistantError) as context:
            async for _ in engine.get_tts("test network error"):
                pass
        self.assertIn("Network error occurred while fetching TTS audio: Test Network Connection Error", str(context.exception))
        await engine.close()

    @patch("aiohttp.ClientSession")
    async def test_cancelled_error_handling(self, MockClientSession):
        """Test CancelledError propagation."""
        mock_session_instance = MockClientSession.return_value
        mock_session_instance.post = AsyncMock(side_effect=asyncio.CancelledError)

        engine = OpenAITTSEngine(self.api_key, self.openai_voice, self.openai_model, self.openai_speed, self.openai_url)

        with self.assertRaises(asyncio.CancelledError):
            async for _ in engine.get_tts("test cancellation"):
                pass
        # Note: We don't call await engine.close() here as the operation was cancelled.
        # Depending on how session is managed, it might be closed by a higher level or GC.
        # For this test, we confirm CancelledError propagates.
        # If there's a specific cleanup expected even on cancellation, that needs testing.
        # Manually close if necessary for subsequent tests if session is reused by test runner
        if engine._session and not engine._session.closed:
             await engine._session.close()


    @patch("aiohttp.ClientSession")
    async def test_close_method(self, MockClientSession):
        """Test that the close method correctly closes the aiohttp session."""
        mock_session_instance = MockClientSession.return_value
        mock_session_instance.close = AsyncMock() # Make close an AsyncMock

        engine = OpenAITTSEngine(self.api_key, self.openai_voice, self.openai_model, self.openai_speed, self.openai_url)
        await engine.close()

        mock_session_instance.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
