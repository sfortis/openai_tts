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

OpenAI TTS turns text into speech inside Home Assistant. It began as a bridge to OpenAI's speech API, and it now works with cloud providers and self-hosted servers that implement the same API. Presets for the common providers fill in the endpoint, the model list and the voice list, and they hide or narrow the settings a provider is known to reject or ignore. The `openai_tts.say` action can announce on any media player, with an optional chime and loudness normalisation, and it restores the previous volume and music afterwards.

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

Each integration entry starts from a preset. The preset fills in the endpoint, offers the models the provider is known to serve and decides where the voice list comes from. It hides the speed slider on Mistral, which rejects it, and on Groq, which ignores it. It shows the extra payload field only on the Custom and Chatterbox presets, and it limits the audio formats on Groq, Chatterbox and OpenRouter. The model field also accepts a typed model name on every preset.

| Provider | Where it runs | Voices in the picker | API key |
|---|---|---|---|
| OpenAI | Cloud | OpenAI's voices, filtered by model | Required |
| Mistral Voxtral | Cloud | The voices on your account, read live, up to a hundred | Required |
| Groq (Orpheus) | Cloud | Six English Orpheus voices from a fixed list, or a typed name such as an Arabic voice | Required |
| Lemonfox.ai (Kokoro) | Cloud | The English voices Lemonfox documents, from a fixed list, or a typed Kokoro voice name | Required |
| OpenRouter (3.10 beta) | Cloud | The voices each speech model accepts, read live | Required |
| Kokoro-FastAPI | Self-hosted | The voice packs installed on the server, read live | Optional |
| Chatterbox | Self-hosted | The voices on the server, read live | Optional |
| Custom | Cloud or self-hosted | Read live when the server lists its voices, typed otherwise | Optional |

The names are shortened from the labels in the provider list.

The **Custom** preset covers every other server that implements the OpenAI speech endpoint, such as LocalAI, pocket-tts or TTS Web UI. When the server publishes no voice list, the voice field is a text field that takes any name the backend understands. When it does publish one, the picker shows that list, and from 3.10 (beta) it also takes a typed name when the list holds plain names. The **audio format** selector helps with a backend that rejects mp3, and the **extra payload** field sends backend-specific JSON parameters with each request.

Speech is always requested through the OpenAI speech API. Voice and model lists are read from a provider's own listing endpoint where one exists. A provider that offers speech only through a different API of its own is not supported.

