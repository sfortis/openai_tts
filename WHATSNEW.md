## v3.11b1

- Warn in Repairs when an announcement targets a speaker that Music Assistant also plays to, and name the Music Assistant entity to use instead
- Accept region language tags such as `en-US` and `de-DE`, which Music Assistant and other callers send

## v3.10.2

- Fix pcm profiles failing on voice assistant satellites
- Fix opus profiles failing on voice assistant satellites

## v3.10.1

- Fix a new TTS agent failing to load and leaving an unnamed device behind
- Measure the length of pcm clips, which was always recorded as zero

## v3.10

- Add an OpenRouter preset, which lists the speech models OpenRouter offers and the voices each model accepts
- Add a gain setting per profile, to raise or lower the speech volume with or without loudness correction
- Hand a volume override to Sonos and Music Assistant speakers, which play the announcement at that level instead of being paused and restored
- Stop breaking up a Music Assistant group when one of its speakers announces with a volume override
- Log a warning when a requested Music Assistant announcement volume is outside its default range of 15 to 75 percent
- Keep announcing on the other speakers of a call with a volume override when its Music Assistant, Sonos or remaining speakers fail, and report the failure
- Accept floors and labels as targets of `openai_tts.say`, next to entities, devices and areas
- Refuse an `openai_tts.say` call whose target names no media player, with a message that says so
- Leave out players that cannot play media when a device, area, floor or label is targeted, instead of failing the announcement on every speaker
- Let the voice picker take a typed voice when the provider lists voices by plain name, such as a Kokoro voice mix `am_michael(1)+am_eric(2)`
- Offer the new model's voices when an OpenRouter profile changes model, instead of keeping a voice the new model does not accept
- Read your own chime files from `/config/openai_tts/chime`, which an update does not touch
- Refuse a chime name that is not a plain mp3 file name
- Check API keys on every provider without producing audio, when an entry is created, when a key is re-entered and when `openai_tts.set_api_key` runs
- Warn in Repairs from 2026-12-23 about every profile that calls OpenAI with `tts-1`, `tts-1-hd` or `gpt-4o-mini-tts`, which OpenAI stops serving on 2027-01-06
- Name new entries after the provider, mark self-hosted ones, and leave the hostname out of cloud provider titles
- Stop repeating the provider name in an entry title when the account name is the same
- Title an entry Custom instead of OpenAI when a reconfigure moves it off its preset to another endpoint
- Name each profile's device and entity after the profile alone, without the model and voice, and show the provider as its manufacturer and the integration version as its software version
- Offer the switch that stops sending the voice name only on the Custom and Chatterbox presets, and always send the voice to providers that require one
- Pass the caller of `openai_tts.say` on to the announcement and to the pause, volume and resume commands, so the logbook shows who made it
- Report a failed announcement as "TTS announcement failed" instead of "TTS speak failed"
- Correct the descriptions of the language, announce, chime sound and volume fields
- Let the update listener reload an entry after a reconfigure or a new API key, as Home Assistant 2026.12 will require, and stop reloading it twice after a new API key
- Require sentence-stream as a minimum version instead of one exact version
- Rename the HACS entry to OpenAI TTS & compatible providers
- Require Home Assistant 2026.9.1 or later

## v3.10b7

- Keep the cache of messages spoken before the update, instead of synthesising and billing every one of them again
- Report a failed announcement when every target is a Sonos or Music Assistant speaker given a volume override, instead of reporting success
- Leave out players that cannot play media when a device, area, floor or label is targeted, instead of failing the announcement on every speaker
- Keep a profile's saved voice when its model changes, except on OpenRouter, whose voices depend on the model
- Leave the voice empty when an OpenRouter profile's model lists no voices, instead of filling in an OpenAI voice
- Keep a Sonos speaker reserved until an announcement that started late has finished, so the next one does not play over it

## v3.10b6

- Show the OpenAI retirement warning from 2026-12-23, two weeks before the shutdown, instead of from the day it was announced

## v3.10b5

- Warn in Repairs about every profile that calls OpenAI with `tts-1`, `tts-1-hd` or `gpt-4o-mini-tts`, which OpenAI stops serving on 2027-01-06

## v3.10b4

- Name each profile's device and entity after the profile alone, without the model and voice, and show the provider as its manufacturer and the integration version as its software version
- Check Groq API keys with a Groq model, which the check could not do before because Groq answers an unknown model with 404
- Let the update listener reload an entry after a reconfigure or a new API key, as Home Assistant 2026.12 will require, and stop reloading it twice after a new API key

## v3.10b3

- Name new entries after the provider, mark self-hosted ones, and leave the hostname out of cloud provider titles
- Stop repeating the provider name in an entry title when the account name is the same
- Title an entry Custom instead of OpenAI when a reconfigure moves it off its preset to another endpoint

