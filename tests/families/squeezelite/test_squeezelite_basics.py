import asyncio
import struct

import pytest

from seamer.families import get_family
from seamer.families.squeezelite import protocol as P
from seamer.simkit import TState
from seamer.simkit.scenario import Scene

from .fake_server import FakeDiscovery
from .helpers import rig, track_url

PROFILES = [
    "squeezelite_linux",
    "picoreplayer_hwvol",
    "squeezelite_esp32",
    "squeezebox_radio",
    "wiim_slimproto",
]


def test_alias_and_profiles():
    fam = get_family("slimproto")
    assert fam.name == "squeezelite"
    assert set(fam.profiles()) == set(PROFILES)


def test_frame_layouts():
    helo = P.helo(device_id=12, revision=0, mac="02:00:00:00:00:09", capabilities="Model=x")
    assert helo[:4] == b"HELO"
    assert struct.unpack("!I", helo[4:8])[0] == 36 + 7
    assert helo[10:16] == bytes([2, 0, 0, 0, 0, 9])
    stat = P.stat("STMt", P.StatFields(elapsed_ms=1500, jiffies=7))
    assert struct.unpack("!I", stat[4:8])[0] == 53
    f = P.STAT_STRUCT.unpack(stat[12:59])
    assert (f[8], f[11], f[13]) == (7, 1, 1500)
    s = P.parse_strm(P.build_strm("s", autostart="3", server_port=9, request=b"GET /a HTTP/1.0"))
    assert (s.command, s.autostart, s.server_port, s.request_line()) == ("s", 3, 9, ("GET", "/a"))
    reader = P.ServerFrameReader()
    data = P.server_frame(b"vers", b"7.9") + P.server_frame(b"setd", b"\x00")
    assert list(reader.feed(data[:5])) == []
    assert list(reader.feed(data[5:])) == [("vers", b"7.9"), ("setd", b"\x00")]


@pytest.mark.parametrize("v", [1, 20, 50, 77, 100])
def test_gain_roundtrip_both_curves(v: int):
    for curve in P.CURVES:
        assert P.gain_to_volume(P.old_gain(v), P.new_gain(v, curve)) == v
    assert P.gain_to_volume(0, 0) == 0


@pytest.mark.parametrize("profile", PROFILES)
async def test_every_profile_says_hello(scene: Scene, profile: str):
    dev, srv = await rig(scene, profile)
    helo = srv.helos[0]
    assert helo[0] == dev.device_id
    assert ":".join(f"{b:02x}" for b in helo[2:8]) == dev.mac
    caps = helo[36:].decode()
    assert f"Model={dev.identity['model']}" in caps
    assert ("CanHTTPS=1" in caps) == (profile not in ("squeezelite_esp32", "squeezebox_radio"))
    name = srv.names[0]
    assert name[0] == 0
    assert name[1:].rstrip(b"\x00").decode() == dev.identity["name"]
    assert name.endswith(b"\x00") == (profile != "squeezebox_radio")
    assert dev.volume == 20
    assert dev.status()["connected"]


async def test_play_to_eof_event_order(scene: Scene):
    dev, srv = await rig(scene)
    url = await track_url(scene, 1, 2.0)
    srv.play(url)
    await srv.wait("STMu", timeout=10)
    ev = [e for e in srv.events() if (e.startswith("STM") and e != "STMt") or e == "RESP"]
    assert ev[:4] == ["STMf", "STMf", "STMf", "STMc"] or ev[:2] == ["STMf", "STMf"]
    order = [e for e in ev if e in ("STMc", "RESP", "STMs", "STMd", "STMu")]
    assert order == ["STMc", "RESP", "STMs", "STMd", "STMu"]
    assert "DSCO" in srv.events()
    assert dev.transport.state == TState.STOPPED
    scene.expect.audible_coverage(track=1, at_least_s=1.8)


async def test_body_held_until_cont(scene: Scene):
    _dev, srv = await rig(scene)
    url = await track_url(scene, 1, 2.0)
    srv.awaiting_stmc = True
    srv.play(url)
    await srv.wait("RESP")
    # the fake server answered codc+cont; the hold was released by cont
    await srv.wait("STMs")
    tl = scene.rec.timeline()
    hold = tl.first(kind="event", name="hold")
    release = tl.first(kind="event", name="release")
    cont = tl.first(kind="cmd", name="cont")
    assert hold
    assert release
    assert cont
    assert hold.t <= cont.t <= release.t


async def test_no_autostart_waits_for_unpause(scene: Scene):
    _dev, srv = await rig(scene)
    url = await track_url(scene, 1, 2.0)
    srv.play(url, autostart=False)
    await srv.wait("STMl")
    i_l = srv.events().index("STMl")
    await srv.wait("STMs")
    assert srv.events().index("STMs") > srv.events().index("STMr") > i_l


