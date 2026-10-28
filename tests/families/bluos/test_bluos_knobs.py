"""One test per BluOS family knob: the quirk it models, switched on and off."""

import asyncio
import time
from pathlib import Path

from bluos_util import FAST, PROLOG, events, fired, get

from seamer.simkit import TState
from seamer.simkit.knobs import knobs_for
from seamer.simkit.scenario import Scene

N130 = "bluos/node_n130"
TRACK = [{"title": "T0", "artist": "A", "album": "B", "secs": 200}]


def test_every_family_knob_has_a_test():
    src = Path(__file__).read_text()
    names = [k for k, v in knobs_for("bluos").items() if v.family == "bluos"]
    assert len(names) >= 50
    missing = [n for n in names if f"def test_{n}(" not in src]
    assert not missing


def test_every_knob_has_quirk_ids_and_confidence():
    for name, k in knobs_for("bluos").items():
        if k.family == "bluos":
            assert k.quirks, name
            assert k.confidence in ("high", "medium", "low"), name


async def _stream(scene: Scene, dev, seconds: float = 2.0, route: str = "media", **query):
    await scene.media_server()
    t = scene.media.track(1, seconds)
    url = scene.media_url(t.path, route=route, **query)
    await get(dev, "/Play", url=url)
    return url


async def _queue(dev, tracks=TRACK):
    await dev.sim_action("load_queue", {"tracks": tracks})


# -- framing and firmware -----------------------------------------------------------


async def test_xml_prolog(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, xml_prolog=False)
    assert (await get(on, "/Status")).text.startswith(PROLOG)
    assert (await get(off, "/Status")).text.startswith("<status ")


async def test_empty_tag_style(scene: Scene):
    pair = await scene.device(N130)
    selfc = await scene.device(N130, empty_tag_style="selfclose")
    assert "<sleep></sleep>" in (await get(pair, "/Status")).body
    assert "<sleep/>" in (await get(selfc, "/Status")).body


async def test_fw_profile(scene: Scene):
    v3 = await scene.device(N130, fw_profile="bluos3")
    v418 = await scene.device(N130, fw_profile="4.18")
    a = (await get(v3, "/SyncStatus")).xml
    b = (await get(v418, "/SyncStatus")).xml
    assert (a.get("schemaVersion"), "version" in a.attrib) == ("32", False)
    assert (b.get("schemaVersion"), "version" in b.attrib) == ("36", True)


async def test_device_class(scene: Scene):
    bar = await scene.device(N130, device_class="soundbar")
    x = (await get(bar, "/SyncStatus")).xml
    assert x.get("class") == "soundbar"
    assert [c.tag for c in x] == ["zoneOptions", "pairWithSub", "bluetoothOutput", "audioPresetUrl"]


async def test_drop_model_attrs(scene: Scene):
    dev = await scene.device(N130, drop_model_attrs=True)
    x = (await get(dev, "/SyncStatus")).xml
    assert "modelName" not in x.attrib
    assert "class" not in x.attrib
    assert "version" not in x.attrib


async def test_error_http_200(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, error_http_200=False)
    assert (await get(on, "/Save")).status == 200
    r = await get(off, "/Save")
    assert (r.status, r.body) == (400, "<error>empty</error>")


async def test_longpoll(scene: Scene):
    dev = await scene.device(N130, longpoll=False)
    etag = (await get(dev, "/Status")).xml.get("etag")
    t0 = time.monotonic()
    r = await get(dev, "/Status", etag=etag, timeout=30)
    assert time.monotonic() - t0 < 1
    assert r.xml.get("etag") == etag


async def test_etag_ignores_secs(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, etag_ignores_secs=False)
    for d in (on, off):
        await _queue(d)
    a1, b1 = (await get(on, "/Status")).xml, (await get(off, "/Status")).xml
    await asyncio.sleep(1.1)
    a2, b2 = (await get(on, "/Status")).xml, (await get(off, "/Status")).xml
    assert a1.find("secs").text != a2.find("secs").text
    assert a1.get("etag") == a2.get("etag")
    assert b1.get("etag") != b2.get("etag")


async def test_longpoll_min_gap_s(scene: Scene):
    dev = await scene.device(N130, longpoll_min_gap_s=0)
    etag = (await get(dev, "/Status")).xml.get("etag")
    for _ in range(3):
        await get(dev, "/Status", etag=etag, timeout=0)
    assert not events(dev, "longpoll_rate_violation")


# -- state words and stream handling --------------------------------------------------


