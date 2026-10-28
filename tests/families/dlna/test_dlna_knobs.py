"""One test (at least) per dlna knob. ``test_every_knob_has_a_test`` keeps it that way."""

from pathlib import Path

import aiohttp
import dlna_helpers as h
import pytest

from seamer.simkit import TState
from seamer.simkit.knobs import knobs_for
from seamer.simkit.scenario import Scene

NEUTRAL = {"play_blocks_until_audio_s": 0.0, "openhome": False}


async def mk(scene: Scene, **knobs):
    """A well-behaved renderer (eversolo with Play not blocking, no OpenHome) + overrides."""
    return await scene.device("dlna/eversolo", **{**NEUTRAL, **knobs})


async def track_url(scene: Scene, n: int = 1, secs: float = 2.0, route: str = "media", **q):
    if scene._media_server is None:
        await scene.media_server()
    t = scene.media.track(n, secs)
    return scene.media_url(t.path, route=route, **q)


async def playing(scene: Scene, c: h.Soap, url: str, dur: str = "0:00:02.000") -> None:
    await c.load(url, duration=dur)
    await c.avt("Play", Speed="1")
    await scene.wait_for(lambda: c.dev.transport.state == TState.PLAYING, 5, "PLAYING")


def fired(scene: Scene, knob: str) -> None:
    scene.expect.eventually(kind="knob", name="fired", knob=knob)


def test_every_knob_has_a_test():
    src = Path(__file__).read_text()
    own = {n: k for n, k in knobs_for("dlna").items() if k.family == "dlna"}
    missing = [n for n in own if f"test_{n}" not in src]
    assert not missing, missing
    assert len(own) >= 40
    for k in own.values():
        assert k.quirks, k.name
        assert k.confidence in ("high", "medium", "low"), k.name


# -- AVTransport ------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "want"), [("stale", "Play,Stop,Seek"), ("empty", "")])
async def test_transport_actions(scene: Scene, mode: str, want: str):
    url = await track_url(scene, secs=3.0)
    dev = await mk(scene, transport_actions=mode)
    async with h.Soap(dev) as c:
        await playing(scene, c, url, "0:00:03.000")
        assert (await c.avt("GetCurrentTransportActions"))["Actions"] == want
    if mode == "stale":
        fired(scene, "transport_actions")


async def test_play_blocks_until_audio_s(scene: Scene):
    url = await track_url(scene)
    dev = await mk(scene, play_blocks_until_audio_s=5.0, load_delay_s=0.4)
    async with h.Soap(dev) as c:
        await c.load(url)
        t0 = dev.clock.monotonic()
        await c.avt("Play", Speed="1")
        assert dev.clock.monotonic() - t0 >= 0.35
        assert dev.transport.state == TState.PLAYING
    fired(scene, "play_blocks_until_audio_s")


async def test_play_response_delay_s(scene: Scene):
    url = await track_url(scene)
    dev = await mk(scene, play_response_delay_s=0.5)
    async with h.Soap(dev) as c:
        await c.load(url)
        t0 = dev.clock.monotonic()
        await c.avt("Play", Speed="1")
        assert dev.clock.monotonic() - t0 >= 0.45


async def test_autoplay_on_set_uri(scene: Scene):
    url = await track_url(scene)
    dev = await mk(scene, autoplay_on_set_uri=True)
    async with h.Soap(dev) as c:
        await c.load(url)
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "autoplay")
    fired(scene, "autoplay_on_set_uri")


async def test_previous_media_lingers_s(scene: Scene):
    a = await track_url(scene, 1, 4.0)
    b = await track_url(scene, 2, 4.0)
    dev = await mk(scene, previous_media_lingers_s=0.6)
    async with h.Soap(dev) as c:
        await playing(scene, c, a, "0:00:04.000")
        await c.load(b, duration="0:00:04.000")
        assert (await c.avt("GetMediaInfo"))["CurrentURI"] == a
        assert await c.state() == "PLAYING"
        await scene.wait_for(lambda: dev.linger is None, 3, "linger over")
        assert (await c.avt("GetMediaInfo"))["CurrentURI"] == b
    fired(scene, "previous_media_lingers_s")


