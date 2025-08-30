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
- **Dynamic model discovery** – Available models are fetched from the Groq API during setup; voices are selected from a built-in list.
- **Per-call options** – Voice and normalization can be changed when calling `tts.speak`.
- **In-memory caching** – Frequently used phrases are cached to reduce API calls. Cache size is configurable in options.

The integration relies on `ffmpeg` for merging chime sounds and for optional loudness normalization. Ensure `ffmpeg` is installed on the system running Home Assistant.

### *Caution! You need a free Groq API key* ###
visit: (https://console.groq.com/)

## Supported Groq TTS Models and Voices

### Models
- `playai-tts`
- `playai-tts-arabic`

### Voices
The integration provides the following built-in voice options from Groq:

- `Ahmad-PlayAI`
- `Amira-PlayAI`
- `Arista-PlayAI`
- `Atlas-PlayAI`
- `Basil-PlayAI`
- `Briggs-PlayAI`
- `Calum-PlayAI`
- `Celeste-PlayAI`
- `Cheyenne-PlayAI`
- `Chip-PlayAI`
- `Cillian-PlayAI`
- `Deedee-PlayAI`
- `Fritz-PlayAI`
- `Gail-PlayAI`
- `Indigo-PlayAI`
- `Khalid-PlayAI`
- `Mamaw-PlayAI`
- `Mason-PlayAI`
- `Mikail-PlayAI`
- `Mitch-PlayAI`
- `Nasser-PlayAI`
- `Quinn-PlayAI`
- `Thunder-PlayAI`

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
    normalize_audio: false               # Enable loudness normalization
    voice: Arista-PlayAI                 # Override voice for this call
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

## Options & Behavior

- Chime: Plays a short MP3 before speech. Choose a sound (or add your own to `custom_components/groq_tts/chime/`).
- Normalize Audio: Applies ffmpeg loudness normalization. Uses more CPU.
- Voice: Override the default voice per entity or call.
- Audio Cache Size: LRU cache for audio responses (default: 256). Set to 0 to disable caching.

Changing options takes effect on the next TTS operation. If you change options frequently, consider reloading the integration to ensure all options propagate.

## Reauthentication

If your Groq API key expires or is revoked, the integration will prompt you to reauthenticate. When a 401/403 is received from the API, Home Assistant starts a reauth flow. You’ll be asked to enter a new Groq API key. The integration updates the saved key and reloads automatically.

Where to find it:
- Settings → Devices & Services → Groq TTS → Reconfigure (or follow the on‑screen prompt)

## Diagnostics

For support, you can download diagnostics for the integration from the device/integration page. Diagnostics include a redacted view of your configuration (API key removed) and useful runtime options (endpoint, model, voice, chime, normalization, cache size). Share diagnostics when reporting bugs to speed up triage.

## Duplicate Prevention

The integration prevents duplicate configuration entries by generating a deterministic unique ID from the combination of the TTS URL and model. If you attempt to add the same pair again, the flow will abort.

## Errors & Troubleshooting

- Invalid URL: The configuration form validates the URL for scheme and host.
- Required Fields: The form marks missing fields as required.
- Unknown errors: Reported as "Unexpected error — please try again".
- Non-audio API responses: The integration rejects non-audio content from the TTS API and logs details.
- ffmpeg errors: If ffmpeg fails (e.g., missing binary or bad filter), the TTS call returns no audio and logs an error.

Ensure `ffmpeg` is installed and available in PATH on the Home Assistant host if you enable chime or normalization.
