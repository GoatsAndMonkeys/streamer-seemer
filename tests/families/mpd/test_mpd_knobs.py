"""One test per MPD knob family: each quirk reproduces and records a knob firing."""

import asyncio

import aiohttp
import pytest

from seamer.families.mpd.knobs import FAMILY
from seamer.simkit import TState
from seamer.simkit.knobs import knobs_for


def fired(dev, knob: str) -> list[dict]:
    return [
        e.data
        for e in dev.rec.rec.events
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    ]


def events(dev, name: str) -> list[dict]:
    return [e.data for e in dev.rec.rec.events if e.name == name]


async def action(dev, name: str, payload: dict) -> object:
    async with (
        aiohttp.ClientSession() as s,
        s.post(f"{dev.base_url}/_sim/action/{name}", json=payload) as r,
    ):
        assert r.status == 200, await r.text()
        return (await r.json())["result"]


def test_every_knob_is_tagged():
    mine = [k for k in knobs_for(FAMILY).values() if k.family == FAMILY]
    assert len(mine) >= 20
    for k in mine:
        assert k.quirks or k.name == "mpd_port", k.name  # the port is plumbing
        assert k.confidence in ("high", "medium", "low"), k.name


async def test_max_connections_closes_without_greeting(scene, connect):
    dev = await scene.device("mpd", max_connections=1)
    await connect(dev.mpd_port)
    extra = await connect(dev.mpd_port, greet=False)
    assert await extra.closed()
    assert dev.mpd.refused == 1
    assert fired(dev, "max_connections")


async def test_greeting_delay(scene, connect):
    dev = await scene.device("mpd", greeting_delay_s=0.4)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    c = await connect(dev.mpd_port)
    assert c.greeting.startswith("OK MPD ")
    assert loop.time() - t0 >= 0.35


async def test_connection_timeout_spares_idlers(scene, connect):
    dev = await scene.device("mpd", connection_timeout_s=1.0)
    quiet = await connect(dev.mpd_port)
    idler = await connect(dev.mpd_port)
    idler.send("idle")
    assert await quiet.closed(timeout=3)
    await idler.cmd("noidle")  # still open: MPD does not time out idling clients


async def test_timeout_idle_too(scene, connect):
    dev = await scene.device("mpd", connection_timeout_s=1.0, timeout_idle_too=True)
    idler = await connect(dev.mpd_port)
    idler.send("idle")
    assert await idler.closed(timeout=3)
    assert fired(dev, "timeout_idle_too")


async def test_drop_connection_command_role(scene, connect):
    """python-mpd2's command client (keepalive `idle database`) gets dropped (MP12)."""
    dev = await scene.device("mpd", drop_connection={"p": 1.0, "which": "command"})
    cmd = await connect(dev.mpd_port)
    idler = await connect(dev.mpd_port)
    cmd.send("idle database")  # marks the connection as python-mpd2's command client
    idler.send("idle")
    await asyncio.sleep(0.1)
    cmd.send("noidle")
    assert await cmd.closed()
    await dev.external("setvol 12")
    assert await idler.reply() == ["changed: mixer", "OK"]  # the idle client survives
    assert fired(dev, "drop_connection")[0]["role"] == "command"


async def test_drop_connection_idle_role(scene, connect):
    dev = await scene.device("mpd", drop_connection={"p": 1.0, "which": "idle"})
    cmd = await connect(dev.mpd_port)
    idler = await connect(dev.mpd_port)
    idler.send("idle")
    await asyncio.sleep(0.1)
    idler.send("noidle")
    assert await idler.closed()
    assert await cmd.cmd("ping") == ["OK"]


async def test_drop_action(scene, connect):
    dev = await scene.device("mpd")
    a = await connect(dev.mpd_port)
    b = await connect(dev.mpd_port)
    b.send("idle")
    await asyncio.sleep(0.1)
    assert len(await action(dev, "drop", {"which": "idle"})) == 1
    assert await b.closed()
    assert await a.cmd("ping") == ["OK"]