On OpenAI the models are `tts-1`, `tts-1-hd` and `gpt-4o-mini-tts`, and `gpt-4o-mini-tts` also takes speaking-style instructions. The voices are `alloy`, `ash`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage` and `shimmer`, and `gpt-4o-mini-tts` adds `ballad`, `cedar`, `marin` and `verse`.

## What's New ![NEW](https://img.shields.io/badge/-NEW-brightgreen)

### Version 3.10 (beta)

Version 3.10 is a beta and needs Home Assistant 2026.9.1 or later. To try it, open the integration in HACS, choose **Redownload** and turn on **Show beta versions**.

- **OpenRouter preset**: the model picker lists the speech models OpenRouter offers, and the voice picker lists the voices of the chosen model. A model that lists no voices takes a typed one. When a profile changes model, the voice defaults to one the new model accepts.
- **Gain** per profile, from -12 to +12 dB in steps of 0.5 dB, to make a quiet voice louder or a loud one softer. It works with or without loudness normalisation, a limiter stops a boost from clipping, and the chime keeps its own level.
- **Announcement volume on Sonos and Music Assistant**: with a volume override, these speakers are given the level and run the announcement themselves, instead of being paused, set and restored by this integration. Music Assistant keeps the level within the range set for each player, 15 to 75 percent by default.
- **Music Assistant groups keep playing**: the integration no longer stops and restarts a Music Assistant group around an announcement on one of its speakers. Music Assistant takes that speaker out of the group for the announcement and returns it afterwards.
- **Typed voices**: when the provider lists voices by plain name, as Kokoro and OpenRouter do, the voice picker also accepts a name that is not in the list, such as the Kokoro voice mix `am_michael(1)+am_eric(2)`.
- **Speaker failures stay separate**: with a volume override, a failure on the Music Assistant speakers, the Sonos speakers or the other speakers no longer cuts the announcement short on the rest. The action then reports the failure.
- **Floor and label targets**: `openai_tts.say` now accepts floors and labels as targets, next to entities, devices and areas.
- **Your own chimes survive updates**: mp3 files in `/config/openai_tts/chime` are listed next to the built-in sounds, and that folder is not touched by an update.
- **API keys are checked on every provider**, when an entry is created, when a key is re-entered and when `openai_tts.set_api_key` runs. The check produces no audio and costs nothing.
- **Send the voice name** is offered only on the Custom and Chatterbox presets, because every hosted provider requires a voice.
- **The logbook shows who made an announcement**, for the announcement itself and for the pause, volume and resume commands around it.

### Version 3.9

Version 3.9 is mostly about backends other than OpenAI, and about what a speaker
does while an announcement is playing.

- **Provider presets**: pick OpenAI, Mistral, Groq, Lemonfox, Kokoro, Chatterbox
  or a custom endpoint when you create an entry. The preset fills in the URL, the
  models and the voices, and limits the audio format to what the provider accepts.
- **Voices from the provider**: on Mistral, Kokoro, Chatterbox and custom endpoints
  the voice picker lists the voices the backend reports, in the profile and in the
  Assist pipeline. Groq and Lemonfox use the list built into their preset.
- **Sentence streaming** for the voice assistant, off by default per profile.
  Speech starts on the first finished sentence instead of the finished reply. It
  needs MP3 or PCM and no chime.
- **Send the voice name** can be turned off per profile, for backends that reject
  the field. It is only offered when the endpoint is not OpenAI.
- **Loudness correction while streaming**, on by default. For MP3, Opus, AAC and
  PCM, correction no longer forces the whole clip to be produced before playback
  starts.
- **Speakers that support announcements** duck and resume the music themselves,
  instead of being paused and restored by this integration, when no volume
  override is given.
- **Repairs** are raised when a voice disappears at the provider, and a rejected
  API key starts Home Assistant's re-authentication prompt, instead of every call
  failing with only an HTTP error.
- **`response_variable`** is supported on `openai_tts.say`.
- **Stream the audio** can be turned off per profile, for a backend that answers
  a streamed read with audio that will not decode while the same request read in
  one go is fine.
- **`openai_tts.set_api_key`** is an admin action that replaces the key on an
  entry, so an automation can rotate a short lived token without anyone opening
  the settings. The key is checked against the endpoint before it is stored,
  unless the call sets `validate: false`.

[WHATSNEW.md](WHATSNEW.md) lists every change, including the fixes.

## Features

### Speech

- Several TTS agents under one entry, each with its own model, voice, speed, audio format and audio processing.
- Audio in `mp3`, `opus`, `aac`, `flac`, `wav` or `pcm`, chosen per profile. Some providers accept fewer formats, and the selector then offers only those: Groq accepts `wav`, Chatterbox accepts `mp3`, `opus` and `wav`, and OpenRouter (3.10 beta) accepts `mp3` and `pcm`.
- Streaming playback, so audio plays as it arrives instead of after the whole clip is written. Streaming works with `mp3`, `opus`, `aac` and `pcm`. A `wav` or `flac` file states its length in a header before any audio exists, so those two formats are always assembled in full first. A chime, or the **Stream the audio** switch turned off, also makes the integration assemble the whole clip before playback starts.
- Sentence streaming for the voice assistant, off by default and set per profile. When the conversation agent streams its reply, speech starts on the first finished sentence rather than on the finished reply. It needs `mp3` or `pcm`, and the profile form refuses it together with a chime or with another format.
- Loudness normalisation, on by default. It runs on the stream for `mp3`, `opus`, `aac` and `pcm`. For `wav` and `flac`, and whenever a chime is added, it runs on the finished clip instead.
- A gain per profile, from -12 to +12 dB in steps of 0.5 dB, with a limiter so a boost cannot clip (3.10 beta).
- A chime before the announcement, picked from five built-in sounds or from your own mp3 files. From 3.10 (beta) your own files go in `/config/openai_tts/chime`, which an update does not touch. On 3.9 they go in `config/custom_components/openai_tts/chime`, which a HACS update replaces, so keep a copy elsewhere.
- The entity declares 54 languages, so it can be chosen for an Assist pipeline in any of them. The provider speaks the language the text is written in, so what actually works depends on the provider, the model and the voice.

### Announcements

- Announcements on any media player, targeted by entity, device or area, and from 3.10 (beta) by floor or label too.
- When the call sets `volume`, each speaker plays the announcement at that level and returns to its own level afterwards. Without `volume`, the integration leaves every speaker's volume alone.
- Speakers that cannot announce on their own are paused before the announcement and resumed after it, when they were playing. Speakers that can, which are Sonos, Music Assistant and any player that reports the announcement feature, duck or handle their own music instead. With `announce: false`, nothing is paused or resumed, so a Cast speaker's music is replaced by the announcement and does not come back.
- With a volume override, every speaker is paused, set and restored, except Sonos and Music Assistant speakers from 3.10 (beta), which receive the level with the announcement and handle their own volume.
- Speakers that are off are switched on together, and the announcement waits up to five seconds for all of them to wake, so a speaker that was off does not start noticeably later than one that was already on.

### Monitoring

- An API status sensor, one per entry, reports the result of the last request: `ok`, `auth_failed`, `quota_exceeded`, `rate_limited`, `server_error`, `network_error` or `unknown_error`.
- A repair is raised when the provider answers that a profile's voice no longer exists. This applies to providers whose voices are read live or typed, which are Mistral, Kokoro-FastAPI, Chatterbox, OpenRouter and Custom.
- A rejected API key starts Home Assistant's re-authentication prompt, so the key can be replaced without removing the entry.

## Installation

Version 3.10 (beta) needs Home Assistant 2026.9.1 or later. Version 3.9.2 needs Home Assistant 2025.7 or later.

### HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sfortis&repository=openai_tts&category=integration)

1. Open HACS in the sidebar, or use the button above.
2. Search for **OpenAI TTS**.
3. Download the integration and restart Home Assistant.
4. Add the integration via *Settings → Devices & services → Add integration → OpenAI TTS*, and pick the provider preset.
5. Enter the API key if the provider needs one. A self-hosted server without authentication can leave it empty. The same form shows the API endpoint. The Custom preset has none and needs one. The Kokoro-FastAPI and Chatterbox presets assume the server runs on the Home Assistant host, so change the address if it runs elsewhere.
6. Add one or more TTS agents (sub-entries) for the voice and audio configurations you want.

### Manual

1. Copy the contents of `custom_components/openai_tts/` into `<config>/custom_components/openai_tts/`, or unpack `openai_tts.zip` from the release page there.
2. Restart Home Assistant.
3. Add the integration via *Settings → Devices & services* as above.

## Configuration

Each integration entry stores the provider, the endpoint and the API key. Add one entry per provider or per account. Each sub-entry (TTS agent) stores the per-profile settings:

- **Profile name**, which also names the TTS entity.
- **Model** and **voice**. On OpenAI the voice list follows the model.
- **Speed**, from 0.25 to 4.0. It is hidden on Mistral, which rejects the field, and on Groq, which ignores it.
- **Audio format**, `mp3` by default (`wav` on Groq), limited to what the provider accepts.
- **Voice instructions** for speaking style, shown only when the model is `gpt-4o-mini-tts`.
- **Extra JSON payload** for backend-specific parameters, on the Custom and Chatterbox presets.
- **Chime**, **chime sound** and **normalise audio** as defaults that the service call can override.
- **Gain** (3.10 beta), from -12 to +12 dB, applied with or without normalisation.
- **Sentence streaming** (off by default) to start speaking on the first finished
  sentence of an assistant reply instead of the finished reply.
- **Stream the audio** (on by default). Turn it off for a backend whose streamed
  response will not decode while the same request read in one go is fine. It also
  turns sentence streaming off.
- **Send the voice name** (on by default). Turn it off for a backend that rejects
  the `voice` field, such as audio.cpp serving Chatterbox or VoxCPM2. From 3.10
  (beta) it is offered only on the Custom and Chatterbox presets.

> A chime turns streaming off for every request it is added to, because a chime has
> to be attached to finished audio. A chime switched on in the profile therefore
> turns streaming off for all of that profile's announcements and voice assistant
> replies, unless the call sets `chime: false`. Loudness normalisation does not turn
> streaming off, and from 3.10 (beta) neither does the gain: both run on the stream
> for `mp3`, `opus`, `aac` and `pcm`.

## Using It With the Voice Assistant

Every TTS agent is a regular Home Assistant text-to-speech entity. To use one for spoken replies, open *Settings → Voice assistants*, pick the assistant and choose the agent under **Text-to-speech**. The assistant's own **Voice** field is sent with every reply and takes precedence over the voice set in the profile.

With **Sentence streaming** switched on in the profile, synthesis starts on the first finished sentence. This happens only when the conversation agent streams its reply and the reply is longer than about 60 characters. The profile must also use MP3 or PCM, have **Stream the audio** switched on, and add no chime.

The entities also work with Home Assistant's own `tts.speak` action. That action sends no volume level. Most speakers play the announcement at their current volume, while Music Assistant uses its own announcement volume setting and Sonos its own default announcement level. On a speaker without an announcement feature, such as Cast, the music it replaces does not come back. `openai_tts.say` adds a volume level for the announcement, and on speakers without an announcement feature it pauses the music and resumes it afterwards where the player allows that.

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
instructions, extra payload, chime, chime sound, normalisation, volume and
announcement behaviour. Only `tts_entity` and `message` are required fields, but
the call must target at least one available media player or it fails. Voice,
speed, instructions, extra payload, chime, chime sound, normalisation and
announcement mode fall back to the profile when they are left out. When `volume`
is left out, the speaker volume is not changed.

`pause_playback` is still accepted as an older name for `announce` so existing
automations keep working, but new ones should use `announce`.

```yaml
action: openai_tts.say
target:
  entity_id: media_player.living_room_speaker
  # area_id: living_room
  # device_id: 12345abcde
  # floor_id: ground_floor   (3.10 beta)
  # label_id: announcements  (3.10 beta)
