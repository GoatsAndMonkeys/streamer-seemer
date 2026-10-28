import asyncio

import aiohttp
import dlna_helpers as h
import pytest

from seamer.families import get_family
from seamer.families.dlna import openhome, proto, upnp
from seamer.simkit import TState
from seamer.simkit.scenario import Scene

PROFILES = ["eversolo", "generic_cheap", "gmrender_resurrect", "lumin_teac", "samsung_tv"]


def test_profiles_listed():
    fam = get_family("dlna")
    assert set(fam.profiles()) == set(PROFILES)
    assert fam.default_profile in PROFILES


@pytest.mark.parametrize("profile", PROFILES)
async def test_every_profile_describes_itself(scene: Scene, profile: str):
    dev = await scene.device(f"dlna/{profile}")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{dev.base_url}/description.xml") as r:
            desc = await r.text()
        assert r.status == 200
        assert f"<UDN>{dev.udn}</UDN>" in desc
        assert "urn:schemas-upnp-org:device:MediaRenderer:1" in desc
        assert "<dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>" in desc
        assert f"<friendlyName>{dev.identity['name']}" in desc.replace("&amp;", "&")
        assert ("av-openhome-org" in desc) == bool(dev.knobs.get("openhome"))
        for key in dev.service_keys():
            async with s.get(f"{dev.base_url}/upnp/{key}/scpd.xml") as r:
                assert r.status == 200
                assert "<actionList>" in await r.text()
    async with h.Soap(dev) as c:
        assert await c.state() == "NO_MEDIA_PRESENT"
        vol = await c.rc("GetVolume")
        assert vol == {"CurrentVolume": dev.identity["volume"]}


async def test_udn_is_stable_and_unique(scene: Scene):
    a = await scene.device("dlna/generic_cheap", device_id="a")
    b = await scene.device("dlna/generic_cheap", device_id="b", start=False)
    a2 = await scene.device("dlna/generic_cheap", device_id="a", start=False)
    assert a.udn != b.udn
    assert a.udn == a2.udn
    assert a.udn.startswith("uuid:")


async def test_play_to_end_really_fetches_audio(scene: Scene):
    await scene.media_server()
    t = scene.media.track(1, 1.5)
    dev = await scene.device("dlna/eversolo", play_blocks_until_audio_s=0.0)
    url = scene.media_url(t.path)
    async with h.Soap(dev) as c:
        await c.load(url, "One", "0:00:01.500")
        assert await c.state() == "STOPPED"
        await c.avt("Play", Speed="1")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
        assert await c.state() == "PLAYING"
        pos = await c.avt("GetPositionInfo")
        assert pos["TrackURI"] == url
        assert pos["TrackDuration"] == "0:00:01"
        assert proto.didl_fields(pos["TrackMetaData"])["title"] == "One"
        await scene.wait_for(lambda: dev.transport.state == TState.STOPPED, 5, "ended")
        assert await c.state() == "STOPPED"
    scene.expect.audible_coverage(track=1, at_least_s=1.4)
    scene.expect.eventually(kind="cmd", name="POST /upnp/avt/control")


async def test_pause_seek_volume_and_readback(scene: Scene):
    await scene.media_server()
    t = scene.media.track(2, 6.0)
    dev = await scene.device("dlna/eversolo", play_blocks_until_audio_s=0.0)
    url = scene.media_url(t.path)
    async with h.Soap(dev) as c:
        await c.load(url, "Two", "0:00:06.000")
        assert (await c.avt("GetCurrentTransportActions"))["Actions"] == "Play,Stop,Seek"
        await c.avt("Play", Speed="1")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
        acts = (await c.avt("GetCurrentTransportActions"))["Actions"]
        assert acts == "Pause,Stop,Seek,Next,Previous"
        await c.avt("Pause")
        assert await c.state() == "PAUSED_PLAYBACK"
        await c.avt("Play", Speed="1")
        await c.avt("Seek", Unit="REL_TIME", Target="0:00:03")
        await scene.wait_for(lambda: dev.transport.truth_position() >= 3.2, 5, "seeked")
        rel = proto.parse_hms((await c.avt("GetPositionInfo"))["RelTime"])
        assert rel is not None
        assert 3 <= rel <= 5
        await c.rc("SetVolume", DesiredVolume=37)
        assert (await c.rc("GetVolume"))["CurrentVolume"] == "37"
        await c.rc("SetMute", DesiredMute=1)
        assert (await c.rc("GetMute"))["CurrentMute"] == "1"
        await scene.wait_for(lambda: dev.transport.truth_position() >= 4.6, 5, "played on")
        await c.avt("Stop")
        assert await c.state() == "STOPPED"
    assert dev.volume == 37
    assert dev.muted
    scene.expect.audible_coverage(track=2, at_least_s=1.0)


