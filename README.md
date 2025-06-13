# OpenAI TTS Custom Component for Home Assistant

The OpenAI TTS component for Home Assistant makes it possible to use the OpenAI API to generate spoken audio from text. This can be used in automations, assistants, scripts, or any other component that supports TTS within Home Assistant. 

## Features  

- **Text-to-Speech** conversion using OpenAI's API  
- **Support for multiple languages and voices** – No special configuration needed; the AI model auto-recognizes the language.  
- **Customizable speech model** – [Check supported voices and models](https://platform.openai.com/docs/guides/text-to-speech).  
- **Integration with Home Assistant** – Works seamlessly with assistants, automations, and scripts.  
- **Custom endpoint option** – Allows you to use your own OpenAI compatible API endpoint.
- **Chime option** – Useful for announcements on speakers. *(See Devices → OpenAI TTS → CONFIGURE button)*
- **User-configurable chime sounds** – Drop your own chime sound into  `config/custom_components/openai_tts/chime` folder (MP3).
- **Audio normalization option** – Uses more CPU but improves audio clarity on mobile phones and small speakers.
- ⭐(New!) **Support for new gpt-4o-mini-tts model** – A fast and powerful language model (note: `gpt-4o-mini-tts` might be a custom model name; official OpenAI models are typically `tts-1`, `tts-1-hd`).
- ⭐(New!) **Text-to-Speech Instructions option** – Instruct the text-to-speech model to speak in a specific way (model support for this varies). [OpenAI new generation audio models](https://openai.com/index/introducing-our-next-generation-audio-models/)
- **Dual Engine Support**: Choose between OpenAI's official API (or a compatible proxy) and a local [Kokoro FastAPI](https://github.com/remsky/Kokoro-FastAPI) instance.
- **Efficient Streaming**: Internally uses streaming from the API for potentially faster and more responsive audio generation, especially for longer texts.



### *Caution! You need an OpenAI API key and some balance available in your OpenAI account if using the official OpenAI service!* ###
For pricing, visit: (https://platform.openai.com/docs/pricing)

Using Kokoro FastAPI as a local engine can avoid OpenAI API costs.

## YouTube sample video (its not a tutorial!)

[![OpenAI TTS Demo](https://img.youtube.com/vi/oeeypI_X0qs/0.jpg)](https://www.youtube.com/watch?v=oeeypI_X0qs)



## Sample Home Assistant service

```
service: tts.speak
target:
  entity_id: tts.openai_nova_engine
data:
  cache: true
  media_player_entity_id: media_player.bedroom_speaker
  message: My speech has improved now!
  options:
    chime: true                          # Enable or disable the chime
    chime_sound: signal2                 # Name of the file in the chime directory, without .mp3 extension
    instructions: "Speak like a pirate"  # Instructions for text-to-speach model on how to speak 
```

## HACS installation ( *preferred!* )

1. Go to the sidebar HACS menu

2. Click on the 3-dot overflow menu in the upper right and select the "Custom Repositories" item.

3. Copy/paste https://github.com/sfortis/openai_tts into the "Repository" textbox and select "Integration" for the category entry.

4. Click on "Add" to add the custom repository.

5. You can then click on the "OpenAI TTS Speech Services" repository entry and download it. Restart Home Assistant to apply the component.

## Configuration

After installation (either via HACS or manually), add the OpenAI TTS integration through the Home Assistant UI:

1.  Go to **Settings → Devices & Services**.
2.  Click **+ Add Integration**.
3.  Search for "OpenAI TTS" and select it.
4.  Follow the configuration steps in the dialog.

You can configure the following options during setup:

*   **TTS Engine**: Choose the engine to use.
    *   `OpenAI (Official or compatible proxy)`: Uses the official OpenAI API or a proxy that implements the same API.
    *   `Kokoro FastAPI`: Uses a local instance of [Kokoro FastAPI](https://github.com/remsky/Kokoro-FastAPI). This is a great option for local processing and avoiding cloud costs.

*   **OpenAI API Key** (`api_key`):
    *   Required if you select the "OpenAI" engine and are using the official API.
    *   Can be left blank if your OpenAI-compatible proxy does not require an API key, or if you select the "Kokoro FastAPI" engine.

*   **OpenAI-compatible API URL** (`url`):
    *   Only used if "OpenAI" engine is selected.
    *   Defaults to `https://api.openai.com/v1/audio/speech` for the official OpenAI service.
    *   Change this if you are using an OpenAI-compatible proxy.

*   **Kokoro FastAPI URL** (`kokoro_url`):
    *   Only used if "Kokoro FastAPI" engine is selected.
    *   Enter the full local URL of your Kokoro FastAPI TTS endpoint (e.g., `http://localhost:8002/tts`).

*   **Model** (`model`):
    *   Select the TTS model (e.g., `tts-1`, `tts-1-hd`).
    *   Availability might depend on the chosen engine and your specific endpoint. The list provides common OpenAI models, but you can enter a custom model name if your backend supports it.

*   **Voice** (`voice`):
    *   Select the desired voice (e.g., `alloy`, `echo`, `shimmer`).
    *   Availability might depend on the chosen engine and your specific endpoint. The list provides common OpenAI voices, but you can enter a custom voice name.

*   **Speed** (`speed`):
    *   Adjust the speech speed (range: 0.25 to 4.0, where 1.0 is the default).

Multiple instances of the integration can be configured, for example, to use different engines, models, or voices simultaneously.

### Modifying Options After Setup

Some options can be modified after the initial setup by navigating to the integration's card under **Settings → Devices & Services**, clicking the three dots, and selecting "Configure" (or by clicking the "CONFIGURE" button on the device page from the "OpenAI TTS" entry). These include:
- Model
- Voice
- Speed
- Instructions for the TTS model
- Chime sound enablement and selection
- Audio normalization

Note: To change the TTS Engine, API Key, or main URLs (OpenAI URL, Kokoro URL), you will need to remove and re-add the integration.

## Manual installation

1. Ensure you have a `custom_components` folder within your Home Assistant configuration directory.

2. Inside the `custom_components` folder, create a new folder named `openai_tts`.

3. Place the repo files inside `openai_tts` folder.

4. Restart Home Assistant

5. Add the integration via UI, provide API key and select required model and voice. Multiple instances may be configured.
