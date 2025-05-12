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
- **Audio normalization option** – Uses more CPU but improves audio clarity on mobile phones and small speakers. *(See Devices → OpenAI TTS → CONFIGURE button)*
- **Support for new gpt-4o-mini-tts model** – A fast and powerful language model.
- **Text-to-Speech Instructions option** – Instruct the text-to-speech model to speak in a specific way (only works with newest gpt-4o-mini-tts model). [OpenAI new generation audio models](https://openai.com/index/introducing-our-next-generation-audio-models/)

### ⭐ New Features in v0.3.4 Beta

- **Volume Restoration** – Automatically restores speaker volumes to their original levels after TTS announcements.
- **Media Pause/Resume** – Pauses currently playing media during announcements and resumes afterward (works with Sonos speakers).
- **Sonos Integration** – Automatically groups and ungroups Sonos speakers for synchronized announcements.
- **New `openai_tts.say` Service** – New service parameters including volume control and pause/resume options.
- **Precise Audio Duration Detection** – Improved timing for TTS playback with better synchronization.
- **Media Player Targeting** – Target speakers using areas, devices, or entity IDs for more flexible automations.
- **Performance Optimizations** – Improved audio processing for faster TTS responses.



### *Caution! You need an OpenAI API key and some balance available in your OpenAI account!* ###
visit: (https://platform.openai.com/docs/pricing)

## YouTube sample video (its not a tutorial!)

[![OpenAI TTS Demo](https://img.youtube.com/vi/oeeypI_X0qs/0.jpg)](https://www.youtube.com/watch?v=oeeypI_X0qs)

## ⭐New TTS say action

```
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
```

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
    instructions: "Speak like a pirate"  # Instructions for text-to-speach model on how to speak 
```

## HACS installation ( *preferred!* ) 

1. Go to the sidebar HACS menu 

2. Click on the 3-dot overflow menu in the upper right and select the "Custom Repositories" item.

3. Copy/paste https://github.com/sfortis/openai_tts into the "Repository" textbox and select "Integration" for the category entry.

4. Click on "Add" to add the custom repository.

5. You can then click on the "OpenAI TTS Speech Services" repository entry and download it. Restart Home Assistant to apply the component.

6. Add the integration via UI, provide API key and select required model and voice. Multiple instances may be configured.

## Manual installation

1. Ensure you have a `custom_components` folder within your Home Assistant configuration directory.

2. Inside the `custom_components` folder, create a new folder named `openai_tts`.

3. Place the repo files inside `openai_tts` folder.

4. Restart Home Assistant

5. Add the integration via UI, provide API key and select required model and voice. Multiple instances may be configured.
