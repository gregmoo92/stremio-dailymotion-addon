"""Pydantic dataclasses for every pipeline artifact.

Every artifact written to disk is one of these models.  Re-runs
deserialize the on-disk JSON back into these instances; the orchestrator
hashes the model JSON to decide whether a stage needs to re-run.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class BeatKind(str, Enum):
    SCENE_HEADING = "scene_heading"
    ACTION = "action"
    CHARACTER_CUE = "character_cue"
    PARENTHETICAL = "parenthetical"
    DIALOGUE = "dialogue"
    TRANSITION = "transition"


class Beat(BaseModel):
    """One atomic unit of screenplay text."""

    beat_id: str = Field(description="Stable ID: scene_idx-beat_idx")
    scene_idx: int
    beat_idx: int
    kind: BeatKind
    text: str
    character: str | None = Field(
        default=None,
        description="For DIALOGUE/PARENTHETICAL/CHARACTER_CUE: the speaking character.",
    )
    extension: str | None = Field(
        default=None,
        description="V.O., O.S., CONT'D etc., stripped from the character cue.",
    )


class Scene(BaseModel):
    scene_idx: int
    heading: str
    beat_ids: list[str]


class Screenplay(BaseModel):
    title: str | None = None
    scenes: list[Scene]
    beats: list[Beat]

    def beats_by_scene(self, scene_idx: int) -> list[Beat]:
        wanted = {b for s in self.scenes if s.scene_idx == scene_idx for b in s.beat_ids}
        return [b for b in self.beats if b.beat_id in wanted]

    def dialogue_beats(self) -> list[Beat]:
        return [b for b in self.beats if b.kind == BeatKind.DIALOGUE]


class CharacterProfile(BaseModel):
    name: str
    age_range: str = Field(description="e.g. '20s', '40-50', 'teen', 'older'")
    gender: Literal["male", "female", "non_binary", "unspecified"]
    vocal_qualities: list[str] = Field(
        description="Adjectives: e.g. 'gravelly', 'measured', 'breathy', 'precise'."
    )
    accent_or_origin: str | None = Field(
        default=None, description="e.g. 'British (RP)', 'rural Texas', None if unspecified."
    )
    emotional_baseline: str = Field(description="Default emotional state at rest.")
    motivations: list[str] = Field(description="What drives the character in this script.")
    arc_summary: str = Field(description="One-sentence arc across the screenplay.")
    role_size: Literal["lead", "supporting", "minor", "background"]


class PerformanceDSL(BaseModel):
    """Performance direction in provider-agnostic axes.

    The caster translates these to ElevenLabs voice_settings deterministically.
    """

    arousal: float = Field(ge=0.0, le=1.0, description="Energy/intensity. 0=flat, 1=peak.")
    valence: float = Field(ge=-1.0, le=1.0, description="Negative=dark, positive=bright.")
    control: float = Field(
        ge=0.0, le=1.0,
        description="Composure. 0=unraveling/volatile, 1=tightly held."
    )
    effort: float = Field(
        ge=0.0, le=1.0,
        description="Vocal effort. 0=whispered, 1=shouted."
    )
    pace: float = Field(ge=0.0, le=1.0, description="Speaking rate. 0=slow, 1=fast.")
    pause_before_ms: int = Field(ge=0, le=10_000, description="Silence before this line.")


class VoiceProfile(BaseModel):
    """Output of the per-character voice-profiling sub-agent."""

    character: str
    recommended_voice_traits: list[str] = Field(
        description="Specific vocal recommendations for casting: e.g. 'low register, "
        "British, weathered, measured cadence'."
    )
    base_dsl: PerformanceDSL = Field(
        description="The character's default performance state, as DSL axes."
    )
    casting_notes: str = Field(description="One-paragraph note for the caster.")


class LexiconEntry(BaseModel):
    surface: str = Field(description="The word as it appears in the screenplay.")
    pronunciation: str = Field(
        description="A phonetic respelling readable by ElevenLabs (e.g. 'KOH-vahl-ski')."
    )
    notes: str | None = None


class Lexicon(BaseModel):
    entries: list[LexiconEntry] = Field(default_factory=list)

    def apply(self, text: str) -> str:
        """Substitute any matching surface form with its pronunciation."""
        out = text
        for e in sorted(self.entries, key=lambda x: -len(x.surface)):
            out = out.replace(e.surface, e.pronunciation)
        return out


class VoiceSettings(BaseModel):
    """ElevenLabs voice_settings, the actual API knobs."""

    stability: float = Field(ge=0.0, le=1.0)
    similarity_boost: float = Field(ge=0.0, le=1.0)
    style: float = Field(ge=0.0, le=1.0)
    use_speaker_boost: bool = True
    speed: float = Field(ge=0.7, le=1.2, default=1.0)


class CastingEntry(BaseModel):
    character: str
    voice_id: str
    voice_name: str
    base_voice_settings: VoiceSettings
    rationale: str


class Casting(BaseModel):
    tts_model_id: str
    entries: list[CastingEntry]

    def by_character(self) -> dict[str, CastingEntry]:
        return {e.character: e for e in self.entries}


class Direction(BaseModel):
    """Per-line performance direction."""

    beat_id: str
    character: str
    text: str
    dsl: PerformanceDSL
    subtext: str = Field(description="What the character is actually communicating beneath the text.")
    intent: str = Field(description="What they want from the other party in this line.")


class DirectionTrack(BaseModel):
    scene_idx: int
    directions: list[Direction]


class RenderRecord(BaseModel):
    line_id: str
    beat_id: str
    character: str
    voice_id: str
    voice_settings: VoiceSettings
    seed: int
    text: str
    previous_text: str | None
    next_text: str | None
    render_hash: str
    wav_path: str
    duration_ms: int


class Manifest(BaseModel):
    run_uuid: str
    tts_model_id: str
    records: list[RenderRecord]


class InputsLock(BaseModel):
    """Per-stage input hashes.  Unchanged hash => stage skipped on re-run."""

    parse: str | None = None
    repair: str | None = None
    extract_characters: str | None = None
    profile_voice: str | None = None
    build_lexicon: str | None = None
    cast: str | None = None
    direct_scenes: str | None = None
