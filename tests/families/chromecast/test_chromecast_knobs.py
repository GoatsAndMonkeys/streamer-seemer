import asyncio

import aiohttp
import pytest

from seamer.families import build_device
from seamer.families.chromecast import protocol as p
from seamer.simkit.clock import VirtualClock
from seamer.simkit.recorder import Recorder
from seamer.simkit.scenario import Scene

from .helpers import CastClient, media_item


def fired(scene: Scene, knob: str) -> list:
    return list(scene.rec.timeline().where(kind="knob", name="fired", knob=knob))


def entries(c: CastClient) -> list[dict]:
    return [e for d in c.messages("MEDIA_STATUS") for e in d["status"]]


async def _play(c: CastClient, urls: list[str], ctype: str = "audio/wav", app: str = p.APP_MA):
    await c.launch(app)
    items = [{"media": media_item(u, ctype, f"T{n}"), "autoplay": True} for n, u in enumerate(urls)]
    return await c.media("QUEUE_LOAD", items=items)


# -- HTTP fetch quirks -----------------------------------------------------------------


async def test_content_length_cutoff_finishes_item_and_fetches_next(scene: Scene):
    """C9/C33: with a Content-Length the device stops at N bytes, no error, next item."""
    dev = await scene.device("chromecast/google_home_speaker", content_length_cutoff_bytes=60000)
    ms = await scene.media_server()
    urls = [scene.media_url(scene.media.track(i, 2.0).path) for i in (1, 2)]
    async with await CastClient.connect(dev) as c:
        st = await _play(c, urls, app=p.APP_MEDIA_RECEIVER)  # C35: no MA receiver here
        second = st["status"][0]["items"][1]["itemId"]
        await c.wait(lambda: any(e.get("currentItemId") == second for e in entries(c)), 5)
        assert not c.messages("ERROR")
    hits = fired(scene, "content_length_cutoff_bytes")
    assert hits
    assert hits[0].get("bytes") == 60000
    assert hits[0].get("quirks") == ["C9", "C33"]
    assert len([r for r in ms.requests if r["path"].endswith(urls[1].rsplit("/", 1)[1])]) >= 1


async def test_cutoff_ignores_chunked_and_time_cap_applies_without_length(scene: Scene):
    """C10: no Content-Length -> the time cap applies instead of the byte cutoff."""
    dev = await scene.device(
        "chromecast/nest_mini",
        content_length_cutoff_bytes=60000,
        no_content_length_stop_after_s=0.5,
    )
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 3.0).path, "stream", rate=0, chunked=1)
    async with await CastClient.connect(dev) as c:
        await _play(c, [url])
        await c.wait(lambda: ("IDLE", "FINISHED") in c.media_states(), 5)
    assert fired(scene, "no_content_length_stop_after_s")
    assert not fired(scene, "content_length_cutoff_bytes")


async def test_stay_buffering_forever_at_end(scene: Scene):
    """C1/C42: groups never report IDLE at the end of the flow."""
    dev = await scene.device("chromecast/cast_group", clip_tail_cut_ms=0)
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 0.5).path)
    async with await CastClient.connect(dev) as c:
        await _play(c, [url])
        await c.wait(lambda: fired(scene, "idle_on_eof"), 5)
        await asyncio.sleep(0.3)
        st = await c.media("GET_STATUS")
        assert st["status"][0]["playerState"] == "BUFFERING"
        assert all(s != "IDLE" for s, _r in c.media_states())


async def test_clip_tail_cut_stops_before_the_end(scene: Scene):
    """C28/#6695: the last N ms of the final item are never heard."""
    dev = await scene.device("chromecast/cast_group", idle_on_eof="FINISHED", clip_tail_cut_ms=600)
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 1.5).path)
    async with await CastClient.connect(dev) as c:
        await _play(c, [url])
        await c.wait(lambda: ("IDLE", "FINISHED") in c.media_states(), 5)
    assert fired(scene, "clip_tail_cut_ms")
    heard = scene.rec.timeline().last(kind="sink", name="heard")
    assert heard is not None
    assert heard.get("played_s") < 1.1


async def test_strict_content_type_fails_the_item(scene: Scene):
    """C12: vendor stacks reject a Content-Type that differs from the LOAD contentType."""
    dev = await scene.device("chromecast/lg_s95tr")
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 2.0).path)
    async with await CastClient.connect(dev) as c:
        await _play(c, [url], ctype="audio/mpeg", app=p.APP_MEDIA_RECEIVER)
        await c.wait(lambda: c.messages("LOAD_FAILED"), 5)
        lf = c.messages("LOAD_FAILED")[0]
        assert set(lf) == {"type", "requestId", "itemId", "detailedErrorCode"}
        assert lf["detailedErrorCode"] == 104
        await c.wait(lambda: ("IDLE", "ERROR") in c.media_states(), 3)
    assert fired(scene, "strict_content_type")


