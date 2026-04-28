"""End-to-end async pipeline + on-disk stage-skip logic."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from . import analyzer, screenplay as sp_module
from .caster import apply_override_json
from .hashing import sha256_hex
from .models import (
    Casting,
    CharacterProfile,
    DirectionTrack,
    InputsLock,
    Lexicon,
    Manifest,
    Screenplay,
    VoiceProfile,
)
from .renderer import ElevenLabsRenderer, render_screenplay

log = logging.getLogger("table_read")


# ---------------------------------------------------------------------------
# On-disk artifact paths
# ---------------------------------------------------------------------------


@dataclass
class Artifacts:
    out_dir: Path
    raw_text: Path
    beats: Path
    characters: Path
    voice_profiles: Path
    lexicon: Path
    casting: Path
    casting_locked: Path
    casting_override: Path
    direction: Path
    manifest: Path
    inputs_lock: Path
    lines_dir: Path
    runs_dir: Path

    @classmethod
    def at(cls, out_dir: Path) -> "Artifacts":
        return cls(
            out_dir=out_dir,
            raw_text=out_dir / "screenplay.txt",
            beats=out_dir / "beats.json",
            characters=out_dir / "characters.json",
            voice_profiles=out_dir / "voice_profiles.json",
            lexicon=out_dir / "lexicon.json",
            casting=out_dir / "casting.json",
            casting_locked=out_dir / "casting.locked.json",
            casting_override=out_dir / "casting.override.json",
            direction=out_dir / "direction.json",
            manifest=out_dir / "manifest.json",
            inputs_lock=out_dir / "inputs.lock.json",
            lines_dir=out_dir / "lines",
            runs_dir=out_dir / "runs",
        )


def _save_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        model.model_dump_json(indent=2),
        encoding="utf-8",
    )


def _load_model(path: Path, model_cls: type[BaseModel]) -> BaseModel | None:
    if not path.exists():
        return None
    return model_cls.model_validate_json(path.read_text(encoding="utf-8"))


def _save_list(path: Path, items: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([i.model_dump(mode="json") for i in items], indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _load_list(path: Path, model_cls: type[BaseModel]) -> list | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [model_cls.model_validate(x) for x in raw]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class RunOptions:
    voice_catalog_path: Path
    out_dir: Path
    skip_repair: bool = False  # disable Claude repair (regex-only beats)
    force_recast: bool = False  # ignore casting.locked.json
    log_cache_hits: bool = True


async def run(
    *,
    script_path: Path,
    options: RunOptions,
) -> dict:
    """Run the full pipeline.  Re-uses on-disk artifacts when their inputs
    haven't changed, recomputes everything that has.

    Returns a dict with the final manifest path and run UUID for the caller
    to assemble or compare.
    """
    artifacts = Artifacts.at(options.out_dir)
    artifacts.out_dir.mkdir(parents=True, exist_ok=True)
    artifacts.lines_dir.mkdir(parents=True, exist_ok=True)
    artifacts.runs_dir.mkdir(parents=True, exist_ok=True)

    raw_text = script_path.read_text(encoding="utf-8")
    artifacts.raw_text.write_text(raw_text, encoding="utf-8")

    inputs_lock = _load_model(artifacts.inputs_lock, InputsLock) or InputsLock()

    # ---------- Stage 1: parse ----------
    beats_hash = sha256_hex(raw_text, "regex_v1")
    if (
        inputs_lock.parse == beats_hash
        and (existing := _load_model(artifacts.beats, Screenplay)) is not None
    ):
        screenplay: Screenplay = existing  # type: ignore[assignment]
        log.info("Skipped parse (cached).")
    else:
        screenplay = sp_module.parse_regex(raw_text, title=script_path.stem)
        _save_model(artifacts.beats, screenplay)
        inputs_lock.parse = beats_hash

    client = AsyncAnthropic()

    # ---------- Stage 2: repair beats ----------
    if not options.skip_repair:
        repair_hash = sha256_hex(beats_hash, "repair_v1")
        if (
            inputs_lock.repair == repair_hash
            and (existing := _load_model(artifacts.beats, Screenplay)) is not None
            and existing.beats != screenplay.beats  # repaired version differs from raw
        ):
            screenplay = existing  # already loaded above; keep it
            log.info("Skipped repair (cached).")
        else:
            log.info("Repairing beats with LLM...")
            screenplay = await analyzer.repair_beats(client, raw_text, screenplay)
            _save_model(artifacts.beats, screenplay)
            inputs_lock.repair = repair_hash

    # ---------- Stage 3: extract characters ----------
    extract_hash = sha256_hex(screenplay, "extract_v1")
    if (
        inputs_lock.extract_characters == extract_hash
        and (existing := _load_list(artifacts.characters, CharacterProfile)) is not None
    ):
        characters: list[CharacterProfile] = existing  # type: ignore[assignment]
        log.info("Skipped character extraction (cached).")
    else:
        log.info("Extracting characters...")
        characters = await analyzer.extract_characters(client, raw_text)
        _save_list(artifacts.characters, characters)
        inputs_lock.extract_characters = extract_hash

    # ---------- Stage 4: voice profiles (parallel) ----------
    profile_hash = sha256_hex(extract_hash, characters, "voice_profile_v1")
    if (
        inputs_lock.profile_voice == profile_hash
        and (existing := _load_list(artifacts.voice_profiles, VoiceProfile)) is not None
    ):
        voice_profiles: list[VoiceProfile] = existing  # type: ignore[assignment]
        log.info("Skipped voice profiling (cached).")
    else:
        log.info("Profiling voices in parallel...")
        voice_profiles = await analyzer.profile_voices(client, raw_text, characters)
        _save_list(artifacts.voice_profiles, voice_profiles)
        inputs_lock.profile_voice = profile_hash

    # ---------- Stage 5: lexicon ----------
    lex_hash = sha256_hex(screenplay, "lexicon_v1")
    if (
        inputs_lock.build_lexicon == lex_hash
        and (existing := _load_model(artifacts.lexicon, Lexicon)) is not None
    ):
        lexicon: Lexicon = existing  # type: ignore[assignment]
        log.info("Skipped lexicon (cached).")
    else:
        log.info("Building lexicon...")
        lexicon = await analyzer.build_lexicon(client, raw_text)
        _save_model(artifacts.lexicon, lexicon)
        inputs_lock.build_lexicon = lex_hash

    # ---------- Stage 6: casting ----------
    casting = await _resolve_casting(
        client=client,
        artifacts=artifacts,
        characters=characters,
        voice_profiles=voice_profiles,
        catalog_path=options.voice_catalog_path,
        force_recast=options.force_recast,
        inputs_lock=inputs_lock,
    )

    # ---------- Stage 7: per-scene direction (parallel) ----------
    direct_hash = sha256_hex(screenplay, characters, casting, "direct_v1")
    if (
        inputs_lock.direct_scenes == direct_hash
        and (existing := _load_list(artifacts.direction, DirectionTrack)) is not None
    ):
        direction_tracks: list[DirectionTrack] = existing  # type: ignore[assignment]
        log.info("Skipped scene direction (cached).")
    else:
        log.info("Directing scenes in parallel...")
        direction_tracks = await analyzer.direct_screenplay(
            client, raw_text, screenplay, characters, casting,
        )
        _save_list(artifacts.direction, direction_tracks)
        inputs_lock.direct_scenes = direct_hash

    _save_model(artifacts.inputs_lock, inputs_lock)

    # ---------- Stage 8: render ----------
    cached_manifest = _load_model(artifacts.manifest, Manifest)
    run_uuid = str(uuid.uuid4())
    log.info("Rendering lines via ElevenLabs (this is the slow part)...")
    async with ElevenLabsRenderer(tts_model_id=casting.tts_model_id) as renderer:
        manifest = await render_screenplay(
            renderer,
            screenplay=screenplay,
            casting=casting,
            direction_tracks=direction_tracks,
            lexicon=lexicon,
            out_dir=artifacts.lines_dir,
            cached_manifest=cached_manifest,
            run_uuid=run_uuid,
        )
    _save_model(artifacts.manifest, manifest)

    # Snapshot this run for A/B compare.
    run_snapshot = artifacts.runs_dir / run_uuid
    run_snapshot.mkdir(parents=True, exist_ok=True)
    _save_model(run_snapshot / "manifest.json", manifest)
    _save_model(run_snapshot / "inputs.lock.json", inputs_lock)
    _save_model(run_snapshot / "casting.json", casting)

    return {
        "run_uuid": run_uuid,
        "manifest_path": str(artifacts.manifest),
        "run_dir": str(run_snapshot),
        "n_lines": len(manifest.records),
    }


async def _resolve_casting(
    *,
    client: AsyncAnthropic,
    artifacts: Artifacts,
    characters: list[CharacterProfile],
    voice_profiles: list[VoiceProfile],
    catalog_path: Path,
    force_recast: bool,
    inputs_lock: InputsLock,
) -> Casting:
    # Locked casting beats everything (unless force_recast).
    if artifacts.casting_locked.exists() and not force_recast:
        log.info("Using casting.locked.json.")
        casting = Casting.model_validate_json(
            artifacts.casting_locked.read_text(encoding="utf-8")
        )
        return apply_override_json(casting, artifacts.casting_override)

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    cast_hash = sha256_hex(characters, voice_profiles, catalog, "cast_v1")
    if (
        inputs_lock.cast == cast_hash
        and (existing := _load_model(artifacts.casting, Casting)) is not None
    ):
        log.info("Skipped casting (cached).")
        return apply_override_json(existing, artifacts.casting_override)

    log.info("Casting characters to voices...")
    casting = await analyzer.cast_characters(
        client,
        characters=characters,
        voice_profiles=voice_profiles,
        voice_catalog=catalog,
        tts_model_id=catalog.get("tts_model_id", "eleven_multilingual_v2"),
    )
    _save_model(artifacts.casting, casting)
    inputs_lock.cast = cast_hash
    return apply_override_json(casting, artifacts.casting_override)


def lock_casting(out_dir: Path) -> Path:
    """Copy casting.json -> casting.locked.json so future runs can't change it."""
    artifacts = Artifacts.at(out_dir)
    if not artifacts.casting.exists():
        raise FileNotFoundError("No casting.json to lock; run the pipeline first.")
    artifacts.casting_locked.write_text(
        artifacts.casting.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return artifacts.casting_locked


def write_recast(out_dir: Path, *, character: str, voice_id: str) -> Path:
    """Append/update an entry in casting.override.json.  Triggers re-render
    of that character's lines on the next run (render hashes change)."""
    artifacts = Artifacts.at(out_dir)
    overrides: dict = {}
    if artifacts.casting_override.exists():
        overrides = json.loads(artifacts.casting_override.read_text(encoding="utf-8"))
    overrides[character] = {"voice_id": voice_id}
    artifacts.casting_override.parent.mkdir(parents=True, exist_ok=True)
    artifacts.casting_override.write_text(
        json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8"
    )
    return artifacts.casting_override
