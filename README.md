# OpenAI TTS Custom Component for Home Assistant

<a href="https://www.buymeacoffee.com/sfortis" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="50" width="200"></a>

The OpenAI TTS component for Home Assistant makes it possible to use the OpenAI API to generate spoken audio from text. This can be used in automations, assistants, scripts, or any other component that supports TTS within Home Assistant. 

## Features  

- **Text-to-Speech** conversion using OpenAI's API  
- **Support for multiple languages and voices** – No special configuration needed; the AI model auto-recognizes the language.  
- **Customizable speech model** – [Check supported voices and models](https://platform.openai.com/docs/guides/text-to-speech).  
- **Integration with Home Assistant** – Works seamlessly with assistants, automations, and scripts.  
- **Custom endpoint option** – Allows you to use your own OpenAI compatible API endpoint.
- **Chime option** – Useful for announcements on speakers. *(See Devices → OpenAI TTS → CONFIGURE button)*
- **User-configurable chime sounds** – Drop your own chime sound into  `config/custom_components/openai_tts/chime` folder (MP3).
- **Audio normalization option** – Uses more CPU but improves audio clarity on mobile phones and small speakers. *(See Devices → OpenAI TTS → CONFIGURE button)*
- **Support for new gpt-4o-mini-tts model** – A fast and powerful language model.
- **Text-to-Speech Instructions option** – Instruct the text-to-speech model to speak in a specific way (only works with newest gpt-4o-mini-tts model). [OpenAI new generation audio models](https://openai.com/
index/introducing-our-next-generation-audio-models/)
- **Volume Restoration** – Automatically restores speaker volumes to their original levels after TTS announcements.
- **Media Pause/Resume** – Pauses currently playing media during announcements and resumes afterward (works with Sonos speakers).
- **Sonos Integration** – Automatically groups and ungroups Sonos speakers for synchronized announcements.
- **New `openai_tts.say` Service** – New service parameters including voice, instructions, etc.
- **Precise Audio Duration Detection** – Improved timing for TTS playback with better synchronization.
- **Performance Optimizations** – Improved audio processing for faster TTS responses.

### ⭐ New Features in 3.7
- **WAV audio support** - Custom TTS backends that return WAV audio are now supported (auto-converted to MP3).
- **Custom voice names** - Use any voice name for custom TTS backends (not limited to OpenAI voices).
- **Extra payload support** - Pass custom JSON parameters to TTS backends via the `extra_payload` field in service calls or in the TTS agent config.

### ⭐ New Features in 3.6
- **Optional API key for custom endpoints** – API key is only required for OpenAI. Local/custom TTS servers can work without authentication.
- **Multi-language support** – 54 languages now available in HA Assist pipeline settings.

### ⭐ New Features in 3.5
- **TTS Streaming** – Reduced latency with streaming support (HA 2025.7+). *Note: Streaming is disabled when chime or audio normalization is enabled.*
- **Reconfigure** – Allows changing the API key and URL endpoint without recreating the entity.
- **Sub-entries support** – Support for sub-entries, HA 2025.7 required.
- **Volume restoration** – Improved timing and logic for volume restoration.
- **Diagnostics** – Added diagnostics support for troubleshooting.

### *Caution! For OpenAI, you need an API key and some balance available in your OpenAI account!* ###
visit: (https://platform.openai.com/docs/pricing)

*Note: API key is optional when using custom endpoints (local TTS servers).*

## ⭐New TTS say action

```yaml
service: openai_tts.say
target:
  entity_id: media_player.living_room_speaker
  # OR target by area
  # area_id: living_room
  # OR target by device
  # device_id: 12345abcde
data:
  tts_entity: tts.openai_tts_tts_1
  message: "This is an announcement with volume control and pause/resume!"
  volume: 0.6  # Temporarily set volume for announcement (0.0-1.0)
  pause_playback: true  # Pause any music playing during the announcement
  chime: true  # Add a chime sound before the announcement
  normalize_audio: true  # Normalize audio (for small speakers)
  voice: my_custom_voice  # Use any voice name (for custom TTS backends)
  extra_payload: '{"temperature": 0.8}'  # Custom JSON for TTS backend
```

## HACS installation ( *preferred!* )

1. Go to the sidebar HACS menu

2. Search for "OpenAI TTS" in the integrations

3. Click on the integration and download it. Restart Home Assistant to apply the component.

4. Add the integration via UI, provide API key and select required model and voice. Multiple instances may be configured.

## Manual installation

1. Ensure you have a `custom_components` folder within your Home Assistant configuration directory.

2. Inside the `custom_components` folder, create a new folder named `openai_tts`.

3. Place the repo files inside `openai_tts` folder.

4. Restart Home Assistant

5. Add the integration via UI, provide API key and select required model and voice. Multiple instances may be configured.