async def test_pause_resume_and_heartbeat(scene: Scene):
    dev, srv = await rig(scene)
    url = await track_url(scene, 1, 4.0)
    srv.play(url)
    await srv.wait("STMs")
    await asyncio.sleep(0.5)
    srv.pause()
    await srv.wait("STMp")
    assert dev.transport.state == TState.PAUSED
    srv.strm("t", replay_gain=4242)
    await srv.wait("STMt", n=srv.count("STMt") + 1)
    st = [p for op, p in srv.frames if op == "STMt"][-1]
    f = P.STAT_STRUCT.unpack(st[4:51])
    assert f[14] == 4242
    paused_ms = f[13]
    await asyncio.sleep(0.4)
    srv.strm("t", replay_gain=4243)
    await srv.wait("STMt", n=srv.count("STMt") + 1)
    assert srv.last_stat()["elapsed_ms"] == paused_ms
    srv.unpause()
    await srv.wait("STMr")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 2, "resume")


async def test_volume_and_hardware_volume(scene: Scene):
    dev, srv = await rig(scene)
    srv.volume(55)
    await scene.wait_for(lambda: dev.volume == 55, 2, "volume 55")
    assert 0 < dev.gain < 1
    hw, srv2 = await rig(scene, "picoreplayer_hwvol")
    srv2.volume(40)
    await scene.wait_for(lambda: hw.volume == 40, 2, "volume 40")
    assert hw.gain == 1.0


async def test_mute_stops_output_and_unmute_does_not_resume(scene: Scene):
    dev, srv = await rig(scene)
    url = await track_url(scene, 1, 4.0)
    srv.play(url)
    await srv.wait("STMs")
    srv.mute(True)
    await scene.wait_for(lambda: dev.transport.state == TState.PAUSED, 2, "muted = stopped")
    srv.mute(False)
    await asyncio.sleep(0.3)
    assert dev.transport.state == TState.PAUSED
    assert "STMp" not in srv.events()  # squeezelite does not tell the server


async def test_gapless_enqueue(scene: Scene):
    _dev, srv = await rig(scene)
    a = await track_url(scene, 1, 2.0)
    b = await track_url(scene, 2, 2.0)
    srv.play(a)
    srv.enqueue(b)
    await srv.wait("STMs", n=2, timeout=10)
    await srv.wait("STMu", timeout=10)
    assert srv.count("STMu") == 1
    assert srv.count("STMd") == 2
    scene.expect.audible_coverage(track=1, at_least_s=1.8)
    scene.expect.audible_coverage(track=2, at_least_s=1.8)


async def test_discovery_tlv_and_legacy(scene: Scene):
    disc, port = await FakeDiscovery.start()
    try:
        dev = await scene.device("squeezelite/squeezelite_linux")
        reply, addr = await dev.discover("127.0.0.1", port)
        assert reply["NAME"] == "Seamer Test Server"
        assert reply["IPAD"] == "192.0.2.10"
        assert addr[1] == port
        assert disc.requests[0][:1] == b"e"
        assert b"IPAD" in disc.requests[0]
        reply, _ = await dev.discover("127.0.0.1", port, legacy=True)
        assert reply == {"IPAD": "192.0.2.10"}
        legacy = disc.requests[1]
        assert len(legacy) == 18
        assert legacy[2] == dev.device_id
    finally:
        assert disc.transport is not None
        disc.transport.close()


async def test_reconnect_sets_flag(scene: Scene):
    _dev, srv = await rig(scene, reconnect_delay_s=0.1)
    assert srv._writer is not None
    srv._writer.close()
    await srv.wait("HELO", n=2)
    first, second = srv.helos
    assert struct.unpack("!H", first[24:26])[0] == 0
    assert struct.unpack("!H", second[24:26])[0] & P.WLAN_RECONNECT


async def test_flush_and_stop(scene: Scene):
    dev, srv = await rig(scene)
    url = await track_url(scene, 1, 4.0)
    srv.play(url)
    await srv.wait("STMs")
    n = srv.count("STMf")
    srv.stop()
    await srv.wait("STMf", n=n + 1)
    assert dev.transport.state == TState.STOPPED
    srv.strm("f")  # nothing left to flush: no STMf
    await asyncio.sleep(0.2)
    assert srv.count("STMf") == n + 1


async def test_helo_lists_codecs_plainly(scene: Scene):
    _dev, srv = await rig(scene)
    caps = srv.helos[0][36:].decode()
    assert "[" not in caps, caps
    assert ",flc," in caps, caps
    assert caps.endswith(",mp3"), caps
    assert ",AccuratePlayPoints=1," in caps
