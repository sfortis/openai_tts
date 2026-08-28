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

- **2-step config flow**: profile setup is split into model first, then voice and audio. Voice picker filters by the chosen model so incompatible voices (e.g. `marin` on `tts-1`) cannot be saved.
- **Audio format selector**: pick `mp3`, `opus`, `aac`, `flac`, `wav` or `pcm` per profile. Requested from OpenAI (or any compatible backend) and delivered end-to-end without a forced mp3 round-trip.
- **New voice catalog**: `marin`, `cedar`, `ballad`, `verse` (gpt-4o-mini-tts only) with model-compatibility validation.
- **Format-aware audio pipeline**: chimes are transcoded on demand to match the TTS codec; chime-only requests skip the TTS decode/encode round-trip via `-c copy`.
- **Volume-restore overhold fix**: blocking TTS targets (Music Assistant, Sonos) no longer hold extra time after the audio finishes.
- **Cached audio reliability (issue #64)**: stale failure sentinels no longer block cached audio playback after a recovered API error.
- **Tag-triggered auto-release**: pushing a `v*` tag creates the GitHub release.

## Core Features

- **Text-to-Speech** via OpenAI's Audio Speech API or any compatible backend.
- **Multiple TTS agents** under one or more OpenAI accounts. Each agent has its own voice, model, speed, audio format and audio-processing settings.
- **Models**: `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts` (with custom speaking-style instructions).
- **Voices**: full OpenAI catalog including `alloy`, `ash`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`, plus the `gpt-4o-mini-tts`-only voices `ballad`, `cedar`, `marin`, `verse`.
- **Audio formats**: `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` per profile.
- **Streaming playback** with HA 2025.7+ for low first-audio latency. Falls back automatically when post-processing is needed.
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

> Enabling chime or normalise audio disables streaming for that profile, since the audio has to be assembled in full before playback.

## `openai_tts.say` service

Targets media players directly, with per-call overrides for voice, speed, instructions, chime, normalise, volume and pause behaviour.

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
  pause_playback: true     # pause music during the announcement
  chime: true              # prepend the configured chime
  normalize_audio: true    # loudness-normalise for small speakers
  voice: nova
  speed: 1.0
  instructions: "Say it warmly"
  extra_payload: '{"temperature": 0.8}'
```

## Custom backends

The integration works with any OpenAI-compatible TTS endpoint. When the URL is not `api.openai.com`:

- The API key field becomes optional.
- The voice field accepts any backend-specific name.
- Use the **audio format** selector to negotiate around backends that reject mp3 (for example `pocket-tts` returning PCM).
- The **extra payload** field forwards backend-specific JSON parameters with the request.

## Contributing

Bug reports, backend reports and pull requests are all welcome. Pull
requests target the `dev` branch, and for anything larger than a small
fix it is worth opening an issue first so the shape can be agreed before
you write it. [CONTRIBUTING.md](CONTRIBUTING.md) has the details.

If you use a backend that behaves differently from the others, saying so
in an issue is useful on its own, even without a patch.

## Notes

> *For OpenAI, an API key with available balance is required.* Pricing: <https://platform.openai.com/docs/pricing>
