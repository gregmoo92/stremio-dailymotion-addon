from table_read.models import VoiceSettings
from table_read.renderer import render_hash


def _vs(**kw):
    base = dict(stability=0.5, similarity_boost=0.75, style=0.3, use_speaker_boost=True, speed=1.0)
    base.update(kw)
    return VoiceSettings(**base)


def test_identical_inputs_same_hash():
    h1 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(), seed=1,
        previous_text=None, next_text=None, tts_model_id="m1",
    )
    h2 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(), seed=1,
        previous_text=None, next_text=None, tts_model_id="m1",
    )
    assert h1 == h2


def test_text_change_invalidates_hash():
    h1 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(), seed=1,
        previous_text=None, next_text=None, tts_model_id="m1",
    )
    h2 = render_hash(
        text="hello!", voice_id="vA", voice_settings=_vs(), seed=1,
        previous_text=None, next_text=None, tts_model_id="m1",
    )
    assert h1 != h2


def test_voice_change_invalidates_hash():
    h1 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(), seed=1,
        previous_text=None, next_text=None, tts_model_id="m1",
    )
    h2 = render_hash(
        text="hello", voice_id="vB", voice_settings=_vs(), seed=1,
        previous_text=None, next_text=None, tts_model_id="m1",
    )
    assert h1 != h2


def test_neighbor_change_invalidates_hash():
    h1 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(), seed=1,
        previous_text=None, next_text=None, tts_model_id="m1",
    )
    h2 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(), seed=1,
        previous_text="...", next_text=None, tts_model_id="m1",
    )
    assert h1 != h2


def test_settings_change_invalidates_hash():
    h1 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(stability=0.4),
        seed=1, previous_text=None, next_text=None, tts_model_id="m1",
    )
    h2 = render_hash(
        text="hello", voice_id="vA", voice_settings=_vs(stability=0.5),
        seed=1, previous_text=None, next_text=None, tts_model_id="m1",
    )
    assert h1 != h2
