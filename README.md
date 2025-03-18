# openai_tts
# OpenAI TTS Custom Component for Home Assistant

This custom component integrates OpenAI's Text-to-Speech (TTS) service with Home Assistant, allowing users to convert text into spoken audio. The service supports various languages and voices, offering customizable options such as voice model.

## Description

The OpenAI TTS component for Home Assistant makes it possible to use the OpenAI API to generate spoken audio from text. This can be used in automations, assistants, scripts, or any other component that supports TTS within Home Assistant. 

## Features  

- 🗣️ **Text-to-Speech** conversion using OpenAI's API  
- 🌍 **Support for multiple languages and voices** No special configuration is needed. The AI model will auto-recognize the language.
- ⭐🔔 **(New!) Chime option** – Useful for announcements on speakers. (See Devices --> OpenAI TTS --> CONNFIGURE button)
- ⭐🔔 **(New!) User configurable chime sounds** – Drop your own chime sound into config/custom_components/openai_tts/chime folder (mp3).
- ⭐🎛️ **(New!) Audio normalization option** – Uses more CPU but provides better audible sound on mobile phones and small speakers. (See Devices --> OpenAI TTS --> CONNFIGURE button)
- 🎙️ **Customizable speech model** ([Check supported voices and models](https://platform.openai.com/docs/guides/text-to-speech))  
- 🏡 **Integration with Home Assistant** – Works with assistant, automations, and scripts.  


### *Caution! You need an OpenAI API key and some balance available in your OpenAI account!* ###
visit: (https://platform.openai.com/docs/pricing)

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
