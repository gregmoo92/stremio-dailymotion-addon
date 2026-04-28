"""Casting helpers: catalog loader, override merge, DSL -> voice_settings."""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    Casting,
    CastingEntry,
    PerformanceDSL,
    VoiceSettings,
)


def load_voice_catalog(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def dsl_to_voice_settings(dsl: PerformanceDSL) -> VoiceSettings:
    """Deterministic translation from performance DSL to ElevenLabs settings.

    The mapping is intentionally simple and inspectable:

    - Higher control => more stable delivery (less prosodic variation).
    - Higher arousal => more stylistic exaggeration.
    - Pace nudges TTS speed in a small band around 1.0.
    - similarity_boost stays at the catalog default; we do not vary it
      per-line, since it primarily affects voice clarity vs. speaker.

    Override this function to tune the mapping for your taste.
    """
    stability = _clamp(0.30 + 0.50 * dsl.control, 0.0, 1.0)
    similarity_boost = 0.75
    style = _clamp(0.20 + 0.50 * dsl.arousal, 0.0, 1.0)
    # Effort and pace both tug speed slightly: high effort + high pace = ~1.1,
    # low effort + low pace = ~0.85.
    speed = _clamp(0.85 + 0.20 * dsl.pace + 0.10 * dsl.effort, 0.7, 1.2)
    return VoiceSettings(
        stability=stability,
        similarity_boost=similarity_boost,
        style=style,
        use_speaker_boost=True,
        speed=speed,
    )


def merge_settings(base: VoiceSettings, override: PerformanceDSL) -> VoiceSettings:
    """Apply per-line DSL on top of a character's base settings.

    The base settings already encode the character's baseline; the per-line
    DSL is the moment-to-moment direction.  We let the per-line DSL fully
    determine the per-line settings rather than blending, so a "screaming"
    line gets full intensity even if the character's baseline is calm.
    """
    return dsl_to_voice_settings(override)


def apply_override_json(casting: Casting, override_path: str | Path | None) -> Casting:
    """Merge a user override JSON onto a Casting object.

    The override JSON has the shape:
        {"CHARACTER NAME": {"voice_id": "...", "voice_name": "..."}}
    Anything missing falls through to the original casting.
    """
    if override_path is None:
        return casting
    p = Path(override_path)
    if not p.exists():
        return casting
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return casting

    by_char = casting.by_character()
    new_entries: list[CastingEntry] = []
    for entry in casting.entries:
        ov = raw.get(entry.character)
        if isinstance(ov, dict) and "voice_id" in ov:
            new_entries.append(
                entry.model_copy(
                    update={
                        "voice_id": ov["voice_id"],
                        "voice_name": ov.get("voice_name", ov["voice_id"]),
                        "rationale": ov.get(
                            "rationale", "User override (apply_override_json)."
                        ),
                    }
                )
            )
        else:
            new_entries.append(entry)
    # Allow overrides to add characters that weren't in the original casting.
    for name, ov in raw.items():
        if name in by_char or not isinstance(ov, dict) or "voice_id" not in ov:
            continue
        new_entries.append(
            CastingEntry(
                character=name,
                voice_id=ov["voice_id"],
                voice_name=ov.get("voice_name", ov["voice_id"]),
                base_voice_settings=VoiceSettings(
                    stability=0.55,
                    similarity_boost=0.75,
                    style=0.30,
                    use_speaker_boost=True,
                    speed=1.0,
                ),
                rationale=ov.get("rationale", "Added by override JSON."),
            )
        )
    return Casting(tts_model_id=casting.tts_model_id, entries=new_entries)