async def test_set_next_ok_advances(scene: Scene):
    a = await track_url(scene, 1, 1.0)
    b = await track_url(scene, 2, 1.0)
    dev = await mk(scene, set_next="ok")
    async with h.Soap(dev) as c:
        await playing(scene, c, a, "0:00:01.000")
        await c.avt("SetNextAVTransportURI", NextURI=b, NextURIMetaData=h.didl("B", uri=b))
        assert (await c.avt("GetMediaInfo"))["NextURI"] == b
        await scene.wait_for(lambda: dev.transport.current.uri == b, 5, "advanced")
        await scene.wait_for(lambda: dev.transport.state == TState.STOPPED, 5, "end")
    scene.expect.audible_coverage(track=2, at_least_s=0.8)


async def test_set_next_absent(scene: Scene):
    dev = await mk(scene, set_next="absent")
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/upnp/avt/scpd.xml") as r:
        assert "SetNextAVTransportURI" not in await r.text()
    async with h.Soap(dev) as c:
        assert await c.fault("avt", "SetNextAVTransportURI", NextURI="x", NextURIMetaData="") == 401


async def test_set_next_ignored(scene: Scene):
    a = await track_url(scene, 1, 1.0)
    b = await track_url(scene, 2, 1.0)
    dev = await mk(scene, set_next="ignored")
    async with h.Soap(dev) as c:
        await playing(scene, c, a, "0:00:01.000")
        await c.avt("SetNextAVTransportURI", NextURI=b, NextURIMetaData=h.didl("B", uri=b))
        assert (await c.avt("GetMediaInfo"))["NextURI"] == b  # reported...
        await scene.wait_for(lambda: dev.transport.state == TState.STOPPED, 5, "end")
        assert dev.transport.current.uri == a  # ...but never played
    fired(scene, "set_next")


@pytest.mark.parametrize("mode", ["playing_only", "fault_701"])
async def test_set_next_faults(scene: Scene, mode: str):
    dev = await mk(scene, set_next=mode)
    async with h.Soap(dev) as c:
        await c.load("http://192.0.2.99/a.wav")
        assert await c.fault("avt", "SetNextAVTransportURI", NextURI="x", NextURIMetaData="") == 701
    fired(scene, "set_next")


async def test_stop_when_idle_701(scene: Scene):
    dev = await mk(scene, stop_when_idle_701=True)
    async with h.Soap(dev) as c:
        assert await c.fault("avt", "Stop") == 701
    plain = await mk(scene, device_id="plain")
    async with h.Soap(plain) as c:
        await c.avt("Stop")
    fired(scene, "stop_when_idle_701")


async def test_skip_settle_delay_s(scene: Scene):
    url = await track_url(scene, 1, 6.0)
    dev = await mk(scene, skip_settle_delay_s=0.5)
    async with h.Soap(dev) as c:
        await playing(scene, c, url, "0:00:06.000")
        await c.avt("Seek", Unit="REL_TIME", Target="0:00:04")
        assert dev.transport.truth_position() < 2.0  # not applied yet
        await scene.wait_for(lambda: dev.transport.truth_position() >= 4.0, 3, "settled")
    fired(scene, "skip_settle_delay_s")


async def test_track_gap_state(scene: Scene):
    a = await track_url(scene, 1, 1.0)
    b = await track_url(scene, 2, 1.0)
    dev = await mk(scene, track_gap_state="stopped", load_delay_s=0.3)
    seen: list[TState] = []

    def watch() -> bool:
        if dev.advancing:
            seen.append(dev.lens.state())
        return dev.transport.current.uri == b and dev.transport.state == TState.PLAYING

    async with h.Soap(dev) as c:
        await playing(scene, c, a, "0:00:01.000")
        await c.avt("SetNextAVTransportURI", NextURI=b, NextURIMetaData=h.didl("B", uri=b))
        await scene.wait_for(watch, 5, "second track playing")
    assert TState.STOPPED in seen
    fired(scene, "track_gap_state")


