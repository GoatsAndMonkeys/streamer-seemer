import asyncio
import ssl

import aiohttp
import pytest

from seamer.families import get_family
from seamer.families.linkplay import proto
from seamer.families.linkplay.certs import openssl_bin
from seamer.simkit import TState
from seamer.simkit.scenario import Scene

from .helpers import Client, attr, didl

pytestmark = pytest.mark.skipif(openssl_bin() is None, reason="needs openssl for test certs")

PROFILES = ["wiim_pro", "wiim_mini", "wiim_amp", "audiopro_mkii", "arylic_a31"]


def test_alias_and_profiles():
    fam = get_family("wiim")
    assert fam.name == "linkplay"
    assert set(fam.profiles()) == set(PROFILES)


@pytest.mark.parametrize("profile", PROFILES)
async def test_every_profile_answers_getstatusex(scene: Scene, profile: str):
    dev = await scene.device(f"linkplay/{profile}")
    async with Client(dev) as c:
        body = await c.cmdj("getStatusEx")
        assert all(isinstance(v, (str, dict)) for v in body.values())
        assert body["uuid"] == dev.uuid
        assert len(body["uuid"]) == 24
        assert body["upnp_uuid"] == dev.udn
        assert body["DeviceName"] == dev.identity["name"]
        async with c.session.get(f"{dev.base_url}/description.xml") as r:
            desc = await r.text()
        assert dev.udn in desc
        assert "AVTransport" in desc
        assert ("PlayQueue" in desc) == (profile not in ("audiopro_mkii", "arylic_a31"))
        if profile == "audiopro_mkii":
            assert await c.cmd("getPlayerStatusEx") == (200, "unknown command")
        else:
            st = await c.cmdj("getPlayerStatusEx")
            assert st["status"] == "none"
            assert st["vol"] == "30"


async def test_https_split_and_wrong_scheme(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", http_scheme="https_only")
    assert set(dev.api_urls) == {"https"}
    assert dev.api_url.startswith("https://127.0.0.1:")
    async with Client(dev) as c:
        # plain http on the UPnP port: the device's 404 text
        assert await c.cmd("getStatusEx", base=dev.base_url) == (404, "404: Not Found")
    a31 = await scene.device("linkplay/arylic_a31")
    assert set(a31.api_urls) == {"http"}
    async with Client(a31) as c:
        body = await c.cmdj("getStatusEx")
        assert body["apcli0"] == ""
        assert "security" not in body
    both = await scene.device("linkplay/wiim_pro", http_scheme="both")
    assert set(both.api_urls) == {"https", "http"}


async def test_mtls_rejects_client_without_cert(scene: Scene):
    dev = await scene.device("linkplay/audiopro_mkii")
    async with Client(dev, with_cert=True) as c:
        assert (await c.cmdj("getStatusEx"))["DeviceName"] == "Audio Pro A10"
    async with Client(dev, with_cert=False) as c:
        with pytest.raises((aiohttp.ClientError, ssl.SSLError, ConnectionError)):
            await c.cmd("getStatusEx")


async def test_upnp_play_to_eof(scene: Scene):
    await scene.media_server()
    t = scene.media.track(1, 2.0)
    url = scene.media_url(t.path)
    dev = await scene.device("linkplay/wiim_pro")
    async with Client(dev) as c:
        await c.avt("SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=didl("One", uri=url))
        info = await c.avt("GetTransportInfo")
        assert info["CurrentTransportState"] == "STOPPED"
        await c.avt("Play", Speed="1")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
        pos = await c.avt("GetPositionInfo")
        assert pos["TrackURI"] == url
        assert pos["TrackDuration"] == "00:00:02"
        ex = await c.avt("GetInfoEx")
        assert ex["PlayMedium"] == "NETWORK"
        assert ex["CurrentTransportState"] == "PLAYING"
        st = await c.cmdj("getPlayerStatusEx")
        assert st["status"] == "play"
        assert st["mode"] == "10"
        assert proto.hexdec(st["Title"]) == "One"
        await scene.wait_for(lambda: dev.transport.state == TState.STOPPED, 6, "eof")
        assert (await c.avt("GetTransportInfo"))["CurrentTransportState"] == "STOPPED"
    scene.expect.audible_coverage(track=1, at_least_s=1.9)


async def test_http_play_pause_seek_volume(scene: Scene):
    await scene.media_server()
    t = scene.media.track(2, 6.0)
    url = scene.media_url(t.path)
    dev = await scene.device("linkplay/wiim_pro")
    async with Client(dev) as c:
        assert await c.cmd(f"setPlayerCmd:play:{url}") == (200, "OK")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
        assert await c.cmd("setPlayerCmd:pause") == (200, "OK")
        assert dev.transport.state == TState.PAUSED
        assert (await c.cmdj("getPlayerStatusEx"))["status"] == "pause"
        assert await c.cmd("setPlayerCmd:resume") == (200, "OK")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "resumed")
        assert await c.cmd("setPlayerCmd:seek:3") == (200, "OK")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "after seek")
        assert dev.transport.truth_position() >= 3.0
        assert await c.cmd("setPlayerCmd:vol:42") == (200, "OK")
        assert (await c.cmdj("getPlayerStatusEx"))["vol"] == "42"
        assert (await c.soap("rc", "GetVolume", InstanceID=0, Channel="Master"))[1] == {
            "CurrentVolume": "42"
        }
        assert await c.cmd("setPlayerCmd:stop") == (200, "OK")
        assert await c.cmd("bogusCommand") == (200, "unknown command")


