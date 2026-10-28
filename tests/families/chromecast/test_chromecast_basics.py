import asyncio

import aiohttp
import pytest

from seamer.families import get_family
from seamer.families.chromecast import protocol as p
from seamer.simkit.scenario import Scene

from .helpers import CastClient, media_item

PROFILES = ["nest_mini", "google_home_speaker", "chromecast_google_tv", "cast_group", "lg_s95tr"]


def test_alias_and_profiles():
    fam = get_family("cast")
    assert fam.name == "chromecast"
    assert set(fam.profiles()) == set(PROFILES)
    assert fam.default_port == 8008


def test_frame_round_trip():
    msg = p.json_message("sender-0", "receiver-0", p.NS_RECEIVER, {"type": "GET_STATUS"})
    raw = p.frame(msg)
    assert int.from_bytes(raw[:4], "big") == len(raw) - 4
    back = p.decode(raw[4:])
    assert back == msg
    assert back.json() == {"type": "GET_STATUS"}
    binary = p.CastMessage("a", "b", p.NS_DEVICEAUTH, payload_binary=b"\x00\x01")
    assert p.decode(p.encode(binary)).payload_binary == b"\x00\x01"


def test_frame_matches_protobuf_if_available():
    pb = pytest.importorskip("pychromecast.generated.cast_channel_pb2")
    msg = p.json_message("sender-0", "receiver-0", p.NS_MEDIA, {"type": "LOAD", "requestId": 7})
    ref = pb.CastMessage()
    ref.ParseFromString(p.encode(msg))
    assert ref.namespace == p.NS_MEDIA
    assert ref.payload_utf8 == msg.payload_utf8
    assert p.decode(ref.SerializeToString()) == msg


@pytest.mark.parametrize("profile", PROFILES)
async def test_every_profile_answers_status_and_eureka(scene: Scene, profile: str):
    dev = await scene.device(f"chromecast/{profile}")
    async with await CastClient.connect(dev) as c:
        st = await c.receiver("GET_STATUS")
        assert st["type"] == "RECEIVER_STATUS"
        vol = st["status"]["volume"]
        assert set(vol) == {"controlType", "level", "muted", "stepInterval"}
        video = dev.cast_type == "cast"
        assert ("isActiveInput" in st["status"]) == video
        assert ("applications" in st["status"]) == video  # Backdrop on video devices
        pong = None
        await c.send(p.NS_HEARTBEAT, {"type": "PING"})
        await c.wait(lambda: c.messages("PONG"))
        pong = c.messages("PONG")[0]
        assert pong == {"type": "PONG"}
    async with aiohttp.ClientSession() as s:
        url = f"{dev.base_url}/setup/eureka_info?params=name,device_info&options=detail"
        async with s.get(url) as r:
            body = await r.json()
    assert body["name"] == dev.friendly_name
    assert body["device_info"]["ssdp_udn"].replace("-", "") == dev.uuid_hex
    assert body["mac_address"].startswith("02:00:")


async def test_play_to_end_reports_finished(scene: Scene):
    dev = await scene.device("chromecast/nest_mini")
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 1.0).path)
    async with await CastClient.connect(dev) as c:
        tid = await c.launch()
        assert tid.startswith("web-")
        st = await c.media("LOAD", media=media_item(url), autoplay=True, currentTime=0)
        assert st["type"] == "MEDIA_STATUS"
        entry = st["status"][0]
        assert entry["playerState"] == "BUFFERING"
        assert entry["supportedMediaCommands"] == 12303
        assert entry["media"]["contentId"] == url
        await c.wait(lambda: ("PLAYING", None) in c.media_states())
        await c.wait(lambda: ("IDLE", "FINISHED") in c.media_states(), timeout=6)
        idle = c.messages("MEDIA_STATUS")[-1]
        assert idle["requestId"] == 0
        assert set(idle["status"][0]) == {
            "mediaSessionId",
            "playerState",
            "idleReason",
            "currentTime",
        }
    assert dev.sink.bytes_read > 0


async def test_queue_advances_and_pause_seek_volume(scene: Scene):
    dev = await scene.device("chromecast/nest_mini")
    await scene.media_server()
    urls = [scene.media_url(scene.media.track(i, 0.6).path) for i in (1, 2)]
    async with await CastClient.connect(dev) as c:
        await c.launch(p.APP_MA)
        st = await c.media(
            "QUEUE_LOAD", items=[{"media": media_item(u), "autoplay": True} for u in urls]
        )
        first = st["status"][0]
        assert [i["orderId"] for i in first["items"]] == [0, 1]
        sid = first["mediaSessionId"]
        await c.wait(
            lambda: any(
                e.get("currentItemId") == first["items"][1]["itemId"]
                for d in c.messages("MEDIA_STATUS")
                for e in d["status"]
            ),
            timeout=5,
        )
        await c.wait(lambda: ("IDLE", "FINISHED") in c.media_states(), timeout=5)
        bad = await c.media("PAUSE", mediaSessionId=sid + 5)
        assert bad == {
            "type": "INVALID_REQUEST",
            "requestId": bad["requestId"],
            "reason": "INVALID_MEDIA_SESSION_ID",
        }
        rs = await c.receiver("SET_VOLUME", volume={"level": 0.62})
        assert rs["status"]["volume"]["level"] == 0.62
        assert dev.status()["volume"]["level"] == 0.62


async def test_load_while_playing_interrupts_old_session(scene: Scene):
    dev = await scene.device("chromecast/nest_mini")
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 5.0).path)
    async with await CastClient.connect(dev) as c:
        await c.launch()
        a = await c.media("LOAD", media=media_item(url))
        await c.wait(lambda: ("PLAYING", None) in c.media_states())
        b = await c.media("LOAD", media=media_item(url))
        assert b["status"][0]["mediaSessionId"] == a["status"][0]["mediaSessionId"] + 1
        assert ("IDLE", "INTERRUPTED") in c.media_states()
        stop = await c.media("STOP", mediaSessionId=b["status"][0]["mediaSessionId"])
        assert stop["status"][0]["idleReason"] == "CANCELLED"


async def test_launch_unknown_app_and_receiver_stop(scene: Scene):
    dev = await scene.device("chromecast/nest_mini")
    async with await CastClient.connect(dev) as c:
        err = await c.receiver("LAUNCH", appId="DEADBEEF")
        assert err == {"type": "LAUNCH_ERROR", "requestId": err["requestId"], "reason": "NOT_FOUND"}
        tid = await c.launch()
        st = await c.receiver("STOP")
        assert "applications" not in st["status"]
        await c.wait(
            lambda: any(
                n == p.NS_CONNECTION and s == tid and d.get("type") == "CLOSE"
                for n, s, d in c.inbox
            )
        )


async def test_multizone_status_for_group(scene: Scene):
    dev = await scene.device("chromecast/cast_group")
    async with await CastClient.connect(dev) as c:
        st = await c.request(p.NS_MULTIZONE, {"type": "GET_STATUS"})
        assert st["type"] == "MULTIZONE_STATUS"
        assert len(st["status"]["devices"]) == 2
        assert st["status"]["playbackSession"]["appAllowsGrouping"] is True
    assert dev.status()["cast_port"] == dev.cast.port


async def test_sim_state_route(scene: Scene):
    dev = await scene.device("chromecast/lg_s95tr")
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/_sim/state") as r:
        body = await r.json()
    assert "cast_port" in str(body)
    await asyncio.sleep(0)