async def test_pause_needs_content_length(scene: Scene):
    url = await track_url(scene, 1, 6.0, route="stream", rate=4.0)
    dev = await mk(scene, pause_needs_content_length=True)
    async with h.Soap(dev) as c:
        await playing(scene, c, url, "0:00:06.000")
        assert dev.stream_chunked
        assert await c.fault("avt", "Pause") == 701
        await c.avt("Stop")
    fired(scene, "pause_needs_content_length")


# -- Seek ---------------------------------------------------------------------------------


async def test_seek_units_rel_time_only(scene: Scene):
    dev = await mk(scene, seek_units="rel_time_only")
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/upnp/avt/scpd.xml") as r:
        text = await r.text()
    assert "<allowedValue>REL_TIME</allowedValue>" in text
    assert "ABS_TIME" not in text
    async with h.Soap(dev) as c:
        await c.load("http://192.0.2.99/a.wav", duration="0:00:10.000")
        assert await c.fault("avt", "Seek", Unit="ABS_TIME", Target="0:00:01") == 710


async def test_seek_units_rel_time_silent(scene: Scene):
    url = await track_url(scene, 1, 6.0)
    dev = await mk(scene, seek_units="rel_time_silent")
    async with h.Soap(dev) as c:
        await playing(scene, c, url, "0:00:06.000")
        await c.avt("Seek", Unit="ABS_TIME", Target="0:00:04")  # 200, nothing happens
        assert dev.transport.truth_position() < 2.0
    fired(scene, "seek_units")


@pytest.mark.parametrize("mode", ["ok", "error", "resume_silently"])
async def test_seek_when_paused(scene: Scene, mode: str):
    url = await track_url(scene, 1, 6.0)
    dev = await mk(scene, seek_when_paused=mode)
    async with h.Soap(dev) as c:
        await playing(scene, c, url, "0:00:06.000")
        await c.avt("Pause")
        if mode == "error":
            assert await c.fault("avt", "Seek", Unit="REL_TIME", Target="0:00:03") == 701
            return
        await c.avt("Seek", Unit="REL_TIME", Target="0:00:03")
        if mode == "ok":
            assert await c.state() == "PAUSED_PLAYBACK"
            rel = (await c.avt("GetPositionInfo"))["RelTime"]
            assert rel == "0:00:03"
            await c.avt("Play", Speed="1")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 3, "playing")
        assert dev.transport.truth_position() >= 2.9
    if mode == "resume_silently":
        fired(scene, "seek_when_paused")


async def test_seek_emits_event(scene: Scene):
    url = await track_url(scene, 1, 6.0)
    dev = await mk(scene, seek_emits_event=False)
    async with h.GenaSink() as g, h.Soap(dev) as c:
        await g.subscribe(dev, "avt")
        await playing(scene, c, url, "0:00:06.000")
        await c.avt("Seek", Unit="REL_TIME", Target="0:00:03")
        await c.avt("Pause")  # a real change inside the mute window
    scene.expect.eventually(kind="event", name="avt_event_suppressed")
    fired(scene, "seek_emits_event")


# -- reporting ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "want"), [("stuck", "0:00:00"), ("not_implemented", "NOT_IMPLEMENTED")]
)
async def test_reltime(scene: Scene, mode: str, want: str):
    url = await track_url(scene, 1, 4.0)
    dev = await mk(scene, reltime=mode)
    async with h.Soap(dev) as c:
        await playing(scene, c, url, "0:00:04.000")
        await scene.wait_for(lambda: dev.transport.truth_position() > 1.2, 3, "progress")
        assert (await c.avt("GetPositionInfo"))["RelTime"] == want


async def test_abs_time_not_implemented(scene: Scene):
    dev = await mk(scene, abs_time_not_implemented=True)
    async with h.Soap(dev) as c:
        await c.load("http://192.0.2.99/a.wav")
        pos = await c.avt("GetPositionInfo")
    assert pos["AbsTime"] == "NOT_IMPLEMENTED"
    assert pos["RelTime"] == "0:00:00"


async def test_didl_malformed(scene: Scene):
    dev = await mk(scene, didl_malformed=True)
    async with h.Soap(dev) as c:
        await c.load("http://192.0.2.99/a.wav")
        meta = (await c.avt("GetMediaInfo"))["CurrentURIMetaData"]
    assert meta.startswith("<DIDL-Lite")
    assert "</DIDL-Lite>" not in meta


