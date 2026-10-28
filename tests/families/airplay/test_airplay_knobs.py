"""One test (or more) per AirPlay knob. Wire-level where possible, the fake helper where the
knob is about the helper's own output or timing."""

import asyncio
import signal
import time
from typing import Any

import pytest

from seamer.families.airplay.device import db_to_volume
from seamer.families.airplay.driver import CliDriver
from seamer.simkit import TState
from seamer.simkit.knobs import knobs_for
from seamer.simkit.scenario import Scene

from .helpers import DacpReceiver, connect, until, wire

WIIM = "airplay/wiim_pro_plus"


def events(dev: Any, name: str) -> list[Any]:
    return [e for e in dev.rec.rec.events if e.name == name]


async def start(dev: Any, scene: Scene, lead_ms: int = 100, seconds: float = 30.0) -> dict:
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, seconds).path)
    at = int(time.time() * 1000) + lead_ms
    return await wire(dev, "start", anchor_unix_ms=at, requested_unix_ms=at, stream_url=url)


async def device(scene: Scene, profile: str = WIIM, **knobs: Any) -> Any:
    knobs.setdefault("starve_after_ms", {"ms": 0})
    knobs.setdefault("connect_ms", 0)
    return await scene.device(profile, **knobs)


def test_every_knob_has_quirks_and_confidence():
    defs = knobs_for("airplay")
    assert sum(d.family == "airplay" for d in defs.values()) >= 30
    for d in (d for d in defs.values() if d.family == "airplay"):
        assert d.quirks, d.name
        assert d.confidence in ("high", "medium", "low"), d.name


# -- route, clock, start --------------------------------------------------------------


async def test_airplay2_false_is_raop_only(scene: Scene):
    dev = await device(scene, airplay2=False)
    refused = await connect(dev, protocol="airplay2")
    assert refused["ok"] is False
    assert refused["code"] == "connect_failed"
    auto = await connect(dev)
    assert auto["protocol"] == "raop"
    assert auto["timing"] == "ntp"


async def test_buffered_route(scene: Scene):
    dev = await device(scene, buffered=True)
    assert (await connect(dev))["buffered"] is True
    plain = await device(scene)
    assert (await connect(plain))["buffered"] is False


async def test_ntp_streams_reject_400(scene: Scene):
    dev = await device(scene, ntp_streams="reject_400")
    r = await connect(dev, timing="ntp")
    assert (r["ok"], r["http"]) == (False, 400)
    assert (await connect(dev, timing="ptp"))["ok"]


async def test_ptp_respond_false_stalls_clock_and_stays_silent(scene: Scene, tmp_path):
    dev = await device(scene, ptp_respond=False)
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("clock_ready mode=ptp state=stalled")
        drv.feed_track(1, 3.0)
        await drv.start_at(100)
        await drv.wait_line("[STATUS] started")
        assert await until(lambda: events(dev, "silent"), 3)
        assert events(dev, "silent")[0].data["reason"] == "clock_stalled"
        assert dev.transport.state != TState.PLAYING
    finally:
        await drv.close()


async def test_first_probe_and_clock_lock_timing(scene: Scene, tmp_path):
    dev = await device(scene, first_probe_ms=300, clock_lock_ms={"min": 700, "max": 700})
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("[STATUS] connected")
        t0 = drv.time_of("[STATUS] connected")
        await drv.wait_line("state=ready", timeout=5)
        probing, ready = drv.time_of("state=probing"), drv.time_of("state=ready")
        assert probing is not None
        assert ready is not None
        assert t0 is not None
        assert 0.25 <= probing - t0 <= 0.6
        assert 0.6 <= ready - t0 <= 1.1
        assert drv.has("ready_at_unix_ms=")
    finally:
        await drv.close()


async def test_clock_lock_is_drawn_per_session_within_range(scene: Scene):
    dev = await device(scene, clock_lock_ms={"min": 1700, "max": 2300})
    locks = [(await connect(dev, active_remote=str(i)))["lock_ms"] for i in range(6)]
    assert all(1700 <= v <= 2300 for v in locks)
    assert len(set(locks)) > 1


async def test_ptp_role_standalone_needs_helper_follow(scene: Scene):
    """cli_ptp_follow too: an OS 27 HomePod is silent unless the helper follows its clock."""
    old = await device(scene, ptp_role="standalone_own_clock", cli_ptp_follow=False)
    assert (await connect(old))["clock_stalled"] is True
    new = await device(scene, ptp_role="standalone_own_clock", cli_ptp_follow=True)
    assert (await connect(new))["clock_stalled"] is False


