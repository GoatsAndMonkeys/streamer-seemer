import asyncio
import plistlib
import subprocess
import sys

import aiohttp
import pytest

from seamer.families import get_family
from seamer.families.airplay import device as ap_device
from seamer.families.airplay import fake_cli
from seamer.families.airplay.device import db_to_volume, volume_to_db
from seamer.families.airplay.driver import CliDriver
from seamer.simkit import TState
from seamer.simkit.scenario import Scene

from .helpers import DacpReceiver, connect, until, wire

PROFILES = [
    "sonos_era100",
    "homepod_mini_os27",
    "wiim_pro_plus",
    "airport_express",
    "shairport_sync",
]


def test_profiles_load():
    fam = get_family("airplay")
    assert set(fam.profiles()) == set(PROFILES)
    assert fam.default_profile in PROFILES


@pytest.mark.parametrize("profile", PROFILES)
async def test_info_plist_and_txt(scene: Scene, profile: str):
    dev = await scene.device(f"airplay/{profile}")
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/info") as r:
        assert r.status == 200
        assert r.content_type == "application/x-apple-binary-plist"
        info = plistlib.loads(await r.read())
    assert info["deviceID"] == dev.device_id()
    assert dev.device_id().startswith("02:")  # locally administered fake MAC
    assert str(dev.identity["ip"]).startswith("192.0.2.")
    assert info["name"] == dev.identity["name"]
    ap2, raop = dev.txt_airplay(), dev.txt_raop()
    assert ap2["deviceid"] == dev.device_id()
    assert ap2["model"] == raop["am"] == info["model"]
    assert dev.raop_service_name() == dev.device_id().replace(":", "") + "@" + info["name"]
    if profile == "airport_express":
        assert "supportedAudioFormatsExtended" not in info
        assert raop["ft"] == "0x0"
    else:
        assert info["supportedAudioFormatsExtended"]["audioStream"]


def test_volume_db_mapping():
    assert volume_to_db(0) == -144.0
    assert volume_to_db(100) == 0.0
    assert volume_to_db(50) == -15.0
    for level in (1, 20, 33, 50, 99, 100, 0):
        assert db_to_volume(volume_to_db(level)) == level


async def test_fake_cli_check_and_ptp_daemon(tmp_path):
    exe = fake_cli.install(tmp_path / "bin")
    out = subprocess.run(  # noqa: ASYNC221 - a short one-shot
        [str(exe), "--check"], capture_output=True, text=True, check=True
    )
    assert "cliairplay" in out.stdout
    assert "check" in out.stdout
    proc = await asyncio.create_subprocess_exec(
        str(exe), "--ptp-daemon", stderr=asyncio.subprocess.PIPE
    )
    assert proc.stderr is not None
    line = await asyncio.wait_for(proc.stderr.readline(), 10)
    assert b"[PTP] daemon up" in line
    proc.terminate()
    assert await asyncio.wait_for(proc.wait(), 5) == 0


async def test_play_pause_flush_stop_through_fake_helper(scene: Scene, tmp_path):
    """MA's sequence against the real fake subprocess: the sink hears what went in."""
    dev = await scene.device("airplay/wiim_pro_plus", starve_after_ms={"ms": 0})
    async with DacpReceiver() as dacp:
        await dacp.attach(dev)
        drv = CliDriver(tmp_path, binary=str(fake_cli.install(tmp_path / "bin")))
        try:
            await drv.start("127.0.0.1", dev.port, active_remote="424242")
            await drv.wait_line("[STATUS] connected")
            await drv.wait_line("route protocol=airplay2")
            drv.feed_track(1, 6.0)
            await drv.wait_line("clock_ready mode=ptp state=ready")
            await drv.wait_line("[STATUS] audio buffered_ms")
            await drv.send("TITLE=Tone", "ARTIST=Seamer", "ACTION=SENDMETA")
            await drv.start_at(300)
            await drv.wait_line("[STATUS] started")
            assert await until(lambda: dev.transport.state == TState.PLAYING, 5)
            await drv.wait_line("playing elapsed_ms")
            await asyncio.sleep(1.5)

            await drv.send("ACTION=PAUSE")
            await drv.wait_line("[STATUS] paused")
            assert dev.transport.state == TState.PAUSED
            await drv.send("ACTION=PLAY")
            assert await until(lambda: dev.transport.state == TState.PLAYING, 5)

            await drv.send("VOLUME=55")
            assert await until(lambda: dev.volume == 55, 5)
            assert await until(lambda: len(dacp.volumes()) == 1, 3)  # the WiiM echoes it
            assert db_to_volume(dacp.volumes()[0]) == 55
            assert dacp.got[0]["active_remote"] == "424242"

            n = len(drv.lines)
            await drv.send("ACTION=FLUSH")
            await drv.wait_line("[STATUS] flushed", after=n)
            assert dev.transport.state == TState.STOPPED
            assert dev.session is not None  # the connection survives a flush
            await asyncio.sleep(1.0)
            assert await drv.stop() == 0
            assert dev.session is None
        finally:
            await drv.close()
    assert dev.status()["session"] is None
    scene.expect.audible_coverage(track=1, at_least_s=1.5)
    assert any(e.name == "metadata" for e in dev.rec.rec.events)


async def test_eof_is_reported(scene: Scene, tmp_path):
    dev = await scene.device("airplay/wiim_pro_plus", starve_after_ms={"ms": 0})
    drv = CliDriver(tmp_path)
    try:
        await drv.start("127.0.0.1", dev.port)
        await drv.wait_line("[STATUS] connected")
        drv.feed_track(2, 1.5, close=True)
        await drv.wait_line("clock_ready mode=ptp state=ready")
        await drv.start_at(200)
        await drv.wait_line("[STATUS] eof", timeout=10)
        assert await drv.stop() == 0
    finally:
        await drv.close()
    scene.expect.audible_coverage(track=2, at_least_s=1.0)


async def test_wire_rejects_stale_session(scene: Scene):
    dev = await scene.device("airplay/wiim_pro_plus")
    first = await connect(dev)
    second = await connect(dev, active_remote="2222")
    assert first["session"] != second["session"]
    assert (await wire(dev, "pause", session=first["session"]))["ok"] is False
    assert (await wire(dev, "events", session=first["session"]))["events"] == ["teardown"]
    ended = [e for e in dev.rec.rec.events if e.name == "session_ended"]
    assert ended[0].data["reason"] == "replaced"


async def test_advertise_publishes_both_services(scene: Scene, monkeypatch):
    published: list[tuple[str, str, dict[str, str]]] = []

    class FakeAdvertiser:
        def __init__(self, stype, name, port, addresses, props):
            self.args = (stype, name, props)

        @staticmethod
        def available() -> bool:
            return True

        async def start(self) -> None:
            published.append(self.args)

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(ap_device, "MdnsAdvertiser", FakeAdvertiser)
    sonos = await scene.device("airplay/sonos_era100")
    assert (await sonos.sim_action("advertise", {}))["advertised"]
    assert [p[0] for p in published] == ["_airplay._tcp.local.", "_raop._tcp.local."]
    assert published[1][1] == "02005E000041@Sonos Era 100"
    published.clear()
    apx = await scene.device("airplay/airport_express")
    await apx.advertise()
    assert [p[0] for p in published] == ["_raop._tcp.local."]  # RAOP only


def test_fake_cli_runs_under_plain_python():
    out = subprocess.run(
        [sys.executable, "-I", fake_cli.__file__, "--check"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == fake_cli.CHECK_LINE