async def test_lastchange_malformed(scene: Scene):
    dev = await mk(scene, lastchange_malformed=1.0)
    async with h.GenaSink() as g, h.Soap(dev) as c:
        await g.subscribe(dev, "avt")
        await scene.wait_for(lambda: g.got, 3, "initial")
        await c.load("http://192.0.2.99/a.wav")
        await scene.wait_for(lambda: len(g.got) >= 2, 3, "change event")
    assert "</Event>" not in g.bodies("avt")[-1]
    fired(scene, "lastchange_malformed")


async def test_pause_emits_empty_uri_event(scene: Scene):
    url = await track_url(scene, 1, 6.0)
    dev = await mk(scene, pause_emits_empty_uri_event=True)
    async with h.GenaSink() as g, h.Soap(dev) as c:
        await g.subscribe(dev, "avt")
        await playing(scene, c, url, "0:00:06.000")
        await c.avt("Pause")
        await scene.wait_for(
            lambda: any(h.attr(b, "AVTransportURI") == "" for b in g.bodies("avt")), 3, "empty"
        )
        assert (await c.avt("GetMediaInfo"))["CurrentURI"] == url  # polling still fine
    fired(scene, "pause_emits_empty_uri_event")


async def test_duplicate_lastchange_and_vars(scene: Scene):
    dev = await mk(scene, duplicate_lastchange_and_vars=True)
    async with h.GenaSink() as g, h.Soap(dev) as c:
        await g.subscribe(dev, "avt")
        await scene.wait_for(lambda: g.got, 3, "initial")
        await c.load("http://192.0.2.99/a.wav")
        await scene.wait_for(lambda: len(g.got) >= 2, 3, "change event")
    raw = g.got[-1][3].decode()
    assert "<LastChange>" in raw
    assert "<TransportState>STOPPED</TransportState>" in raw


# -- SOAP / description ----------------------------------------------------------------------


async def test_non_utf8_bodies(scene: Scene):
    dev = await mk(scene, non_utf8_bodies=True)
    async with h.Soap(dev) as c:
        await c.avt("GetTransportInfo")
    assert b"\xe9" in c.last_raw
    with pytest.raises(UnicodeDecodeError):
        c.last_raw.decode("utf-8")


async def test_content_length_off_by_one(scene: Scene):
    dev = await mk(scene, content_length_off_by_one=True)
    async with h.Soap(dev) as c:
        await c.avt("GetTransportInfo")  # other actions fine
        with pytest.raises(aiohttp.ClientPayloadError):
            await c.call("avt", "GetMediaInfo", InstanceID=0)
    fired(scene, "content_length_off_by_one")


async def test_soap_fault_http200(scene: Scene):
    dev = await mk(scene, soap_fault_http200=True)
    async with h.Soap(dev) as c:
        status, out = await c.call("avt", "Play", InstanceID=0, Speed="1")
    assert status == 200
    assert out["errorCode"] == "701"
    fired(scene, "soap_fault_http200")


async def test_relative_presentation_url(scene: Scene):
    dev = await mk(scene, relative_presentation_url=True)
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/description.xml") as r:
        assert "<presentationURL>/</presentationURL>" in await r.text()


# -- RenderingControl -------------------------------------------------------------------------


async def test_volume_max(scene: Scene):
    dev = await mk(scene, volume_max=50)
    async with h.Soap(dev) as c:
        await c.rc("SetVolume", DesiredVolume=50)
        assert await c.fault("rc", "SetVolume", Channel="Master", DesiredVolume=51) == 402
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/upnp/rc/scpd.xml") as r:
        assert "<maximum>50</maximum>" in await r.text()


async def test_volume_before_play_ignored(scene: Scene):
    url = await track_url(scene)
    dev = await mk(scene, volume_before_play_ignored=True)
    async with h.Soap(dev) as c:
        await c.load(url)
        await c.rc("SetVolume", DesiredVolume=45)
        assert (await c.rc("GetVolume"))["CurrentVolume"] == "45"
        await playing(scene, c, url)
        assert (await c.rc("GetVolume"))["CurrentVolume"] == "20"  # amp put it back
    scene.expect.eventually(kind="event", name="volume_reverted", level=20)
    fired(scene, "volume_before_play_ignored")


