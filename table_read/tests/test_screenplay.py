from pathlib import Path

import pytest

from table_read import screenplay as sp
from table_read.models import BeatKind


SAMPLE_PATH = Path(__file__).resolve().parent.parent / "samples" / "sample_scene.txt"


def test_parses_sample_scene():
    text = SAMPLE_PATH.read_text(encoding="utf-8")
    result = sp.parse_regex(text, title="sample_scene")

    assert result.title == "sample_scene"
    assert len(result.scenes) == 1
    scene = result.scenes[0]
    assert scene.heading.upper().startswith("INT.")

    kinds = [b.kind for b in result.beats]
    assert kinds[0] == BeatKind.SCENE_HEADING
    assert BeatKind.ACTION in kinds
    assert BeatKind.DIALOGUE in kinds
    assert BeatKind.CHARACTER_CUE in kinds
    assert BeatKind.PARENTHETICAL in kinds
    assert BeatKind.TRANSITION in kinds


def test_dialogue_beats_have_characters():
    text = SAMPLE_PATH.read_text(encoding="utf-8")
    result = sp.parse_regex(text)
    speakers = {b.character for b in result.beats if b.kind == BeatKind.DIALOGUE}
    assert "MAYA" in speakers
    assert "KOWALSKI" in speakers
    assert "ROSIE" in speakers


def test_pdf_input_is_rejected():
    with pytest.raises(ValueError, match="PDF"):
        sp.parse_regex("%PDF-1.4\nbinary content...")


def test_parenthetical_attached_to_speaker():
    text = SAMPLE_PATH.read_text(encoding="utf-8")
    result = sp.parse_regex(text)
    parens = [b for b in result.beats if b.kind == BeatKind.PARENTHETICAL]
    assert parens, "expected at least one parenthetical"
    for p in parens:
        assert p.character is not None
