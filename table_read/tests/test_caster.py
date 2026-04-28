from table_read.caster import dsl_to_voice_settings
from table_read.models import PerformanceDSL


def _dsl(**kw) -> PerformanceDSL:
    base = dict(arousal=0.5, valence=0.0, control=0.5, effort=0.5, pace=0.5, pause_before_ms=0)
    base.update(kw)
    return PerformanceDSL(**base)


def test_dsl_axes_are_clamped_into_voice_settings_range():
    extremes = [
        _dsl(arousal=0.0, control=0.0, pace=0.0, effort=0.0),
        _dsl(arousal=1.0, control=1.0, pace=1.0, effort=1.0),
        _dsl(arousal=0.5, control=0.5, pace=0.5, effort=0.5),
    ]
    for d in extremes:
        vs = dsl_to_voice_settings(d)
        assert 0.0 <= vs.stability <= 1.0
        assert 0.0 <= vs.similarity_boost <= 1.0
        assert 0.0 <= vs.style <= 1.0
        assert 0.7 <= vs.speed <= 1.2


def test_higher_control_increases_stability():
    low = dsl_to_voice_settings(_dsl(control=0.0))
    high = dsl_to_voice_settings(_dsl(control=1.0))
    assert high.stability > low.stability


def test_higher_arousal_increases_style():
    low = dsl_to_voice_settings(_dsl(arousal=0.0))
    high = dsl_to_voice_settings(_dsl(arousal=1.0))
    assert high.style > low.style


def test_higher_pace_increases_speed():
    slow = dsl_to_voice_settings(_dsl(pace=0.0, effort=0.0))
    fast = dsl_to_voice_settings(_dsl(pace=1.0, effort=1.0))
    assert fast.speed > slow.speed
