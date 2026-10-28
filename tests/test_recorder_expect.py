import pytest

from seamer.simkit.clock import VirtualClock
from seamer.simkit.expect import Expect, ExpectationError
from seamer.simkit.recorder import Recorder, Timeline


async def _filled(tmp_path):
    clock = VirtualClock()
    rec = Recorder(tmp_path / "t.jsonl", clock)
    dev = rec.bind("dev:a")
    dev.cmd("Play", uri="u1")
    await clock.advance(1)
    dev.state("transport", old="stopped", new="playing")
    for i in range(5):
        await clock.advance(1)
        rec.emit("ma", "sample", "elapsed", elapsed=float(i + 1))
    dev.sink("heard", played_s=5.0, tracks={"1": 3.0, "2": 2.0})
    rec.close()
    return rec


async def test_jsonl_roundtrip_and_queries(tmp_path):
    rec = await _filled(tmp_path)
    tl = Timeline.load(tmp_path / "t.jsonl")
    assert len(tl) == len(rec.events) == 8
    assert tl.first(kind="state").data["new"] == "playing"
    assert tl.first(name="transport", new="playing") is not None
    assert len(tl.where(src="dev:*")) == 3
    assert tl.series("ma", "elapsed")[0] == (2.0, 1.0)
    assert len(tl.between(3, 4)) == 2
    assert tl.where(lambda e: e.t > 5).names() == ["elapsed", "heard"]


async def test_expect_helpers(tmp_path):
    rec = await _filled(tmp_path)
    ex = Expect(rec)
    ex.eventually(kind="state", new="playing", within=2)
    ex.never(kind="sink", name="underrun")
    ex.count(kind="sample", exactly=5)
    ex.order({"name": "Play"}, {"name": "transport"}, {"name": "heard"})
    ex.max_jump(rec.timeline().series("ma", "elapsed"), limit=0.1)
    ex.audible_coverage(track=1, at_least_s=3.0)
    ex.played_close_to(5.0, 0.01)
    with pytest.raises(ExpectationError, match="timeline"):
        ex.never(kind="cmd")
    with pytest.raises(ExpectationError):
        ex.count(kind="sample", at_most=2)
    with pytest.raises(ExpectationError):
        ex.audible_coverage(track=2, at_least_s=2.5)
    with pytest.raises(ExpectationError):
        ex.eventually(kind="state", within=0.5)


async def test_wait_event_live():
    clock = VirtualClock()
    rec = Recorder(clock=clock)
    ex = Expect(rec)
    clock.call_later(3, lambda: rec.emit("x", "event", "boom"))
    import asyncio

    fut = asyncio.ensure_future(ex.wait_event(timeout=10, name="boom"))
    await clock.advance(3.1)
    assert (await fut).name == "boom"
