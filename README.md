<div align="center">

# OpenAI TTS

**Text-to-speech for Home Assistant from OpenAI, Mistral, Groq, Lemonfox, Kokoro, Chatterbox or any server that implements the OpenAI speech API.**

[![Release](https://img.shields.io/github/v/release/sfortis/openai_tts?logo=github)](https://github.com/sfortis/openai_tts/releases/latest)
[![Stars](https://img.shields.io/github/stars/sfortis/openai_tts?logo=github)](https://github.com/sfortis/openai_tts/stargazers)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://img.shields.io/github/actions/workflow/status/sfortis/openai_tts/validate.yml?branch=main&label=validate&logo=github-actions)](https://github.com/sfortis/openai_tts/actions/workflows/validate.yml)
![Home Assistant](https://img.shields.io/badge/HA-2026.9.1%2B-41BDF5?logo=home-assistant&logoColor=white)
[![License](https://img.shields.io/github/license/sfortis/openai_tts?logo=open-source-initiative&logoColor=white)](LICENSE)

<a href="https://www.buymeacoffee.com/sfortis" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="42" width="170"></a>

</div>

---

OpenAI TTS turns text into speech inside Home Assistant. It began as a bridge to OpenAI's speech API, and it now works with any cloud provider or self-hosted server that offers the same API. Presets for the common providers fill in the endpoint, the models and the voices, so a profile cannot be saved with settings the backend will reject. Announcements can target any media player, with an optional chime, loudness correction for small speakers, and the original volume and music restored afterwards.

## Contents

- [Supported Providers](#supported-providers)
- [What's New](#whats-new-)
- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Using It With the Voice Assistant](#using-it-with-the-voice-assistant)
- [openai_tts.say service](#openai_ttssay-service)
- [Choosing the Target Speaker](#choosing-the-target-speaker)
- [openai_tts.set_api_key action](#openai_ttsset_api_key-action)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [API Keys and Costs](#api-keys-and-costs)

## Supported Providers

Each integration entry starts from a preset. The preset sets the endpoint, the models the provider offers and where the voice list comes from, and it hides the settings that provider rejects.

| Provider | Where it runs | Voices in the picker | API key |
|---|---|---|---|
| OpenAI | Cloud | OpenAI's voices, filtered by model | Required |
| Mistral Voxtral | Cloud | The voices on your account, read live | Required |
| Groq (Orpheus) | Cloud | The voices Orpheus offers | Required |
| Lemonfox.ai (Kokoro) | Cloud | The voices Lemonfox offers | Required |
| OpenRouter (3.10 beta) | Cloud | The voices each speech model accepts, read live | Required |
| Kokoro-FastAPI | Self-hosted | The voice packs installed on the server, read live | Optional |
| Chatterbox | Self-hosted | The voices on the server, read live | Optional |
| Custom | Cloud or self-hosted | Read live when the server lists its voices, typed otherwise | Optional |

The **Custom** preset covers every other server that implements the OpenAI speech endpoint, such as LocalAI, pocket-tts or TTS Web UI. On a custom endpoint the voice field accepts any name the backend understands. The **audio format** selector helps with a backend that rejects mp3, and the **extra payload** field sends backend-specific JSON parameters with each request.

The integration only uses the OpenAI speech API. A provider that offers speech through a different API of its own is not supported.

On OpenAI the models are `tts-1`, `tts-1-hd` and `gpt-4o-mini-tts`, and `gpt-4o-mini-tts` also takes speaking-style instructions. The voices are `alloy`, `ash`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage` and `shimmer`, and `gpt-4o-mini-tts` adds `ballad`, `cedar`, `marin` and `verse`.

## What's New ![NEW](https://img.shields.io/badge/-NEW-brightgreen)

### Version 3.10 (beta)

Version 3.10 is a beta and needs Home Assistant 2026.9.1 or later. To try it, open the integration in HACS, choose **Redownload** and turn on **Show beta versions**.

- **OpenRouter preset**: the model picker lists the speech models OpenRouter offers, and the voice picker lists the voices of the chosen model.
- **Gain** per profile, from -12 to +12 dB, to make a quiet voice louder or a loud one softer. It works with or without loudness normalisation, and a limiter stops a boost from clipping.
- **Announcement volume on Sonos and Music Assistant**: with a volume override, these speakers play the announcement at the requested level and duck their own music, instead of being paused and restored. A Music Assistant speaker that belongs to a group stays in its group.
- **Typed voices** on Kokoro and OpenRouter: the voice picker also accepts a name that is not in the list, such as the Kokoro voice mix `am_michael(1)+am_eric(2)`.
- **One failing speaker** in a group no longer stops the others. The rest play to the end, and the action then reports which speaker failed.

### Version 3.9

Version 3.9 is mostly about backends other than OpenAI, and about what a speaker
does while an announcement is playing.

- **Provider presets**: pick OpenAI, Mistral, Groq, Lemonfox, Kokoro, Chatterbox
  or a custom endpoint when you create an entry. The preset fills in the URL, the models and
  the voices the provider publishes, so a profile cannot be saved with a
  combination the backend will reject.
- **Voices from the provider**: the voice picker lists what the backend reports
  rather than OpenAI's catalogue, both in the profile and in the Assist pipeline.
- **Sentence streaming** for the voice assistant, off by default per profile.
  Speech starts on the first finished sentence instead of the finished reply.
- **Send the voice name** can be turned off per profile, for backends that reject
  the field. It is only offered when the endpoint is not OpenAI.
- **Loudness correction while streaming**, on by default. Correction no longer
  forces the whole clip to be produced before playback starts.
- **Speakers that support announcements** duck and resume the music themselves
  instead of being paused and restored by this integration.
- **Repairs** are raised when a voice disappears at the provider or an API key is
  rejected, instead of every call failing with no explanation.
- **`response_variable`** is supported on `openai_tts.say`.
- **Stream the audio** can be turned off per profile, for a backend that answers
  a streamed read with audio that will not decode while the same request read in
  one go is fine.
- **`openai_tts.set_api_key`** is an admin action that replaces the key on an
  entry, so an automation can rotate a short lived token without anyone opening
  the settings. The key is checked against the endpoint before it is stored.

[WHATSNEW.md](WHATSNEW.md) lists every change, including the fixes.

## Features

### Speech

- Several TTS agents under one entry, each with its own model, voice, speed, audio format and audio processing.
- Audio in `mp3`, `opus`, `aac`, `flac`, `wav` or `pcm`, chosen per profile.
- Streaming playback, so audio plays as it arrives instead of after the whole clip is written. Streaming works with `mp3`, `opus`, `aac` and `pcm`. A `wav` or `flac` file states its length in a header before any audio exists, so those two formats are always assembled in full first.
- Sentence streaming for the voice assistant, off by default and set per profile. Speech starts on the first finished sentence rather than on the finished reply. It needs `mp3` or `pcm`, because the other formats cannot be joined end to end.
- Loudness normalisation for small speakers and mobile playback, on by default and applied while the audio streams.
- A gain per profile, from -12 to +12 dB, with a limiter so a boost cannot clip (3.10 beta).
- A chime before the announcement, from a library you can extend by dropping your own mp3 files in `config/custom_components/openai_tts/chime`.
- 54 languages through the Home Assistant Assist pipeline.

### Announcements

- Announcements on any media player, targeted by entity, device or area.
- The speaker volume is restored to its original level after the announcement.
- Music is paused and resumed on players that need it, and players that support announcements duck their own music instead.
- Sonos speakers duck their own music during an announcement.
- With a volume override, Sonos and Music Assistant speakers play the announcement at that level and duck their own music (3.10 beta).
- Several Cast speakers are warmed up together so they start in sync.

### Monitoring

- An API status sensor reports authentication, quota, rate limit and connectivity errors.
- A repair is raised when a profile's voice no longer exists at the provider.
- A rejected API key starts Home Assistant's re-authentication prompt, so the key can be replaced without removing the entry.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sfortis&repository=openai_tts&category=integration)

1. Open HACS in the sidebar, or use the button above.
2. Search for **OpenAI TTS**.
3. Download the integration and restart Home Assistant.
4. Add the integration via *Settings → Devices & Services → Add Integration → OpenAI TTS*, and pick the provider preset. Enter the API key if the provider needs one. A self-hosted server without authentication can leave it empty.
5. Add one or more TTS agents (sub-entries) for the voice and audio configurations you want.

### Manual

1. Copy the contents of `custom_components/openai_tts/` into `<config>/custom_components/openai_tts/`.
2. Restart Home Assistant.
3. Add the integration via *Settings → Devices & Services* as above.

## Configuration

Each integration entry stores the provider, the endpoint and the API key. Add one entry per provider or per account. Each sub-entry (TTS agent) stores the per-profile settings:

- **Model** and **voice** (filtered by model compatibility).
- **Speed** (0.25 - 4.0).
- **Audio format** (mp3 default, others on demand).
- **Custom instructions** (gpt-4o-mini-tts only) for speaking style.
- **Extra JSON payload** for custom backends.
- **Chime**, **chime sound** and **normalise audio** as defaults that the service call can override.
- **Gain** (3.10 beta), from -12 to +12 dB, applied with or without normalisation.
- **Sentence streaming** (off by default) to start speaking on the first finished
  sentence of an assistant reply instead of the finished reply.
- **Stream the audio** (on by default). Turn it off for a backend whose streamed
  response will not decode while the same request read in one go is fine.
- **Send the voice name** (on by default). Turn it off for a backend that rejects
  the `voice` field, such as audio.cpp serving Chatterbox or VoxCPM2.

> Enabling chime disables streaming for that profile, since a chime has to be attached
> to finished audio. Loudness normalisation does not: it runs on the stream for `mp3`,
> `opus`, `aac` and `pcm`.

## Using It With the Voice Assistant

Every TTS agent is a regular Home Assistant text-to-speech entity. To use one for spoken replies, open *Settings → Voice assistants*, pick the assistant and choose the agent under **Text-to-speech**. Sentence streaming in the profile starts the reply on its first finished sentence.

The entities also work with Home Assistant's own `tts.speak` action. That action plays at the speaker's current volume, and on a speaker without its own announcement feature the music it replaces does not come back. Setting the volume and bringing the music back are what `openai_tts.say` adds.

```yaml
action: tts.speak
target:
  entity_id: tts.openai_tts_living_room
data:
  media_player_entity_id: media_player.kitchen
  message: "The washing machine has finished"
```

## `openai_tts.say` service

Targets media players directly, with per-call overrides for voice, speed,
instructions, chime, normalise, volume and announcement behaviour. Only
`tts_entity` and `message` are required, and anything left out falls back to the
profile.

`pause_playback` is still accepted as an older name for `announce` so existing
automations keep working, but new ones should use `announce`.

```yaml
action: openai_tts.say
target:
  entity_id: media_player.living_room_speaker
  # area_id: living_room
  # device_id: 12345abcde
data:
  tts_entity: tts.openai_tts_living_room
  message: "Dinner is ready"
  volume: 0.6              # snapshot and restore the speaker volume
  announce: true           # let the speaker duck its own music where it can
  chime: true              # prepend the configured chime
  chime_sound: threetone.mp3
  normalize_audio: true    # loudness-normalise for small speakers
  voice: nova
  speed: 1.0
  language: en
  instructions: "Say it warmly"
  extra_payload: '{"temperature": 0.8}'
```

With `response_variable` the action reports the outcome instead of raising an error. The response is `success: true`, or `success: false` together with an `error` message, so an automation can send a notification when an announcement did not play.

## Choosing the Target Speaker

Some speakers appear in Home Assistant more than once. A speaker that Music Assistant plays to usually has a Music Assistant entity and a second entity from its own integration, such as ESPHome or Cast. Target the Music Assistant entity. The pause and resume commands sent to the other entity reach Music Assistant indirectly, and on a speaker that belongs to a Music Assistant group they stop the whole group and restart the speaker on its own queue.

On 3.9 a volume override on a grouped Music Assistant speaker stops the group in the same way. From 3.10 (beta) the level is handed to Music Assistant, which takes the speaker out of the group for the announcement and puts it back afterwards while the group keeps playing.

## `openai_tts.set_api_key` action

Replaces the stored API key on an entry, so an automation can rotate a short
lived token without anyone opening the settings. It needs an administrator, and
it targets either `config_entry_id` or `tts_entity`, not both.

The key is checked against the endpoint before it is stored, so a token the
endpoint refuses leaves the working one in place. Add `validate: false` to store
it without checking, which is what a backend that rejects the probe request
needs. The entry reloads straight away, so no restart is required.

```yaml
action: openai_tts.set_api_key
data:
  config_entry_id: 01ABCDEF...
  api_key: "{{ token.content.access_token }}"
  # validate: false
```

With `response_variable` the call reports what it did: `changed` is false when the
key was already the one stored, and `reloading` says whether the running entity
picked it up or will do so at the next load.

## Known Limitations

- Music Assistant keeps every announcement within a volume range set for each player, 15 to 75 percent by default. A requested level outside that range is changed by Music Assistant. From 3.10 (beta) the integration logs a warning when that happens.
- The `announce` field changes nothing on Sonos and Music Assistant speakers, because they always announce on their own.
- A chime turns streaming off for that profile, because the chime has to be joined to finished audio.
- Groq accepts only `wav`, and OpenRouter accepts only `mp3` and `pcm`. The audio format selector offers only what the provider accepts.

## Troubleshooting

Turn on debug logging for the integration to see each request to the provider, which speakers are paused and how the volume is set and restored:

```yaml
logger:
  logs:
    custom_components.openai_tts: debug
```

Check the API status sensor of the entry. Its state names the last problem, such as `auth_failed` or `quota_exceeded`, and its attributes hold the last error message and when it happened.

When you open an issue, attach the diagnostics file from *Settings → Devices & Services → OpenAI TTS → ⋮ → Download diagnostics*. The API key is removed from that file.

## Contributing

Bug reports, backend reports and pull requests are all welcome. Pull
requests target the `dev` branch, and for anything larger than a small
fix it is worth opening an issue first so the shape can be agreed before
you write it. [CONTRIBUTING.md](CONTRIBUTING.md) has the details.

If you use a backend that behaves differently from the others, saying so
in an issue is useful on its own, even without a patch.

## API Keys and Costs

Cloud providers need an API key on an account with available balance or credits. OpenAI's pricing is at <https://platform.openai.com/docs/pricing>.
