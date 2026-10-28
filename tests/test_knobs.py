import pytest

from seamer.families import get_family
from seamer.simkit.knobs import KnobError, KnobSet, ProfileError, knob, knobs_for, load_profile
from seamer.simkit.recorder import Recorder
from seamer.simkit.rng import SeededRng


def test_core_and_family_knobs_present():
    ks = knobs_for("example")
    assert ks["load_delay_s"].family == "core"
    assert ks["ignore_pause"].quirks == ("EX-01",)


def test_type_validation():
    ks = KnobSet("example")
    assert ks.set_live("latency_s", 1) == 1.0
    assert ks.set_live("read_mode", "burst") == "burst"
    with pytest.raises(KnobError):
        ks.set_live("read_mode", "warp")
    with pytest.raises(KnobError):
        ks.set_live("latency_s", "fast")
    with pytest.raises(KnobError):
        ks.set_live("http_error", 1.5)
    with pytest.raises(KnobError):
        ks.get("nope")
    assert ks.set_live("http_error", {"p": 0.3, "status": 502}) == {"p": 0.3, "status": 502}
    assert ks.param("http_error", "status") == 502


def test_family_knob_may_not_shadow_core():
    with pytest.raises(KnobError):
        knob("latency_s", family="zzz", type="float", default=0.0)


def test_profile_extends_faults_and_overrides():
    fam = get_family("example")
    p = fam.load_profile(
        "quirky+faults/slow_commands+faults/events_dead", overrides={"prebuffer_s": 1.0}
    )
    assert p.identity["model"] == "Example Player"  # from _base
    assert p.identity["firmware"] == "0.9.1-beta"  # overridden
    assert p.knobs["latency_s"] == 1.5
    assert p.knobs["no_state_events"] is True
    assert p.knobs["prebuffer_s"] == 1.0
    assert p.surfaces == ["example_http"]


def test_fault_bundle_drops_unknown_knobs(tmp_path, monkeypatch):
    # events_dead sets no_state_events; a family without it must still load
    p = load_profile("faults/events_dead", "nofamily")
    assert p.knobs == {}


def test_bad_profile_errors(tmp_path, monkeypatch):
    (tmp_path / "bad.toml").write_text("[knobs]\nnot_a_knob = 1\n")
    (tmp_path / "badval.toml").write_text('[knobs]\nread_mode = "warp"\n')
    with pytest.raises(ProfileError):
        load_profile("bad", "example", tmp_path)
    with pytest.raises(KnobError):
        load_profile("badval", "example", tmp_path)
    with pytest.raises(ProfileError):
        load_profile("missing", "example", tmp_path)
    with pytest.raises(ProfileError):
        load_profile("basic", "example", get_family("example").profiles_dir, {"zz": 1})


def test_extra_profile_path_env(tmp_path, monkeypatch):
    (tmp_path / "mine.toml").write_text('extends = ["_base"]\n[knobs]\nlatency_s = 0.2\n')
    monkeypatch.setenv("SEAMER_PROFILE_PATH", str(tmp_path))
    p = get_family("example").load_profile("mine")
    assert p.knobs["latency_s"] == 0.2


def _fire_seq(seed):
    rec = Recorder()
    ks = KnobSet("example", {"ignore_pause": {"p": 0.3}}, SeededRng(seed, "d1"), rec.bind("d"))
    return [ks.fires("ignore_pause") for _ in range(50)], rec


def test_prob_knob_seeded_and_recorded():
    a, rec = _fire_seq(1)
    b, _ = _fire_seq(1)
    c, _ = _fire_seq(2)
    assert a == b
    assert a != c
    fired = rec.timeline().where(kind="knob", name="fired")
    assert len(fired) == sum(a) > 0
    assert fired[0].data["quirks"] == ["EX-01"]
    assert fired[0].data["confidence"] == "low"


def test_set_live_recorded():
    rec = Recorder()
    ks = KnobSet("example", rec=rec.bind("d"))
    ks.set_live("offline", True)
    assert rec.timeline().first(kind="knob", name="set").data == {
        "knob": "offline",
        "old": False,
        "new": True,
    }
    assert ks.non_default() == {"offline": True}
