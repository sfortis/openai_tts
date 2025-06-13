import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

# Adjust import paths as necessary
from custom_components.openai_tts.tts import OpenAITTSEntity, async_setup_entry
from custom_components.openai_tts.openaitts_engine import OpenAITTSEngine
from custom_components.openai_tts.const import (
    DOMAIN,
    CONF_API_KEY,
    CONF_MODEL,
    CONF_VOICE,
    CONF_SPEED,
    CONF_URL,
    CONF_TTS_ENGINE,
    OPENAI_ENGINE,
    KOKORO_FASTAPI_ENGINE,
    CONF_KOKORO_URL,
    UNIQUE_ID, # Assuming UNIQUE_ID is used in config
)

# Minimal HomeAssistant mock
class MockHomeAssistant(MagicMock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data = {}
        # Mock async_add_executor_job to run functions directly for simplicity in these tests
        # For more complex scenarios, you might need a proper event loop and executor.
        self.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))


class TestOpenAITTSEntity(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.hass = MockHomeAssistant()

        self.openai_config_data = {
            CONF_TTS_ENGINE: OPENAI_ENGINE,
            CONF_API_KEY: "fake_openai_key",
            CONF_URL: "https://api.openai.com/v1/audio/speech",
            CONF_MODEL: "tts-1",
            CONF_VOICE: "alloy",
            CONF_SPEED: 1.0,
            UNIQUE_ID: "openai-test-unique-id"
        }
        self.kokoro_config_data = {
            CONF_TTS_ENGINE: KOKORO_FASTAPI_ENGINE,
            CONF_KOKORO_URL: "http://localhost:8002/tts",
            CONF_MODEL: "kokoro-model",
            CONF_VOICE: "kokoro-voice",
            CONF_SPEED: 1.0,
            UNIQUE_ID: "kokoro-test-unique-id"
            # No API key for Kokoro in this test setup
        }
        self.mock_engine = AsyncMock(spec=OpenAITTSEngine)
        # Default title for config entry, can be overridden in tests
        self.config_entry_title = "Test TTS Config"


    def _setup_entity(self, config_data: dict) -> OpenAITTSEntity:
        """Helper to create an entity instance with mocked ConfigEntry and engine."""
        mock_config_entry = MagicMock(spec=ConfigEntry)
        mock_config_entry.data = config_data
        mock_config_entry.options = {} # Start with empty options
        # Mock the title property of the config_entry
        type(mock_config_entry).title = PropertyMock(return_value=self.config_entry_title)

        # The engine is now created inside async_setup_entry, so we patch OpenAITTSEngine directly
        # or we can pass a pre-mocked engine if we refactor entity creation slightly for tests
        # For now, let's assume we pass the engine in if testing entity methods directly.
        # If testing async_setup_entry, we'd patch the engine's constructor.

        entity = OpenAITTSEntity(self.hass, mock_config_entry, self.mock_engine)
        return entity

    async def test_device_info_openai(self):
        """Test device_info for OpenAI configuration."""
        self.config_entry_title = "OpenAI TTS tts-1" # Match expected name format
        entity = self._setup_entity(self.openai_config_data)
        device_info = entity.device_info
        self.assertEqual(device_info["manufacturer"], "OpenAI")
        self.assertEqual(device_info["model"], self.openai_config_data[CONF_MODEL])
        self.assertEqual(device_info["name"], self.config_entry_title)


    async def test_device_info_kokoro(self):
        """Test device_info for Kokoro FastAPI configuration."""
        self.config_entry_title = "Kokoro FastAPI TTS kokoro-model" # Match expected name format
        entity = self._setup_entity(self.kokoro_config_data)
        device_info = entity.device_info
        self.assertEqual(device_info["manufacturer"], "Kokoro FastAPI")
        self.assertEqual(device_info["model"], self.kokoro_config_data[CONF_MODEL])
        self.assertEqual(device_info["name"], self.config_entry_title)

    async def test_name_property_openai(self):
        """Test name property for OpenAI configuration."""
        self.config_entry_title = "OpenAI TTS tts-1"
        entity = self._setup_entity(self.openai_config_data)
        self.assertEqual(entity.name, self.config_entry_title)

    async def test_name_property_kokoro(self):
        """Test name property for Kokoro FastAPI configuration."""
        self.config_entry_title = "Kokoro FastAPI TTS kokoro-model"
        entity = self._setup_entity(self.kokoro_config_data)
        self.assertEqual(entity.name, self.config_entry_title)

    async def test_async_get_tts_audio_streaming_success(self):
        """Test successful audio streaming via async_get_tts_audio."""
        entity = self._setup_entity(self.kokoro_config_data) # Using Kokoro for this test

        test_audio_chunks = [b"Hello", b" ", b"World"]
        # Mock the engine's get_tts to be an async generator
        async def mock_stream_audio(*args, **kwargs):
            for chunk in test_audio_chunks:
                yield chunk
        self.mock_engine.get_tts = mock_stream_audio

        # Call the method that internally calls get_tts_audio
        fmt, audio_data = await entity.async_get_tts_audio("Hello World", "en-US", options={})

        self.assertEqual(fmt, "mp3") # Assuming mp3 is the format
        self.assertEqual(audio_data, b"Hello World")
        # self.mock_engine.get_tts.assert_called_once_with("Hello World", speed=ANY, voice=ANY, instructions=ANY)
        # We can make assertions about arguments passed to self.mock_engine.get_tts if needed

    @patch("subprocess.run") # Patch subprocess.run
    async def test_async_get_tts_audio_with_ffmpeg_processing(self, mock_subprocess_run):
        """Test audio streaming with ffmpeg (chime/normalization) correctly called."""
        # Setup mock for subprocess.run
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")

        # Enable chime to trigger ffmpeg processing
        entity = self._setup_entity(self.kokoro_config_data)
        entity._config.options = { # Simulate options being set
            "chime": True,
            "chime_sound": "threetone.mp3",
            "normalize_audio": True
        }

        test_audio_chunks = [b"raw_audio_chunk1", b"raw_audio_chunk2"]
        expected_raw_audio = b"raw_audio_chunk1raw_audio_chunk2"

        async def mock_stream_audio(*args, **kwargs):
            for chunk in test_audio_chunks:
                yield chunk
        self.mock_engine.get_tts = mock_stream_audio

        # Mock tempfile operations
        mock_temp_file_tts = MagicMock()
        mock_temp_file_tts.name = "/tmp/fake_tts.mp3"
        mock_temp_file_tts.__enter__.return_value = mock_temp_file_tts # For 'with' statement

        mock_temp_file_merged = MagicMock()
        mock_temp_file_merged.name = "/tmp/fake_merged.mp3"
        mock_temp_file_merged.__enter__.return_value = mock_temp_file_merged

        # Patch tempfile.NamedTemporaryFile and open
        with patch("tempfile.NamedTemporaryFile", side_effect=[mock_temp_file_tts, mock_temp_file_merged]) as mock_tempfile_creator, \
             patch("builtins.open", MagicMock(return_value=MagicMock(read=MagicMock(return_value=b"processed_audio")))) as mock_open, \
             patch("os.path.join", MagicMock(return_value="/mock/path/to/chime.mp3")), \
             patch("os.path.dirname", MagicMock(return_value="/mock/path")), \
             patch("os.remove") as mock_os_remove:

            fmt, audio_data = await entity.async_get_tts_audio("Test message", "en-US", options={})

            self.assertEqual(fmt, "mp3")
            self.assertEqual(audio_data, b"processed_audio") # Audio comes from mocked open after ffmpeg

            # Check that tempfile.NamedTemporaryFile was called to create tts and merged files
            self.assertEqual(mock_tempfile_creator.call_count, 2)

            # Check that tts_file.write was called with the concatenated raw audio
            mock_temp_file_tts.write.assert_called_once_with(expected_raw_audio)

            # Check that hass.async_add_executor_job was used for subprocess.run
            self.hass.async_add_executor_job.assert_called()
            # Check that subprocess.run was called (args depend on chime/norm options)
            mock_subprocess_run.assert_called()
            # Check that temp files were removed
            self.assertGreaterEqual(mock_os_remove.call_count, 2)


    async def test_async_get_tts_audio_engine_error(self):
        """Test error handling when the TTS engine's get_tts fails."""
        entity = self._setup_entity(self.openai_config_data)
        self.mock_engine.get_tts = AsyncMock(side_effect=HomeAssistantError("Engine failed"))

        fmt, audio_data = await entity.async_get_tts_audio("Test error", "en-US", options={})

        self.assertIsNone(fmt)
        self.assertIsNone(audio_data)
        # Add log check if possible/needed: _LOGGER.exception("Unknown error in get_tts_audio")

    async def test_async_will_remove_from_hass(self):
        """Test that async_will_remove_from_hass calls engine.close()."""
        entity = self._setup_entity(self.openai_config_data)
        self.mock_engine.close = AsyncMock() # Ensure close is an AsyncMock

        await entity.async_will_remove_from_hass()
        self.mock_engine.close.assert_called_once()

    @patch('custom_components.openai_tts.tts.OpenAITTSEngine') # Patch where it's used
    async def test_async_setup_entry_kokoro(self, MockOpenAITTSEngineConstructor):
        """Test async_setup_entry for Kokoro configuration."""
        mock_engine_instance = MockOpenAITTSEngineConstructor.return_value
        self.hass.data[DOMAIN] = {} # Ensure domain data exists

        mock_config_entry = MagicMock(spec=ConfigEntry)
        mock_config_entry.data = self.kokoro_config_data
        mock_config_entry.options = {}

        async_add_entities_mock = MagicMock()

        await async_setup_entry(self.hass, mock_config_entry, async_add_entities_mock)

        MockOpenAITTSEngineConstructor.assert_called_once_with(
            api_key=None, # Kokoro specific
            voice=self.kokoro_config_data[CONF_VOICE],
            model=self.kokoro_config_data[CONF_MODEL],
            speed=self.kokoro_config_data[CONF_SPEED],
            url=self.kokoro_config_data[CONF_KOKORO_URL] # Kokoro URL
        )
        async_add_entities_mock.assert_called_once()
        # Further checks on the entity passed to async_add_entities_mock can be added.

    @patch('custom_components.openai_tts.tts.OpenAITTSEngine')
    async def test_async_setup_entry_openai(self, MockOpenAITTSEngineConstructor):
        """Test async_setup_entry for OpenAI configuration."""
        mock_engine_instance = MockOpenAITTSEngineConstructor.return_value
        self.hass.data[DOMAIN] = {}

        mock_config_entry = MagicMock(spec=ConfigEntry)
        mock_config_entry.data = self.openai_config_data
        mock_config_entry.options = {}

        async_add_entities_mock = MagicMock()

        await async_setup_entry(self.hass, mock_config_entry, async_add_entities_mock)

        MockOpenAITTSEngineConstructor.assert_called_once_with(
            api_key=self.openai_config_data[CONF_API_KEY],
            voice=self.openai_config_data[CONF_VOICE],
            model=self.openai_config_data[CONF_MODEL],
            speed=self.openai_config_data[CONF_SPEED],
            url=self.openai_config_data[CONF_URL]
        )
        async_add_entities_mock.assert_called_once()

if __name__ == '__main__':
    unittest.main()
