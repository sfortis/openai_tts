<div align="center">

# OpenAI TTS for Home Assistant

**Text-to-Speech component that connects Home Assistant to OpenAI's TTS API and any OpenAI-compatible backend.**

[![Release](https://img.shields.io/github/v/release/sfortis/openai_tts?logo=github)](https://github.com/sfortis/openai_tts/releases/latest)
[![Stars](https://img.shields.io/github/stars/sfortis/openai_tts?logo=github)](https://github.com/sfortis/openai_tts/stargazers)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://img.shields.io/github/actions/workflow/status/sfortis/openai_tts/validate.yml?branch=main&label=validate&logo=github-actions)](https://github.com/sfortis/openai_tts/actions/workflows/validate.yml)
![Home Assistant](https://img.shields.io/badge/HA-2025.7%2B-41BDF5?logo=home-assistant&logoColor=white)
[![License](https://img.shields.io/github/license/sfortis/openai_tts?logo=open-source-initiative&logoColor=white)](LICENSE)

<a href="https://www.buymeacoffee.com/sfortis" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="42" width="170"></a>

</div>

---

OpenAI TTS turns text into speech inside Home Assistant. It works with the official OpenAI Audio Speech API and any compatible self-hosted backend (Chatterbox, pocket-tts, LocalAI, TTS Web UI, and others). Configure one or more TTS agents per OpenAI account, target announcements at any media player, optionally prepend a chime, normalise loudness for small speakers, and have the original volume and music restored after the announcement.

## Contents

- [What's New](#whats-new-)
- [Core Features](#core-features)
- [Installation](#installation)
- [Configuration](#configuration)
- [openai_tts.say service](#openai_ttssay-service)
- [Custom backends](#custom-backends)
- [Notes](#notes)

## What's New ![NEW](https://img.shields.io/badge/-NEW-brightgreen)

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

## Core Features

- **Text-to-Speech** via OpenAI's Audio Speech API or any compatible backend.
- **Multiple TTS agents** under one or more OpenAI accounts. Each agent has its own voice, model, speed, audio format and audio-processing settings.
- **Models**: `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts` (with custom speaking-style instructions).
- **Voices**: full OpenAI catalog including `alloy`, `ash`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`, plus the `gpt-4o-mini-tts`-only voices `ballad`, `cedar`, `marin`, `verse`.
- **Audio formats**: `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` per profile.
- **Streaming playback** with HA 2025.7+ for low first-audio latency. The audio is
  played as it arrives instead of after the whole clip is written. Works with `mp3`,
  `opus`, `aac` and `pcm`; `wav` and `flac` state their length in a header before any
  audio exists, so they are always assembled in full first.
- **Sentence streaming** (per profile, off by default) for the voice assistant. Speech
  starts on the first finished sentence rather than the finished reply. Needs `mp3`
  or `pcm`, since the other formats cannot be joined end to end.
- **Chime prefix** with a user-configurable library (drop your own mp3 in `config/custom_components/openai_tts/chime`).
- **Loudness normalisation** for small speakers and mobile playback.
- **Volume restoration** to the original speaker level after the announcement.
- **Media pause and resume** during the announcement on supported platforms.
- **Sonos** announcement feature with native group handling.
- **Multi-target playback** with cast warm-up sync to keep multiple speakers aligned.
- **API health sensor** that surfaces auth, quota, rate-limit and connectivity errors.
- **Custom-endpoint support** with optional API key, custom voice text input, and `extra_payload` for backend-specific JSON parameters.
- **54 languages** available through the HA Assist pipeline.

## Installation

### HACS (recommended)

1. Open HACS in the sidebar.
2. Search for **OpenAI TTS** in *Integrations*.
3. Download the integration and restart Home Assistant.
4. Add the integration via *Settings → Devices & Services → Add Integration → OpenAI TTS*. Enter the API key (or leave empty for a custom endpoint without auth).
5. Add one or more TTS agents (sub-entries) for the voice and audio configurations you want.

### Manual

1. Copy the contents of `custom_components/openai_tts/` into `<config>/custom_components/openai_tts/`.
2. Restart Home Assistant.
3. Add the integration via *Settings → Devices & Services* as above.

## Configuration

Each integration entry stores the API credentials and endpoint. Each sub-entry (TTS agent) stores the per-profile settings:

- **Model** and **voice** (filtered by model compatibility).
- **Speed** (0.25 - 4.0).
- **Audio format** (mp3 default, others on demand).
- **Custom instructions** (gpt-4o-mini-tts only) for speaking style.
- **Extra JSON payload** for custom backends.
- **Chime**, **chime sound** and **normalise audio** as defaults that the service call can override.

> Enabling chime disables streaming for that profile, since a chime has to be attached
> to finished audio. Loudness normalisation does not: it runs on the stream for `mp3`,
> `opus`, `aac` and `pcm`.

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

## Custom backends

The integration works with any OpenAI-compatible TTS endpoint. Mistral, Groq,
Lemonfox and Kokoro have presets that fill in the endpoint and the catalogue for
you, and anything else is configured as a custom endpoint. When the URL is not
`api.openai.com`:

- The API key field becomes optional.
- The voice field accepts any backend-specific name.
- Use the **audio format** selector to negotiate around backends that reject mp3 (for example `pocket-tts` returning PCM).
- The **extra payload** field forwards backend-specific JSON parameters with the request.
- **Send the voice name** can be turned off for backends that reject the `voice` key.

## Contributing

Bug reports, backend reports and pull requests are all welcome. Pull
requests target the `dev` branch, and for anything larger than a small
fix it is worth opening an issue first so the shape can be agreed before
you write it. [CONTRIBUTING.md](CONTRIBUTING.md) has the details.

If you use a backend that behaves differently from the others, saying so
in an issue is useful on its own, even without a patch.

## Notes

> *For OpenAI, an API key with available balance is required.* Pricing: <https://platform.openai.com/docs/pricing>
