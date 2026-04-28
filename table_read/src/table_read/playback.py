"""Stitch per-line WAVs into one table-read MP3.

Concatenation is lossless (PCM through), encoded to MP3 only at the very
end via ffmpeg.  Pauses come from two sources:

1. Per-line `pause_before_ms` from the Direction (if available).
2. Per-action-beat scaled silence: action lines map to a duration
   proportional to word count, capped to a configurable max.

Requires `ffmpeg` on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

from .models import (
    Beat,
    BeatKind,
    Direction,
    DirectionTrack,
    Manifest,
    Screenplay,
)


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH.  Install ffmpeg and retry, or skip "
            "playback assembly with `tableread render` (which still produces "
            "per-line WAVs)."
        )


def _silence_wav(duration_ms: int, sample_rate: int, path: Path) -> None:
    n_samples = int(round(sample_rate * duration_ms / 1000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_samples)


def _action_silence_ms(beat: Beat, *, ms_per_word: int = 220, cap_ms: int = 4000) -> int:
    n_words = max(1, len(beat.text.split()))
    return min(cap_ms, n_words * ms_per_word)


def _scene_break_ms() -> int:
    return 1500


def assemble(
    *,
    screenplay: Screenplay,
    direction_tracks: list[DirectionTrack],
    manifest: Manifest,
    out_dir: Path,
    output_path: Path,
    sample_rate: int = 24_000,
    bitrate: str = "192k",
    narrator_voice_id: str | None = None,
) -> Path:
    """Concatenate the rendered lines + silence + (optional narration) into MP3.

    `narrator_voice_id` is reserved for future use; this function currently
    inserts silence for action lines.  TTS narration would be a follow-up.
    """
    ensure_ffmpeg()

    direction_by_beat: dict[str, Direction] = {
        d.beat_id: d for t in direction_tracks for d in t.directions
    }
    record_by_beat: dict[str, list] = {}
    for r in manifest.records:
        record_by_beat.setdefault(r.beat_id, []).append(r)

    # Build the ordered wave-file segment list, inserting silence where
    # appropriate.
    silence_dir = out_dir / "_silences"
    silence_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []

    last_scene = -1
    for beat in screenplay.beats:
        if beat.scene_idx != last_scene:
            if last_scene != -1:
                p = silence_dir / f"scene_break_{last_scene}_to_{beat.scene_idx}.wav"
                _silence_wav(_scene_break_ms(), sample_rate, p)
                segments.append(p)
            last_scene = beat.scene_idx

        if beat.kind == BeatKind.DIALOGUE:
            d = direction_by_beat.get(beat.beat_id)
            if d and d.dsl.pause_before_ms > 0:
                p = silence_dir / f"pause_{beat.beat_id}.wav"
                _silence_wav(d.dsl.pause_before_ms, sample_rate, p)
                segments.append(p)
            for r in record_by_beat.get(beat.beat_id, []):
                segments.append(Path(r.wav_path))
        elif beat.kind == BeatKind.ACTION:
            ms = _action_silence_ms(beat)
            p = silence_dir / f"action_{beat.beat_id}.wav"
            _silence_wav(ms, sample_rate, p)
            segments.append(p)
        # SCENE_HEADING / TRANSITION / CHARACTER_CUE / PARENTHETICAL emit
        # nothing audible (the dialogue beat that follows carries them).

    if not segments:
        raise RuntimeError("Nothing to assemble: no rendered lines or silence.")

    # ffmpeg concat-demuxer file list.
    list_file = out_dir / "_concat.txt"
    list_file.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in segments),
        encoding="utf-8",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-codec:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", str(sample_rate),
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")

    return output_path