async def test_connecting_on_buffer_refill(scene: Scene):
    on = await scene.device(N130, **FAST)
    off = await scene.device(N130, **FAST, connecting_on_buffer_refill=False)
    for d in (on, off):
        d.audio_started = True
        d.transport.state = TState.BUFFERING
    assert on.lens.state_word() == "connecting"
    assert off.lens.state_word() == "stream"


async def test_connecting_at_tail(scene: Scene):
    dev = await scene.device(N130, **FAST, on_server_close="drain")
    await _stream(scene, dev, 3.0)
    await scene.wait_for(lambda: dev.tail, 5, "tail")
    assert dev.lens.state_word() == "connecting"
    assert dev.transport.state == TState.PLAYING
    assert fired(dev, "connecting_at_tail") == 1
    off = await scene.device(N130, **FAST, on_server_close="drain", connecting_at_tail=0.0)
    await _stream(scene, off, 3.0)
    await scene.wait_for(lambda: off._stream.get("ended_by_server"), 5, "eof")
    assert not off.tail


async def test_connect_delay_s(scene: Scene):
    dev = await scene.device(N130, connect_delay_s={"min": 0.6, "max": 0.6})
    await get(dev, "/Play", url="http://192.0.2.9/x.mp3")
    assert (await get(dev, "/Status")).field("state") == "connecting"
    await asyncio.sleep(0.9)
    assert not dev.connecting


async def test_loop_on_http_profile(scene: Scene):
    dev = await scene.device(N130, **FAST, loop_on_http_profile={"p": 1.0, "max_loops": 1})
    await _stream(scene, dev, 1.0, route="stream", rate=0)
    await scene.wait_for(lambda: events(dev, "stream_loop"), 10, "loop")
    assert events(dev, "stream_loop")[0]["loop"] == 1
    off = await scene.device(N130, **FAST, loop_on_http_profile=0.0)
    await _stream(scene, off, 1.0, route="stream", rate=0)
    await scene.wait_for(lambda: off.transport.state == TState.STOPPED, 10, "stopped")
    assert not events(off, "stream_loop")


async def test_pause_drops_quality(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, pause_drops_quality=False)
    for d in (on, off):
        await _queue(d)
        await get(d, "/Pause")
    assert (await get(on, "/Status")).field("quality") is None
    assert (await get(off, "/Status")).field("quality") == "cd"
    assert (await get(on, "/Status")).field("title1") == "T0"


async def test_stop_keeps_metadata(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, stop_keeps_metadata=False)
    for d in (on, off):
        await _queue(d)
        await get(d, "/Stop")
    a, b = await get(on, "/Status"), await get(off, "/Status")
    assert (a.field("title1"), a.field("secs")) == ("T0", "0")
    assert b.field("title1") is None


async def test_fresh_stop_bare(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, fresh_stop_bare=False)
    for d in (on, off):
        d.item = {"title1": "Left over", "service": "TuneIn"}
    assert (await get(on, "/Status")).field("title1") is None
    assert (await get(off, "/Status")).field("title1") == "Left over"


async def test_metadata_style(scene: Scene):
    dev = await scene.device(N130, metadata_style="named", connect_delay_s={"min": 5, "max": 5})
    await get(dev, "/Play", url="http://192.0.2.9/x.mp3", title="Radio X")
    assert (await get(dev, "/Status")).field("name") == "Radio X"


async def test_quality_mode(scene: Scene):
    dev = await scene.device(N130, quality_mode="bps", connect_delay_s={"min": 5, "max": 5})
    await get(dev, "/Play", url="http://192.0.2.9/x.mp3")
    assert (await get(dev, "/Status")).field("quality") == "320000"


async def test_extra_status_fields(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, extra_status_fields=False)
    for d in (on, off):
        await _queue(d)
    a, b = await get(on, "/Status"), await get(off, "/Status")
    assert (a.field("canMovePlayback"), a.field("serviceName")) == ("true", "Library")
    assert (b.field("canMovePlayback"), b.field("serviceName")) == (None, None)


async def test_inject_state(scene: Scene):
    dev = await scene.device(N130, inject_state={"p": 1.0, "value": "unknown"})
    r1 = await get(dev, "/Status")
    assert r1.field("state") == "unknown"
    r2 = await get(dev, "/Status", etag=r1.xml.get("etag"), timeout=0)
    assert r2.xml.get("etag") == r1.xml.get("etag")  # etag ignores the injected word
    assert fired(dev, "inject_state") >= 2
    assert dev.lens.state_word() == "stop"


async def test_indexing(scene: Scene):
    dev = await scene.device(N130, indexing=True)
    assert (await get(dev, "/Status")).field("indexing") == "1"


