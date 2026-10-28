import asyncio

import aiohttp
import pytest

from seamer.cli import main, parse_knob
from seamer.families import discover, get_family
from seamer.simkit import TState, run_scenario, scenario
from seamer.simkit.scenario import Scene


def test_registry_discovers_example():
    fams = discover()
    assert "example" in fams
    assert set(get_family("example").profiles()) >= {"basic", "quirky"}


async def _get(session, url):
    async with session.get(url) as r:
        return r.status, await r.json()


async def test_example_device_plays_a_stream(scene: Scene):
    await scene.media_server()
    t = scene.media.track(1, 1.5)
    dev = await scene.device("example/basic")
    url = scene.media_url(t.path)
    async with aiohttp.ClientSession() as s:
        st, body = await _get(s, f"{dev.base_url}/api/play?url={url}&duration=1.5&title=One")
        assert st == 200
        assert body == {"ok": True}
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
        _, status = await _get(s, f"{dev.base_url}/api/status")
        assert status["state"] == "play"
        assert status["title"] == "One"
        # long-poll wakes when playback ends
        _, status = await _get(s, f"{dev.base_url}/api/status?etag={status['etag']}&timeout=5")
        await scene.wait_for(lambda: dev.transport.state == TState.STOPPED, 5, "stopped")
        _, sim = await _get(s, f"{dev.base_url}/_sim/state")
        assert sim["transport"]["sink"]["played_s"] == pytest.approx(1.5, abs=0.01)
    scene.expect.audible_coverage(track=1, at_least_s=1.4)
    scene.expect.eventually(kind="cmd", name="GET /api/play")


async def test_example_sim_api_knobs_and_actions(scene: Scene):
    dev = await scene.device("example/quirky", latency_s=0.0)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{dev.base_url}/_sim/knobs", json={"offline": True}) as r:
            assert r.status == 200
        async with s.get(f"{dev.base_url}/api/status") as r:
            assert r.status == 503
        async with s.post(f"{dev.base_url}/_sim/knobs", json={"read_mode": "warp"}) as r:
            assert r.status == 400
        async with s.post(
            f"{dev.base_url}/_sim/action/take_over", json={"owner": "hdmi", "emit_event": False}
        ) as r:
            assert r.status == 200
        async with s.post(f"{dev.base_url}/_sim/action/nope", json={}) as r:
            assert r.status == 404
        _, state = await _get(s, f"{dev.base_url}/_sim/state")
        assert state["transport"]["owner"] == "hdmi"
        _, events = await _get(s, f"{dev.base_url}/_sim/events")
        assert any(e["kind"] == "knob" for e in events)


@scenario("example_pause_quirk", families=["example"], quirks=["EX-01"], seed=3)
async def _pause_quirk(s: Scene, profile: str = "example/quirky") -> None:
    dev = await s.device(profile, load_delay_s=0.0, no_state_events=False)
    await dev.transport.set_uri("tone:", duration_s=None)
    ignored = 0
    for _ in range(20):
        if dev.knobs.fires("ignore_pause"):
            ignored += 1
    s.log.log("ignored", n=ignored)


async def test_scenario_runner_is_seed_deterministic(tmp_path):
    a = await run_scenario("example_pause_quirk", tmp_path / "a")
    b = await run_scenario("example_pause_quirk", tmp_path / "b")
    c = await run_scenario("example_pause_quirk", tmp_path / "c", seed=99)
    for r in (a, b, c):
        assert r.passed, r.error
    assert len(a.knobs_fired) == len(b.knobs_fired)
    assert a.timeline.exists()
    assert "low-confidence" in a.summary()


async def test_scenario_failure_is_reported(tmp_path):
    async def boom(s: Scene) -> None:
        s.expect.eventually(name="never-happens")

    r = await run_scenario(boom, tmp_path)
    assert not r.passed
    assert "never-happens" in r.error


def test_cli_lists(capsys):
    assert main(["families"]) == 0
    assert "example" in capsys.readouterr().out
    assert main(["profiles", "example"]) == 0
    out = capsys.readouterr().out
    assert "quirky" in out
    assert "faults/slow_commands" in out
    assert main(["knobs", "example", "--profile", "quirky"]) == 0
    assert "ignore_pause" in capsys.readouterr().out


def test_parse_knob():
    assert parse_knob("latency_s=1.5") == ("latency_s", 1.5)
    assert parse_knob("ignore_pause={p=0.2}") == ("ignore_pause", {"p": 0.2})
    assert parse_knob("read_mode=burst") == ("read_mode", "burst")
    assert parse_knob("offline=true") == ("offline", True)


async def test_cli_run_subprocess(tmp_path):
    import sys

    tl = tmp_path / "t.jsonl"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "seamer",
        "run",
        "example",
        "--profile",
        "basic",
        "--port",
        "0",
        "--knob",
        "latency_s=0.01",
        "--timeline",
        str(tl),
        "--duration",
        "5",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    line = (await asyncio.wait_for(proc.stdout.readline(), 10)).decode()
    assert "listening on http://127.0.0.1:" in line, line
    base = line.split("listening on ")[1].split()[0]
    async with aiohttp.ClientSession() as s, s.get(f"{base}/api/status") as r:
        assert (await r.json())["state"] == "none"
    proc.terminate()
    assert await asyncio.wait_for(proc.wait(), 10) == 0
    assert "GET /api/status" in tl.read_text()
