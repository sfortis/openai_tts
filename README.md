# openai_tts
# OpenAI TTS Custom Component for Home Assistant

This custom component integrates OpenAI's Text-to-Speech (TTS) service with Home Assistant, allowing users to convert text into spoken audio. The service supports various languages and voices, offering customizable options such as voice model and speed.

## Description

The OpenAI TTS component for Home Assistant makes it possible to use the OpenAI API to generate spoken audio from text. This can be used in automations, assistants, scripts, or any other component that supports TTS within Home Assistant. *You need an openAI API key.*

## Features

- Text-to-Speech conversion using OpenAI's API
- Support for multiple languages and voices
- Customizable speech model and speed (check https://platform.openai.com/docs/guides/text-to-speech for supported voices and models)
- Integration with Home Assistant's assistant, automations and scripts

## Sample

  [https://www.youtube.com/watch?v=oeeypI_X0qs](https://www.youtube.com/shorts/otTe6-YkQjI)

## Installation Instructions

1. Ensure you have a `custom_components` folder within your Home Assistant configuration directory.

2. Inside the `custom_components` folder, create a new folder named `openai_tts`.

3. Place the repo files inside `openai_tts` folder.

4. Restart Home Assistant

5. Configure the component in your `configuration.yaml` file:

```yaml
tts:
  - platform: openai_tts
    api_key: "your_api_key_here"
    # Optional parameters:
    language: "en-US"
    model: "tts-1"
    voice: "shimmer"
    speed: "1"