@pytest.mark.parametrize(
    ("mode", "extra"), [("ok", ["OK"]), ("ack", ['ACK [5@0] {} unknown command "noidle"'])]
)
async def test_stray_noidle_bogus_reply(scene, connect, mode, extra):
    dev = await scene.device("mpd", stray_noidle=mode)
    c = await connect(dev.mpd_port)
    c.send("noidle")
    assert await c.reply() == extra
    assert await c.cmd("ping") == ["OK"]
    assert fired(dev, "stray_noidle")


async def test_mixer_none_moode(scene, connect):
    dev = await scene.device("mpd/moode")
    c = await connect(dev.mpd_port)
    assert (await c.kv("status"))["volume"] == "-1"
    assert await c.cmd("setvol 40") == ["ACK [52@0] {setvol} problems setting volume"]
    assert await c.cmd("getvol") == ["OK"]
    assert fired(dev, "mixer")


async def test_mixer_none_omits_volume(scene, connect):
    dev = await scene.device("mpd", mixer="none")
    c = await connect(dev.mpd_port)
    assert "volume" not in await c.kv("status")


async def test_mixer_failing_reports_but_rejects(scene, connect):
    dev = await scene.device("mpd", mixer="failing", setvol_error="Failed to set mixer")
    c = await connect(dev.mpd_port)
    assert (await c.kv("status"))["volume"] == "50"
    assert await c.cmd("setvol 10") == ["ACK [52@0] {setvol} Failed to set mixer"]


async def _queue_one(scene, connect, dev, secs=20.0, route="media"):
    await scene.media_server()
    t = scene.media.track(1, secs)
    url = scene.media_url(t.path, route=route)
    c = await connect(dev.mpd_port)
    await c.cmd(f'add "{url}"')
    return c, url


@pytest.mark.parametrize(("noop", "state"), [(True, "stop"), (False, "play")])
async def test_pause_in_stop(scene, connect, noop, state):
    dev = await scene.device("mpd", pause_in_stop_noop=noop)
    c, _ = await _queue_one(scene, connect, dev)
    assert await c.cmd("pause 0") == ["OK"]
    await asyncio.sleep(0.3)
    assert (await c.kv("status"))["state"] == state
    assert bool(fired(dev, "pause_in_stop_noop")) is noop


async def test_not_seekable_without_length(scene, connect):
    dev = await scene.device("mpd")
    c, _ = await _queue_one(scene, connect, dev, route="stream")
    await c.cmd("play")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    st = await c.kv("status")
    assert "duration" not in st  # no Content-Length: unknown length
    assert await c.cmd("seekcur 5") == ["ACK [5@0] {} Not seekable"]
    assert fired(dev, "not_seekable_without_length")


async def test_seekable_with_knob_off(scene, connect):
    dev = await scene.device("mpd", not_seekable_without_length=False)
    c, _ = await _queue_one(scene, connect, dev, route="stream")
    await c.cmd("play")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    assert await c.cmd("seekcur 5") == ["OK"]


async def test_report_duration_none(scene, connect):
    dev = await scene.device("mpd", report_duration="none")
    c, _ = await _queue_one(scene, connect, dev)
    await c.cmd("play")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    st = await c.kv("status")
    assert "duration" not in st
    assert "Time" not in await c.kv("currentsong")


async def test_codec_probe_fail_on_unsized_stream(scene, connect):
    dev = await scene.device("mpd", codec_probe_fail="wav")
    c, url = await _queue_one(scene, connect, dev, route="stream")
    assert await c.cmd("play") == ["OK"]
    await scene.wait_for(lambda: dev.mpd_state() == "stop" and dev.error, 5, "decode failure")
    st = await c.kv("status")
    assert st["error"] == f"Failed to decode {url}"
    assert fired(dev, "codec_probe_fail")
    assert await c.cmd("clearerror") == ["OK"]
    assert "error" not in await c.kv("status")


