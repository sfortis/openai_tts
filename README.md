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

### *Caution! You need a Groq API key and some balance available in your Groq account!* ###
visit: (https://console.groq.com/)

## Supported Groq TTS Models and Voices

### Models
- `groq-tts-1`

### Voices
- `emma` (English, Female)
- `liam` (English, Male)
- `olivia` (English, Female)
- `noah` (English, Male)

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

## HACS installation (preferred!)

1. Go to the sidebar HACS menu
2. Click on the 3-dot overflow menu in the upper right and select the "Custom Repositories" item.
3. Copy/paste [https://github.com/sfortis/groq_tts](https://github.com/sfortis/groq_tts) into the "Repository" textbox and select "Integration" for the category entry.
4. Click on "Add" to add the custom repository.
5. You can then click on the "Groq TTS Speech Services" repository entry and download it. Restart Home Assistant to apply the component.
6. Add the integration via UI, provide API key and select required model and voice. Multiple instances may be configured.

## Manual installation

1. Ensure you have a `custom_components` folder within your Home Assistant configuration directory.
2. Inside the `custom_components` folder, create a new folder named `groq_tts`.
3. Place the repo files inside `groq_tts` folder.
4. Restart Home Assistant
5. Add the integration via UI, provide API key and select required model and voice. Multiple instances may be configured.

## Important: Accept PlayAI TTS Model Terms

Before you can use the PlayAI TTS models with Groq, you must accept the terms for the model in your Groq account:

1. Go to: [https://console.groq.com/playground?model=playai-tts](https://console.groq.com/playground?model=playai-tts)
2. Log in with your Groq account.
3. Accept the terms for the PlayAI TTS model.
4. After accepting, you can use the integration and generate speech.
