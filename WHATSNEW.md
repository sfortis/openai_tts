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
