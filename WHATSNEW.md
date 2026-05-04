## v3.8.1b1

### Fixes

- **`extra_payload` JSON parsing is lenient again (issue #65)**. v3.8 made invalid `extra_payload` raise a hard error, which broke working setups (Qwen3 / Alibaba DashScope etc.) where the parameter was silently dropped on v3.7. The parser now trims whitespace, strips ` ```json ` code fences, and on still-malformed input logs a single warning with the offending text and continues the TTS request without those extra parameters.
- **Mistral Voxtral support (issue #63)**. The engine now detects JSON-wrapped audio responses (Content-Type `application/json`) and base64-decodes the audio payload (`audio_data` / `audio` / `data` field) before handing the bytes back to Home Assistant. Combined with omitting the `speed` field when it equals the default 1.0, this lets `voxtral-mini-tts-latest` work end-to-end through the existing custom-endpoint flow.
- **Upstream error bodies are surfaced**. HTTP 4xx errors from the TTS provider now include the response body (first 200 chars) in the exception message, so issues like "Invalid model", "Voice not found" or schema-rejection details show up directly in the logs instead of just `HTTP 4xx`.
- **Custom voice text persists across reconfigure**. When the endpoint is not OpenAI, the voice field is a free-text input (Mistral slugs, custom cloned voice ids, etc.). Reconfigure was silently dropping the saved value back to the OpenAI default; it now keeps whatever the user typed.
- **Chipmunk effect with chime enabled is fixed**. The bundled chimes are at 44.1 kHz while OpenAI TTS is at 24 kHz; the concat-copy fast path was leaving the TTS half playing at the chime's sample rate. The chime is now pre-resampled to 24 kHz mono in the requested codec the first time it is used.
- **Friendlier error messages**. Provider rejections are no longer labelled "OpenAI API error" when you're using Mistral or another backend. Auth, quota, rate-limit and server errors now carry a one-line hint pointing at the likely fix.

## v3.8

### Highlights

- **2-step config flow** for TTS agent profiles. Pick the model first, then voice and audio options on a follow-up step. The voice picker is filtered by the chosen model so incompatible combinations (e.g. `marin` on `tts-1`) are rejected up front.
- **Audio format selector** per profile (`mp3`, `opus`, `aac`, `flac`, `wav`, `pcm`). The selected format is requested from OpenAI (or any compatible custom backend) and delivered end-to-end without a forced mp3 round-trip. PCM is sent with explicit `-f s16le -ar 24000 -ac 1` flags so headerless input is handled correctly.
- **Format-aware ffmpeg pipeline**. Chimes are transcoded on demand to match the TTS codec (cached on disk next to the source mp3). Chime-only requests use the concat demuxer with `-c copy`, skipping a full TTS decode/encode round-trip. Loudness normalization stays in the requested format.
- **New voice catalog**: `marin`, `cedar`, `ballad`, `verse` (gpt-4o-mini-tts only), with model compatibility validation in the service handler so misuse surfaces a clear error instead of an opaque OpenAI 400.
- **Volume-restore overhold fix** for blocking TTS targets (Music Assistant, Sonos). The hold window now starts at speak-issued time, not speak-completed, so the post-audio idle is bounded by the buffer (~1.5s) instead of stretching to the full clip duration.
- **Cache poisoning fix (issue #64)**: stale failure sentinels stop blocking cached audio playback after a recovered API error. The sentinel is also actively cleared from the shared cache when a successful speak surfaces it, so the false-positive does not recur.
- **Account name** field on the parent entry to distinguish multiple OpenAI accounts in the integrations list.
- **Auto-release CI**: pushing a `v*` tag now creates a GitHub release. The workflow validates the tag matches `manifest.json`, marks beta tags (`b` / `-rc`) as pre-release, and uses `WHATSNEW.md` for the description when present.

### Other fixes and refinements

Faster, more stable volume control; tighter restore timing; HA URL extension respects the chosen format; full translation parity for `en` / `cs` / `de` / `el`; clearer field descriptions in service and config flow.
