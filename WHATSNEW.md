## v3.8.1b3

### Fixes

- **Simpler chime + TTS audio pipeline**. Per-stream resampling and channel-layout normalisation that was added when chimes shipped at 44.1 kHz is now gone — the bundled chimes are already 24 kHz mono, so the extra step only added complexity. The output bitstream is closer to what shipped in v3.6, which should restore compatibility for HomePod / Apple TV setups that started failing on v3.8.

## v3.8.1b2

### Fixes

- **Sonos announcements no longer cut to just the chime**. When background music was playing, the volume restore was firing mid-announcement and chopping the rest of the clip; the speaker now finishes the full TTS before volume is restored.
- **No more lingering high volume after Music Assistant TTS**. The post-audio hold matches the time MA actually takes to play the announcement instead of adding a second copy of it.
- **HomePod / Apple TV announcements with chime play reliably**. The merged audio output drops a leading metadata frame that the RAOP decoder rejected, so previously-broken cached announcements now decode and play.

## v3.8.1b1

### Fixes

- **Custom backend compatibility**. The engine recognises JSON-wrapped audio responses (base64 inside an `audio_data` / `audio` / `data` field) in addition to raw bytes, so OpenAI-compatible providers that return a JSON envelope work end to end. The `speed` field is omitted from the request when it equals the default `1.0` so providers with stricter schemas don't reject the call.
- **More forgiving `extra_payload` parsing**. Whitespace and `` ```json `` code-fence wrappers are stripped before parsing. If the JSON is still malformed the integration logs a single warning and continues the request without those parameters instead of failing the whole TTS call.
- **Custom voice text survives reconfigure**. When the endpoint isn't OpenAI, the voice field is a free-text input. Reconfigure no longer drops a saved custom value back to a default; it keeps whatever the user typed.
- **Friendlier error messages**. Provider rejections aren't labelled "OpenAI API error" when you're using a different backend. Auth, quota, rate-limit and server errors carry a one-line hint pointing at the likely fix, and HTTP 4xx errors include the upstream's actual error body so problems like wrong model id or unsupported field are visible directly in the logs.
- **Chime + TTS playback works on every target**. The chime+TTS concat now goes through a single re-encode pass (`filter_complex` with `aresample` + `aformat` + optional `loudnorm` + concat → libmp3lame). Both inputs are normalised to 24 kHz mono before being glued together, so the output is a single consistent bitstream that decodes cleanly on Cast, Sonos, Music Assistant, AirPlay (Apple TV / HomePod) and anything else Home Assistant can target. User-supplied chimes at any sample rate, channel layout or codec are handled by the same filter graph.

## v3.8

### Highlights

- **2-step config flow** for TTS agent profiles. Pick the model first, then voice and audio options on a follow-up step. The voice picker is filtered by the chosen model so incompatible combinations are rejected up front.
- **Audio format selector** per profile (`mp3`, `opus`, `aac`, `flac`, `wav`, `pcm`). The selected format is requested from OpenAI (or any compatible custom backend) and delivered end-to-end without a forced mp3 round-trip. PCM uses explicit raw-stream flags so headerless input is handled correctly.
- **Format-aware ffmpeg pipeline**. Chime mixing and loudness normalization stay in the requested format end to end.
- **Expanded voice catalog**: `marin`, `cedar`, `ballad`, `verse` (gpt-4o-mini-tts only), with model compatibility validation in the service handler so misuse surfaces a clear error instead of an opaque API 400.
- **Volume-restore overhold fix** for blocking TTS targets (Music Assistant, Sonos). The post-audio idle is bounded by the small buffer instead of stretching to the full clip duration.
- **Cached audio reliability**. Stale failure markers stop blocking cached audio playback after a recovered API error, and they're actively cleared once playback succeeds again.
- **Account name** field on the parent entry to distinguish multiple accounts in the integrations list.
- **Auto-release CI**: pushing a `v*` tag now creates a GitHub release with `WHATSNEW.md` as the description and a downloadable `openai_tts.zip` asset.

### Other fixes and refinements

Faster, more stable volume control; tighter restore timing; HA URL extension respects the chosen format; full translation parity for `en` / `cs` / `de` / `el`; clearer field descriptions in service and config flow.
