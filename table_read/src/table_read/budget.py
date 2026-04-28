"""Pre-flight cost estimator.

Numbers below are list prices (USD) at time of writing.  They're meant to
be in the ballpark, not exact billing, and can be adjusted via env vars.

  * Anthropic: claude-opus-4-7 input $5/M output $25/M, claude-sonnet-4-6
    input $3/M output $15/M.  Cache reads ~10% of input, writes ~125%.
  * ElevenLabs: roughly per-character; eleven_multilingual_v2 falls in the
    $0.30/1k char band on developer plans.  We estimate using
    TR_ELEVENLABS_USD_PER_KCHAR (default 0.30).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .models import Screenplay


# --- Anthropic price knobs ---------------------------------------------------

OPUS_IN = float(os.environ.get("TR_OPUS_IN_USD_PER_MTOK", "5.00"))
OPUS_OUT = float(os.environ.get("TR_OPUS_OUT_USD_PER_MTOK", "25.00"))
SONNET_IN = float(os.environ.get("TR_SONNET_IN_USD_PER_MTOK", "3.00"))
SONNET_OUT = float(os.environ.get("TR_SONNET_OUT_USD_PER_MTOK", "15.00"))
CACHE_READ_MULT = 0.10  # cache reads are ~1/10 of input price
CACHE_WRITE_MULT = 1.25  # cache writes ~1.25x

# Crude tokens-per-char approximation for English text (1 token ~= 4 chars).
CHARS_PER_TOKEN = 4.0


# --- ElevenLabs price knobs --------------------------------------------------

ELEVEN_USD_PER_KCHAR = float(os.environ.get("TR_ELEVENLABS_USD_PER_KCHAR", "0.30"))


@dataclass
class Estimate:
    claude_input_tokens: int
    claude_output_tokens: int
    claude_usd: float
    eleven_chars: int
    eleven_usd: float

    @property
    def total_usd(self) -> float:
        return self.claude_usd + self.eleven_usd

    def pretty(self) -> str:
        return (
            f"Claude:    ~{self.claude_input_tokens:,} input + "
            f"{self.claude_output_tokens:,} output tokens  ~${self.claude_usd:0.3f}\n"
            f"ElevenLabs: ~{self.eleven_chars:,} characters of dialogue  "
            f"~${self.eleven_usd:0.3f}\n"
            f"Estimated total:                                          "
            f"~${self.total_usd:0.3f}"
        )


def estimate(screenplay: Screenplay) -> Estimate:
    """Rough estimate using a typical pipeline shape.

    Pipeline calls per run (no skipping):
      * 1x repair (Sonnet)
      * 1x extract_characters (Opus)
      * Nx profile_voice (Opus, N = number of speaking characters)
      * 1x build_lexicon (Sonnet)
      * 1x cast (Sonnet)
      * Sx direct_scene (Opus, S = number of scenes)

    We assume the screenplay text is sent as a cached prefix.  The first
    call pays the cache write; the rest pay cache reads.
    """
    text_chars = sum(len(b.text) for b in screenplay.beats)
    text_tokens = int(text_chars / CHARS_PER_TOKEN)

    # Estimate distinct speaking characters (used for voice-profile fan-out).
    speakers = sorted({b.character for b in screenplay.dialogue_beats() if b.character})
    n_speakers = max(1, len(speakers))
    n_scenes = max(1, len(screenplay.scenes))
    n_dialogue = sum(1 for b in screenplay.dialogue_beats())

    # Per-call output tokens (rough order-of-magnitude budgets).
    output_per_call = {
        "repair": 4_000,
        "extract": 6_000,
        "voice_per_char": 1_000,
        "lexicon": 1_000,
        "cast": 2_000,
        "direct_per_scene": 4_000,
    }

    # Sonnet calls.
    sonnet_in = (
        text_tokens          # repair
        + text_tokens        # lexicon
        + 2_000              # cast (no screenplay prefix)
    )
    sonnet_out = (
        output_per_call["repair"]
        + output_per_call["lexicon"]
        + output_per_call["cast"]
    )
    sonnet_cost = (
        sonnet_in * SONNET_IN / 1_000_000
        + sonnet_out * SONNET_OUT / 1_000_000
    )

    # Opus calls.  We model 1 cache write + (N-1) cache reads on the
    # screenplay portion across all calls in the same prompt-prefix family.
    n_opus_calls = 1 + n_speakers + n_scenes
    opus_in_screenplay_write = text_tokens * CACHE_WRITE_MULT
    opus_in_screenplay_reads = (n_opus_calls - 1) * text_tokens * CACHE_READ_MULT
    opus_in_other = 1_000 * n_opus_calls  # per-call non-cached suffix
    opus_in = opus_in_screenplay_write + opus_in_screenplay_reads + opus_in_other
    opus_out = (
        output_per_call["extract"]
        + n_speakers * output_per_call["voice_per_char"]
        + n_scenes * output_per_call["direct_per_scene"]
    )
    opus_cost = (
        opus_in * OPUS_IN / 1_000_000
        + opus_out * OPUS_OUT / 1_000_000
    )

    claude_in_total = int(sonnet_in + opus_in)
    claude_out_total = sonnet_out + opus_out
    claude_usd = sonnet_cost + opus_cost

    eleven_chars = sum(
        len(b.text) for b in screenplay.dialogue_beats()
    )
    eleven_usd = eleven_chars / 1000 * ELEVEN_USD_PER_KCHAR

    return Estimate(
        claude_input_tokens=claude_in_total,
        claude_output_tokens=claude_out_total,
        claude_usd=claude_usd,
        eleven_chars=eleven_chars,
        eleven_usd=eleven_usd,
    )
