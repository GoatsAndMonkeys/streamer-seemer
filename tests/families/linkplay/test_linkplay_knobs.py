"""One test per LinkPlay family knob (the quirk it models, switched on or off)."""

import asyncio
import json
import socket
import time
from pathlib import Path

import aiohttp
import pytest

from seamer.families.linkplay import proto
from seamer.families.linkplay.certs import openssl_bin
from seamer.simkit import TState
from seamer.simkit.knobs import knobs_for
from seamer.simkit.scenario import Scene

from .helpers import Client, GenaSink, attr, didl

pytestmark = pytest.mark.skipif(openssl_bin() is None, reason="needs openssl for test certs")

FAST = {"load_delay_s": 0.0}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start(scene: Scene, c: Client, seconds: float = 4.0, n: int = 1, title: str = "One"):
    """SetAVTransportURI + Play a fresh track; returns its URL once PLAYING."""
    await scene.media_server()
    t = scene.media.track(n, seconds)
    url = scene.media_url(t.path)
    dur = proto.hms(seconds) + ".000"
    await c.avt("SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=didl(title, dur, url))
    await c.avt("Play", Speed="1")
    dev = c.dev
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
    return url


def test_every_family_knob_has_a_test():
    src = Path(__file__).read_text()
    names = [k for k, v in knobs_for("linkplay").items() if v.family == "linkplay"]
    missing = [n for n in names if f"def test_{n}(" not in src]
    assert not missing


# -- transport, TLS, identity -------------------------------------------------------------


async def test_http_scheme(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", http_scheme="http_only")
    assert dev.api_url.startswith("http://")
    async with Client(dev) as c:
        assert (await c.cmdj("getStatusEx"))["DeviceName"] == "WiiM Pro"


async def test_https_port(scene: Scene):
    port = _free_port()
    dev = await scene.device("linkplay/wiim_pro", https_port=port)
    assert dev.api_urls["https"] == f"https://127.0.0.1:{port}"


async def test_http_port(scene: Scene):
    port = _free_port()
    dev = await scene.device("linkplay/arylic_a31", http_port=port)
    assert dev.api_urls["http"] == f"http://127.0.0.1:{port}"


async def test_client_cert_required(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", client_cert_required=True)
    async with Client(dev, with_cert=False) as c:
        with pytest.raises(aiohttp.ClientError):
            await c.cmd("getStatusEx")
    async with Client(dev) as c:
        assert (await c.cmd("getStatusEx"))[0] == 200


async def test_wrong_scheme_reply(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", wrong_scheme_reply="conn_reset")
    async with Client(dev) as c:
        with pytest.raises(aiohttp.ClientError):
            await c.cmd("getStatusEx", base=dev.base_url)


async def test_uuid_form(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", uuid_form="full32")
    async with Client(dev) as c:
        body = await c.cmdj("getStatusEx")
    assert body["uuid"] == dev.uuid + dev.uuid[:8]
    assert body["upnp_uuid"].replace("-", "").removeprefix("uuid:") == body["uuid"]


async def test_wmrm_version(scene: Scene):
    lead = await scene.device("linkplay/wiim_pro")
    old = await scene.device("linkplay/wiim_mini", wmrm_version="2.0")
    async with Client(lead) as c:
        assert (await c.cmdj("multiroom:getSlaveList"))["wmrm_version"] == "4.2"
    async with Client(old) as c:
        # answered OK, never grouped (lq:Q49)
        assert await c.cmd(f"multiroom:JoinGroup:IP={lead.ip}:uuid={lead.uuid}") == (200, "OK")
    await asyncio.sleep(0.05)
    assert lead.followers == []


async def test_netfield_style(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", netfield_style="a31")
    async with Client(dev) as c:
        body = await c.cmdj("getStatusEx")
    assert body["apcli0"] == ""
    assert body["security_version"] == "2.0"
    assert "security" not in body


# -- HTTP payloads ---------------------------------------------------------------------


async def test_metadata_encoding(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", metadata_encoding="plain", **FAST)
    async with Client(dev) as c:
        await _start(scene, c, title="Plain Title")
        st = await c.cmdj("getPlayerStatusEx")
    assert st["Title"] == "Plain Title"
    assert st["Artist"] == "Test Artist"


async def test_idle_unknown_sentinel(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", idle_unknown_sentinel="Nada")
    async with Client(dev) as c:
        st = await c.cmdj("getPlayerStatusEx")
    assert proto.hexdec(st["Title"]) == "Nada"


async def test_artwork_sentinel(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", artwork_sentinel="un_known", **FAST)
    async with Client(dev) as c:
        await _start(scene, c)
        meta = (await c.cmdj("getMetaInfo"))["metaData"]
    assert meta["albumArtURI"] == "un_known"


async def test_duplicate_vendor_key(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", duplicate_vendor_key=True)
    async with Client(dev) as c:
        st = await c.cmdj("getPlayerStatusEx")
    assert st["vendor"] == ""
    assert st["Vendor"] == "unknown"


async def test_boot_status_none_s(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", boot_status_none_s=0.3)
    async with Client(dev) as c:
        st = await c.cmdj("getPlayerStatusEx")
        assert (st["status"], st["mode"]) == ("none", "-1")
        await asyncio.sleep(0.35)
        assert (await c.cmdj("getPlayerStatusEx"))["mode"] == "0"


async def test_json_corruption(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", json_corruption={"p": 1.0})
    async with Client(dev) as c:
        status, text = await c.cmd("getPlayerStatusEx")
    assert status == 200
    assert ',,"mode"' in text
    assert text.endswith("#")
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


async def test_pos_units(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", pos_units="us", **FAST)
    async with Client(dev) as c:
        await _start(scene, c, seconds=2.0)
        st = await c.cmdj("getPlayerStatusEx")
    assert st["totlen"] == "2000000"


async def test_metainfo(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", metainfo="trailing_space", **FAST)
    async with Client(dev) as c:
        await _start(scene, c)
        meta = (await c.cmdj("getMetaInfo"))["metaData"]
        assert "sampleRate " in meta
        assert "sampleRate" not in meta
        dev.knobs.set_live("metainfo", "unsupported")
        assert await c.cmd("getMetaInfo") == (200, "unknown command")


async def test_failure_word(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", failure_word="Fail")
    async with Client(dev) as c:
        assert await c.cmd("getMetaInfo") == (200, "Fail")
        assert await c.cmd("setPlayerCmd:vol:loud") == (200, "Fail")


async def test_reboot_reply(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro")
    async with Client(dev) as c:
        assert await c.cmd("reboot") == (200, '{"status":"OK"}')
        dev.knobs.set_live("reboot_reply", "empty")
        assert await c.cmd("reboot") == (200, "")


async def test_eq_reply_json(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", eq_reply_json=False)
    async with Client(dev) as c:
        assert await c.cmd("EQOn") == (200, "OK")
        dev.knobs.set_live("eq_reply_json", True)
        assert await c.cmd("EQOn") == (200, '{"status":"OK"}')


async def test_unsupported_commands(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", unsupported_commands="getStatus,EQOn")
    async with Client(dev) as c:
        assert await c.cmd("getStatus") == (200, "unknown command")
        assert await c.cmd("EQOn") == (200, "unknown command")
        assert (await c.cmd("getStatusEx"))[0] == 200


async def test_slavelist_shape(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", slavelist_shape="legacy_caps_strings")
    async with Client(dev) as c:
        assert await c.cmdj("multiroom:getSlaveList") == {"slaves": "0", "slave_list": []}
        dev.knobs.set_live("slavelist_shape", "modern")
        assert await c.cmdj("multiroom:getSlaveList") == {"slaves": 0, "wmrm_version": "4.2"}


# -- position / state misreports -----------------------------------------------------


async def test_play_start_delay_s(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", play_start_delay_s=0.5, **FAST)
    async with Client(dev) as c:
        t0 = time.monotonic()
        await _start(scene, c)
    assert time.monotonic() - t0 >= 0.5


async def test_stale_prev_position(scene: Scene):
    dev = await scene.device(
        "linkplay/wiim_pro",
        stale_prev_position={"p": 1.0, "window_s": 1.0},
        play_start_delay_s=0.3,
        load_delay_s=0.4,
    )
    async with Client(dev) as c:
        await _start(scene, c, seconds=8.0)
        await asyncio.sleep(2.2)
        t = scene.media.track(2, 8.0)
        url2 = scene.media_url(t.path)
        await c.avt("SetAVTransportURI", CurrentURI=url2, CurrentURIMetaData=didl("Two", uri=url2))
        await c.avt("Play", Speed="1")
        await scene.wait_for(lambda: dev.transport.state == TState.LOADING, 2, "loading")
        pos = await c.avt("GetPositionInfo")
        assert pos["TrackURI"] == url2
        assert proto.parse_hms(pos["RelTime"]) >= 2  # the old stream's position (lq:Q17)
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 3, "playing")
        assert proto.parse_hms((await c.avt("GetPositionInfo"))["RelTime"]) < 2
    scene.expect.eventually(kind="knob", name="fired")


async def test_end_position_after_load_s(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", end_position_after_load_s=0.6, **FAST)
    async with Client(dev) as c:
        await _start(scene, c, seconds=5.0)
        pos = await c.avt("GetPositionInfo")
        assert pos["RelTime"] == pos["TrackDuration"] == "00:00:05"  # lq:Q16
        await asyncio.sleep(0.7)
        assert (await c.avt("GetPositionInfo"))["RelTime"] < "00:00:02"


async def test_false_playing_without_uri_s(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", false_playing_without_uri_s=0.5)
    async with Client(dev) as c:
        await c.avt("Play", Speed="1")
        info = await c.avt("GetTransportInfo")
        assert info["CurrentTransportState"] == "PLAYING"  # lq:Q18
        assert (await c.avt("GetPositionInfo"))["TrackURI"] == ""
        await asyncio.sleep(0.55)
        assert (await c.avt("GetTransportInfo"))["CurrentTransportState"] == "NO_MEDIA_PRESENT"


async def test_curpos_negative_on_restart(scene: Scene):
    dev = await scene.device(
        "linkplay/wiim_pro", curpos_negative_on_restart={"p": 1.0}, load_delay_s=0.4
    )
    async with Client(dev) as c:
        await _start(scene, c)
        t = scene.media.track(2, 4.0)
        url2 = scene.media_url(t.path)
        await c.avt("SetAVTransportURI", CurrentURI=url2, CurrentURIMetaData="")
        await c.avt("Play", Speed="1")
        st = await c.cmdj("getPlayerStatusEx")
    assert st["status"] == "load"
    assert st["curpos"].startswith("-")


async def test_released_source_looks_paused(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", **FAST)
    async with Client(dev) as c:
        await _start(scene, c)
        await dev.app_takeover("Phone queue")
        assert (await c.avt("GetTransportInfo"))["CurrentTransportState"] == "PAUSED_PLAYBACK"
        assert dev.transport.state == TState.PLAYING  # truth: the app's queue plays
    dev2 = await scene.device("linkplay/wiim_pro", released_source_looks_paused=False, **FAST)
    async with Client(dev2) as c:
        await _start(scene, c, n=2)
        await dev2.app_takeover()
        assert (await c.avt("GetTransportInfo"))["CurrentTransportState"] == "PLAYING"


async def test_status_stop_while_connect_playing(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro")
    async with Client(dev) as c:
        await dev.switch_source("qobuz")
        st = await c.cmdj("getPlayerStatusEx")
        assert (st["status"], st["vendor"]) == ("stop", "Qobuz Connect")
        dev.knobs.set_live("status_stop_while_connect_playing", False)
        assert (await c.cmdj("getPlayerStatusEx"))["status"] == "play"


# -- sources -------------------------------------------------------------------------


async def test_input_switch_without_upnp_event(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device(
            "linkplay/wiim_pro", input_switch_without_upnp_event=True, input_switch_stop_s=0.2
        )
        assert (await sink.subscribe(dev, "avt"))[0] == 200
        await asyncio.sleep(0.1)
        await dev.switch_source("hdmi")
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 2, "hdmi")
        await asyncio.sleep(0.1)
        later = sink.bodies("avt")[1:]
        assert later
        assert all("PlaybackStorageMedium" not in b for b in later)
        async with Client(dev) as c:
            assert (await c.cmdj("getPlayerStatusEx"))["mode"] == "49"  # HTTP knows
            assert (await c.avt("GetInfoEx"))["PlayMedium"] == "HDMI"
    finally:
        await sink.stop()


async def test_input_switch_stop_s(scene: Scene):
    dev = await scene.device("linkplay/wiim_amp", input_switch_stop_s=0.4, **FAST)
    async with Client(dev) as c:
        await _start(scene, c)
        await dev.switch_source("hdmi")
        assert dev.transport.state == TState.STOPPED
        await asyncio.sleep(0.2)
        assert dev.transport.state == TState.STOPPED
        await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 1, "hdmi plays")
        assert dev.transport.owner == "hdmi"


async def test_stale_metadata_after_switch(scene: Scene):
    dev = await scene.device(
        "linkplay/wiim_pro", stale_metadata_after_switch=True, input_switch_stop_s=0.0, **FAST
    )
    async with Client(dev) as c:
        url = await _start(scene, c, seconds=4.0)
        await dev.switch_source("line-in")
        await scene.wait_for(lambda: dev.transport.owner == "line-in", 1, "line-in")
        pos = await c.avt("GetPositionInfo")
    assert pos["TrackURI"] == url  # lq:Q23
    assert pos["TrackDuration"] == "00:00:04"


async def test_switchmode_empty_body_s(scene: Scene):
    dev = await scene.device(
        "linkplay/wiim_pro", switchmode_empty_body_s=0.3, input_switch_stop_s=0.0
    )
    async with Client(dev) as c:
        assert await c.cmd("setPlayerCmd:switchmode:optical") == (200, "OK")
        assert await c.cmd("getPlayerStatusEx") == (200, "")
        await asyncio.sleep(0.35)
        assert (await c.cmdj("getPlayerStatusEx"))["mode"] == "43"


async def test_switchmode_silent_noop(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", switchmode_silent_noop="optical,hdmi")
    async with Client(dev) as c:
        assert await c.cmd("setPlayerCmd:switchmode:hdmi") == (200, "OK")
        assert dev.source == "ma"
        assert await c.cmd("setPlayerCmd:switchmode:nowhere") == (200, "Failed")


# -- queue / transport commands -------------------------------------------------------


async def test_set_next_supported(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", set_next_supported=False)
    async with Client(dev) as c:
        status, out = await c.soap(
            "avt", "SetNextAVTransportURI", InstanceID=0, NextURI="http://x/", NextURIMetaData=""
        )
    assert status == 500
    assert out["errorCode"] == "401"


async def test_http_add_is_noop(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", **FAST)
    async with Client(dev) as c:
        await _start(scene, c)
        assert await c.cmd("setPlayerCmd:add:http://192.0.2.99/next.wav") == (200, "OK")
        assert dev.transport.next_slot is None
        dev.knobs.set_live("http_add_is_noop", False)
        await c.cmd("setPlayerCmd:add:http://192.0.2.99/next.wav")
        assert dev.transport.next_slot is not None


async def _gapless(scene: Scene, dev, c: Client) -> str:
    await _start(scene, c, seconds=1.0)
    t = scene.media.track(2, 3.0)
    url2 = scene.media_url(t.path)
    await c.avt("SetNextAVTransportURI", NextURI=url2, NextURIMetaData=didl("Two", uri=url2))
    return url2


async def test_advance_event_delay_s(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device("linkplay/wiim_pro", advance_event_delay_s=0.8, **FAST)
        await sink.subscribe(dev, "avt")
        async with Client(dev) as c:
            url2 = await _gapless(scene, dev, c)
            await scene.wait_for(lambda: dev.transport.tracks_started == 2, 3, "advanced")
            t_adv = time.monotonic()
            await scene.wait_for(
                lambda: any(url2 in b for b in sink.bodies("avt")), 3, "advance event"
            )
        assert time.monotonic() - t_adv >= 0.5  # lq:Q35
    finally:
        await sink.stop()


async def test_drop_advance_event(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device("linkplay/wiim_pro", drop_advance_event={"p": 1.0}, **FAST)
        await sink.subscribe(dev, "avt")
        async with Client(dev) as c:
            url2 = await _gapless(scene, dev, c)
            await scene.wait_for(lambda: dev.transport.tracks_started == 2, 3, "advanced")
            await asyncio.sleep(0.5)
            assert not any(url2 in b for b in sink.bodies("avt"))
            await c.avt("GetTransportInfo")  # any command un-mutes the transport
            await scene.wait_for(
                lambda: any(url2 in b for b in sink.bodies("avt")), 2, "late event"
            )
    finally:
        await sink.stop()


async def test_resume_701(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", resume_701={"p": 1.0}, **FAST)
    async with Client(dev) as c:
        await _start(scene, c)
        await c.avt("Pause")
        status, out = await c.soap("avt", "Play", InstanceID=0, Speed="1")
    assert status == 500
    assert out["errorCode"] == "701"
    assert dev.transport.state == TState.PAUSED


async def test_pause_in_stop_fault(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", pause_in_stop_fault=False)
    async with Client(dev) as c:
        status, _ = await c.soap("avt", "Pause", InstanceID=0)
    assert status == 200


async def test_playqueue(scene: Scene):
    hidden = await scene.device("linkplay/wiim_pro", playqueue="hidden")
    absent = await scene.device("linkplay/wiim_pro", playqueue="absent")
    async with Client(hidden) as c:
        async with c.session.get(f"{hidden.base_url}/description.xml") as r:
            assert "PlayQueue" not in await r.text()
        status, out = await c.soap("pq", "BrowseQueue", QueueName="CurrentQueue")
        assert status == 200
        assert "<PlayList>" in out["QueueContext"]
    async with Client(absent) as c:
        assert (await c.soap("pq", "BrowseQueue", QueueName="CurrentQueue"))[0] == 404


async def test_playqueue_loopmode_tag(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device("linkplay/wiim_pro", playqueue_loopmode_tag="LoopMpde")
        await sink.subscribe(dev, "pq")
        async with Client(dev) as c:
            await c.soap("pq", "SetQueueLoopMode", LoopMode=2)
            assert (await c.soap("pq", "GetQueueLoopMode"))[1]["LoopMode"] == "2"
        await scene.wait_for(lambda: len(sink.bodies("pq")) >= 2, 2, "pq event")
        assert attr(sink.bodies("pq")[-1], "LoopMpde") == "2"
    finally:
        await sink.stop()


async def test_getinfoex_supported(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", getinfoex_supported=False)
    async with Client(dev) as c:
        async with c.session.get(f"{dev.base_url}/upnp/avt/scpd.xml") as r:
            assert "GetInfoEx" not in await r.text()
        status, out = await c.soap("avt", "GetInfoEx", InstanceID=0)
    assert status == 500
    assert out["errorCode"] == "401"


# -- events ---------------------------------------------------------------------------


async def test_no_state_events(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device("linkplay/wiim_pro", no_state_events=True, **FAST)
        await sink.subscribe(dev, "avt")
        async with Client(dev) as c:
            await _start(scene, c)
            await c.avt("Pause")
        await asyncio.sleep(0.2)
        assert len(sink.bodies("avt")) == 1  # just the initial NOTIFY (LP-07)
    finally:
        await sink.stop()


async def test_echo_http_volume_events(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device("linkplay/wiim_pro", echo_http_volume_events=False)
        await sink.subscribe(dev, "rc")
        await scene.wait_for(lambda: len(sink.bodies("rc")) == 1, 2, "initial")
        async with Client(dev) as c:
            await c.cmd("setPlayerCmd:vol:61")
            await asyncio.sleep(0.2)
            assert len(sink.bodies("rc")) == 1  # L64
            await c.soap("rc", "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=62)
        await scene.wait_for(lambda: len(sink.bodies("rc")) == 2, 2, "upnp volume event")
        assert attr(sink.bodies("rc")[-1], "Volume") == "62"
    finally:
        await sink.stop()


async def test_notify_delivery(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device("linkplay/wiim_pro", notify_delivery="drop_silently")
        status, headers = await sink.subscribe(dev, "rc")
        assert status == 200
        assert headers["SID"].startswith("uuid:")
        await asyncio.sleep(0.2)
        assert sink.bodies("rc") == []  # L66
        scene.expect.eventually(kind="event", name="gena_dropped")
    finally:
        await sink.stop()


async def test_gena_timeout_s(scene: Scene):
    sink = await GenaSink().start()
    try:
        dev = await scene.device("linkplay/wiim_pro", gena_timeout_s=120)
        _, headers = await sink.subscribe(dev, "avt")
        assert headers["TIMEOUT"] == "Second-120"
    finally:
        await sink.stop()


# -- volume ---------------------------------------------------------------------------


async def test_vol_collision_window_s(scene: Scene):
    dev = await scene.device("linkplay/wiim_pro", vol_collision_window_s=0.3)
    async with Client(dev) as c:
        assert await c.cmd("setPlayerCmd:vol:40") == (200, "OK")
        assert await c.cmd("setPlayerCmd:vol:45") == (200, "OK")
    assert dev.volume == 40  # the second one was swallowed (L62)


# -- multiroom ------------------------------------------------------------------------


async def test_leader_restart_on_first_join(scene: Scene):
    lead = await scene.device("linkplay/wiim_pro", **FAST)
    fol = await scene.device("linkplay/wiim_mini")
    async with Client(lead) as c:
        await _start(scene, c, seconds=8.0)
    await asyncio.sleep(1.5)
    assert lead.transport.truth_position() > 1.0
    async with Client(fol) as c:
        assert await c.cmd(f"multiroom:JoinGroup:IP={lead.ip}:uuid={lead.uuid}") == (200, "OK")
    await scene.wait_for(lambda: fol.leader is lead, 2, "joined")
    await scene.wait_for(lambda: lead.transport.state == TState.PLAYING, 3, "replaying")
    assert lead.transport.truth_position() < 1.0  # restarted from 0 (lq:Q46)
    async with Client(lead) as c:
        slaves = await c.cmdj("multiroom:getSlaveList")
    assert slaves["slaves"] == 1
    assert slaves["slave_list"][0]["uuid"] == fol.uuid


async def test_role_propagation_delay_s(scene: Scene):
    lead = await scene.device("linkplay/wiim_pro")
    fol = await scene.device("linkplay/wiim_mini", role_propagation_delay_s=0.4)
    async with Client(fol) as c:
        await c.cmd(f"ConnectMasterAp:JoinGroupMaster:eth{lead.ip}:wifi0.0.0.0")
        assert (await c.cmdj("getStatusEx"))["group"] == "0"
        await asyncio.sleep(0.5)
        body = await c.cmdj("getStatusEx")
    assert body["group"] == "1"
    assert body["master_uuid"] == lead.uuid


async def test_follower_status(scene: Scene):
    lead = await scene.device("linkplay/wiim_pro", **FAST)
    fol = await scene.device("linkplay/wiim_mini", follower_status="mirror_leader")
    async with Client(fol) as c:
        await c.cmd(f"multiroom:JoinGroup:IP={lead.ip}:uuid={lead.uuid}")
    await scene.wait_for(lambda: fol.leader is lead, 2, "joined")
    async with Client(lead) as c:
        await _start(scene, c)
    async with Client(fol) as c:
        st = await c.cmdj("getPlayerStatusEx")
    assert (st["type"], st["status"], st["mode"]) == ("1", "play", "10")
    fol.knobs.set_live("follower_status", "stay_idle")
    async with Client(fol) as c:
        st = await c.cmdj("getPlayerStatusEx")
    assert (st["status"], st["mode"]) == ("none", "99")
