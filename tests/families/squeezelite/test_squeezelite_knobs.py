"""One test per squeezelite knob (connection knobs share a few)."""

import asyncio

from seamer.simkit import TState
from seamer.simkit.scenario import Scene

from .helpers import rig, track_url


def heard(scene: Scene, track: int) -> float:
    total = 0.0
    for ev in scene.rec.timeline().where(kind="sink", name="heard"):
        total += float(ev.data.get("tracks", {}).get(str(track), 0.0))
    return total


async def test_helo_delay(scene: Scene):
    dev, srv = await rig(scene, helo_delay_s=0.5)
    tl = scene.rec.timeline()
    conn = tl.first(kind="event", name="connected")
    helo = tl.first(kind="resp", name="tx:HELO")
    assert conn and helo and helo.t - conn.t >= 0.45


async def test_server_silence_timeout_reconnects(scene: Scene):
    dev, srv = await rig(scene, server_silence_timeout_s=0.4, reconnect_delay_s=0.1)
    await srv.wait("HELO", n=2, timeout=5)
    assert scene.rec.timeline().first(kind="event", name="server_silence")


async def test_heartbeats_keep_connection(scene: Scene):
    dev, srv = await rig(scene, heartbeat_s=0.1, server_silence_timeout_s=0.4)
    await asyncio.sleep(1.0)
    assert srv.connections == 1


async def test_batch_frames_per_recv(scene: Scene):
    dev, srv = await rig(scene, batch_frames_per_recv=4)
    url = await track_url(scene, 1, 1.0)
    srv.play(url)
    await srv.wait("STMu", timeout=10)
    assert dev.writes < len(dev.tx) / 2


async def test_send_bye_keep_socket(scene: Scene):
    dev, srv = await rig(scene, reconnect_delay_s=0.1)
    await dev.sim_action("bye", {})
    await srv.wait("BYE!")
    await asyncio.sleep(0.4)
    assert srv.connections == 1 and not dev.connected
    dev2, srv2 = await rig(scene, reconnect_delay_s=0.1, send_bye_keep_socket=True)
    await dev2.sim_action("bye", {})
    await srv2.wait("BYE!")
    # the device did not hang up after BYE!: when the server drops it, it comes back
    await srv2.wait("HELO", n=2, timeout=3)


async def test_hello_caps_override(scene: Scene):
    dev, srv = await rig(scene, hello_caps="Model=odd,pcm")
    assert srv.helos[0][36:] == b"Model=odd,pcm"


async def test_can_https_false_rejects_https(scene: Scene):
    dev, srv = await rig(scene, can_https=False)
    assert b"CanHTTPS" not in srv.helos[0]
    srv.play("https://127.0.0.1:9/x.flac")
    await srv.wait("STMn")
    assert dev.transport.state != TState.PLAYING


async def test_max_pcm_bits(scene: Scene):
    dev, srv = await rig(scene, "squeezelite_esp32")
    srv.strm("s", autostart="3", codec="p2321", server_port=9, request=b"GET /x HTTP/1.0\r\n\r\n")
    await srv.wait("STMn")
    assert scene.rec.timeline().first(kind="event", name="unsupported_pcm").data["bits"] == 24


async def test_setd_name_nul(scene: Scene):
    dev, srv = await rig(scene, setd_name_nul=False)
    assert not srv.names[0].endswith(b"\x00")


async def test_stream_buffer_kb(scene: Scene):
    dev, srv = await rig(scene, stream_buffer_kb=512)
    srv.strm("t", replay_gain=1)
    await srv.wait("STMt")
    assert srv.last_stat()["buf_size"] == 512 * 1024


async def test_stmd_early_cuts_current_track(scene: Scene):
    """Early STMd makes the server send the next strm s mid-stream: track 1 is cut."""
    dev, srv = await rig(scene, stmd_early_s=0.5, buffer_capacity_s=1.0)
    a = await track_url(scene, 1, 6.0, route="stream")
    b = await track_url(scene, 2, 1.5)
    srv.play(a)
    srv.enqueue(b)
    await srv.wait("STMu", timeout=15)
    assert scene.rec.timeline().first(kind="event", name="early_stmd")
    assert heard(scene, 1) < 4.5
    assert heard(scene, 2) > 1.3