async def test_icy_metadata_header(scene, connect):
    dev = await scene.device("mpd", icy_metadata_header=True)
    c, _ = await _queue_one(scene, connect, dev)
    await c.cmd("play")
    await scene.wait_for(lambda: events(dev, "http_request"), 5)
    assert events(dev, "http_request")[0]["headers"]["Icy-MetaData"] == "1"


async def test_art_probe_gets(scene, connect):
    dev = await scene.device("mpd/mpd_0_24")
    c, url = await _queue_one(scene, connect, dev)
    assert await c.cmd(f'albumart "{url}" 0') == ["ACK [50@0] {albumart} No file exists"]
    assert await c.cmd(f'readpicture "{url}" 0') == ["OK"]
    probes = [e["url"] for e in events(dev, "art_probe")]
    assert probes == [url.rsplit("/", 1)[0] + "/cover.png", url]
    assert len(fired(dev, "art_probe_get")) == 2


async def test_art_probe_off(scene, connect):
    dev = await scene.device("mpd/mpd_0_24", art_probe_get=False)
    c, url = await _queue_one(scene, connect, dev)
    await c.cmd(f'readpicture "{url}" 0')
    assert not events(dev, "art_probe")


async def test_mopidy_add_probe_get(scene, connect):
    dev = await scene.device("mpd/mopidy")
    _, url = await _queue_one(scene, connect, dev)
    await scene.wait_for(lambda: events(dev, "add_probe"), 5, "add probe")
    assert [e["url"] for e in events(dev, "add_probe")] == [url]
    assert fired(dev, "add_probe_get")
    assert dev.transport.state != TState.PLAYING  # a probe is not playback


async def test_volumio_initial_consume(scene, connect):
    dev = await scene.device("mpd/volumio")
    c = await connect(dev.mpd_port)
    assert (await c.kv("status"))["consume"] == "1"
    await c.cmd("consume 0")
    assert (await c.kv("status"))["consume"] == "0"


async def test_consume_removes_played_song(scene, connect):
    await scene.media_server()
    urls = [scene.media_url(scene.media.track(i, 1.0).path) for i in (1, 2)]
    dev = await scene.device("mpd/volumio")
    c = await connect(dev.mpd_port)
    for u in urls:
        await c.cmd(f'add "{u}"')
    await c.cmd("play")
    await scene.wait_for(lambda: len(dev.queue) == 1, 5, "consumed")
    assert (await c.kv("currentsong"))["file"] == urls[1]


async def test_output_busy_renderer(scene, connect):
    dev = await scene.device("mpd/moode")
    c, _ = await _queue_one(scene, connect, dev)
    assert await action(dev, "renderer", {"renderer": "airplay"}) == {
        "renderer": "airplay",
        "queue": 1,
    }
    assert await c.cmd("play") == ["OK"]
    st = await c.kv("status")
    assert st["state"] == "stop"
    assert "Device or resource busy" in st["error"]
    assert fired(dev, "output_busy")
    assert not events(dev, "http_request")
    await action(dev, "renderer", {"renderer": None})
    await c.cmd("play")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)


async def test_ownqueue_clears_mpd(scene, connect):
    dev = await scene.device("mpd/moode")
    c, _ = await _queue_one(scene, connect, dev)
    await c.cmd("play")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    res = await action(dev, "renderer", {"renderer": "upnp"})
    assert res == {"renderer": "upnp", "queue": 0}
    st = await c.kv("status")
    assert (st["state"], st["playlistlength"]) == ("stop", "0")
    assert fired(dev, "ownqueue_clears_mpd")


async def test_live_knob_change_via_http(scene, connect):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    async with (
        aiohttp.ClientSession() as s,
        s.post(f"{dev.base_url}/_sim/knobs", json={"mixer": "none"}) as r,
    ):
        assert r.status == 200
    assert "volume" not in await c.kv("status")
