import asyncio
import time
import types

import pytest

from seamer.simkit.clock import RealClock, VirtualClock, patch_time, wait_with_timeout
from seamer.simkit.rng import SeededRng


async def test_virtual_sleepers_wake_in_order():
    clock = VirtualClock()
    order = []

    async def sleeper(name, s):
        await clock.sleep(s)
        order.append((name, clock.monotonic()))

    tasks = [asyncio.create_task(sleeper(n, s)) for n, s in (("c", 3), ("a", 1), ("b", 2))]
    await clock.advance(2.5)
    assert order == [("a", 1), ("b", 2)]
    await clock.advance(1)
    assert [n for n, _ in order] == ["a", "b", "c"]
    await asyncio.gather(*tasks)


async def test_virtual_call_later_and_cancel():
    clock = VirtualClock()
    hits = []
    clock.call_later(5, lambda: hits.append("five"))
    h = clock.call_later(1, lambda: hits.append("one"))
    h.cancel()
    await clock.advance(4.9)
    assert hits == []
    await clock.advance(0.2)
    assert hits == ["five"]


async def test_virtual_minute_in_no_time():
    clock = VirtualClock()
    done = asyncio.Event()

    async def idle_timeout():
        await clock.sleep(60)  # e.g. EXTERNAL_PAUSE_IDLE_TIMEOUT
        done.set()

    task = asyncio.create_task(idle_timeout())
    t0 = time.monotonic()
    assert await clock.run_until(done.is_set, max_s=120)
    assert clock.monotonic() == pytest.approx(60)
    assert time.monotonic() - t0 < 1.0
    await task


async def test_periodic_loop_on_virtual_clock():
    clock = VirtualClock()
    ticks = []

    async def poller():
        while True:
            ticks.append(clock.monotonic())
            await clock.sleep(5)

    task = asyncio.create_task(poller())
    await clock.advance(30)
    task.cancel()
    assert ticks == [0, 5, 10, 15, 20, 25, 30]


async def test_wait_with_timeout_virtual():
    clock = VirtualClock()
    ev = asyncio.Event()
    fut = asyncio.ensure_future(wait_with_timeout(clock, ev.wait(), 10))
    await clock.advance(10.1)
    ok, _ = await fut
    assert ok is False


async def test_real_clock_sleep():
    c = RealClock()
    t0 = c.monotonic()
    await c.sleep(0.05)
    assert c.monotonic() - t0 >= 0.04


def test_patch_time_replaces_and_restores():
    mod = types.ModuleType("fake_ma_mod")
    mod.time = time
    clock = VirtualClock(start=42.0)
    with patch_time(clock, [mod]):
        assert mod.time.monotonic() == 42.0
        assert mod.time.sleep is time.sleep
    assert mod.time is time


def test_patch_time_missing_attr_is_loud():
    mod = types.ModuleType("no_time")
    with pytest.raises(AttributeError), patch_time(VirtualClock(), [mod]):
        pass


def test_rng_streams_deterministic_and_independent():
    a = SeededRng(7, "dev1")
    b = SeededRng(7, "dev1")
    assert [a.stream("x").random() for _ in range(5)] == [b.stream("x").random() for _ in range(5)]
    # drawing from another stream does not disturb "y"
    c = SeededRng(7, "dev1")
    c.stream("x").random()
    d = SeededRng(7, "dev1")
    assert c.stream("y").random() == d.stream("y").random()
    assert SeededRng(8, "dev1").stream("x").random() != SeededRng(7, "dev1").stream("x").random()
    assert SeededRng(7, "dev2").stream("x").random() != SeededRng(7, "dev1").stream("x").random()
