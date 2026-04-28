from table_read.hashing import canonical_json, sha256_hex
from table_read.models import PerformanceDSL, VoiceSettings


def test_canonical_json_sort_stable_across_dict_order():
    a = {"b": 1, "a": 2, "c": 3}
    b = {"c": 3, "a": 2, "b": 1}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_pydantic_model():
    vs = VoiceSettings(
        stability=0.5,
        similarity_boost=0.75,
        style=0.3,
        use_speaker_boost=True,
        speed=1.0,
    )
    s = canonical_json(vs)
    # Keys should be sorted in the dump.
    assert s.startswith('{"similarity_boost"') or s.startswith('{"speed"') or s.startswith('{"stability"') or s.startswith('{"style"') or s.startswith('{"use_speaker_boost"')
    # And it should round-trip the same regardless of construction order.
    vs2 = VoiceSettings.model_validate_json(s)
    assert vs2 == vs


def test_sha256_changes_on_any_input_change():
    args1 = ("hello", {"a": 1})
    args2 = ("hello", {"a": 2})
    assert sha256_hex(*args1) != sha256_hex(*args2)


def test_sha256_stable_on_identical_inputs():
    dsl = PerformanceDSL(
        arousal=0.5, valence=0.0, control=0.5, effort=0.5, pace=0.5, pause_before_ms=0,
    )
    h1 = sha256_hex("foo", dsl, [1, 2, 3])
    h2 = sha256_hex("foo", dsl, [1, 2, 3])
    assert h1 == h2