data:
  tts_entity: tts.openai_tts_living_room
  message: "Dinner is ready"
  volume: 0.6              # this announcement only, see Features for how each speaker applies it
  announce: true           # default; pause and resume speakers that cannot announce
  chime: true              # play a chime first; turns streaming off
  chime_sound: threetone.mp3
  normalize_audio: true    # loudness normalisation, on by default
  voice: nova
  speed: 1.0
  language: en             # checked by Home Assistant, not sent to the provider
  instructions: "Say it warmly"           # gpt-4o-mini-tts only
  extra_payload: '{"temperature": 0.8}'   # Custom and Chatterbox; merged into the request body
```

With `response_variable`, an error that happens while the announcement runs is returned instead of raised. The response is `success: true`, or `success: false` together with an `error` message, so an automation can send a notification when an announcement did not play. A call that fails validation, for example one with `speed` out of range, still raises an error. Unavailable targets are skipped, and the call reports success when at least one speaker played.

## Choosing the Target Speaker

Some speakers appear in Home Assistant more than once. A speaker that Music Assistant plays to usually has a Music Assistant entity and a second entity from its own integration, such as ESPHome or Cast. Target the Music Assistant entity. The integration pauses a Cast entity before every announcement, and an ESPHome entity when a volume is given, and Music Assistant only hears about those pauses through the speaker itself. On a speaker in a Music Assistant group, this can stop the whole group.

On 3.9, a volume override on a Music Assistant speaker makes the integration stop the speaker and resume it afterwards. Music Assistant passes a stop on a grouped speaker on to the group, so the whole group stops. From 3.10 (beta) the level is handed to Music Assistant instead. A speaker that plays announcements natively plays it over the group's music. Otherwise Music Assistant takes the speaker out of its group for the announcement and adds it back afterwards, while the rest of the group keeps playing. If the speaker leads the group, or the group cannot change its members, the group stops for the announcement.

## `openai_tts.set_api_key` action

Replaces the stored API key on an entry, so an automation can rotate a short
lived token without anyone opening the settings. It needs an administrator, and
it targets either `config_entry_id` or `tts_entity`, not both. Automations that
run without a user are allowed.

Before storing the key, the action checks it against the endpoint. If the
endpoint refuses the key, or the check cannot complete for another reason, the
old key stays in place and the action raises an error. A key that equals the
stored one is neither checked nor written. From 3.10 (beta) the check sends a
speech request with an empty text, which every provider refuses only after it
has looked at the key, so the check works on every provider, produces no audio
and costs nothing. On 3.9 the check is a real speech request with the model
`tts-1` and the voice `alloy`, which most providers other than OpenAI answer with
an error that says nothing about the key, so add `validate: false` there to store
the key without checking it.

When the entry is loaded and Home Assistant is running, the entry reloads with
the new key, so no restart is required. Otherwise the key takes effect the next
time the entry loads.

```yaml
action: openai_tts.set_api_key
data:
  config_entry_id: 01ABCDEF...
  api_key: "{{ token.content.access_token }}"
  # validate: false