async def test_anchor_400_then_helper_retries(scene: Scene, tmp_path):
    dev = await device(scene, anchor_400={"p": 1.0, "count": 2, "retry_ms": 200})
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("state=ready")
        drv.feed_track(1, 3.0)
        await drv.start_at(100)
        await drv.wait_line("[STATUS] started", timeout=5)
        assert sum("SETRATEANCHORTIME rejected (400)" in ln for _t, _s, ln in drv.lines) == 2
        assert len(events(dev, "anchor_refused")) == 2
        assert await until(lambda: dev.transport.state == TState.PLAYING, 5)
    finally:
        await drv.close()


async def test_buffered_anchor_always_400(scene: Scene, tmp_path):
    dev = await device(scene, buffered=True, buffered_anchor_always_400=True)
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("state=ready")
        await drv.start_at(100)
        line = await drv.wait_line("error code=start_failed", timeout=10)
        assert "http=400" in line
    finally:
        await drv.close()


async def test_min_audible_latency_drops_the_head(scene: Scene):
    dev = await device(scene, min_audible_latency_ms=2500)
    await connect(dev)
    assert (await start(dev, scene, lead_ms=500))["ok"]
    dropped = events(dev, "audio_dropped_short_latency")
    assert dropped
    assert 1900 <= dropped[0].data["dropped_ms"] <= 2100


async def test_output_latency_reported(scene: Scene, tmp_path):
    dev = await device(scene, output_latency_ms=2500)
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        assert "lead_ms=2500" in await drv.wait_line("[STATUS] latency")
    finally:
        await drv.close()


# -- session, auth, errors ------------------------------------------------------------


async def test_auth_password(scene: Scene, tmp_path):
    dev = await device(scene, auth="password")
    assert dev.txt_raop()["pw"] == "true"
    assert dev.txt_airplay()["pw"] == "true"
    assert (await connect(dev))["code"] == "auth_required"
    assert (await connect(dev, password="nope"))["code"] == "auth_failed"
    assert (await connect(dev, password="seamer"))["ok"]
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)  # no --password: what MA logs on a 401
        assert "http=401" in await drv.wait_line("error code=auth_required")
        assert await drv.wait_exit() == 1
    finally:
        await drv.close()


async def test_auth_pin_until_paired(scene: Scene):
    dev = await device(scene, auth="pin")
    r = await connect(dev)
    assert (r["code"], r["http"]) == ("auth_required", 470)
    assert (await connect(dev, has_auth=True))["ok"]
    other = await device(scene, auth="pin")
    await other.sim_action("pair", {})
    assert (await connect(other))["ok"]


async def test_raop_mode_auth_setup_404_and_silent(scene: Scene):
    broken = await device(scene, raop_mode="auth_setup_404")
    r = await connect(broken, protocol="raop")
    assert (r["ok"], r["http"]) == (False, 404)
    assert (await connect(broken))["protocol"] == "airplay2"  # AP2 still works
    silent = await device(scene, raop_mode="silent")
    assert (await connect(silent, protocol="raop"))["ok"]
    await start(silent, scene)
    assert await until(lambda: events(silent, "silent"), 3)
    assert events(silent, "silent")[0].data["reason"] == "raop_silent"


async def test_rtsp_first_status(scene: Scene, tmp_path):
    dev = await device(scene, rtsp_first_status=403)
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        assert "http=403" in await drv.wait_line("error code=connect_failed")
        assert await drv.wait_exit() == 1
    finally:
        await drv.close()


async def test_connect_ms(scene: Scene):
    dev = await scene.device(WIIM, connect_ms=400)
    t0 = time.monotonic()
    await connect(dev)
    assert time.monotonic() - t0 >= 0.38


async def test_starve_first_play_only_and_play_recovers(scene: Scene):
    dev = await scene.device(
        WIIM, connect_ms=0, starve_after_ms={"ms": 600, "first_play_only": True}
    )
    await connect(dev)
    await start(dev, scene)
    assert await until(lambda: dev.transport.state == TState.PLAYING, 3)
    assert await until(lambda: events(dev, "audio_starved"), 3)
    assert await until(lambda: dev.transport.state == TState.STOPPED, 1)
    assert dev.session is not None  # the session survives, only the audio stops
    await wire(dev, "play")
    assert await until(lambda: dev.transport.state == TState.PLAYING, 3)
    await wire(dev, "flush")
    await start(dev, scene)  # second START of the session: no drop-out
    await asyncio.sleep(1.2)
    assert len(events(dev, "audio_starved")) == 1