async def test_soap_faults(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro")
    async with Client(dev) as c:
        status, out = await c.soap("avt", "Pause", InstanceID=0)
        assert status == 500
        assert out["errorCode"] == "701"
        status, out = await c.soap("avt", "Frobnicate", InstanceID=0)
        assert status == 500
        assert out["errorCode"] == "401"
        status, out = await c.soap("avt", "Seek", InstanceID=0, Unit="TRACK_NR", Target="1")
        assert status == 500
        assert out["errorCode"] == "710"


async def test_gena_subscribe_and_notify(scene: Scene):
    got: list[tuple[str, str]] = []
    seen = asyncio.Event()

    async def notify(req: aiohttp.web.Request) -> aiohttp.web.Response:
        got.append((req.headers["SID"], await req.text()))
        seen.set()
        return aiohttp.web.Response()

    from aiohttp import web

    from seamer.simkit.kits.http import HttpServer

    app = web.Application()
    app.router.add_route("NOTIFY", "/cb", notify)
    cb = HttpServer(app)
    await cb.start()
    try:
        dev = await scene.device("linkplay/wiim_pro")
        async with aiohttp.ClientSession() as s:
            async with s.request(
                "SUBSCRIBE",
                f"{dev.base_url}/upnp/rc/event",
                headers={
                    "CALLBACK": f"<{cb.base_url}/cb>",
                    "NT": "upnp:event",
                    "TIMEOUT": "Second-300",
                },
            ) as r:
                assert r.status == 200
                sid = r.headers["SID"]
                assert r.headers["TIMEOUT"].startswith("Second-")
            await asyncio.wait_for(seen.wait(), 3)
            assert got[0][0] == sid
            assert "Volume" in got[0][1]
            seen.clear()
            async with Client(dev) as c:
                await c.soap("rc", "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=55)
            await asyncio.wait_for(seen.wait(), 3)
            assert 'val="55"' in got[-1][1].replace("&quot;", '"')
            async with s.request(
                "SUBSCRIBE", f"{dev.base_url}/upnp/rc/event", headers={"SID": "uuid:nope"}
            ) as r:
                assert r.status == 412
            async with s.request(
                "UNSUBSCRIBE", f"{dev.base_url}/upnp/rc/event", headers={"SID": sid}
            ) as r:
                assert r.status == 200
    finally:
        await cb.stop()
    assert attr("<x/>", "Volume") is None