```

With `response_variable` the call reports what it did. `success` is true, and
`changed` is false when the key was already the one stored, with nothing else
reported in that case. When the key changed, `reloading` is true if the entry is
being reloaded with it now, and false if the key will be used at the next load. A
failed check raises an error instead of returning a response.

> Automation traces keep the data an action was called with, so the key passed to
> this action is visible in the trace of the automation that called it.

## Known Limitations

- Music Assistant keeps announcement volume within a range set for each player, 15 to 75 percent by default, and changes a requested level outside that range. From 3.10 (beta) the integration logs a warning when the requested level is outside the default range. It cannot see a range changed in Music Assistant.
- From 3.10 (beta), Music Assistant ignores the requested level on a player whose announcement volume strategy is set to **none**, and the announcement plays at the player's current volume.
- A Sonos speaker without AudioClip support, which is older S1 hardware, falls back to ordinary playback. From 3.10 (beta) a volume override is then ignored on that speaker and its music does not come back. Home Assistant's Sonos integration also supports announcements only in `mp3` and `wav`.
- The `announce` field changes nothing on speakers that announce on their own: Sonos, Music Assistant and any player that reports the announcement feature. On other speakers it decides whether they are paused and resumed, but only when the call sets no volume, because a volume override always pauses them.
- A chime turns streaming off for every request it is added to, including every voice assistant reply of a profile that has the chime switched on.
- All the speakers that the integration pauses and restores in one call share one playback request. When it fails on one of them, the announcement counts as failed for all of them, their volumes and music are restored straight away, and the action reports an error.
- Groq accepts only `wav`, so a Groq profile never streams. A `wav` or `flac` profile never streams on any provider.
- Audio that the integration processes, for a chime, loudness normalisation or gain, is written as 24 kHz mono, which is what every supported provider produces.
- The language of a call is not sent to the provider. The provider speaks the language the message is written in.
- An OpenAI message longer than 4096 characters is refused by `openai_tts.say`. `tts.speak` and the voice assistant do not check this.
- Mistral voice lists are read up to a hundred voices.

## Troubleshooting

Turn on debug logging for the integration to see each request to the provider, which speakers are paused and how the volume is set and restored:

```yaml
logger:
  logs:
    custom_components.openai_tts: debug
