import asyncio
import sys
import types
import pytest
from unittest.mock import patch

# Provide minimal stubs for Home Assistant modules used during import
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.helpers", types.ModuleType("helpers"))
sys.modules.setdefault("homeassistant.data_entry_flow", types.ModuleType("data_entry_flow"))
sys.modules["homeassistant.data_entry_flow"].AbortFlow = type("AbortFlow", (Exception,), {})
config_entries_mod = types.ModuleType("config_entries")
class _ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass

class _OptionsFlow:
    def __init_subclass__(cls, **kwargs):
        pass
config_entries_mod.ConfigFlow = _ConfigFlow
config_entries_mod.OptionsFlow = _OptionsFlow
config_entries_mod.ConfigEntry = type("ConfigEntry", (object,), {})
sys.modules.setdefault("homeassistant.config_entries", config_entries_mod)
sys.modules.setdefault("homeassistant.helpers.selector", types.ModuleType("selector"))
sys.modules["homeassistant.helpers.selector"].selector = lambda x: x
aiohttp_mod = types.ModuleType("aiohttp_client")
aiohttp_mod.async_get_clientsession = lambda hass: None
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_mod)
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
sys.modules["aiohttp"].ClientSession = object
sys.modules["aiohttp"].ClientError = Exception
vol_mod = types.ModuleType("voluptuous")
vol_mod.Schema = lambda x: x
vol_mod.Optional = lambda *args, **kwargs: None
vol_mod.Required = lambda *args, **kwargs: None
sys.modules.setdefault("voluptuous", vol_mod)
exceptions_mod = types.ModuleType("exceptions")
class _HAError(Exception):
    pass
exceptions_mod.HomeAssistantError = _HAError
class _ConfigEntryAuthFailed(Exception):
    pass
exceptions_mod.ConfigEntryAuthFailed = _ConfigEntryAuthFailed
sys.modules.setdefault("homeassistant.exceptions", exceptions_mod)

from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

COMP_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "groq_tts"

# Create a fake package to satisfy relative imports inside the modules
pkg_name = "testpkg"
pkg = types.ModuleType(pkg_name)
pkg.__path__ = [str(COMP_DIR)]
sys.modules[pkg_name] = pkg

# Additional stubs required by tts module
sys.modules.setdefault("homeassistant.components", types.ModuleType("components"))
tts_mod = types.ModuleType("tts")
class _TextToSpeechEntity:
    pass
tts_mod.TextToSpeechEntity = _TextToSpeechEntity
sys.modules["homeassistant.components.tts"] = tts_mod
helpers_entity_mod = types.ModuleType("homeassistant.helpers.entity")
helpers_entity_mod.generate_entity_id = lambda fmt, base, hass=None: "tts.groq_tts_" + (base or "entity")
sys.modules["homeassistant.helpers.entity"] = helpers_entity_mod
sys.modules.setdefault("homeassistant.helpers.entity_platform", types.ModuleType("homeassistant.helpers.entity_platform"))
sys.modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (object,), {})

spec_const = spec_from_file_location(f"{pkg_name}.const", COMP_DIR / "const.py")
const = module_from_spec(spec_const)
spec_const.loader.exec_module(const)
sys.modules[f"{pkg_name}.const"] = const

spec_cf = spec_from_file_location(f"{pkg_name}.config_flow", COMP_DIR / "config_flow.py")
config_flow = module_from_spec(spec_cf)
spec_cf.loader.exec_module(config_flow)

spec_engine = spec_from_file_location(f"{pkg_name}.groqtts_engine", COMP_DIR / "groqtts_engine.py")
groqtts_engine = module_from_spec(spec_engine)
spec_engine.loader.exec_module(groqtts_engine)
sys.modules["groqtts_engine"] = groqtts_engine

spec_tts = spec_from_file_location(f"{pkg_name}.tts", COMP_DIR / "tts.py")
tts_module = module_from_spec(spec_tts)
spec_tts.loader.exec_module(tts_module)

