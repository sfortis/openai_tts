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
