"""The ffmpeg audio filter chain applied to synthesised speech.

Both delivery paths use the chain built here: the atomic path runs it
against a finished file in ``utils``, and the streaming path runs it on
a pipe in ``loudness``. Deciding in one place whether a filter is needed
at all keeps the two paths from disagreeing about it.

This module imports nothing from Home Assistant, so it can be tested on
its own.
"""
from __future__ import annotations

# Loudness correction for speech.
#
# Two filters in series, and both are needed for different reasons.
#
# ``dynaudnorm`` replaced ``loudnorm=I=-16:TP=-1:LRA=5``. Measured
# against three engines on 2026-08-29, loudnorm made a short OpenAI clip
# six decibels QUIETER than it started (-24.25 LUFS in, -30.20 out): it
# is designed to run in two passes, and given only one it has not
# converged before a two second announcement ends. dynaudnorm corrects
# continuously instead, so it needs neither a second pass nor a complete
# file, which is also what lets normalisation run on a stream.
#
# ``acompressor`` in front of it answers a separate complaint: the clip
# was loud enough on average while individual words were still swallowed.
# Levelling the average does not lift a quiet syllable, and a fast
# compressor does. Measured on a thirteen second announcement, the
# quiet passages rose from -17.4 to -14.8 LUFS with the loudness range
# unchanged at 1.9 LU.
#
# The peak target is 0.85 rather than the filter's own 0.95 default.
# At 0.95 two of the three engines came out above -0.5 dBTP once the mp3
# encoder had added its overshoot, which clips. At 0.85 the worst of the
# three sits at -1.29 dBTP with the quiet passages only half a decibel
# lower, which is a good trade.
LOUDNESS_FILTER = (
    "acompressor=threshold=-28dB:ratio=4:attack=3:release=60,"
    "dynaudnorm=p=0.85:m=20:f=40:g=5"
)

# The gain a profile may apply, in decibels.
GAIN_DB_MIN = -12.0
GAIN_DB_MAX = 12.0
GAIN_DB_STEP = 0.5
DEFAULT_GAIN_DB = 0.0

# A positive gain can push peaks past full scale, so a limiter follows
# it. Three details of this limiter were settled by measurement, on a
# 22 second OpenAI clip at -21.1 LUFS with ffmpeg 8.1 on 2026-09-30.
#
# ``level=disabled`` is required. By default alimiter scales its output
# back up to full scale, which would turn a gain setting into a second
# normaliser.
#
# The limiter runs at four times the output rate. At the output rate it
# only holds sample peaks, and the peaks between samples still reached
# +0.4 dBTP on +12 dB of gain once encoded to mp3. Oversampled, the
# worst case over the whole range, with and without normalisation, was
# -0.7 dBTP. Loudness was unchanged, and the extra cost was 10 ms on
# that clip.
#
# The ceiling is 0.89, about -1 dBFS, which leaves the margin the mp3
# encoder needs for its own overshoot.
_LIMITER = (
    "aresample=96000,"
    "alimiter=limit=0.89:level=disabled,"
    "aresample=24000"
)


def clamp_gain_db(value: object) -> float:
    """Return ``value`` as a gain within the allowed range.

    A stored value that is missing or unreadable counts as no gain, so a
    profile that predates the setting behaves exactly as it did.
    """
    try:
        gain = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_GAIN_DB
    if gain != gain:  # NaN
        return DEFAULT_GAIN_DB
    return max(GAIN_DB_MIN, min(GAIN_DB_MAX, gain))


def build_audio_filter(normalize: bool, gain_db: object = 0.0) -> str | None:
    """Return the ffmpeg ``-af`` chain for these settings, or ``None``.

    ``None`` means there is nothing to apply, so the caller can hand the
    provider's audio through untouched.

    The gain comes after normalisation on purpose. Placed before it,
    ``dynaudnorm`` would level most of the gain away again.
    """
    gain = clamp_gain_db(gain_db)
    steps: list[str] = []
    if normalize:
        steps.append(LOUDNESS_FILTER)
    if gain != 0.0:
        steps.append(f"volume={gain:g}dB")
        if gain > 0.0:
            steps.append(_LIMITER)
    return ",".join(steps) or None