```

Check the entry's API status sensor. It is a diagnostic entity named **Status** on the entry's provider device, for example "OpenAI TTS API". Its state is `ok` or the kind of the most recent failure, such as `auth_failed` or `quota_exceeded`, and it returns to `ok` after the next successful request. Its attributes hold a description, the last error message, and the times of the last success and the last error. It starts as `ok` after a restart.

After `auth_failed` or `quota_exceeded`, the entry's TTS entities report unavailable, and every announcement is refused for ten minutes without contacting the provider. After that, one request is let through to check again. A rejected key also starts Home Assistant's re-authentication prompt on the entry, and entering a new key there, or with `openai_tts.set_api_key`, reloads the entry and clears the status. An HTTP 403 counts as a rejected key, whatever the provider meant by it.

When you open an issue, attach the diagnostics file from *Settings → Devices & services → OpenAI TTS → ⋮ → Download diagnostics*, for the entry that has the problem. The API key is removed from that file. The endpoint URL, the entry title, a short hash of the API key and the profile settings, including instructions and extra payload, are kept, so remove anything secret from them before attaching the file.

## Contributing

Bug reports, backend reports and pull requests are all welcome. Pull
requests target the `dev` branch. For anything larger than a small fix, open
an issue or a discussion first, so that the approach can be agreed before you
write the code. [CONTRIBUTING.md](CONTRIBUTING.md) has the details.

If you use a backend that behaves differently from the others, saying so
in an issue is useful on its own, even without a patch.

## API Keys and Costs

Cloud providers need an API key, and on paid providers the account needs available balance or credits. OpenAI's pricing is at <https://platform.openai.com/docs/pricing>. The entry's API status sensor shows `quota_exceeded` when the provider reports that the balance has run out.
