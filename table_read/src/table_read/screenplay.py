"""Screenplay parser.

Two-stage:
1. Regex pass: handles ~80% of standard format (INT./EXT., ALL-CAPS character
   cues, parentheticals, dialogue, transitions).  Cheap, deterministic.
2. LLM repair pass (in analyzer.repair_beats): fixes dual dialogue,
   (MORE)/(CONT'D) continuations, V.O./O.S. extensions, and similar edge
   cases the regex misses.

The two stages are decoupled so the regex can ship without the LLM repair
when you want to test parsing in isolation.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Beat, BeatKind, Scene, Screenplay

SCENE_HEADING_RE = re.compile(
    r"^\s*(INT\.?|EXT\.?|INT\.?/EXT\.?|EXT\.?/INT\.?|I\.?/E\.?)\s+.+",
    re.IGNORECASE,
)
TRANSITION_RE = re.compile(
    r"^\s*(FADE (IN|OUT|TO BLACK)|CUT TO|DISSOLVE TO|SMASH CUT TO|MATCH CUT TO|"
    r"FADE TO BLACK|THE END|END)\.?:?\s*$",
    re.IGNORECASE,
)
# Character cue: line is mostly uppercase, may end with " (V.O.)", " (O.S.)",
# " (CONT'D)", etc.  Allows letters, spaces, periods, hyphens, apostrophes,
# digits, parenthesized extension.
CHARACTER_CUE_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9 .'\-]*?)(?:\s*\(([A-Z0-9 .'\-/]+)\))?\s*$"
)
PARENTHETICAL_RE = re.compile(r"^\s*\((.+?)\)\s*$")


def _is_blank(line: str) -> bool:
    return not line.strip()


def _looks_like_character_cue(line: str) -> bool:
    """Heuristic: at least 2 letters, mostly uppercase, not too long."""
    s = line.strip()
    if not s or len(s) > 60:
        return False
    # Must contain at least one letter and start with a letter.
    if not s[0].isalpha():
        return False
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 2:
        return False
    upper = sum(1 for c in letters if c.isupper())
    # Allow dotted abbreviations & parenthesized extensions to drop the ratio
    # slightly.
    if upper / len(letters) < 0.85:
        return False
    return SCENE_HEADING_RE.match(s) is None and TRANSITION_RE.match(s) is None


def parse_regex(text: str, title: str | None = None) -> Screenplay:
    """Best-effort regex parse.  Output is fed into the LLM repair stage."""
    if text.startswith("%PDF"):
        raise ValueError(
            "Input looks like a PDF.  Convert to plain text first "
            "(e.g. `pdftotext -layout script.pdf script.txt`)."
        )

    lines = text.splitlines()
    beats: list[Beat] = []
    scenes: list[Scene] = []

    scene_idx = -1  # incremented to 0 on first scene heading
    beat_idx = 0
    current_scene_beats: list[str] = []
    pending_character: str | None = None
    pending_extension: str | None = None
    last_kind: BeatKind | None = None

    def flush_scene() -> None:
        nonlocal current_scene_beats
        if scene_idx >= 0 and current_scene_beats:
            heading_beat_id = current_scene_beats[0]
            heading = next(b.text for b in beats if b.beat_id == heading_beat_id)
            scenes.append(
                Scene(
                    scene_idx=scene_idx,
                    heading=heading,
                    beat_ids=list(current_scene_beats),
                )
            )
        current_scene_beats = []

    def add(kind: BeatKind, text: str, character: str | None = None,
            extension: str | None = None) -> None:
        nonlocal beat_idx
        beat_id = f"{max(scene_idx, 0)}-{beat_idx}"
        beats.append(
            Beat(
                beat_id=beat_id,
                scene_idx=max(scene_idx, 0),
                beat_idx=beat_idx,
                kind=kind,
                text=text.strip(),
                character=character,
                extension=extension,
            )
        )
        current_scene_beats.append(beat_id)
        beat_idx += 1

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if _is_blank(line):
            # Blank lines are only structural; reset pending character cue if
            # the previous non-blank wasn't a parenthetical or dialogue.
            if last_kind not in (BeatKind.CHARACTER_CUE, BeatKind.PARENTHETICAL):
                pending_character = None
                pending_extension = None
            i += 1
            continue

        # Scene heading.
        if SCENE_HEADING_RE.match(stripped):
            flush_scene()
            scene_idx += 1
            beat_idx = 0
            add(BeatKind.SCENE_HEADING, stripped)
            last_kind = BeatKind.SCENE_HEADING
            pending_character = None
            pending_extension = None
            i += 1
            continue

        # Transition.
        if TRANSITION_RE.match(stripped):
            add(BeatKind.TRANSITION, stripped)
            last_kind = BeatKind.TRANSITION
            pending_character = None
            pending_extension = None
            i += 1
            continue

        # Parenthetical (only meaningful right after a character cue).
        m = PARENTHETICAL_RE.match(stripped)
        if m and pending_character:
            add(
                BeatKind.PARENTHETICAL,
                stripped,
                character=pending_character,
                extension=pending_extension,
            )
            last_kind = BeatKind.PARENTHETICAL
            i += 1
            continue

        # Character cue: looks ALL CAPS and the next non-blank line is
        # dialogue or a parenthetical.
        if _looks_like_character_cue(line):
            cue_match = CHARACTER_CUE_RE.match(stripped)
            if cue_match:
                # Look ahead for dialogue.
                j = i + 1
                while j < len(lines) and _is_blank(lines[j]):
                    j += 1
                if j < len(lines):
                    nxt = lines[j].strip()
                    looks_dialogue = (
                        nxt
                        and not SCENE_HEADING_RE.match(nxt)
                        and not TRANSITION_RE.match(nxt)
                        and not _looks_like_character_cue(lines[j])
                    )
                    if looks_dialogue:
                        pending_character = cue_match.group(1).strip()
                        pending_extension = (
                            cue_match.group(2).strip() if cue_match.group(2) else None
                        )
                        add(
                            BeatKind.CHARACTER_CUE,
                            stripped,
                            character=pending_character,
                            extension=pending_extension,
                        )
                        last_kind = BeatKind.CHARACTER_CUE
                        i += 1
                        continue

        # Dialogue: comes after a character cue (possibly with parenthetical
        # in between).  Collect contiguous non-blank lines.
        if pending_character and last_kind in (
            BeatKind.CHARACTER_CUE,
            BeatKind.PARENTHETICAL,
            BeatKind.DIALOGUE,
        ):
            buf = [stripped]
            j = i + 1
            while j < len(lines) and not _is_blank(lines[j]):
                # Mid-dialogue parenthetical breaks the run.
                if PARENTHETICAL_RE.match(lines[j].strip()):
                    break
                buf.append(lines[j].strip())
                j += 1
            add(
                BeatKind.DIALOGUE,
                " ".join(buf),
                character=pending_character,
                extension=pending_extension,
            )
            last_kind = BeatKind.DIALOGUE
            i = j
            continue

        # Default: action.
        # Glob contiguous non-blank action lines.
        buf = [stripped]
        j = i + 1
        while j < len(lines) and not _is_blank(lines[j]):
            nxt = lines[j].strip()
            if (
                SCENE_HEADING_RE.match(nxt)
                or TRANSITION_RE.match(nxt)
                or _looks_like_character_cue(lines[j])
            ):
                break
            buf.append(nxt)
            j += 1
        add(BeatKind.ACTION, " ".join(buf))
        last_kind = BeatKind.ACTION
        pending_character = None
        pending_extension = None
        i = j

    flush_scene()

    # Ensure there's at least scene 0 even if the script never had a heading.
    if not scenes and beats:
        scenes.append(
            Scene(scene_idx=0, heading="(unspecified)", beat_ids=[b.beat_id for b in beats])
        )

    return Screenplay(title=title, scenes=scenes, beats=beats)


def load(path: str | Path) -> Screenplay:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return parse_regex(text, title=p.stem)