## v3.10b2

- Accept floors and labels as targets of `openai_tts.say`, next to entities, devices and areas
- Refuse an `openai_tts.say` call whose target names no media player, with a message that says so
- Read your own chime files from `/config/openai_tts/chime`, which an update does not touch
- Refuse a chime name that is not a plain mp3 file name
- Check API keys on every provider without producing audio, when an entry is created, when a key is re-entered and when `openai_tts.set_api_key` runs
- Offer the switch that stops sending the voice name only on the Custom and Chatterbox presets, and always send the voice to providers that require one
- Pass the caller of `openai_tts.say` on to the pause, volume and resume commands as well
- Correct the descriptions of the language, announce, chime sound and volume fields
- Rename the HACS entry to OpenAI TTS & compatible providers
- Require Home Assistant 2026.9.1 or later

## v3.10b1

- Add an OpenRouter preset, which lists the speech models OpenRouter offers and the voices each model accepts
- Add a gain setting per profile, to raise or lower the speech volume with or without loudness correction
- Offer the new model's voices when a profile changes model, instead of keeping a voice the new model does not accept
- Hand a volume override to Sonos and Music Assistant speakers, which play the announcement at that level instead of being paused and restored
- Stop breaking up a Music Assistant group when one of its speakers announces with a volume override
- Log a warning when a requested Music Assistant announcement volume is outside its default range of 15 to 75 percent
- Let the voice picker take a typed voice when the provider lists voices by plain name, such as a Kokoro voice mix `am_michael(1)+am_eric(2)`
- Keep announcing on the other speakers of a call with a volume override when its Music Assistant, Sonos or remaining speakers fail, and report the failure
- Pass the caller of `openai_tts.say` on to the announcement, so the logbook shows who made it
- Report a failed announcement as "TTS announcement failed" instead of "TTS speak failed"
- Require sentence-stream as a minimum version instead of one exact version

## v3.9.2

- Show up to a hundred voices of a Mistral account, instead of only the first ten

## v3.9.1

- Fix the settings form of an existing profile refusing to open

## v3.9

- Pick a provider from a list: OpenAI, Mistral, Groq, Lemonfox, Kokoro, Chatterbox or a custom endpoint
- Offer the voices that Mistral, Kokoro, Chatterbox and custom endpoints publish, in the profile and in the voice picker
- Start speaking before the reply is finished, sentence by sentence, as an option per profile
- Let speakers that support announcements duck and resume the music themselves when no volume override is given
- Apply loudness correction while speech is streaming, and turn it on by default
- Lift quiet words instead of levelling only the average of a clip
- Start speech about a second sooner when correction is on
- Add an admin action, `openai_tts.set_api_key`, so an automation can rotate a short lived key without anyone opening the settings
- Add a per profile switch to stop sending the voice name, for backends that reject the field
- Let a profile turn streaming off, for a backend that answers a streamed read with audio that will not decode
- Raise a repair when a voice disappears at the provider, instead of failing on every call
- Ask for a new API key when the current one is rejected
- Support `response_variable` on `openai_tts.say`
- Produce wav and flac in full before playback, so their header states a real length instead of the placeholder that strict players read as hours of audio
- Set the announcement volume while the audio is generated, so paused music is not heard rising to it
- Wait for a speaker to report a volume instead of assuming the change landed
- Refuse a profile that turns on sentence streaming together with a chime or a format other than MP3 or PCM, instead of falling back silently at playback time
- Show the loudness correction setting on the entity, next to the other profile settings
- Name the status sensor after its provider, and follow the interface language
- Translate the fields of `openai_tts.say`
- Keep measured clip lengths across a restart
- Keep the `say` action available while an entry reloads
- Take ffmpeg from the path configured for Home Assistant
- Measure clip length off the event loop
- Keep the voice catalogue out of the recorder
- Remove what an entry or a profile stored when it is deleted
- Fix loudness correction making short announcements quieter than the original
- Fix the announcement volume landing on music that was still fading out of a paused speaker, which made it swell before it stopped
- Fix a speaker left quiet after two announcements in quick succession
- Fix announcements stalling for a minute when several profiles are configured
- Fix the volume staying down after a cancelled announcement
- Fix two announcements on one speaker taking each other's lowered volume for the original
- Fix the volume staying down for a minute on a message Home Assistant already had cached
- Fix an announcement playing twice on speakers without an announcement feature
- Stop music a speaker resumes on its own after an announcement, without cutting the announcement short
- Leave a speaker alone when it never reports its volume
- Leave a speaker playing when it supports neither pause nor stop
- Restore the volume even when the speaker reports a stale level
- Recover from a blocked API once the block ages out, rather than on a reload
- Fix speech failing on every call when a profile is set not to send the voice name
- Keep the model, voice and speed that were set through the old options dialog when an older entry is migrated
- Fix reauthentication refusing a working key on backends that do not accept OpenAI's default model, voice or format
- Honour the switch that turns streaming off on the sentence streaming path as well
- Fix music being restarted a second time when an announcement failed early
- Report a failure when sentence streaming breaks, instead of holding the speaker
- Report an announcement that reached no speaker, instead of reporting success
- Refuse a blank endpoint when reconfiguring, as creating one already did
- Limit a diagnostics download to the entry it was requested for
- Keep the body of an authentication failure out of the stored error message
- Treat the same key at a different endpoint as a separate account
- Keep a chime that is no longer on disk selectable, so reconfiguring does not replace it
- Follow the new endpoint when one is moved to a different provider
- Deliver finished audio even when measuring its length fails
- Remove a temporary file left behind on every call in the default configuration
- Evict remembered clip lengths by last use, matching what the shared cache does
- Skip an ID3v2.4 footer when joining streamed audio

