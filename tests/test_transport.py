import pytest

from seamer.simkit.clock import VirtualClock
from seamer.simkit.knobs import KnobSet
from seamer.simkit.recorder import Recorder
from seamer.simkit.transport import ReportLens, TransportMachine, TState


def make_tm(**knobs):
    clock = VirtualClock()
    rec = Recorder(clock=clock)
    em = rec.bind("dev:t")
    ks = KnobSet("example", knobs, rec=em)
    return TransportMachine(clock, em, ks, sink=None), clock, rec


async def test_play_pause_seek_position_clock_mode():
    tm, clock, _ = make_tm(load_delay_s=1.0)
    changes = []
    tm.on_change(lambda old, new: changes.append(new))
    await tm.set_uri("http://x/1", duration_s=100)
    assert tm.state == TState.STOPPED
    await tm.play()
    await clock.advance(0.5)
    assert tm.state == TState.LOADING
    await clock.advance(0.5)
    assert tm.state == TState.PLAYING
    await clock.advance(10)
    assert tm.truth_position() == pytest.approx(10)
    await tm.pause()
    await clock.advance(5)
    assert tm.truth_position() == pytest.approx(10)
    await tm.play()
    await clock.advance(2)
    assert tm.truth_position() == pytest.approx(12)
    await tm.seek(50)
    await clock.advance(1)
    assert tm.truth_position() == pytest.approx(51)
    await tm.stop()
    assert tm.state == TState.STOPPED
    assert changes == [
        TState.STOPPED,
        TState.LOADING,
        TState.PLAYING,
        TState.PAUSED,
        TState.PLAYING,
        TState.STOPPED,
    ]


async def test_auto_advance_with_gap_and_end():
    tm, clock, rec = make_tm(transition_gap_s=2.0)
    await tm.set_uri("u1", duration_s=10)
    await tm.set_next("u2", duration_s=5)
    await tm.play()
    await clock.advance(10.01)
    assert tm.state == TState.LOADING
    assert tm.current.uri == "u2"
    await clock.advance(2.0)
    assert tm.state == TState.PLAYING
    await clock.advance(5.1)
    assert tm.state == TState.STOPPED
    ended = rec.timeline().where(kind="state", name="track_ended")
    assert [e.data["uri"] for e in ended] == ["u1", "u2"]
    assert tm.tracks_started == 2


async def test_next_and_take_over():
    tm, clock, rec = make_tm()
    await tm.set_uri("u1", duration_s=100)
    assert await tm.next() is False
    await tm.set_next("u2", duration_s=100)
    await tm.play()
    await clock.advance(1)
    assert await tm.next() is True
    await clock.advance(0.1)
    assert tm.current.uri == "u2"
    tm.take_over("hdmi", emit_event=False)
    await clock.advance(3)
    assert tm.owner == "hdmi"
    assert tm.state == TState.PLAYING
    assert tm.truth_position() == pytest.approx(3)
    ev = rec.timeline().last(kind="cmd", name="take_over")
    assert ev.data == {"owner": "hdmi", "emit_event": False}


async def test_lens_generic_quirks():
    tm, clock, rec = make_tm(report_position_zero=True, report_duration_zero=True)
    lens = ReportLens(tm, tm.knobs)
    await tm.set_uri("u1", duration_s=60)
    await tm.play()
    await clock.advance(5)
    assert lens.position() == 0.0
    assert lens.duration() == 0.0
    assert tm.truth_position() == pytest.approx(5)
    reports = rec.timeline().where(kind="report")
    assert {e.name for e in reports} == {"position", "duration"}


async def test_lens_position_refresh():
    tm, clock, _ = make_tm(report_position_refresh_s=5.0)
    lens = ReportLens(tm, tm.knobs)
    await tm.set_uri("u1", duration_s=100)
    await tm.play()
    await clock.advance(0.01)
    assert lens.position() == pytest.approx(0, abs=0.02)
    await clock.advance(3)
    assert lens.position() == pytest.approx(0, abs=0.02)  # not refreshed yet
    await clock.advance(3)
    assert lens.position() == pytest.approx(6, abs=0.02)


async def test_lens_stale_after_load():
    tm, clock, _ = make_tm(report_stale_after_load={"p": 1.0, "window_s": 3.0})
    lens = ReportLens(tm, tm.knobs)
    await tm.set_uri("u1", duration_s=100)
    await tm.set_next("u2", duration_s=100)
    await tm.play()
    await clock.advance(2)
    assert lens.position() == pytest.approx(0, abs=0.02)  # stale window after the first load
    await clock.advance(18)
    assert lens.position() == pytest.approx(20, abs=0.02)
    await tm.next()
    await clock.advance(1)
    assert lens.position() == pytest.approx(20, abs=0.02)  # previous item's position
    await clock.advance(2.5)
    assert lens.position() == pytest.approx(3.5, abs=0.05)