async def test_no_early_stmd_plays_both_fully(scene: Scene):
    dev, srv = await rig(scene, buffer_capacity_s=1.0)
    a = await track_url(scene, 1, 3.0, route="stream")
    b = await track_url(scene, 2, 1.5)
    srv.play(a)
    srv.enqueue(b)
    await srv.wait("STMu", timeout=15)
    assert heard(scene, 1) > 2.8


async def test_stale_stmt_after_flush(scene: Scene):
    dev, srv = await rig(scene, stale_stmt_after_flush=1)
    url = await track_url(scene, 1, 4.0)
    srv.play(url)
    await srv.wait("STMs")
    await asyncio.sleep(1.0)
    srv.stop()
    await srv.wait("STMf", n=srv.count("STMf") + 1)
    for i in range(2):
        srv.strm("t", replay_gain=100 + i)
        await srv.wait("STMt", n=srv.count("STMt") + 1)
        elapsed = srv.last_stat()["elapsed_ms"]
        assert (elapsed > 500) if i == 0 else (elapsed == 0)


async def test_stale_resp_after_flush(scene: Scene):
    dev, srv = await rig(scene, stale_resp_after_flush=True)
    url = await track_url(scene, 1, 4.0)
    srv.play(url)
    await srv.wait("STMs")
    srv.play(url)
    await srv.wait("STMs", n=2)
    assert scene.rec.timeline().first(kind="event", name="stale_resp")
    assert srv.count("RESP") == 3


async def test_codc_after_body_noise(scene: Scene):
    dev, srv = await rig(scene, codc_after_body_noise=True)
    url = await track_url(scene, 1, 4.0)
    srv.play(url)
    await srv.wait("STMs")
    assert not scene.rec.timeline().first(kind="event", name="hold")
    srv.send(b"codc", b"p1321")
    await scene.wait_for(
        lambda: scene.rec.timeline().first(kind="event", name="decoder_reopen_noise") is not None,
        2,
        "noise",
    )


async def test_flac_sync_silent(scene: Scene):
    await scene.media_server()
    t = scene.media.track(1, 2.0, fmt="flac")
    dev, srv = await rig(scene, flac_sync_silent=True)
    srv.play(scene.media_url(t.path))
    await srv.wait("RESP")
    await asyncio.sleep(1.0)
    assert "STMs" not in srv.events()
    assert dev.sink.audible.played_s == 0


async def test_stmn_decode_error(scene: Scene):
    dev, srv = await rig(scene, stmn_decode_error={"p": 1, "at_s": 0.5}, buffer_capacity_s=1.0)
    url = await track_url(scene, 1, 5.0, route="stream")
    srv.play(url)
    await srv.wait("STMn", timeout=5)
    await srv.wait("STMu", timeout=10)
    assert "STMd" not in srv.events()
    assert heard(scene, 1) < 3.5


async def test_stmo_underrun_at(scene: Scene):
    dev, srv = await rig(scene, stmo_underrun_at={"p": 1, "at_s": 0.3, "stall_s": 0.5})
    url = await track_url(scene, 1, 3.0)
    srv.play(url)
    await srv.wait("STMo", timeout=5)
    assert dev.transport.state == TState.PAUSED
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 2, "self-resume")
    assert "STMr" not in srv.events()


async def test_dsco_reason(scene: Scene):
    dev, srv = await rig(scene, dsco_reason=4)
    url = await track_url(scene, 1, 1.0)
    srv.play(url)
    assert await srv.wait("DSCO") == b"\x04"


async def test_initial_gain_zero(scene: Scene):
    lone = await scene.device("squeezelite/squeezelite_linux", initial_gain_zero=True)
    assert lone.gain == 0.0
    dev, srv = await rig(scene, initial_gain_zero=True)
    assert dev.audg_seen and dev.gain > 0


