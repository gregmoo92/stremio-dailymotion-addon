"""Claude-driven analysis stages.

Each stage is an async function that takes the relevant inputs and returns a
pydantic model.  All Claude calls go through the helpers `_parse_create` and
`_parse_stream` which centralize:

- model + thinking + effort selection
- structured-output JSON schema enforcement
- prompt-prefix caching (the screenplay is the stable cached prefix)
- pydantic validation of the response
"""

from __future__ import annotations

import asyncio
import os
from typing import TypeVar

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from .models import (
    Beat,
    BeatKind,
    Casting,
    CharacterProfile,
    Direction,
    DirectionTrack,
    Lexicon,
    PerformanceDSL,
    Screenplay,
    VoiceProfile,
)

# ---------------------------------------------------------------------------
# Models per call
# ---------------------------------------------------------------------------

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

_CLAUDE_SEMAPHORE = asyncio.Semaphore(int(os.environ.get("TR_CLAUDE_CONCURRENCY", "8")))


# ---------------------------------------------------------------------------
# Wrappers around the Anthropic SDK
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


def _add_additional_properties_false(schema: dict) -> dict:
    """Recursively force ``additionalProperties: false`` on every object schema.

    Anthropic's structured-output endpoint rejects an object schema that
    doesn't pin this explicitly; pydantic's default ``model_json_schema()``
    leaves it implicit.
    """
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            schema.setdefault("additionalProperties", False)
        for key in ("properties", "$defs", "definitions"):
            sub = schema.get(key)
            if isinstance(sub, dict):
                for v in sub.values():
                    _add_additional_properties_false(v)
        for key in ("items", "not"):
            sub = schema.get(key)
            if isinstance(sub, dict):
                _add_additional_properties_false(sub)
        # additionalProperties may itself be a schema (rare, but happens).
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            _add_additional_properties_false(ap)
        for key in ("anyOf", "oneOf", "allOf"):
            sub = schema.get(key)
            if isinstance(sub, list):
                for s in sub:
                    _add_additional_properties_false(s)
    return schema


def _schema_for(model: type[BaseModel], name: str) -> dict:
    # Anthropic structured-output format object: {"type": "json_schema",
    # "schema": ...}.  No "name" field — the API rejects it as an extra input.
    schema = _add_additional_properties_false(model.model_json_schema())
    schema.setdefault("title", name)
    return {
        "type": "json_schema",
        "schema": schema,
    }


def _cached_user_prefix(screenplay_text: str) -> dict:
    """A user content block holding the screenplay, marked for prompt caching.

    Caching is keyed on the exact bytes of every block before (and including)
    the cache_control marker, so this block must be byte-identical across
    every call that wants to reuse the cache.
    """
    return {
        "type": "text",
        "text": f"<screenplay>\n{screenplay_text}\n</screenplay>",
        "cache_control": {"type": "ephemeral"},
    }


async def _parse_create(
    client: AsyncAnthropic,
    *,
    model: str,
    system: str,
    user_blocks: list[dict],
    response_model: type[T],
    response_name: str,
    thinking: dict | None,
    effort: str | None,
    max_tokens: int = 16_000,
) -> T:
    """Non-streaming structured-output call.  Use only when output is small."""

    extra: dict = {}
    if thinking is not None:
        extra["thinking"] = thinking
    output_config: dict = {"format": _schema_for(response_model, response_name)}
    if effort is not None:
        output_config["effort"] = effort

    async with _CLAUDE_SEMAPHORE:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_blocks}],
            output_config=output_config,
            **extra,
        )

    text = _extract_text(response)
    return response_model.model_validate_json(text)


async def _parse_stream(
    client: AsyncAnthropic,
    *,
    model: str,
    system: str,
    user_blocks: list[dict],
    response_model: type[T],
    response_name: str,
    thinking: dict | None,
    effort: str | None,
    max_tokens: int = 32_000,
) -> T:
    """Streaming structured-output call.  Required for max_tokens > ~16K."""

    extra: dict = {}
    if thinking is not None:
        extra["thinking"] = thinking
    output_config: dict = {"format": _schema_for(response_model, response_name)}
    if effort is not None:
        output_config["effort"] = effort

    async with _CLAUDE_SEMAPHORE:
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_blocks}],
            output_config=output_config,
            **extra,
        ) as stream:
            final = await stream.get_final_message()

    text = _extract_text(final)
    return response_model.model_validate_json(text)


