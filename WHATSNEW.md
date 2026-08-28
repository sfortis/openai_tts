## v3.9b2

### Fixes

- **Two announcements in quick succession no longer leave a speaker quiet**. The second one could take the volume the first had lowered to as the speaker's real level, because the speaker had not yet reported the level it was put back to.
- **Announcements no longer stall for a minute when several profiles are configured**. Measured clip lengths were being thrown away at startup, one profile discarding another's, so a message whose audio Home Assistant already had cached was left with no length to work from and the volume stayed down while nothing answered.

## v3.9b1

### New

- **Providers are picked from a list**: OpenAI, Mistral, Groq, Lemonfox, Kokoro or a custom endpoint. The choice fills in the endpoint, the models and voices on offer, and the audio formats accepted.
- **Voices come from the provider**. Backends that publish a voice list are asked for it, both when creating a profile and in the voice dropdown Home Assistant shows. OpenAI publishes none, so its catalogue stays built in.
- **Speech can start before the reply is finished**. A per-profile option synthesises sentence by sentence as a voice assistant produces the answer. It needs mp3, wav or pcm output with chime and normalisation off. A profile that cannot meet that is refused when saved.
- **Announcement mode**. Speakers that advertise the announcement feature, along with Sonos and Music Assistant, use their own announcement layer, so music ducks and comes back on the device. Speakers without it are paused and resumed instead.
- **A repair when a voice disappears**. A voice removed at the provider raises a repair naming the profile, rather than failing quietly on every call.
- **A prompt to reauthenticate**. A rejected API key asks for a new one instead of raising the same error on every announcement.
- **`openai_tts.say` supports `response_variable`**. Automations and scripts can capture a `{success, error}` dict and branch on a failed announcement (e.g. send a phone notification when TTS playback errors out). Existing fire-and-forget callers are unchanged.

### Fixes

- **Volume comes back after a cancelled announcement**. An automation set to restart, or a shutdown mid-clip, used to leave the speaker quiet and the paused music paused.
- **Two announcements on the same speaker wait for each other**. Overlapping ones each took the other's lowered volume for the original, which could leave a speaker quiet for good.
- **A speaker that never reports its volume is left alone** instead of being set to announcement volume and never restored.
- **Announcing a message Home Assistant already has cached no longer holds the volume down** for up to a minute while nothing answers.
- **Measured clip lengths survive a restart** instead of being discarded by the first announcement afterwards.
- **Music a speaker resumes on its own after an announcement is stopped again**, without cutting the announcement short.
- **The `say` action stays available while an entry reloads**. Automations firing in that window were told the action did not exist.
- **Deleting an entry or a profile removes what it stored.**
- **A blocked API recovers on its own**. After a rejected key or an exhausted quota, calls resume once the block ages out rather than waiting for a reload. The engine reports itself unavailable while it is refusing them.
- **The status sensor is named after its provider**, and its name follows the interface language.
- **The fields of `openai_tts.say` are translatable.**
- **ffmpeg comes from the path configured for Home Assistant** rather than being assumed to be on the search path.
- **Generating speech no longer blocks Home Assistant** while the clip length is measured.
- **History no longer stores the voice catalogue** with every state change.
- **Speakers that support neither pause nor stop are left playing**. Asking anyway logged an error and left a stray resume afterwards.
- **The volume comes back even when the speaker reports a stale level**. A speaker whose volume updates never reached Home Assistant kept the announcement volume for good.
- **An announcement no longer plays twice on speakers without an announcement feature**. The clip replaced what was playing and the resume replayed the clip; the original stream is restored instead.

## v3.8.1b4

### Fixes

- **Sonos announcements no longer cut to just the chime**. When background music was playing, the volume restore was firing mid-announcement and chopping the rest of the clip; the speaker now finishes the full TTS before volume is restored.
- **No more lingering high volume after Music Assistant TTS**. The post-audio hold matches the time MA actually takes to play the announcement instead of adding a second copy of it.

## v3.8.1b1

### Fixes

- **Custom backend compatibility**. The engine recognises JSON-wrapped audio responses (base64 inside an `audio_data` / `audio` / `data` field) in addition to raw bytes, so OpenAI-compatible providers that return a JSON envelope work end to end. The `speed` field is omitted from the request when it equals the default `1.0` so providers with stricter schemas don't reject the call.
- **More forgiving `extra_payload` parsing**. Whitespace and `` ```json `` code-fence wrappers are stripped before parsing. If the JSON is still malformed the integration logs a single warning and continues the request without those parameters instead of failing the whole TTS call.
- **Custom voice text survives reconfigure**. When the endpoint isn't OpenAI, the voice field is a free-text input. Reconfigure no longer drops a saved custom value back to a default; it keeps whatever the user typed.
- **Friendlier error messages**. Provider rejections aren't labelled "OpenAI API error" when you're using a different backend. Auth, quota, rate-limit and server errors carry a one-line hint pointing at the likely fix, and HTTP 4xx errors include the upstream's actual error body so problems like wrong model id or unsupported field are visible directly in the logs.
- **Chime + TTS playback works on every target**. The chime+TTS concat now goes through a single re-encode pass (`filter_complex` with `aresample` + `aformat` + optional `loudnorm` + concat → libmp3lame). Both inputs are normalised to 24 kHz mono before being glued together, so the output is a single consistent bitstream that decodes cleanly on Cast, Sonos, Music Assistant, AirPlay (Apple TV / HomePod) and anything else Home Assistant can target. User-supplied chimes at any sample rate, channel layout or codec are handled by the same filter graph.