async def test_no_rc_events(scene: Scene):
    dev = await mk(scene, no_rc_events=True)
    async with h.GenaSink() as g, h.Soap(dev) as c:
        await g.subscribe(dev, "rc")
        await scene.wait_for(lambda: g.got, 3, "initial")
        await c.rc("SetVolume", DesiredVolume=33)
        await scene.clock.sleep(0.3)
    assert len(g.bodies("rc")) == 1
    fired(scene, "no_rc_events")


async def test_rc_lastchange_namespace(scene: Scene):
    dev = await mk(scene, rc_lastchange_namespace="avt")
    async with h.GenaSink() as g:
        await g.subscribe(dev, "rc")
        await scene.wait_for(lambda: g.got, 3, "initial")
    assert 'xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/"' in g.bodies("rc")[0]


# -- media checks / streaming --------------------------------------------------------------


async def test_protocolinfo_filter(scene: Scene):
    dev = await mk(scene, protocolinfo_filter=True)
    async with h.Soap(dev) as c:
        url = "http://192.0.2.99/a.xyz"
        meta = h.didl("X", uri=url, mime="audio/x-unknown")
        _st, out = await c.call(
            "avt", "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=meta
        )
        assert out["errorCode"] == "714"
        await c.load("http://192.0.2.99/a.flac")  # audio/wav accepted
    fired(scene, "protocolinfo_filter")


async def test_protocolinfo_empty_entries(scene: Scene):
    dev = await mk(scene, protocolinfo_empty_entries=True)
    async with h.Soap(dev) as c:
        _st, out = await c.call("cm", "GetProtocolInfo")
    assert ",," in out["Sink"]
    assert out["Sink"].endswith(",")


async def test_head_check_on_set_uri(scene: Scene):
    ok = await track_url(scene)
    live = await track_url(scene, 2, 2.0, route="stream")  # this route has no HEAD
    dev = await mk(scene, head_check_on_set_uri=True)
    async with h.Soap(dev) as c:
        await c.load(ok)
        _st, out = await c.call(
            "avt", "SetAVTransportURI", InstanceID=0, CurrentURI=live, CurrentURIMetaData=""
        )
        assert out["errorCode"] == "716"
    fired(scene, "head_check_on_set_uri")


async def test_range_required_silent_fakes_playing(scene: Scene):
    """#6775: a 200 to a Range request -> PLAYING with RelTime counting, but no audio."""
    url = await track_url(scene, 1, 3.0, route="stream", rate=4.0)
    dev = await mk(scene, range_required="silent", openhome=True)
    async with h.Soap(dev) as c:
        await c.load(url, duration="0:00:03.000")
        await c.avt("Play", Speed="1")
        assert await c.state() == "PLAYING"
        await scene.clock.sleep(1.2)
        rel = (await c.avt("GetPositionInfo"))["RelTime"]
        assert rel in ("0:00:01", "0:00:02")
        _st, det = await c.call("ohinfo", "Details")
        assert det["CodecName"] == ""
        assert det["SampleRate"] == "0"
    assert dev.fake_play is not None
    assert dev.range_probes[0]["status"] == 200
    scene.expect.never(kind="sink", name="first_audio")
    fired(scene, "range_required")


async def test_range_required_error_and_ok(scene: Scene):
    live = await track_url(scene, 1, 2.0, route="stream")
    good = await track_url(scene, 2, 1.0)
    dev = await mk(scene, range_required="error")
    async with h.Soap(dev) as c:
        await c.load(live)
        assert await c.fault("avt", "Play", Speed="1") == 716
        await playing(scene, c, good, "0:00:01.000")  # /media/ answers 206
    assert [p["status"] for p in dev.range_probes[-3:]] == [206, 206, 206]


