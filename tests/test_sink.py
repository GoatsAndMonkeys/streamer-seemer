import asyncio
import time

import pytest

from seamer.simkit.clock import RealClock, VirtualClock
from seamer.simkit.expect import Expect
from seamer.simkit.knobs import KnobSet
from seamer.simkit.media import CHANNELS, RATE
from seamer.simkit.recorder import Recorder
from seamer.simkit.sink import HttpSink, SinkState

BPS = RATE * CHANNELS * 2


def make_sink(clock=None, **knobs):
    clock = clock or RealClock()
    rec = Recorder(clock=clock)
    ks = KnobSet("example", knobs, rec=rec.bind("dev:t"))
    return HttpSink(clock, rec.bind("dev:t/sink"), ks), rec


async def wait_real(pred, timeout=10.0):
    t0 = time.monotonic()
    while not pred():
        assert time.monotonic() - t0 < timeout, "timed out"
        await asyncio.sleep(0.01)


async def test_realtime_pull_plays_at_wall_rate(media, media_srv):
    _, base = media_srv
    t = media.track(1, 2.0)
    sink, rec = make_sink(prebuffer_s=0.2)
    t0 = time.monotonic()
    await sink.open(f"{base}/media/{t.name}")
    assert await sink.wait_ended(10)
    wall = time.monotonic() - t0
    await sink.aclose()
    heard = rec.timeline().last(name="heard")
    assert heard.data["played_s"] == pytest.approx(2.0, abs=0.01)
    assert heard.data["tracks"]["1"] == pytest.approx(2.0, abs=0.15)
    assert 1.9 < wall < 2.8
    Expect(rec).order(
        {"name": "opened"},
        {"name": "eof"},
        {"name": "first_audio"},
        {"name": "drained"},
        {"name": "ended"},
    )


@pytest.mark.parametrize(("mode", "played"), [("drain", 3.0), ("drop", 0.0)])
async def test_drain_vs_drop_on_server_close(media, media_srv, mode, played):
    _, base = media_srv
    t = media.track(2, 3.0)
    clock = VirtualClock()
    sink, rec = make_sink(clock, on_server_close=mode, read_mode="burst", prebuffer_s=0.5)
    await sink.open(f"{base}/stream/{t.name}?rate=0")
    # the body arrives in real time; virtual time stands still meanwhile
    await wait_real(lambda: rec.timeline().first(name="eof") is not None)
    await clock.run_until(lambda: not sink.active, max_s=10, step=0.05)
    heard = rec.timeline().last(name="heard")
    assert heard.data["played_s"] == pytest.approx(played, abs=0.05)
    if mode == "drop":
        assert rec.timeline().first(name="dropped").data["seconds"] == pytest.approx(3.0, abs=0.05)
        assert heard.data["reason"] == "dropped"
    await sink.aclose()


async def test_backpressure_limits_what_the_server_sends(media, media_srv):
    ms, base = media_srv
    t = media.track(3, 30.0)
    sink, _rec = make_sink(buffer_capacity_s=1.0)
    await sink.open(f"{base}/stream/{t.name}?rate=0")
    await asyncio.sleep(1.0)
    sent_s = ms.sent[-1]["bytes"] / BPS
    assert sink.buffer.fill_s <= 1.0 + 0.2
    assert sent_s < 15, f"server pushed {sent_s:.1f}s of audio despite a 1s buffer"
    await sink.close("test")
    await sink.aclose()


async def test_audible_truth_follows_tones(media, media_srv):
    _, base = media_srv
    flow = media.concat_wav(media.album(3, 1.0), "flow3.wav")
    clock = VirtualClock()
    sink, rec = make_sink(clock, read_mode="burst")
    await sink.open(f"{base}/stream/{flow.name}?rate=0")
    await wait_real(lambda: rec.timeline().first(name="eof") is not None)
    await clock.run_until(lambda: not sink.active, max_s=10, step=0.05)
    changes = rec.timeline().where(name="track_change")
    assert [e.data["track"] for e in changes] == [1, 2, 3]
    assert changes[1].data["position_s"] == pytest.approx(1.0, abs=0.12)
    for n in (1, 2, 3):
        Expect(rec).audible_coverage(track=n, at_least_s=0.85)
    await sink.aclose()


async def test_byte_cap(media, media_srv):
    _, base = media_srv
    t = media.track(4, 5.0)
    clock = VirtualClock()
    cap = 44 + BPS  # one second plus header
    sink, rec = make_sink(clock, byte_cap=cap, read_mode="burst")
    await sink.open(f"{base}/media/{t.name}")
    await wait_real(lambda: rec.timeline().first(name="cap_hit") is not None)
    await clock.run_until(lambda: not sink.active, max_s=10, step=0.05)
    heard = rec.timeline().last(name="heard")
    assert heard.data["played_s"] == pytest.approx(1.0, abs=0.01)
    assert heard.data["reason"] == "cap_hit"
    await sink.aclose()


async def test_underrun_when_server_is_slow(media, media_srv):
    _, base = media_srv
    t = media.track(5, 1.0)
    sink, rec = make_sink(prebuffer_s=0.2)
    await sink.open(f"{base}/stream/{t.name}?rate=0.5")
    assert await sink.wait_ended(10)
    tl = rec.timeline()
    assert len(tl.where(name="underrun")) >= 1
    assert len(tl.where(name="rebuffered")) >= 1
    assert tl.last(name="heard").data["played_s"] == pytest.approx(1.0, abs=0.01)
    await sink.aclose()


async def test_pause_stops_playout(media, media_srv):
    _, base = media_srv
    t = media.track(6, 4.0)
    clock = VirtualClock()
    sink, rec = make_sink(clock, read_mode="burst")
    await sink.open(f"{base}/media/{t.name}")
    await wait_real(lambda: rec.timeline().first(name="eof") is not None)
    await clock.advance(1.0)
    sink.pause()
    before = sink.buffer.played_s
    await clock.advance(2.0)
    assert sink.state == SinkState.PAUSED
    assert sink.buffer.played_s == pytest.approx(before)
    sink.resume()
    await clock.run_until(lambda: not sink.active, max_s=10, step=0.05)
    assert rec.timeline().last(name="heard").data["played_s"] == pytest.approx(4.0, abs=0.01)
    await sink.aclose()


async def test_reject_chunked_and_range_rules(media, media_srv):
    ms, base = media_srv
    t = media.track(7, 0.5)
    sink, rec = make_sink(reject_chunked=True)
    await sink.open(f"{base}/stream/{t.name}?rate=0")
    assert await sink.wait_ended(5)
    assert "chunked" in rec.timeline().first(name="error").data["error"]
    assert sink.state == SinkState.ERROR
    await sink.aclose()

    sink, rec = make_sink(require_range=True, probe="head")
    await sink.open(f"{base}/media/{t.name}")
    assert await sink.wait_ended(5)
    assert rec.timeline().first(name="error") is None
    assert rec.timeline().first(name="probe").data["method"] == "HEAD"
    assert any(r["method"] == "HEAD" for r in ms.requests)
    assert any(r["range"] == "bytes=0-" for r in ms.requests)
    await sink.aclose()


async def test_client_close_records_summary(media, media_srv):
    _, base = media_srv
    t = media.track(8, 10.0)
    sink, rec = make_sink()
    await sink.open(f"{base}/media/{t.name}")
    await wait_real(lambda: sink.state == SinkState.PLAYING)
    await sink.close("stop")
    assert sink.state == SinkState.IDLE
    heard = rec.timeline().last(name="heard")
    assert heard.data["reason"] == "stop"
    assert heard.data["unplayed_s"] > 0
    await sink.aclose()