async def test_public_dns_only_and_reject_http(scene: Scene):
    """C13: .local names fail; C36: http contentId silently plays nothing."""
    dev = await scene.device("chromecast/nest_mini", public_dns_only=True)
    async with await CastClient.connect(dev) as c:
        await c.launch()
        await c.media("LOAD", media=media_item("http://ma.local:8097/flow/x.mp3"))
        await c.wait(lambda: c.messages("LOAD_FAILED"))
        assert fired(scene, "public_dns_only")
    dev2 = await scene.device("chromecast/nest_mini", reject_http_media="silent")
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 1.0).path)
    async with await CastClient.connect(dev2) as c:
        await c.launch()
        st = await c.media("LOAD", media=media_item(url))
        assert st["status"][0]["playerState"] == "BUFFERING"
        await asyncio.sleep(0.3)
        assert dev2.sink.bytes_read == 0
        assert not c.messages("LOAD_FAILED")


@pytest.mark.parametrize(("route", "expect_error"), [("media", False), ("stream", True)])
async def test_range_rerequest(scene: Scene, route: str, expect_error: bool):
    """X5: past N bytes the device asks again with Range; a 200 answer drops the item."""
    dev = await scene.device(
        "chromecast/lg_s95tr", range_rerequest_after_bytes=50000, strict_content_type=False
    )
    ms = await scene.media_server()
    query = {"rate": 1, "chunked": 0} if route == "stream" else {}
    url = scene.media_url(scene.media.track(1, 3.0).path, route, **query)
    async with await CastClient.connect(dev) as c:
        await _play(c, [url], app=p.APP_MEDIA_RECEIVER)
        await c.wait(lambda: fired(scene, "range_rerequest_after_bytes"), 5)
        hit = fired(scene, "range_rerequest_after_bytes")[0]
        assert any((r.get("range") or "").startswith("bytes=") for r in ms.requests)
        if expect_error:
            assert hit.get("status") == 200
            await c.wait(lambda: ("IDLE", "ERROR") in c.media_states(), 3)
        else:
            assert hit.get("status") == 206
            await asyncio.sleep(0.2)
            assert ("IDLE", "ERROR") not in c.media_states()


# -- media session -------------------------------------------------------------------------


async def test_spontaneous_error_sequence(scene: Scene):
    """C30: ERROR 102 twice ~30 ms apart, IDLE/ERROR + extendedStatus, then the next item."""
    dev = await scene.device("chromecast/google_home_speaker")
    await scene.media_server()
    urls = [scene.media_url(scene.media.track(i, 3.0).path) for i in (1, 2)]
    async with await CastClient.connect(dev) as c:
        st = await _play(c, urls, app=p.APP_MEDIA_RECEIVER)
        nxt = st["status"][0]["items"][1]["itemId"]
        await c.wait(lambda: ("PLAYING", None) in c.media_states())
        assert await dev.sim_action("spontaneous_error", {}) is True
        errs = c.messages("ERROR")
        assert len(errs) == 2
        assert errs[0] == {"type": "ERROR", "requestId": 0, "detailedErrorCode": 102, "itemId": 1}
        idle = next(e for e in entries(c) if e["playerState"] == "IDLE")
        assert idle["idleReason"] == "ERROR"
        assert idle["extendedStatus"]["playerState"] == "LOADING"
        assert idle["extendedStatus"]["loadingItemId"] == nxt
        await c.wait(
            lambda: any(
                e.get("currentItemId") == nxt and e["playerState"] == "PLAYING" for e in entries(c)
            ),
            3,
        )
    ev = fired(scene, "spontaneous_error")[0]
    assert ev.get("quirks") == ["C30", "C46", "CC-04"]
    assert ev.get("confidence") == "high"