async def test_status_fail(scene: Scene):
    dev = await scene.device(N130, status_fail={"p": 1.0, "status": 503})
    assert (await get(dev, "/Status")).status == 503
    assert (await get(dev, "/SyncStatus")).status == 200
    hang = await scene.device(N130, status_fail={"p": 1.0, "mode": "timeout", "hold_s": 0.5})
    t0 = time.monotonic()
    assert (await get(hang, "/Status")).status == 503
    assert time.monotonic() - t0 >= 0.45
    assert fired(hang, "status_fail") == 1


# -- volume ---------------------------------------------------------------------------


async def test_volume_db_range(scene: Scene):
    dev = await scene.device(N130, volume_db_range={"lo": -60.0, "hi": -10.0})
    assert (await get(dev, "/Volume", level=50)).xml.get("db") == "-35"


async def test_volume_limits_db(scene: Scene):
    dev = await scene.device(N130, volume_limits_db={"max": -40.0})
    r = await get(dev, "/Volume", level=90)
    assert (r.xml.text, r.xml.get("db")) == ("50", "-40")
    assert events(dev, "volume_clamped")[0]["requested"] == 90


async def test_fixed_volume(scene: Scene):
    dev = await scene.device(N130, fixed_volume=True)
    r = await get(dev, "/Volume", level=70)
    assert r.xml.text == "-1"
    assert dev.volume == 30
    assert (await get(dev, "/Status")).field("volume") == "-1"
    assert fired(dev, "fixed_volume") == 1


async def test_volume_step_stuck(scene: Scene):
    dev = await scene.device(N130, volume_step_stuck=True)
    await get(dev, "/Volume", level=50)
    r = await get(dev, "/Volume", level=51)  # -39.2 dB rounds down to -40
    assert r.xml.text == "50"
    assert fired(dev, "volume_step_stuck") == 2


async def test_tell_slaves_default(scene: Scene):
    a = await scene.device(N130, **FAST)
    b = await scene.device(N130, **FAST)
    await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    await get(a, "/Volume", level=60)
    assert b.volume == 30
    await get(a, "/Volume", level=62, tell_slaves=1)
    assert b.volume == 62
    c = await scene.device(N130, **FAST, tell_slaves_default=True)
    d = await scene.device(N130, **FAST)
    await get(c, "/AddSlave", slave="127.0.0.1", port=d.port)
    await get(c, "/Volume", level=70)
    assert d.volume == 70


async def test_volume_source_attr(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, volume_source_attr=False)
    assert "source" in (await get(on, "/Volume")).xml.attrib
    assert "source" not in (await get(off, "/Volume")).xml.attrib


async def test_mute_param_inverted(scene: Scene):
    dev = await scene.device(N130, mute_param_inverted=True)
    assert (await get(dev, "/Volume", mute=0)).xml.get("mute") == "1"
    assert fired(dev, "mute_param_inverted") == 1


# -- grouping -------------------------------------------------------------------------


async def _pair(scene: Scene, **knobs):
    a = await scene.device(N130, **FAST, **knobs)
    b = await scene.device("bluos/powernode", **FAST, **knobs)
    await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    return a, b


async def test_follower_status_mirror(scene: Scene):
    a, b = await _pair(scene, follower_status_mirror=False)
    await get(a, "/Volume", level=70)
    assert (await get(b, "/Status")).field("volume") == "30"


async def test_follower_proxies_commands(scene: Scene):
    a, b = await _pair(scene, follower_proxies_commands=False)
    await _queue(a)
    await get(b, "/Pause")
    assert a.lens.truth_word() == "play"
    a2, b2 = await _pair(scene)
    await _queue(a2)
    await get(b2, "/Pause")
    assert a2.lens.truth_word() == "pause"
    assert fired(b2, "follower_proxies_commands") == 1


async def test_group_name_style(scene: Scene):
    a, _ = await _pair(scene, group_name_style="names")
    assert (await get(a, "/SyncStatus")).xml.get("group") == "Living Room Node+Kitchen Powernode"


async def test_addslave_on_follower(scene: Scene):
    _, b = await _pair(scene)
    c = await scene.device(N130, **FAST)
    assert (await get(b, "/AddSlave", slave="127.0.0.1", port=c.port)).body == (
        "<addSlave></addSlave>"
    )
    assert c.leader is None
    a2, b2 = await _pair(scene, addslave_on_follower="proxy")
    c2 = await scene.device(N130, **FAST)
    await get(b2, "/AddSlave", slave="127.0.0.1", port=c2.port)
    assert c2.leader is a2