async def test_reset_control_after_s(scene: Scene, tmp_path):
    dev = await device(scene, reset_control_after_s=0.8)
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("[STATUS] connected")
        line = await drv.wait_line('detail="control channel reset"', timeout=5)
        assert "error code=connect_failed" in line
        assert await drv.wait_exit() == 1
        assert dev.session is None
    finally:
        await drv.close()


async def test_flush_ack_never(scene: Scene, tmp_path):
    dev = await device(scene, flush_ack="never")
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("[STATUS] connected")
        await drv.send("ACTION=FLUSH")
        assert await until(lambda: events(dev, "flushed"), 3)
        await asyncio.sleep(0.3)
        assert not drv.has("[STATUS] flushed")
    finally:
        await drv.close()


async def test_pause_reject(scene: Scene):
    dev = await device(scene, pause_reject=True)
    await connect(dev)
    await start(dev, scene)
    assert await until(lambda: dev.transport.state == TState.PLAYING, 3)
    assert (await wire(dev, "pause"))["ok"] is False
    assert dev.transport.state == TState.PLAYING


async def test_require_metadata_before_audio(scene: Scene):
    dev = await device(scene, require_metadata_before_audio=True)
    await connect(dev)
    await start(dev, scene)
    assert await until(lambda: events(dev, "silent"), 3)
    assert events(dev, "silent")[0].data["reason"] == "waiting_for_metadata"
    await wire(dev, "meta", title="Tone")
    assert await until(lambda: dev.transport.state == TState.PLAYING, 3)


# -- volume and DACP ------------------------------------------------------------------


async def test_volume_tick_applies_queue_and_echoes_every_level(scene: Scene):
    dev = await device(
        scene,
        volume_apply_tick_s={"min": 0.4, "max": 0.4, "step": 0},
        volume_echo={"enabled": True, "delay_ms": 50, "burst": 1},
    )
    async with DacpReceiver() as dacp:
        await dacp.attach(dev)
        await connect(dev)
        await wire(dev, "volume", volume=40)
        await wire(dev, "volume", volume=50)
        assert dev.volume == 30  # nothing applied before the tick
        assert await until(lambda: len(dacp.volumes()) == 2, 3)
        assert [db_to_volume(v) for v in dacp.volumes()] == [40, 50]
        gap = dacp.got[1]["t"] - dacp.got[0]["t"]
        assert 0.01 <= gap <= 0.2
        assert dev.volume == 50


async def test_volume_tick_step_limits_each_tick(scene: Scene):
    dev = await device(
        scene,
        volume_apply_tick_s={"min": 0.2, "max": 0.2, "step": 5},
        volume_echo={"enabled": False},
    )
    await connect(dev)
    await wire(dev, "volume", volume=45)
    assert await until(lambda: dev.volume == 45, 3)
    ticks = [e.data["applied"] for e in events(dev, "volume_tick")]
    assert ticks == [[35], [40], [45]]


async def test_volume_echo_delay_and_burst(scene: Scene):
    dev = await device(scene, volume_echo={"enabled": True, "delay_ms": 300, "burst": 2})
    async with DacpReceiver() as dacp:
        await dacp.attach(dev)
        await connect(dev, active_remote="7777")
        t0 = time.monotonic()
        await wire(dev, "volume", volume=60)
        assert await until(lambda: len(dacp.got) == 2, 3)
        assert dacp.got[0]["t"] - t0 >= 0.28
        assert {g["active_remote"] for g in dacp.got} == {"7777"}
        assert dacp.got[0]["dacp_id"] == "A1B2C3D4E5F60708"
    quiet = await device(scene, volume_echo={"enabled": False})
    await connect(quiet)
    await wire(quiet, "volume", volume=60)
    await asyncio.sleep(0.4)
    assert quiet.dacp_sent == []


