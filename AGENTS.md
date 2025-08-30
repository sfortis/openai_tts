# Repository Guidelines

## Project Structure & Module Organization
- `custom_components/groq_tts/`: Home Assistant integration source.
  - `config_flow.py`: UI setup/options logic.
  - `tts.py`: TTS entity and audio post‑processing (ffmpeg).
  - `groqtts_engine.py`: Async client for Groq TTS API.
  - `const.py`: Constants and built‑ins (models, voices).
  - `chime/`: MP3 assets used for optional chime.
- `tests/`: Pytest unit tests and stubs.
- `README.md`, `hacs.json`, `manifest.json`: Integration metadata and docs.

## Build, Test, and Development Commands
- Run tests: `python -m pytest -q`
  - Executes unit tests in `tests/` with asyncio support and HA stubs.
- Lint/format (optional): use your local `ruff`/`black` if preferred; keep diffs minimal.
- No build step required. For end‑to‑end checks, install in a HA instance per README.

## Coding Style & Naming Conventions
- Python 3.11+, 4‑space indentation, PEP 8 style.
- Use type hints; prefer `async`/await for I/O and HA APIs.
- Filenames and module symbols: `snake_case`; constants in `const.py` are `UPPER_SNAKE_CASE`.
- Log with `_LOGGER` and avoid logging secrets (API keys, tokens).

## Testing Guidelines
- Framework: `pytest` with `@pytest.mark.asyncio` for async tests.
- Add tests under `tests/` named `test_*.py`; functions start with `test_`.
- Cover: config validation (`config_flow.validate_user_input`), chime option discovery, and network/error paths in `GroqTTSEngine`.
- Run: `pytest -q` locally; keep tests isolated (use stubs/mocks).

## Commit & Pull Request Guidelines
- Commits: concise, imperative mood (e.g., "Add dynamic voice selector").
- PRs must include:
  - Clear description and rationale; link related issues.
  - Tests for new logic or bug fixes.
  - Updates to `README.md` if options/models/usage change.
  - Screenshots of HA config screens if UI flows change.

## Security & Configuration Tips
- Do not commit API keys or real endpoints beyond defaults.
- Network calls must be async and resilient; never block the event loop.
- When adding assets, place MP3s in `custom_components/groq_tts/chime/`.