validate_user_input = config_flow.validate_user_input
get_chime_options = config_flow.get_chime_options
GroqTTSEngine = groqtts_engine.GroqTTSEngine
HomeAssistantError = groqtts_engine.HomeAssistantError
GroqTTSEntity = tts_module.GroqTTSEntity


@pytest.mark.asyncio
async def test_validate_user_input_missing_model():
    with pytest.raises(ValueError):
        await validate_user_input({})


@pytest.mark.asyncio
async def test_validate_user_input_missing_voice():
    with pytest.raises(ValueError):
        await validate_user_input({"model": "playai-tts"})


def test_get_chime_options():
    opts = get_chime_options()
    assert isinstance(opts, list)
    assert all(opt["value"].endswith(".mp3") for opt in opts)


class DummySession:
    def post(self, *args, **kwargs):
        raise sys.modules["aiohttp"].ClientError("boom")


class DummyHass:
    pass


@pytest.mark.asyncio
async def test_async_get_tts_network_error():
    engine = GroqTTSEngine(None, "voice", "model", "http://example.com")

    with patch.object(
        groqtts_engine, "async_get_clientsession", return_value=DummySession()
    ):
        with pytest.raises(HomeAssistantError):
            await engine.async_get_tts(DummyHass(), "hi")


class DummyResponse:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = headers
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyOkJsonSession:
    def post(self, *args, **kwargs):
        headers = {"content-type": "application/json"}
        body = b"{\"ok\": true}"
        return DummyResponse(200, headers, body)


@pytest.mark.asyncio
async def test_async_get_tts_non_audio_2xx():
    engine = GroqTTSEngine(None, "voice", "model", "http://example.com")
    with patch.object(groqtts_engine, "async_get_clientsession", return_value=DummyOkJsonSession()):
        with pytest.raises(HomeAssistantError):
            await engine.async_get_tts(DummyHass(), "hello")


class Dummy401Session:
    def post(self, *args, **kwargs):
        headers = {"content-type": "text/plain"}
        body = b"unauthorized"
        return DummyResponse(401, headers, body)


@pytest.mark.asyncio
async def test_async_get_tts_raises_config_entry_auth_failed_on_401():
    engine = GroqTTSEngine(None, "voice", "model", "http://example.com")
    with patch.object(groqtts_engine, "async_get_clientsession", return_value=Dummy401Session()):
        with pytest.raises(exceptions_mod.ConfigEntryAuthFailed):
            await engine.async_get_tts(DummyHass(), "hello")


class DummyEngine:
    class _Resp:
        def __init__(self, content: bytes):
            self.content = content

    async def async_get_tts(self, hass, text, voice=None):
        return self._Resp(b"audio-bytes")


class DummyConfigEntry:
    def __init__(self, data: dict, options: dict):
        self.data = data
        self.options = options
        self.unique_id = data.get("unique_id")


@pytest.mark.asyncio
async def test_tts_missing_chime_returns_none():
    # Setup config with chime enabled to a non-existent file
    data = {"url": "http://example.com", "model": "playai-tts", "voice": "Arista-PlayAI", "unique_id": "uid"}
    options = {"chime": True, "chime_sound": "does_not_exist.mp3"}
    entity = GroqTTSEntity(DummyHass(), DummyConfigEntry(data, options), DummyEngine())
    ext, payload = await entity.async_get_tts_audio("Hello", "en", options=None)
    assert ext is None and payload is None


class DummyProc:
    def __init__(self, returncode: int):
        self.returncode = returncode

    async def communicate(self, input=None):  # noqa: A002
        return b"", b"ffmpeg error"


@pytest.mark.asyncio
async def test_tts_ffmpeg_failure_returns_none(monkeypatch):
    # Enable normalize so ffmpeg runs without requiring chime file
    data = {"url": "http://example.com", "model": "playai-tts", "voice": "Arista-PlayAI", "unique_id": "uid"}
    options = {"normalize_audio": True}
    entity = GroqTTSEntity(DummyHass(), DummyConfigEntry(data, options), DummyEngine())

    async def fake_exec(*args, **kwargs):  # noqa: ANN001, D401
        return DummyProc(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ext, payload = await entity.async_get_tts_audio("Hello", "en", options=None)
    assert ext is None and payload is None
