# Groq TTS Custom Component for Home Assistant

The Groq TTS component for Home Assistant makes it possible to use the Groq API to generate spoken audio from text. This can be used in automations, assistants, scripts, or any other component that supports TTS within Home Assistant. 

## Features  

- **Text-to-Speech** conversion using Groq's API  
- **Support for multiple languages and voices** – No special configuration needed; the AI model auto-recognizes the language.  
- **Customizable speech model** – [Check supported voices and models](https://console.groq.com/docs/text-to-speech).  
- **Integration with Home Assistant** – Works seamlessly with assistants, automations, and scripts.  
- **Custom endpoint option** – Allows you to use your own Groq API endpoint.
- **Chime option** – Useful for announcements on speakers. *(See Devices → Groq TTS → CONFIGURE button)*
- **User-configurable chime sounds** – Drop your own chime sound into  `config/custom_components/groq_tts/chime` folder (MP3).
- **Audio normalization option** – Uses more CPU but improves audio clarity on mobile phones and small speakers. *(See Devices → Groq TTS → CONFIGURE button)*
- **Dynamic voice discovery** – Available models and voices are fetched from the Groq API during setup.
- **Per-call options** – Voice and normalization can be changed when calling `tts.speak`.
- **In-memory caching** – Frequently used phrases are cached to reduce API calls.

The integration relies on `ffmpeg` for merging chime sounds and for optional loudness normalization. Ensure `ffmpeg` is installed on the system running Home Assistant.

### *Caution! You need a free Groq API key* ###
visit: (https://console.groq.com/)

## Supported Groq TTS Models and Voices

### Models
- `playai-tts`
- `playai-tts-arabic`

### Voices
- `Arista-PlayAI`
- `Atlas-PlayAI`
- `Basil-PlayAI`
- `Briggs-PlayAI`

> For the latest list of models and voices, see the [Groq TTS documentation](https://console.groq.com/docs/text-to-speech).

## Sample Home Assistant service

```
service: tts.speak
target:
  entity_id: tts.groq_tts_engine
data:
  cache: true
  media_player_entity_id: media_player.bedroom_speaker
  message: My speech has improved now!
  options:
    chime: true                          # Enable or disable the chime
```

## HACS installation (preferred)

1. **Install HACS** if you haven't already
2. Search for **Groq TTS**
3. Select the Integration and click **Download**
4. **Restart Home Assistant**
5. Go to Settings -> Devices & Services -> **+ Add Integration** and search for Groq TTS

## Manual installation

1. **Download** the contents of this repo
2. **Copy** the `custom_components/groq_tts` folder to your Home Assistant `custom_components` directory
```bash
    <homeassistant_config_dir>/
    └── custom_components/
        └── groq_tts/
            ├── __init__.py
            └── ... (other files)
```
3. **Restart Home Assistant**
4. Go to Settings -> Devices & Services -> **+ Add Integration** and search for Groq TTS

## Important: Accept PlayAI TTS Model Terms

Before you can use the PlayAI TTS models with Groq, you must accept the terms for the model in your Groq account:

1. Go to: [https://console.groq.com/playground?model=playai-tts](https://console.groq.com/playground?model=playai-tts)
2. Log in with your Groq account.
3. Accept the terms for the PlayAI TTS model.
4. After accepting, you can use the integration and generate speech.