def _extract_text(response: anthropic.types.Message) -> str:
    """Concatenate every text block in the response."""
    parts: list[str] = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    if not parts:
        raise RuntimeError(
            f"Claude returned no text blocks (stop_reason={response.stop_reason})."
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Stage 1: parser repair (Sonnet, mechanical)
# ---------------------------------------------------------------------------


class _RepairedScreenplay(BaseModel):
    beats: list[Beat]


REPAIR_SYSTEM = """You are a screenplay-format expert correcting a regex-based
parser's output.  Given the original screenplay text and the parser's
best-effort beat list, return a corrected beat list that:

- Splits dual dialogue into separate beats for each speaker.
- Resolves (MORE)/(CONT'D) by merging continuation dialogue into the
  preceding character's utterance and dropping the cue.
- Strips V.O., O.S., CONT'D, and similar parenthetical extensions from the
  character cue, putting them in `extension` instead.
- Preserves scene_idx and beat_idx ordering of the original where possible;
  re-assign beat_id as `{scene_idx}-{beat_idx}` if you change positions.
- Does not invent dialogue, action, or characters that aren't in the source.

Return only the corrected beats array.  Do not return scene headings as a
separate list."""


async def repair_beats(
    client: AsyncAnthropic,
    screenplay_text: str,
    raw: Screenplay,
) -> Screenplay:
    raw_beats_json = [b.model_dump(mode="json") for b in raw.beats]
    user_blocks: list[dict] = [
        _cached_user_prefix(screenplay_text),
        {
            "type": "text",
            "text": (
                "Here is the regex parser's beat list as JSON:\n"
                f"{raw_beats_json}\n\n"
                "Return the corrected beats."
            ),
        },
    ]
    repaired = await _parse_create(
        client,
        model=SONNET,
        system=REPAIR_SYSTEM,
        user_blocks=user_blocks,
        response_model=_RepairedScreenplay,
        response_name="RepairedScreenplay",
        thinking={"type": "disabled"},
        effort=None,
        max_tokens=16_000,
    )
    # Rebuild scenes from the repaired beats so scene.beat_ids stays in sync.
    return _rebuild_scenes(raw.title, repaired.beats)


def _rebuild_scenes(title: str | None, beats: list[Beat]) -> Screenplay:
    from .models import Scene  # local import to avoid cycle

    scenes: list[Scene] = []
    current_scene_idx = -1
    current_heading = ""
    current_beat_ids: list[str] = []

    def flush() -> None:
        nonlocal current_beat_ids
        if current_scene_idx >= 0 and current_beat_ids:
            scenes.append(
                Scene(
                    scene_idx=current_scene_idx,
                    heading=current_heading,
                    beat_ids=list(current_beat_ids),
                )
            )
        current_beat_ids = []

    for b in beats:
        if b.kind == BeatKind.SCENE_HEADING:
            flush()
            current_scene_idx = b.scene_idx
            current_heading = b.text
        current_beat_ids.append(b.beat_id)
    flush()
    if not scenes and beats:
        scenes.append(
            Scene(scene_idx=0, heading="(unspecified)", beat_ids=[b.beat_id for b in beats])
        )
    return Screenplay(title=title, scenes=scenes, beats=beats)


# ---------------------------------------------------------------------------
# Stage 2: character extraction (Opus, adaptive thinking)
# ---------------------------------------------------------------------------


class _CharacterList(BaseModel):
    characters: list[CharacterProfile]


EXTRACT_SYSTEM = """You are a casting director and dramaturg analyzing a
screenplay.  Identify every speaking character and produce a profile for
each that will guide voice casting and direction.

Be specific about vocal qualities and emotional baselines: not "tough" but
"clipped, breath-light, restrained even when angry."  Motivations should
reflect what the character actually wants in this script, not generic
archetype descriptions.  arc_summary in one sentence; do not summarize the
whole plot.

Only return characters who speak at least once.  Group ROSIE and ROSIE
(O.S.) as the same character."""


async def extract_characters(
    client: AsyncAnthropic,
    screenplay_text: str,
) -> list[CharacterProfile]:
    user_blocks: list[dict] = [
        _cached_user_prefix(screenplay_text),
        {
            "type": "text",
            "text": "Identify every speaking character and return their profiles.",
        },
    ]
    result = await _parse_create(
        client,
        model=OPUS,
        system=EXTRACT_SYSTEM,
        user_blocks=user_blocks,
        response_model=_CharacterList,
        response_name="CharacterList",
        thinking={"type": "adaptive"},
        effort="high",
        max_tokens=16_000,
    )
    # Stable order for downstream caching.
    return sorted(result.characters, key=lambda c: c.name)


# ---------------------------------------------------------------------------
# Stage 3: per-character voice profiling (Opus, adaptive thinking, parallel)
# ---------------------------------------------------------------------------


VOICE_PROFILE_SYSTEM = """You are a voice director.  Given a screenplay and a
character profile, produce a voice profile that:

- Lists 4-8 specific vocal traits (register, timbre, pace, accent if any,
  characteristic mannerisms) suitable as casting criteria.
- Sets the character's BASELINE PerformanceDSL: arousal/valence/control/
  effort/pace/pause_before_ms representing their default emotional state at
  rest, not their peak.  Per-line direction will deviate from this baseline.
- Writes a single paragraph of casting notes the caster will use to pick a
  voice ID from a catalog.

Be concrete.  "Low-medium register, slight rasp, deliberate cadence with
late-vowel emphasis" beats "deep voice"."""


async def profile_voice(
    client: AsyncAnthropic,
    screenplay_text: str,
    character: CharacterProfile,
) -> VoiceProfile:
    user_blocks: list[dict] = [
        _cached_user_prefix(screenplay_text),
        {
            "type": "text",
            "text": (
                "Character profile:\n"
                f"{character.model_dump_json(indent=2)}\n\n"
                "Return their voice profile."
            ),
        },
    ]
    return await _parse_create(
        client,
        model=OPUS,
        system=VOICE_PROFILE_SYSTEM,
        user_blocks=user_blocks,
        response_model=VoiceProfile,
        response_name="VoiceProfile",
        thinking={"type": "adaptive"},
        effort="high",
        max_tokens=8_000,
    )


async def profile_voices(
    client: AsyncAnthropic,
    screenplay_text: str,
    characters: list[CharacterProfile],
) -> list[VoiceProfile]:
    """Parallel voice profiling across all characters."""
    tasks = [profile_voice(client, screenplay_text, c) for c in characters]
    return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Stage 4: lexicon (Sonnet, no thinking)
# ---------------------------------------------------------------------------


LEXICON_SYSTEM = """You build a pronunciation lexicon for a TTS system.
Given a screenplay, list any proper nouns, foreign words, invented terms,
or unusual names whose default English-speaker pronunciation is likely
wrong, and provide a phonetic respelling using simple uppercase syllables
joined by hyphens (e.g. KOH-vahl-ski, LAY-mee).

Skip common English words.  Skip character names that are unambiguous
(e.g. MAYA, KOWALSKI is fine if you'd say it correctly).  When in doubt,
include it."""


async def build_lexicon(
    client: AsyncAnthropic,
    screenplay_text: str,
) -> Lexicon:
    user_blocks: list[dict] = [
        _cached_user_prefix(screenplay_text),
        {
            "type": "text",
            "text": "Return the pronunciation lexicon.",
        },
    ]
    return await _parse_create(
        client,
        model=SONNET,
        system=LEXICON_SYSTEM,
        user_blocks=user_blocks,
        response_model=Lexicon,
        response_name="Lexicon",
        thinking={"type": "disabled"},
        effort=None,
        max_tokens=4_000,
    )


# ---------------------------------------------------------------------------
# Stage 5: per-scene direction (Opus, adaptive thinking, parallel per scene)
# ---------------------------------------------------------------------------


DIRECT_SCENE_SYSTEM = """You direct a table-read voice performance.

For each DIALOGUE beat in the named scene, produce a Direction:
- text: the exact dialogue text from the beat (do not paraphrase)
- subtext: what the character is *actually* communicating beneath the words
- intent: what they want from the other party in this line
- dsl: PerformanceDSL axes representing the delivery of THIS line, deviating
  from the character's baseline as the moment demands:
  * arousal 0..1: energy/intensity
  * valence -1..1: dark to bright
  * control 0..1: composed (1) to volatile/unraveling (0)
  * effort 0..1: whisper (0) to shout (1)
  * pace 0..1: slow (0) to fast (1)
  * pause_before_ms: silence preceding this line, 0..10000

Use the action lines and parentheticals immediately surrounding each
dialogue beat as direction context.  pause_before_ms should be larger after
heavy action beats or charged silence; smaller in rapid-fire exchange.

Return Directions ONLY for beats whose kind is 'dialogue'.  Skip action,
parentheticals, scene headings, transitions, character cues."""


async def direct_scene(
    client: AsyncAnthropic,
    screenplay_text: str,
    characters_blob: str,
    casting_blob: str,
    scene_idx: int,
    scene_beats: list[Beat],
) -> DirectionTrack:
    beats_blob = "\n".join(
        f"[{b.beat_id}] {b.kind.value} "
        f"{('(' + b.character + ')') if b.character else ''}"
        f"{(' ext=' + b.extension) if b.extension else ''}: {b.text}"
        for b in scene_beats
    )

    user_blocks: list[dict] = [
        _cached_user_prefix(screenplay_text),
        {
            "type": "text",
            "text": (
                f"Cast and characters (for context):\n{characters_blob}\n\n"
                f"Casting decisions (informational):\n{casting_blob}\n\n"
                f"Scene {scene_idx} beats (annotate dialogue lines only):\n"
                f"{beats_blob}"
            ),
        },
    ]

    class _DirectionList(BaseModel):
        directions: list[Direction]

    result = await _parse_stream(
        client,
        model=OPUS,
        system=DIRECT_SCENE_SYSTEM,
        user_blocks=user_blocks,
        response_model=_DirectionList,
        response_name="DirectionList",
        thinking={"type": "adaptive"},
        effort="high",
        max_tokens=32_000,
    )

    return DirectionTrack(scene_idx=scene_idx, directions=result.directions)


async def direct_screenplay(
    client: AsyncAnthropic,
    screenplay_text: str,
    screenplay: Screenplay,
    characters: list[CharacterProfile],
    casting: Casting,
) -> list[DirectionTrack]:
    characters_blob = "\n".join(
        f"- {c.name} ({c.role_size}): {c.arc_summary}" for c in characters
    )
    casting_blob = "\n".join(
        f"- {e.character} -> {e.voice_name} ({e.voice_id})"
        for e in casting.entries
    )
    tasks = [
        direct_scene(
            client,
            screenplay_text,
            characters_blob,
            casting_blob,
            scene.scene_idx,
            screenplay.beats_by_scene(scene.scene_idx),
        )
        for scene in screenplay.scenes
    ]
    return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Stage 6: catalog matching (Sonnet, mechanical)
# ---------------------------------------------------------------------------


CAST_SYSTEM = """You match characters to TTS voices.  Given character
profiles, voice profiles, and a catalog of available voices, choose the
single best voice_id for each character.  Optimize for:

1. Distinguishability across the cast (don't pick the same voice or two
   indistinguishable voices for two characters).
2. Match to recommended_voice_traits (gender, age band, accent, qualities).
3. Coverage of the role (lead voices that can carry many lines vs. brief
   supporting voices).

For each character, also output base_voice_settings tuned to their baseline
PerformanceDSL.  Use these heuristics as a starting point:

  stability        = clamp01(0.3 + 0.5 * control)        # composed -> stable
  similarity_boost = 0.75                                 # default
  style            = clamp01(0.2 + 0.5 * arousal)        # higher arousal -> more style
  speed            = clamp(0.85 + 0.3 * pace, 0.7, 1.2)
  use_speaker_boost = true

Then nudge by character: heavier/older voices benefit from slightly higher
stability; expressive leads benefit from slightly lower stability + higher
style.

Return rationale (1-2 sentences) per character explaining the choice."""


async def cast_characters(
    client: AsyncAnthropic,
    characters: list[CharacterProfile],
    voice_profiles: list[VoiceProfile],
    voice_catalog: dict,
    tts_model_id: str,
) -> Casting:
    user_blocks: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Voice catalog (use only voice_ids from this list):\n"
                f"{voice_catalog}\n\n"
                f"Character profiles:\n"
                f"{[c.model_dump(mode='json') for c in characters]}\n\n"
                f"Voice profiles:\n"
                f"{[v.model_dump(mode='json') for v in voice_profiles]}\n\n"
                f"Return Casting with tts_model_id='{tts_model_id}'."
            ),
        },
    ]
    return await _parse_create(
        client,
        model=SONNET,
        system=CAST_SYSTEM,
        user_blocks=user_blocks,
        response_model=Casting,
        response_name="Casting",
        thinking={"type": "disabled"},
        effort=None,
        max_tokens=8_000,
    )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def baseline_dsl(profile: VoiceProfile) -> PerformanceDSL:
    return profile.base_dsl