async def test_range_rerequest_after_bytes(scene: Scene):
    live = await track_url(scene, 1, 3.0, route="stream", rate=8.0)
    good = await track_url(scene, 2, 1.0)
    dev = await mk(scene, range_rerequest_after_bytes=65536)
    async with h.Soap(dev) as c:
        await playing(scene, c, good, "0:00:01.000")
        await scene.wait_for(lambda: dev.transport.state == TState.STOPPED, 5, "good done")
        await playing(scene, c, live, "0:00:03.000")
        await scene.wait_for(lambda: dev.transport.state != TState.PLAYING, 5, "cut off")
    scene.expect.audible_coverage(track=2, at_least_s=0.9)
    scene.expect.eventually(kind="sink", name="opened", part=1, status=200)
    fired(scene, "range_rerequest_after_bytes")


# -- GENA ----------------------------------------------------------------------------------


async def test_notify_before_subscribe_response(scene: Scene):
    dev = await mk(scene, notify_before_subscribe_response=True)
    async with h.GenaSink() as g:
        await g.subscribe(dev, "avt")
    assert g.sub_replies == [1]
    plain = await mk(scene, device_id="plain")
    async with h.GenaSink() as g2:
        await g2.subscribe(plain, "avt")
    assert g2.sub_replies == [0]


@pytest.mark.parametrize(
    ("value", "want"), [(0, "Second-300"), (-1, "Second-infinite"), (120, "Second-120")]
)
async def test_granted_timeout_s(scene: Scene, value: int, want: str):
    dev = await mk(scene, granted_timeout_s=value)
    async with h.GenaSink() as g:
        _st, hdrs = await g.subscribe(dev, "avt", timeout="Second-300")
    assert hdrs["TIMEOUT"] == want


async def test_new_sid_on_renew(scene: Scene):
    dev = await mk(scene, new_sid_on_renew=True)
    async with h.GenaSink() as g:
        _st, hdrs = await g.subscribe(dev, "avt")
        st, hdrs2 = await g.renew(dev, "avt", hdrs["SID"])
        assert st == 200
        assert hdrs2["SID"] != hdrs["SID"]
        assert (await g.renew(dev, "avt", hdrs["SID"]))[0] == 412  # old SID gone


async def test_renew_response_without_sid(scene: Scene):
    dev = await mk(scene, renew_response_without_sid=True)
    async with h.GenaSink() as g:
        _st, hdrs = await g.subscribe(dev, "avt")
        st, hdrs2 = await g.renew(dev, "avt", hdrs["SID"])
    assert st == 200
    assert "SID" not in hdrs2
    assert hdrs2["TIMEOUT"] == "Second-300"


async def test_reject_subscribe(scene: Scene):
    dev = await mk(scene, reject_subscribe=True)
    async with h.GenaSink() as g:
        st, _ = await g.subscribe(dev, "avt")
    assert st == 500


async def test_subscribe_ok_no_notify(scene: Scene):
    dev = await mk(scene, subscribe_ok_no_notify=True)
    async with h.GenaSink() as g, h.Soap(dev) as c:
        st, _ = await g.subscribe(dev, "avt")
        await c.load("http://192.0.2.99/a.wav")
        await scene.clock.sleep(0.4)
    assert st == 200
    assert g.got == []


async def test_event_delay_s(scene: Scene):
    dev = await mk(scene, event_delay_s=0.5)
    async with h.GenaSink() as g:
        t0 = dev.clock.monotonic()
        await g.subscribe(dev, "avt")
        await scene.wait_for(lambda: g.got, 3, "late initial")
        assert dev.clock.monotonic() - t0 >= 0.45


# -- surfaces -------------------------------------------------------------------------------


async def test_openhome(scene: Scene):
    on = await mk(scene, openhome=True)
    off = await mk(scene, device_id="off")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{on.base_url}/description.xml") as r:
            assert "urn:av-openhome-org:service:Info:1" in await r.text()
        async with s.get(f"{off.base_url}/upnp/ohinfo/scpd.xml") as r:
            assert r.status == 404
    async with h.GenaSink() as g:
        st, _ = await g.subscribe(on, "ohinfo")
        await scene.wait_for(lambda: g.got, 3, "oh initial")
    assert st == 200
    assert "<CodecName>" in g.got[0][3].decode()