async def test_app_error_and_sparse_status_and_omit_idle_reason(scene: Scene):
    dev = await scene.device(
        "chromecast/nest_mini",
        app_error_rate={"p": 1.0, "on": ["SEEK"]},
        omit_idle_reason=True,
        status_sparse_media=True,
    )
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 0.6).path)
    async with await CastClient.connect(dev) as c:
        st = await _play(c, [url], app=p.APP_MEDIA_RECEIVER)
        assert "media" in st["status"][0]
        sid = st["status"][0]["mediaSessionId"]
        err = await c.media("SEEK", mediaSessionId=sid, currentTime=0.1)
        assert err == {"type": "ERROR", "reason": "APP_ERROR", "requestId": err["requestId"]}
        await c.wait(lambda: ("IDLE", None) in c.media_states(), 4)
        later = [e for e in entries(c)[1:] if e["playerState"] != "IDLE"]
        assert later
        assert all("media" not in e for e in later)


async def test_empty_status_after_stop_and_idle_app_timeout(scene: Scene):
    dev = await scene.device(
        "chromecast/nest_mini", empty_status_after_stop=True, idle_app_timeout_s=0.3
    )
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 3.0).path)
    async with await CastClient.connect(dev) as c:
        st = await _play(c, [url], app=p.APP_MEDIA_RECEIVER)
        stop = await c.media("STOP", mediaSessionId=st["status"][0]["mediaSessionId"])
        assert stop["status"] == []
        await c.wait(lambda: fired(scene, "idle_app_timeout_s"), 3)
        rs = await c.receiver("GET_STATUS")
        assert "applications" not in rs["status"]


# -- receiver / app ----------------------------------------------------------------------------


async def test_launch_never_and_broadcast_only(scene: Scene):
    dev = await scene.device("chromecast/nest_mini", launch="never")
    async with await CastClient.connect(dev) as c:
        with pytest.raises(TimeoutError):
            await c.receiver("LAUNCH", appId=p.APP_MA, timeout=0.5)
        assert dev.app is None
    dev2 = await scene.device("chromecast/nest_mini", launch="broadcast_only")
    async with await CastClient.connect(dev2) as c:
        with pytest.raises(TimeoutError):
            await c.receiver("LAUNCH", appId=p.APP_MA, timeout=0.5)
        bc = [d for d in c.messages("RECEIVER_STATUS") if d["requestId"] == 0]
        assert bc
        assert bc[-1]["status"]["applications"][0]["appId"] == p.APP_MA
    assert fired(scene, "launch")


async def test_launch_error_and_fail_once(scene: Scene):
    """C35: the MA receiver is refused (NOT_FOUND); the first LAUNCH may fail with no reason."""
    dev = await scene.device("chromecast/google_home_speaker", launch_fail_once=True)
    async with await CastClient.connect(dev) as c:
        first = await c.receiver("LAUNCH", appId=p.APP_MEDIA_RECEIVER)
        assert first == {"type": "LAUNCH_ERROR", "requestId": first["requestId"]}
        ma = await c.receiver("LAUNCH", appId=p.APP_MA)
        assert ma["type"] == "LAUNCH_ERROR"
        assert ma["reason"] == "NOT_FOUND"
        assert await c.launch(p.APP_MEDIA_RECEIVER)
        av = await c.receiver("GET_APP_AVAILABILITY", appId=[p.APP_MA, p.APP_MEDIA_RECEIVER])
        assert av["availability"] == {
            p.APP_MA: "APP_UNAVAILABLE",
            p.APP_MEDIA_RECEIVER: "APP_AVAILABLE",
        }


async def test_quit_ack_delay(scene: Scene):
    dev = await scene.device("chromecast/nest_mini", quit_ack_delay_s=0.4)
    async with await CastClient.connect(dev) as c:
        await c.launch()
        t0 = asyncio.get_running_loop().time()
        await c.receiver("STOP")
        assert asyncio.get_running_loop().time() - t0 >= 0.35


async def test_foreign_takeover_timeline():
    """C40: off +0, foreign app +2 s, BUFFERING +7 s, PLAYING +10 s (virtual clock)."""
    clock = VirtualClock()
    dev = build_device("chromecast", "nest_mini", recorder=Recorder(), clock=clock)
    await dev.start()
    try:
        async with await CastClient.connect(dev) as c:
            await c.launch(p.APP_MA)
            task = asyncio.create_task(dev.sim_action("foreign_takeover", {}))
            await clock.advance(0)
            await c.wait(
                lambda: any(
                    "applications" not in d["status"] for d in c.messages("RECEIVER_STATUS")
                )
            )
            await clock.advance(2.0)
            await c.wait(
                lambda: any(
                    d["status"].get("applications", [{}])[0].get("appId") == p.APP_MEDIA_RECEIVER
                    for d in c.messages("RECEIVER_STATUS")
                )
            )
            assert dev.player_state() == "IDLE"
            await clock.advance(5.0)
            assert dev.player_state() == "BUFFERING"
            await clock.advance(3.0)
            assert await task is True
            assert dev.player_state() == "PLAYING"
            assert dev.transport.owner == "foreign"
    finally:
        await dev.stop()