async def test_mute_aude_stops_output_off(scene: Scene):
    dev, srv = await rig(scene, mute_aude_stops_output=False)
    url = await track_url(scene, 1, 3.0)
    srv.play(url)
    await srv.wait("STMs")
    srv.mute(True)
    await asyncio.sleep(0.3)
    assert dev.transport.state == TState.PLAYING


async def test_early_start_before_unpause_at(scene: Scene):
    dev, srv = await rig(scene, early_start_before_unpause_at=True)
    srv.auto_unpause = False
    url = await track_url(scene, 1, 2.0)
    srv.play(url, autostart=False)
    await srv.wait("STMs", timeout=5)
    assert "STMr" not in srv.events()


async def test_without_unpause_device_waits(scene: Scene):
    dev, srv = await rig(scene)
    srv.auto_unpause = False
    url = await track_url(scene, 1, 2.0)
    srv.play(url, autostart=False)
    await srv.wait("STMl")
    await asyncio.sleep(0.5)
    assert "STMs" not in srv.events()


async def _elapsed_after_pause(scene: Scene, **knobs) -> tuple[int, int]:
    dev, srv = await rig(scene, **knobs)
    url = await track_url(scene, 1, 6.0)
    srv.play(url)
    await srv.wait("STMs")
    await asyncio.sleep(0.5)
    srv.pause()
    await srv.wait("STMp")
    a = dev.elapsed_ms()
    await asyncio.sleep(0.6)
    return a, dev.elapsed_ms()


async def test_heartbeat_while_paused_advances(scene: Scene):
    a, b = await _elapsed_after_pause(scene)
    assert b == a
    a, b = await _elapsed_after_pause(scene, heartbeat_while_paused_advances=True)
    assert b - a >= 500


async def test_pause_resume_position_jump(scene: Scene):
    dev, srv = await rig(scene, pause_resume_position_jump={"p": 1, "jump_s": 2.0})
    url = await track_url(scene, 1, 6.0)
    srv.play(url)
    await srv.wait("STMs")
    await asyncio.sleep(0.3)
    srv.pause()
    await srv.wait("STMp")
    before = dev.elapsed_ms()
    srv.unpause()
    await srv.wait("STMr")
    await asyncio.sleep(0.1)
    assert dev.elapsed_ms() - before >= 1900


async def test_seek_elapsed_from_zero_off(scene: Scene):
    for from_zero in (True, False):
        dev, srv = await rig(scene, seek_elapsed_from_zero=from_zero)
        url = await track_url(scene, 1, 6.0)
        srv.play(url)
        await srv.wait("STMs")
        await asyncio.sleep(1.0)
        srv.play(url)  # a seek is a flush + new stream
        await srv.wait("STMs", n=2)
        assert (dev.elapsed_ms() < 500) == from_zero


async def test_elapsed_ms_bits(scene: Scene):
    dev, srv = await rig(scene, elapsed_ms_bits=8)
    url = await track_url(scene, 1, 3.0)
    srv.play(url)
    await srv.wait("STMs")
    await asyncio.sleep(0.6)
    assert dev.elapsed_ms() < 256


async def test_stmt_interval(scene: Scene):
    dev, srv = await rig(scene, stmt_interval_s=0.0)
    url = await track_url(scene, 1, 2.0)
    srv.play(url)
    await srv.wait("STMs")
    await asyncio.sleep(1.2)
    assert srv.count("STMt") == 0
    dev2, srv2 = await rig(scene, stmt_interval_s=0.3)
    srv2.play(url)
    await srv2.wait("STMt", n=2, timeout=3)


async def test_jiffies_offset_and_drift(scene: Scene):
    base = await scene.device("squeezelite/squeezelite_linux")
    off = await scene.device("squeezelite/squeezelite_linux", jiffies_offset_ms=100_000)
    assert 99_000 < (off.jiffies() - base.jiffies()) & 0xFFFFFFFF < 101_000
    fast = await scene.device("squeezelite/squeezelite_linux", drift_ppm=100_000.0)
    a, fa = base.jiffies(), fast.jiffies()
    await asyncio.sleep(0.5)
    b, fb = base.jiffies(), fast.jiffies()
    assert (fb - fa) - (b - a) >= 30
