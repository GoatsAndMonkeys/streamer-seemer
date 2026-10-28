"""BluOS long-poll, etags and grouping (leader/follower mirror, handover)."""

import asyncio
import time

from bluos_util import FAST, events, get

from seamer.simkit.scenario import Scene


async def test_status_longpoll_holds_until_change(scene: Scene):
    dev = await scene.device("bluos/node_n130", **FAST)
    etag = (await get(dev, "/Status")).xml.get("etag")
    t0 = time.monotonic()
    held = asyncio.ensure_future(get(dev, "/Status", etag=etag, timeout=30))
    await asyncio.sleep(0.4)
    assert not held.done()
    await get(dev, "/Volume", level=44)
    r = await asyncio.wait_for(held, 5)
    assert time.monotonic() - t0 < 3
    assert r.xml.get("etag") != etag
    assert r.field("volume") == "44"


async def test_status_longpoll_times_out_with_same_body(scene: Scene):
    dev = await scene.device("bluos/node_n130", **FAST)
    first = await get(dev, "/Status")
    t0 = time.monotonic()
    r = await get(dev, "/Status", etag=first.xml.get("etag"), timeout=1)
    assert 0.9 <= time.monotonic() - t0 < 3
    assert r.status == 200
    assert r.xml.get("etag") == first.xml.get("etag")


async def test_syncstatus_longpoll_wakes_on_rename(scene: Scene):
    dev = await scene.device("bluos/node_n130", **FAST)
    s = (await get(dev, "/SyncStatus")).xml.get("etag")
    held = asyncio.ensure_future(get(dev, "/SyncStatus", etag=s, timeout=30))
    await asyncio.sleep(0.3)
    assert not held.done()
    await dev.sim_action("set_name", {"name": "Den"})
    r = await asyncio.wait_for(held, 5)
    assert r.xml.get("name") == "Den"
    assert int(r.xml.get("syncStat")) > int(s)


async def test_longpoll_rate_violation_recorded(scene: Scene):
    dev = await scene.device("bluos/node_n130", **FAST)
    etag = (await get(dev, "/Status")).xml.get("etag")
    await get(dev, "/Status", etag=etag, timeout=0)
    await get(dev, "/Status", etag=etag, timeout=0)
    viol = events(dev, "longpoll_rate_violation")
    assert viol
    assert viol[0]["resource"] == "status"


async def test_group_add_mirror_and_remove(scene: Scene):
    a = await scene.device("bluos/node_n130", **FAST, staged_sync_reads=0)
    b = await scene.device("bluos/powernode", **FAST)
    r = await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    assert r.body == f'<addSlave><slave id="127.0.0.1" port="{b.port}"></slave></addSlave>'
    sa = (await get(a, "/SyncStatus")).xml
    assert sa.get("group") == "Living Room Node + 1"
    slave = sa.find("slave")
    assert (slave.get("id"), slave.get("port"), slave.get("model")) == (
        "127.0.0.1",
        str(b.port),
        "N330",
    )
    sb = (await get(b, "/SyncStatus")).xml
    assert sb.find("master").text == "127.0.0.1"
    assert sb.find("master").get("port") == str(a.port)
    await get(a, "/Volume", level=61)
    sta, stb = await get(a, "/Status"), await get(b, "/Status")
    assert stb.body == sta.body  # follower mirrors its leader (bl2:B29)
    assert sta.field("groupName") == "Living Room Node + 1"
    r = await get(a, "/RemoveSlave", slave="127.0.0.1", port=b.port)
    assert r.xml.tag == "SyncStatus"
    assert r.xml.find("slave") is None
    assert b.leader is None


async def test_follower_transport_goes_to_leader(scene: Scene):
    a = await scene.device("bluos/node_n130", **FAST)
    b = await scene.device("bluos/node_n130", **FAST)
    tracks = [{"title": "T", "artist": "A", "album": "B", "secs": 100}]
    await dev_queue(a, tracks)
    await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    assert (await get(b, "/Pause")).body == "<state>pause</state>"
    assert a.lens.truth_word() == "pause"


async def dev_queue(dev, tracks):
    await dev.sim_action("load_queue", {"tracks": tracks})


async def test_handover_in_three_player_group(scene: Scene):
    a = await scene.device("bluos/node_n130", **FAST)
    b = await scene.device("bluos/powernode", **FAST)
    c = await scene.device("bluos/pulse_flex_2i", **FAST)
    await dev_queue(a, [{"title": "T", "artist": "A", "album": "B", "secs": 100}])
    await get(a, "/AddSlave", slaves="127.0.0.1,127.0.0.1", ports=f"{b.port},{c.port}")
    assert a.followers == [b, c]
    await get(a, "/RemoveSlave", slave="127.0.0.1", port=a.port)
    assert c.leader is None
    assert c.followers == [b]
    assert b.leader is c
    assert c.lens.truth_word() == "play"  # the queue moves with it
    assert a.lens.truth_word() == "stop"
    assert events(a, "group_handover")[0]["new"] == c.id


async def test_follower_leaving_stops(scene: Scene):
    a = await scene.device("bluos/node_n130", **FAST)
    b = await scene.device("bluos/node_n130", **FAST)
    await dev_queue(a, [{"title": "T", "artist": "A", "album": "B", "secs": 100}])
    await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    await get(a, "/RemoveSlave", slave="127.0.0.1", port=b.port)
    assert (await get(b, "/Status")).field("state") == "stop"
    assert (await get(a, "/Status")).field("state") == "play"