## v3.9b6

- Add a Chatterbox preset, which fills in the endpoint, the voices it publishes and the three audio formats it actually accepts
- Play opus and aac as they arrive instead of waiting for the whole clip, which loudness correction used to prevent
- Add an admin action, `openai_tts.set_api_key`, so an automation can rotate a short lived key without anyone opening the settings
- Let a profile turn streaming off, for a backend that answers a streamed read with audio that will not decode
- Say plainly which formats stream and which do not, in the README and in the profile options

## v3.9b5

- Fix the announcement volume landing on music that was still fading out of a paused speaker, which made it swell before it stopped
- Fix loudness correction turning itself off when a profile's own settings could not be read
- Show the loudness correction setting on the entity, next to the other profile settings

## v3.9b4

- Add a per profile switch to stop sending the voice name, for backends that reject the field
- Write a real length into wav and flac instead of the placeholder a streaming producer has to use, which strict players read as hours of audio
- Wait for a speaker to report a volume instead of assuming the change landed
- Set the announcement volume while the audio is generated, so paused music is not heard rising to it
- Say which formats can actually stream, and drop a profile rule that could never fire

## v3.9b3

- Apply loudness correction while speech is streaming
- Turn loudness correction on by default
- Fix loudness correction making short announcements quieter than the original
- Lift quiet words instead of levelling only the average of a clip
- Start speech about a second sooner when correction is on
- Refuse a profile that cannot stream and correct at once, instead of falling back silently at playback time

## v3.9b2

- Fix a speaker left quiet after two announcements in quick succession
- Fix announcements stalling for a minute when several profiles are configured

## v3.9b1

- Pick a provider from a list: OpenAI, Mistral, Groq, Lemonfox, Kokoro or a custom endpoint
- Offer the voices the provider publishes, in the profile and in the voice picker
- Start speaking before the reply is finished, sentence by sentence, as an option per profile
- Let speakers that support announcements duck and resume the music themselves
- Raise a repair when a voice disappears at the provider, instead of failing on every call
- Ask for a new API key when the current one is rejected
- Support `response_variable` on `openai_tts.say`
- Fix the volume staying down after a cancelled announcement
- Fix two announcements on one speaker taking each other's lowered volume for the original
- Leave a speaker alone when it never reports its volume
- Fix the volume staying down for a minute on a message Home Assistant already had cached
- Keep measured clip lengths across a restart
- Stop music a speaker resumes on its own after an announcement, without cutting the announcement short
- Keep the `say` action available while an entry reloads
- Remove what an entry or a profile stored when it is deleted
- Recover from a blocked API once the block ages out, rather than on a reload
- Name the status sensor after its provider, and follow the interface language
- Translate the fields of `openai_tts.say`
- Take ffmpeg from the path configured for Home Assistant
- Measure clip length off the event loop
- Keep the voice catalogue out of the recorder
- Leave a speaker playing when it supports neither pause nor stop
- Restore the volume even when the speaker reports a stale level
- Fix an announcement playing twice on speakers without an announcement feature

## v3.8.1b4

- Fix Sonos announcements cut to just the chime when music was playing
- Fix lingering high volume after a Music Assistant announcement

## v3.8.1b1

- Accept JSON-wrapped audio responses from OpenAI-compatible backends
- Omit `speed` from the request when it equals the default
- Strip whitespace and code fences before parsing `extra_payload`, and continue without it when still malformed
- Keep a custom voice through reconfigure on a non-OpenAI endpoint
- Report provider errors as the provider's, with the upstream error body in the log
- Fix chime and speech playback on Cast, Sonos, Music Assistant and AirPlay