# -- volume ---------------------------------------------------------------------------------


async def test_fixed_volume_and_idle_volume_not_applied(scene: Scene):
    dev = await scene.device("chromecast/nest_mini", control_type="fixed")
    async with await CastClient.connect(dev) as c:
        rs = await c.receiver("SET_VOLUME", volume={"level": 0.2})
        assert rs["status"]["volume"] == {
            "controlType": "fixed",
            "level": 1.0,
            "muted": False,
            "stepInterval": 0.05,
        }
    dev2 = await scene.device("chromecast/nest_mini", ignore_idle_volume="not_applied")
    async with await CastClient.connect(dev2) as c:
        rs = await c.receiver("SET_VOLUME", volume={"level": 0.8})
        assert rs["status"]["volume"]["level"] == 0.8
        st = await c.receiver("LAUNCH", appId=p.APP_MEDIA_RECEIVER)
        assert st["status"]["volume"]["level"] == 0.3  # C8: the idle change was lost
    assert fired(scene, "ignore_idle_volume")


# -- connection / discovery --------------------------------------------------------------------


async def test_heartbeat_stop(scene: Scene):
    dev = await scene.device("chromecast/nest_mini", heartbeat_stop_after_s=0.01)
    await asyncio.sleep(0.05)
    async with await CastClient.connect(dev) as c:
        await c.send(p.NS_HEARTBEAT, {"type": "PING"})
        await asyncio.sleep(0.3)
        assert not c.messages("PONG")
    assert fired(scene, "heartbeat_stop_after_s")


async def test_kill_control_socket_after_flow_restart(scene: Scene):
    """C34: a second LOAD makes the LG kill every CASTV2 socket N s later."""
    dev = await scene.device(
        "chromecast/lg_s95tr",
        kill_control_socket_after_flow_restart_s=0.3,
        strict_content_type=False,
    )
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 3.0).path)
    async with await CastClient.connect(dev) as c:
        await c.launch()
        await c.media("LOAD", media=media_item(url))
        await asyncio.sleep(0.4)
        assert c.closed is None
        await c.media("LOAD", media=media_item(url))
        await c.wait(lambda: c.closed is not None, 3)
    assert fired(scene, "kill_control_socket_after_flow_restart_s")


async def test_leader_move_resets_and_reappears(scene: Scene):
    """C25: the group endpoint drops every socket, refuses for a while, then comes back."""
    dev = await scene.device("chromecast/cast_group", leader_move_gap_s=0.4)
    c = await CastClient.connect(dev)
    try:
        await c.receiver("GET_STATUS")
        task = asyncio.create_task(dev.sim_action("leader_move", {}))
        await c.wait(lambda: c.closed is not None, 2)
        await asyncio.sleep(0.1)
        with pytest.raises(OSError):
            await CastClient.connect(dev)
        assert await task == 1
        async with await CastClient.connect(dev) as c2:
            assert (await c2.receiver("GET_STATUS"))["type"] == "RECEIVER_STATUS"
    finally:
        await c.close()
    assert fired(scene, "leader_moves_after_s")


async def test_offline_refuses_cast_connections(scene: Scene):
    dev = await scene.device("chromecast/nest_mini", offline=True)
    c = CastClient(dev.cast.host, dev.cast.port)
    try:
        await c.open()
        await c.wait(lambda: c.closed is not None, 2)
    except (OSError, ConnectionError):
        pass
    finally:
        await c.close()


@pytest.mark.parametrize(("mode", "status"), [("404", 404), ("empty", 200), ("tls_fail", 404)])
async def test_eureka_info_modes(scene: Scene, mode: str, status: int):
    dev = await scene.device("chromecast/nest_mini", eureka_info=mode)
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/setup/eureka_info") as r:
        assert r.status == status
        if mode == "empty":
            assert await r.json() == {}


async def test_current_time_jump(scene: Scene):
    dev = await scene.device("chromecast/nest_mini", current_time_jump={"p": 1.0, "by_s": 7.0})
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 3.0).path)
    async with await CastClient.connect(dev) as c:
        await _play(c, [url], app=p.APP_MEDIA_RECEIVER)
        await c.wait(lambda: ("PLAYING", None) in c.media_states())
        st = await c.media("GET_STATUS")
        assert st["status"][0]["currentTime"] >= 7.0
    assert fired(scene, "current_time_jump")