@pytest.mark.parametrize(
    ("mode", "want"), [("session", "2222"), ("first_seen", "1111"), ("previous", "1111")]
)
async def test_dacp_active_remote(scene: Scene, mode: str, want: str):
    dev = await device(scene, dacp_active_remote=mode, volume_echo={"delay_ms": 0})
    async with DacpReceiver() as dacp:
        await dacp.attach(dev)
        await connect(dev, active_remote="1111")
        await connect(dev, active_remote="2222")
        await dev.sim_action("press", {"button": "volume", "level": 44})
        assert dacp.got[-1]["active_remote"] == want


async def test_dacp_after_teardown(scene: Scene):
    dev = await device(scene, dacp_after_teardown_ms=300, volume_echo={"enabled": False})
    async with DacpReceiver() as dacp:
        await dacp.attach(dev)
        await connect(dev, active_remote="9999")
        await wire(dev, "teardown")
        assert dacp.got == []
        assert await until(lambda: len(dacp.got) == 1, 2)
        assert dacp.got[0]["active_remote"] == "9999"
        assert "device-volume" in dacp.got[0]["path"]


async def test_dacp_report_idle(scene: Scene):
    for idle, want in ((False, 0), (True, 1)):
        dev = await device(scene, dacp_report_idle=idle)
        async with DacpReceiver() as dacp:
            await dacp.attach(dev)
            await connect(dev, active_remote="5555")
            await wire(dev, "teardown")
            await dev.sim_action("press", {"button": "volume", "level": 70})
            assert dev.volume == 70
            assert len(dacp.got) == want
            if want:
                assert dacp.got[0]["active_remote"] == "5555"  # the last id it saw


async def test_volume_reverts_in_session(scene: Scene):
    dev = await device(scene, volume_reverts_in_session=True, volume_echo={"enabled": False})
    await connect(dev)
    await wire(dev, "volume", volume=60)
    assert dev.volume == 60
    assert await until(lambda: dev.volume == 30, 2.5)


async def test_dacp_buttons(scene: Scene):
    dev = await device(scene)
    async with DacpReceiver() as dacp:
        await dacp.attach(dev)
        await connect(dev, active_remote="3333")
        assert (await dev.sim_action("press", {"button": "next"}))["status"] == 204
        assert dacp.got[-1]["path"] == "/ctrl-int/1/nextitem"


# -- helper process -------------------------------------------------------------------


async def _playing_helper(dev: Any, tmp_path: Any, seconds: float = 1.0) -> CliDriver:
    drv = CliDriver(tmp_path)
    await drv.start("127.0.0.1", dev.port)
    await drv.wait_line("state=ready")
    drv.feed_track(1, seconds, close=True)
    await drv.start_at(100)
    return drv


async def test_cli_exit_after_eof(scene: Scene, tmp_path):
    for flag in (True, False):
        dev = await device(scene, cli_exit_after_eof=flag)
        drv = await _playing_helper(dev, tmp_path / str(flag))
        try:
            await drv.wait_line("[STATUS] eof", timeout=6)
            if flag:
                assert await drv.wait_exit(3) == 0
            else:
                await asyncio.sleep(0.8)
                assert drv.proc is not None
                assert drv.proc.returncode is None
        finally:
            await drv.close()


async def test_cli_ignore_sigterm(scene: Scene, tmp_path):
    dev = await device(scene, cli_ignore_sigterm=True)
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("[STATUS] connected")
        assert drv.proc is not None
        drv.proc.send_signal(signal.SIGTERM)
        await drv.wait_line("SIGTERM ignored")
        await asyncio.sleep(0.3)
        assert drv.proc.returncode is None
    finally:
        await drv.close()  # SIGKILL, as MA escalates
    assert drv.proc.returncode == -signal.SIGKILL


async def test_cli_die_after_ms(scene: Scene, tmp_path):
    dev = await device(scene, cli_die_after_ms=500)
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("[STATUS] connected")
        assert await drv.wait_exit(3) == 1
        assert not drv.has("[STATUS] error")  # dies without a word
    finally:
        await drv.close()


@pytest.mark.parametrize("ack", ["missing", "late"])
async def test_cli_start_ack(scene: Scene, tmp_path, ack: str):
    dev = await device(scene, cli_start_ack=ack)
    drv = await _playing_helper(dev, tmp_path, seconds=6.0)
    try:
        await drv.wait_line("playing elapsed_ms", timeout=5)
        assert not drv.has("[STATUS] started")
        if ack == "late":
            await drv.wait_line("[STATUS] started", timeout=5)
        else:
            await asyncio.sleep(0.5)
            assert not drv.has("[STATUS] started")
    finally:
        await drv.close()