async def test_addslave_nest_leaders(scene: Scene):
    b, c = await _pair(scene)
    a = await scene.device(N130, **FAST)
    await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    assert (b.leader, c.leader) == (a, b)  # three levels
    y, z = await _pair(scene, addslave_nest_leaders=False)
    x = await scene.device(N130, **FAST, addslave_nest_leaders=False)
    await get(x, "/AddSlave", slave="127.0.0.1", port=y.port)
    assert (y.leader, z.leader) == (x, None)


async def test_addslave_reorder(scene: Scene):
    a = await scene.device(N130, **FAST, addslave_reorder=True)
    b = await scene.device(N130, **FAST)
    c = await scene.device(N130, **FAST)
    r = await get(a, "/AddSlave", slaves="127.0.0.1,127.0.0.1", ports=f"{b.port},{c.port}")
    assert [s.get("port") for s in r.xml] == [str(c.port), str(b.port)]


async def test_addslave_multi_broken(scene: Scene):
    a = await scene.device(N130, **FAST, addslave_multi_broken=True)
    b = await scene.device(N130, **FAST)
    r = await get(a, "/AddSlave", slaves="127.0.0.1", ports=str(b.port))
    assert r.xml.find("slave").get("port") == str(b.port)
    assert b.leader is None


async def test_staged_sync_reads(scene: Scene):
    a = await scene.device(N130, **FAST, staged_sync_reads=2)
    b = await scene.device(N130, **FAST)
    await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    seen = [(await get(a, "/SyncStatus")).xml.find("slave") is not None for _ in range(3)]
    assert seen == [False, False, True]


async def test_syncstat_step(scene: Scene):
    dev = await scene.device(N130, syncstat_step={"min": 10, "max": 10})
    s0 = dev.sync_stat
    await get(dev, "/Volume", level=50)
    assert dev.sync_stat == s0 + 10


async def test_leader_leave_on_input(scene: Scene):
    a, _ = await _pair(scene)
    await a.sim_action("select_input", {"id": "input1"})
    r = await get(a, "/RemoveSlave", slave="127.0.0.1", port=a.port)
    assert r.body == "<error>Cannot move input source</error>"
    a2, b2 = await _pair(scene, leader_leave_on_input="allow")
    await a2.sim_action("select_input", {"id": "input1"})
    await get(a2, "/RemoveSlave", slave="127.0.0.1", port=a2.port)
    assert b2.leader is None


async def _trio(scene: Scene, **knobs):
    a = await scene.device(N130, **FAST, **knobs)
    b = await scene.device(N130, **FAST)
    c = await scene.device(N130, **FAST)
    await get(a, "/AddSlave", slaves="127.0.0.1,127.0.0.1", ports=f"{b.port},{c.port}")
    await get(a, "/RemoveSlave", slave="127.0.0.1", port=a.port)
    return a, b, c


async def test_handover_pick(scene: Scene):
    _, b, c = await _trio(scene, handover_pick="first")
    assert (b.leader, c.leader) == (None, b)


async def test_regroup_on_primary_remove(scene: Scene):
    _, b, c = await _trio(scene, regroup_on_primary_remove=False)
    assert (b.leader, c.leader, b.followers, c.followers) == (None, None, [], [])


async def test_wedged_pause_state(scene: Scene):
    dev = await scene.device(N130, **FAST, wedged_pause_state=1.0)
    await _queue(dev)
    await get(dev, "/Pause")
    dev.transport.take_over("airplay", title="From phone")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 3, "playing")
    if dev.transport.state != TState.PLAYING:
        await dev.transport.play()
    assert dev.wedged
    assert (await get(dev, "/Status")).field("state") == "pause"
    assert (await get(dev, "/Pause")).body == "<state>pause</state>"
    assert dev.transport.state == TState.PLAYING
    await get(dev, "/Pause", toggle=1)
    assert (await get(dev, "/Status")).field("state") == "stream"


# -- inputs, presets, queue -----------------------------------------------------------


async def test_inputs_missing_attr(scene: Scene):
    dev = await scene.device(N130, inputs_missing_attr="image")
    items = (await get(dev, "/RadioBrowse", service="Capture")).xml.findall("item")
    assert all("image" not in i.attrib and "text" in i.attrib for i in items)
    assert events(dev, "inputs_missing_attr")[0]["attr"] == "image"


async def test_input_secs_origin(scene: Scene):
    dev = await scene.device(N130, input_secs_origin=0.0)
    await dev.sim_action("select_input", {"id": "input1"})
    assert int((await get(dev, "/Status")).field("secs")) < 5


