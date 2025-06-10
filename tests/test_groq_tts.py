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
sys.modules.setdefault("homeassistant.config_entries", config_entries_mod)
sys.modules.setdefault("homeassistant.helpers.selector", types.ModuleType("selector"))
sys.modules["homeassistant.helpers.selector"].selector = lambda x: x
aiohttp_mod = types.ModuleType("aiohttp_client")
aiohttp_mod.async_get_clientsession = lambda hass: None
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_mod)
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
sys.modules["aiohttp"].ClientSession = object
vol_mod = types.ModuleType("voluptuous")
vol_mod.Schema = lambda x: x
vol_mod.Optional = lambda *args, **kwargs: None
vol_mod.Required = lambda *args, **kwargs: None
sys.modules.setdefault("voluptuous", vol_mod)
exceptions_mod = types.ModuleType("exceptions")
class _HAError(Exception):
    pass
exceptions_mod.HomeAssistantError = _HAError
sys.modules.setdefault("homeassistant.exceptions", exceptions_mod)

from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

COMP_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "groq_tts"

# Create a fake package to satisfy relative imports inside the modules
pkg_name = "testpkg"
pkg = types.ModuleType(pkg_name)
pkg.__path__ = [str(COMP_DIR)]
sys.modules[pkg_name] = pkg

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

validate_user_input = config_flow.validate_user_input
get_chime_options = config_flow.get_chime_options
GroqTTSEngine = groqtts_engine.GroqTTSEngine
HomeAssistantError = groqtts_engine.HomeAssistantError


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
    async def post(self, *args, **kwargs):
        raise Exception("boom")


class DummyHass:
    pass


@pytest.mark.asyncio
async def test_async_get_tts_network_error():
    engine = GroqTTSEngine(None, "voice", "model", "http://example.com")

    with patch("groqtts_engine.async_get_clientsession", return_value=DummySession()):
        with pytest.raises(HomeAssistantError):
            await engine.async_get_tts(DummyHass(), "hi")
