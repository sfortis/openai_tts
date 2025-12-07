# Contributing to OpenAI TTS

Thank you for your interest in contributing to the OpenAI TTS custom component for Home Assistant!

## Reporting Issues

Before opening an issue:
1. Check [existing issues](https://github.com/sfortis/openai_tts/issues) to avoid duplicates
2. Include your Home Assistant version
3. Include the integration version (from HACS or manifest.json)
4. Provide relevant logs from Home Assistant

## Development Setup

1. Fork and clone the repository
2. Create a feature branch from `beta`:
   ```bash
   git checkout beta
   git checkout -b feature/your-feature-name
   ```
3. Copy files to your HA test instance:
   ```bash
   cp -r custom_components/openai_tts /path/to/ha/config/custom_components/
   ```
4. Restart Home Assistant and test your changes

## Code Guidelines

- Follow Home Assistant's [integration development guidelines](https://developers.home-assistant.io/docs/creating_integration_manifest)
- Run validations before submitting:
  ```bash
  # HACS validation
  docker run --rm -v $(pwd):/github/workspace ghcr.io/hacs/action:main

  # Hassfest validation
  docker run --rm -v $(pwd):/github/workspace homeassistant/ci-hassfest
  ```

## Pull Request Process

1. Target the `beta` branch (not `main`)
2. Ensure HACS and Hassfest checks pass
3. Update translations if adding new strings
4. Describe your changes clearly
5. One feature/fix per PR

## Branch Strategy

- `beta` - Development branch, PRs target here
- `main` - Stable releases only, merged from beta after testing

## Translations

If adding or modifying user-facing strings:
1. Update `custom_components/openai_tts/strings.json` (English source)
2. Update corresponding keys in `custom_components/openai_tts/translations/` for:
   - `cs.json` (Czech)
   - `de.json` (German)
   - `el.json` (Greek)

## Questions?

Feel free to open a [discussion](https://github.com/sfortis/openai_tts/discussions) or issue if you have questions.