async def test_settings_group_mismatch(scene: Scene):
    dev = await scene.device("bluos/powernode", settings_group_mismatch=True)
    await dev.sim_action("select_input", {"id": "input5"})
    st = await get(dev, "/Status")
    assert (st.field("inputId"), st.field("settingsGroupId")) == ("input5", "capture-input2")


async def test_service_without_input_id(scene: Scene):
    dev = await scene.device(N130, service_without_input_id=False)
    await _queue(dev)
    assert (await get(dev, "/Status")).field("inputId") == "LocalMusic"


async def test_preset_url_ignores_volume(scene: Scene):
    dev = await scene.device(N130)
    await get(dev, "/Play", url="TuneIn:s1002")
    assert dev.volume == 30
    assert fired(dev, "preset_url_ignores_volume") == 1
    off = await scene.device(N130, preset_url_ignores_volume=False)
    await get(off, "/Play", url="TuneIn:s1002")
    assert off.volume == 20


async def test_playlist_len_style(scene: Scene):
    dev = await scene.device(N130, playlist_len_style="children")
    await _queue(dev)
    x = (await get(dev, "/Playlist", length=1)).xml
    assert x.find("length").text == "1"
    assert not x.attrib


async def test_pause_toggle_param(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, pause_toggle_param="ignore")
    for d in (on, off):
        await _queue(d)
        await get(d, "/Pause")
        await get(d, "/Pause", toggle=1)
    assert on.lens.truth_word() == "play"
    assert off.lens.truth_word() == "pause"


async def test_legacy_browse_400(scene: Scene):
    on = await scene.device(N130)
    off = await scene.device(N130, legacy_browse_400=False)
    r = await get(on, "/Browse", key="/Albums?service=LocalMusic")
    assert (r.status, r.text) == (400, "")
    assert (await get(off, "/Browse", key="/Albums?service=LocalMusic")).status == 200


# -- discovery, zones, reboot, connection ---------------------------------------------


async def test_mdns_omit_mac(scene: Scene):
    dev = await scene.device(N130, mdns_omit_mac=True)
    assert "mac" not in dev.mdns_txt()
    assert fired(dev, "mdns_omit_mac") >= 1


async def test_mdns_object_type(scene: Scene):
    dev = await scene.device(N130, mdns_object_type="")
    assert "_objectType" not in dev.mdns_txt()


async def test_ipv6_only_announce(scene: Scene):
    dev = await scene.device(N130, ipv6_only_announce=True)
    assert dev.mdns_addresses() == ["127.0.0.1"]
    addrs = await dev.sim_action("network_blip", {})
    assert len(addrs) == 1
    assert ":" in addrs[0]
    assert addrs[0].lower().startswith("fd")


async def test_zones(scene: Scene):
    dev = await scene.device(N130, zones=3)
    assert len(dev.chassis) == 3
    z2 = dev.zone_children[0]
    assert z2.port == dev.port + 10
    assert z2.mac == dev.mac
    assert z2.mdns_service_type() == "_musp._tcp.local."
    assert (await get(z2, "/SyncStatus")).xml.get("name") == "Living Room Node 2"


async def test_chassis_reboot_all(scene: Scene):
    dev = await scene.device(N130, zones=2, reboot_downtime_s=0.3)
    await get(dev, "/reboot")
    await scene.wait_for(lambda: dev.zone_children[0]._down, 2, "zone down")
    await scene.wait_for(lambda: not dev.zone_children[0]._down, 5, "zone up")
    solo = await scene.device(N130, zones=2, reboot_downtime_s=0.3, chassis_reboot_all=False)
    await get(solo, "/reboot")
    await scene.wait_for(lambda: solo._down, 2, "down")
    assert not solo.zone_children[0]._down


async def test_reboot_downtime_s(scene: Scene):
    dev = await scene.device(N130, reboot_downtime_s=0.5)
    t0 = time.monotonic()
    await dev.sim_action("reboot", {})
    await scene.wait_for(lambda: dev._down, 2, "down")
    await scene.wait_for(lambda: not dev._down, 5, "up")
    assert time.monotonic() - t0 >= 0.5
    assert events(dev, "reboot")[0]["downtime_s"] == 0.5


async def test_serialized_connection(scene: Scene):
    dev = await scene.device(N130, serialized_connection=True)
    etag = (await get(dev, "/Status")).xml.get("etag")
    held = asyncio.ensure_future(get(dev, "/Status", etag=etag, timeout=1))
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    await get(dev, "/SyncStatus")
    assert time.monotonic() - t0 >= 0.6
    await held
    assert events(dev, "serialized_wait")