async def test_standard_faults(scene: Scene):
    dev = await scene.device("dlna/eversolo")
    async with h.Soap(dev) as c:
        assert await c.fault("avt", "Play", Speed="1") == 701  # no media
        assert await c.fault("avt", "Bogus") == 401
        assert (
            await c.fault("avt", "SetAVTransportURI", CurrentURI="", CurrentURIMetaData="") == 714
        )
        st, out = await c.call("avt", "GetTransportInfo", InstanceID=3)
        assert st == 500
        assert out["errorCode"] == "718"
        await c.load("http://192.0.2.99/x.wav", duration="0:00:10.000")
        assert await c.fault("avt", "Seek", Unit="X_DLNA_REL_BYTE", Target="0") == 710
        assert await c.fault("avt", "Seek", Unit="REL_TIME", Target="nonsense") == 711
        assert await c.fault("avt", "Seek", Unit="REL_TIME", Target="1:00:00") == 711
        assert await c.fault("rc", "SetVolume", Channel="LF", DesiredVolume=1) == 402
        assert await c.fault("rc", "SetVolume", Channel="Master", DesiredVolume=101) == 402
        assert await c.fault("avt", "Next") == 701
    raw = c.last_raw.decode()
    assert "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>" in raw
    assert '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">' in raw
    assert "<errorDescription>Transition not available</errorDescription>" in raw


async def test_scpd_allowed_values(scene: Scene):
    dev = await scene.device("dlna/eversolo", volume_max=60)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{dev.base_url}/upnp/avt/scpd.xml") as r:
            avt = await r.text()
        async with s.get(f"{dev.base_url}/upnp/rc/scpd.xml") as r:
            rc = await r.text()
    assert "<allowedValue>REL_TIME</allowedValue>" in avt
    assert "<name>SetNextAVTransportURI</name>" in avt
    assert "<allowedValue>PAUSED_PLAYBACK</allowedValue>" in avt
    assert "<minimum>0</minimum><maximum>60</maximum><step>1</step>" in rc


async def test_gena_initial_and_change_events(scene: Scene):
    await scene.media_server()
    t = scene.media.track(3, 1.0)
    dev = await scene.device("dlna/eversolo", play_blocks_until_audio_s=0.0)
    async with h.GenaSink() as g, h.Soap(dev) as c:
        st, hdrs = await g.subscribe(dev, "avt")
        assert st == 200
        assert hdrs["SID"].startswith("uuid:")
        assert hdrs["TIMEOUT"] == "Second-300"
        await g.subscribe(dev, "rc")
        await scene.wait_for(lambda: len(g.got) >= 2, 3, "initial notifies")
        first = g.bodies("avt")[0]
        assert h.attr(first, "TransportState") == "NO_MEDIA_PRESENT"
        assert 'xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/"' in first
        assert next(x for x in g.got if x[0] == "avt")[2]["SEQ"] == "0"
        await c.load(scene.media_url(t.path))
        await c.avt("Play", Speed="1")
        await scene.wait_for(
            lambda: any(h.attr(b, "TransportState") == "PLAYING" for b in g.bodies("avt")),
            5,
            "PLAYING event",
        )
        await c.rc("SetVolume", DesiredVolume=5)
        await scene.wait_for(
            lambda: any(h.attr(b, "Volume") == "5" for b in g.bodies("rc")), 3, "volume event"
        )
        rc_body = next(b for b in g.bodies("rc") if h.attr(b, "Volume") == "5")
        assert 'xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/"' in rc_body
        assert '<Volume channel="Master" val="5"/>' in rc_body
        st, hdrs2 = await g.renew(dev, "avt", hdrs["SID"])
        assert st == 200
        assert hdrs2["SID"] == hdrs["SID"]
        assert (await g.renew(dev, "avt", "uuid:nope"))[0] == 412


async def test_openhome_info_shows_real_codec(scene: Scene):
    await scene.media_server()
    t = scene.media.track(4, 2.0)
    dev = await scene.device("dlna/eversolo", play_blocks_until_audio_s=0.0)
    assert "ohinfo" in dev.service_keys()
    async with h.Soap(dev) as c:
        await c.load(scene.media_url(t.path))
        await c.avt("Play", Speed="1")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
        st, det = await c.call("ohinfo", "Details")
        assert st == 200
        assert det["CodecName"] == "WAV"
        assert det["SampleRate"] == "44100"
        st, src = await c.call("ohproduct", "SourceXml")
        assert "<Name>UPnP AV</Name>" in src["Value"]
        st, vol = await c.call("ohvolume", "Volume")
        assert vol["Value"] == dev.identity["volume"]
        st, tm = await c.call("ohtime", "Time")
        assert tm["TrackCount"] == "1"
    assert openhome.INFO in upnp.SERVICES["ohinfo"][0]


async def test_ssdp_answers_msearch(scene: Scene):
    dev = await scene.device("dlna/gmrender_resurrect")
    assert dev.ssdp is not None
    loop = asyncio.get_running_loop()
    got: list[bytes] = []

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            got.append(data)

    tr, _ = await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
    try:
        msg = (
            'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
            "MX: 1\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
        )
        tr.sendto(msg.encode(), ("127.0.0.1", dev.ssdp.port))
        await scene.wait_for(lambda: bool(got), 3, "ssdp reply")
    finally:
        tr.close()
    text = got[0].decode()
    assert f"{dev.base_url}/description.xml" in text
    assert dev.udn in text


async def test_sim_actions(scene: Scene):
    dev = await scene.device("dlna/generic_cheap")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{dev.base_url}/_sim/action/local_volume", json={"level": 9}) as r:
            assert (await r.json())["result"] == 9
        async with s.post(f"{dev.base_url}/_sim/action/nope", json={}) as r:
            assert r.status == 404
        async with s.get(f"{dev.base_url}/_sim/state") as r:
            st = await r.json()
    assert st["family_state"]["volume"] == 9
    assert st["family_state"]["reported_state"] == "NO_MEDIA_PRESENT"
