"""MPD protocol fidelity: greeting, status, playback, idle/noidle, command lists, ACKs."""

import asyncio

import aiohttp
import pytest

from seamer.families import discover, get_family
from seamer.families.mpd.protocol import TokenizeError, tokenize
from seamer.simkit import TState


def _fired(scene, knob: str) -> list:
    return [
        e
        for e in scene.rec.events
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    ]


def test_registry_and_profiles():
    assert "mpd" in discover()
    fam = get_family("mpd")
    assert set(fam.profiles()) == {"mpd_0_23", "mpd_0_24", "moode", "volumio", "mopidy"}
    assert fam.default_profile == "mpd_0_23"


@pytest.mark.parametrize(
    ("profile", "greeting"),
    [
        ("mpd_0_23", "OK MPD 0.23.5\n"),
        ("mpd_0_24", "OK MPD 0.24.0\n"),
        ("moode", "OK MPD 0.23.5\n"),
        ("volumio", "OK MPD 0.21.25\n"),
        ("mopidy", "OK MPD 0.19.0\n"),
    ],
)
async def test_greeting_per_profile(scene, connect, profile, greeting):
    dev = await scene.device(f"mpd/{profile}")
    c = await connect(dev.mpd_port)
    assert c.greeting == greeting
    assert await c.cmd("ping") == ["OK"]


async def test_tokenizer():
    assert tokenize('add "http://h/a b.mp3"') == ["add", "http://h/a b.mp3"]
    assert tokenize(r'find "x\"y" "a\\b"') == ["find", 'x"y', "a\\b"]
    assert tokenize("  setvol   5 ") == ["setvol", "5"]
    for bad, msg in (
        ('add "abc', "Missing closing"),
        ('add "a"b', "Space expected"),
        ("add a'b", "Invalid unquoted"),
    ):
        with pytest.raises(TokenizeError, match=msg):
            tokenize(bad)


async def test_status_stopped_exact(scene, connect):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    assert await c.cmd("status") == [
        "volume: 50",
        "repeat: 0",
        "random: 0",
        "single: 0",
        "consume: 0",
        "partition: default",
        "playlist: 1",
        "playlistlength: 0",
        "mixrampdb: 0",
        "state: stop",
        "OK",
    ]
    assert await c.cmd("currentsong") == ["OK"]


async def test_play_stream_status_and_end(scene, connect):
    await scene.media_server()
    t = scene.media.track(1, 1.5)
    url = scene.media_url(t.path)
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    assert await c.cmd(f'addid "{url}"') == ["Id: 1", "OK"]
    assert await c.cmd("play") == ["OK"]
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5, "playing")
    st = await c.kv("status")
    assert st["state"] == "play"
    assert (st["song"], st["songid"], st["playlist"], st["playlistlength"]) == ("0", "1", "2", "1")
    assert st["audio"] == "44100:16:2"
    assert st["bitrate"] == "1411"
    assert float(st["duration"]) == pytest.approx(1.5, abs=0.01)  # from Content-Length
    assert st["time"].endswith(":2")
    song = await c.kv("currentsong")
    assert song == {"file": url, "Time": "2", "duration": "1.500", "Pos": "0", "Id": "1"}
    assert await c.cmd("playlistinfo") == [*(f"{k}: {v}" for k, v in song.items()), "OK"]
    await scene.wait_for(lambda: dev.transport.state == TState.STOPPED, 5, "ended")
    st = await c.kv("status")
    assert st["state"] == "stop"
    assert "elapsed" not in st
    assert "time" not in st
    scene.expect.audible_coverage(track=1, at_least_s=1.4)
    req = next(e for e in scene.rec.events if e.name == "http_request")
    assert req.data["headers"]["User-Agent"] == "Music Player Daemon 0.23.5"


async def test_pause_resume_seek_volume(scene, connect):
    await scene.media_server()
    t = scene.media.track(1, 20.0)
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    await c.cmd("clear")
    await c.cmd(f'add "{scene.media_url(t.path)}"')
    await c.cmd("play 0")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    assert await c.cmd("pause 1") == ["OK"]
    assert (await c.kv("status"))["state"] == "pause"
    assert await c.cmd("pause 1") == ["OK"]  # pausing a paused player: no-op
    assert (await c.kv("status"))["state"] == "pause"
    assert await c.cmd("pause 0") == ["OK"]
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    assert await c.cmd("pause") == ["OK"]  # toggle
    assert (await c.kv("status"))["state"] == "pause"
    # seek while paused stays paused (MPD semantics)
    assert await c.cmd("seekcur 10") == ["OK"]
    await scene.wait_for(lambda: dev.mpd_state() == "pause" and dev.sink.active, 5, "paused")
    st = await c.kv("status")
    assert float(st["elapsed"]) == pytest.approx(10, abs=0.6)
    assert await c.cmd("pause 0") == ["OK"]
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    assert await c.cmd("seekcur +2") == ["OK"]
    assert await c.cmd("setvol 33") == ["OK"]
    assert (await c.kv("getvol")) == {"volume": "33"}
    assert await c.cmd("volume -3") == ["OK"]
    assert (await c.kv("status"))["volume"] == "30"
    assert await c.cmd("stop") == ["OK"]
    assert (await c.kv("status"))["state"] == "stop"
    assert await c.cmd("seekcur 5") == ["ACK [55@0] {seekcur} Not playing"]


async def test_idle_semantics(scene, connect):
    await scene.media_server()
    t = scene.media.track(1, 10.0)
    dev = await scene.device("mpd")
    a = await connect(dev.mpd_port)
    b = await connect(dev.mpd_port)
    a.send("idle")
    await asyncio.sleep(0.1)
    assert await b.cmd(f'add "{scene.media_url(t.path)}"') == ["OK"]
    assert await a.reply() == ["changed: playlist", "OK"]
    # flags raised while not idling are kept and returned at once
    await b.cmd("setvol 40")
    await b.cmd("repeat 1")
    assert await a.cmd("idle") == ["changed: mixer", "changed: options", "OK"]
    # a mask only wakes on its subsystems; others stay pending until then...
    a.send("idle player")
    await b.cmd("setvol 41")
    await b.cmd("play")
    assert await a.reply() == ["changed: player", "OK"]
    # ...but a notify clears every pending flag (MPD's Client::IdleNotify)
    a.send("idle mixer")
    await asyncio.sleep(0.2)
    a.send("noidle")
    assert await a.reply() == ["OK"]
    assert await a.cmd("idle bogus") == ["ACK [2@0] {idle} Unrecognized idle event: bogus"]
    # a command other than noidle while idling closes the connection
    a.send("idle", "status")
    assert await a.closed()


async def test_stray_noidle_is_ignored(scene, connect):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    c.send("noidle")
    assert await c.cmd("ping") == ["OK"]  # no extra OK in between (MP2)


async def test_command_lists(scene, connect):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    c.send("command_list_ok_begin", "ping", "setvol 20", "status", "command_list_end")
    lines = await c.reply()
    assert lines[:2] == ["list_OK", "list_OK"]
    assert "volume: 20" in lines
    assert lines[-2:] == ["list_OK", "OK"]
    c.send("command_list_begin", "setvol 21", "setvol 200", "setvol 22", "command_list_end")
    assert await c.reply() == ["ACK [2@1] {setvol} Number too large: 200"]
    assert (await c.kv("status"))["volume"] == "21"  # the list stopped at the error
    c.send("command_list_ok_begin", "ping", "frobnicate", "command_list_end")
    assert await c.reply() == ["list_OK", 'ACK [5@1] {} unknown command "frobnicate"']


@pytest.mark.parametrize(
    ("line", "ack"),
    [
        ("frobnicate", 'ACK [5@0] {} unknown command "frobnicate"'),
        ("setvol", 'ACK [2@0] {setvol} wrong number of arguments for "setvol"'),
        ("seek 1", 'ACK [2@0] {seek} wrong number of arguments for "seek"'),
        ("pause 1 2", 'ACK [2@0] {pause} too many arguments for "pause"'),
        ("add", 'ACK [2@0] {add} too few arguments for "add"'),
        ("setvol x", "ACK [2@0] {setvol} Integer expected: x"),
        ("setvol -1", "ACK [2@0] {setvol} Number is negative: -1"),
        ("setvol 101", "ACK [2@0] {setvol} Number too large: 101"),
        ("pause 2", "ACK [2@0] {pause} Boolean (0/1) expected: 2"),
        ("seekcur abc", "ACK [2@0] {seekcur} Float expected: abc"),
        ("play 3", "ACK [2@0] {play} Bad song index"),
        ("playid 99", "ACK [50@0] {playid} No such song"),
        ("deleteid 99", "ACK [50@0] {deleteid} No such song"),
        ('add "foo/bar.mp3"', "ACK [50@0] {add} Not found"),
        ('add "ftp://h/x.mp3"', "ACK [50@0] {add} Unsupported URI scheme"),
        ("seekcur 3", "ACK [55@0] {seekcur} Not playing"),
        ("password nope", "ACK [3@0] {password} incorrect password"),
        ("config", "ACK [4@0] {config} Command only permitted to local clients"),
        ('add "unterminated', "ACK [2@0] {} Missing closing '\"'"),
        ("disableoutput 7", "ACK [50@0] {disableoutput} No such audio output"),
    ],
)
async def test_ack_formats(scene, connect, line, ack):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    assert await c.cmd(line) == [ack]
    assert await c.cmd("ping") == ["OK"]  # the connection survives an ACK


async def test_malformed_line_closes(scene, connect):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    c.send("Status")  # must start with a lower-case letter
    assert await c.closed()


async def test_close_command(scene, connect):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    c.send("close")
    assert await c.closed()


async def test_password_and_permissions(scene, connect):
    dev = await scene.device("mpd", password="s3cret")
    c = await connect(dev.mpd_port)
    assert await c.cmd("status") == ['ACK [4@0] {status} you don\'t have permission for "status"']
    assert await c.cmd("ping") == ["OK"]
    assert await c.cmd("password wrong") == ["ACK [3@0] {password} incorrect password"]
    assert await c.cmd("password s3cret") == ["OK"]
    assert (await c.kv("status"))["state"] == "stop"
    # permission is checked before the argument count (MPD order)
    d = await connect(dev.mpd_port)
    assert await d.cmd("setvol") == ['ACK [4@0] {setvol} you don\'t have permission for "setvol"']


async def test_default_permissions_read_only(scene, connect):
    dev = await scene.device("mpd", default_permissions="read")
    c = await connect(dev.mpd_port)
    assert (await c.kv("status"))["state"] == "stop"
    assert await c.cmd("setvol 5") == ['ACK [4@0] {setvol} you don\'t have permission for "setvol"']


async def test_commands_list_reflects_version(scene, connect):
    dev = await scene.device("mpd/mopidy")
    c = await connect(dev.mpd_port)
    names = {ln.split(": ", 1)[1] for ln in (await c.cmd("commands"))[:-1]}
    assert {"status", "idle", "setvol", "add"} <= names
    assert "getvol" not in names
    assert "readpicture" not in names
    dev2 = await scene.device("mpd/mpd_0_24")
    c2 = await connect(dev2.mpd_port)
    names2 = {ln.split(": ", 1)[1] for ln in (await c2.cmd("commands"))[:-1]}
    assert {"getvol", "readpicture", "albumart", "binarylimit"} <= names2


async def test_queue_commands(scene, connect):
    await scene.media_server()
    urls = [scene.media_url(scene.media.track(i, 5.0).path) for i in (1, 2, 3)]
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    for u in urls:
        await c.cmd(f'add "{u}"')
    assert await c.cmd("playlist") == [f"{i}: file: {u}" for i, u in enumerate(urls)] + ["OK"]
    assert await c.cmd("delete 1") == ["OK"]
    st = await c.kv("status")
    assert (st["playlist"], st["playlistlength"]) == ("5", "2")
    ids = [ln for ln in await c.cmd("playlistid") if ln.startswith("Id: ")]
    assert ids == ["Id: 1", "Id: 3"]
    assert await c.cmd("add 0:2") == ["ACK [50@0] {add} Not found"]
    assert await c.cmd(f'add "{urls[1]}" 0') == ["OK"]  # position arg (MPD >= 0.23.3)
    assert [ln for ln in await c.cmd("playlistinfo 0") if ln.startswith("Id")] == ["Id: 4"]
    assert await c.cmd("playlistinfo 9") == ["ACK [2@0] {playlistinfo} Bad song index"]
    assert await c.cmd("playid 3") == ["OK"]
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    st = await c.kv("status")
    assert (st["song"], st["songid"]) == ("2", "3")
    assert await c.cmd("previous") == ["OK"]
    st = await c.kv("status")
    assert (st["song"], st["songid"]) == ("1", "1")
    assert (st["nextsong"], st["nextsongid"]) == ("2", "3")
    assert await c.cmd("clear") == ["OK"]
    st = await c.kv("status")
    assert (st["state"], st["playlistlength"]) == ("stop", "0")


async def test_auto_advance_to_next_song(scene, connect):
    await scene.media_server()
    urls = [scene.media_url(scene.media.track(i, 1.0).path) for i in (1, 2)]
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    for u in urls:
        await c.cmd(f'add "{u}"')
    await c.cmd("play")
    await scene.wait_for(lambda: dev.current == 1, 5, "advanced")
    st = await c.kv("status")
    assert st["songid"] == "2"
    assert "nextsong" not in st
    await scene.wait_for(lambda: dev.mpd_state() == "stop", 5, "end of queue")


async def test_mopidy_status_dialect(scene, connect):
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 10.0).path)
    dev = await scene.device("mpd/mopidy")
    c = await connect(dev.mpd_port)
    await c.cmd(f'add "{url}"')
    await c.cmd("play")
    await scene.wait_for(lambda: dev.transport.state == TState.PLAYING, 5)
    st = await c.kv("status")
    assert list(st)[:9] == [
        "volume",
        "repeat",
        "random",
        "single",
        "consume",
        "playlist",
        "playlistlength",
        "xfade",
        "state",
    ]
    assert st["volume"] == "50"
    assert "partition" not in st
    assert "audio" not in st
    assert "duration" not in st
    assert await c.cmd("getvol") == ['ACK [5@0] {} unknown command "getvol"']
    song = await c.kv("currentsong")
    assert list(song) == ["file", "Time", "Pos", "Id"]


async def test_mpd_0_24_extras(scene, connect):
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 3.0).path)
    dev = await scene.device("mpd/mpd_0_24")
    c = await connect(dev.mpd_port)
    assert await c.cmd("consume oneshot") == ["OK"]
    assert (await c.kv("status"))["consume"] == "oneshot"
    await c.cmd(f'add "{url}"')
    assert "Added" in await c.kv("playlistinfo")
    dev23 = await scene.device("mpd/mpd_0_23")
    c23 = await connect(dev23.mpd_port)
    assert await c23.cmd("consume oneshot") == [
        "ACK [2@0] {consume} Boolean (0/1) expected: oneshot"
    ]


async def test_sim_external_action_wakes_idle(scene, connect):
    dev = await scene.device("mpd")
    c = await connect(dev.mpd_port)
    c.send("idle")
    await asyncio.sleep(0.1)
    async with (
        aiohttp.ClientSession() as s,
        s.post(f"{dev.base_url}/_sim/action/external", json={"cmds": ["setvol 9", "bad"]}) as r,
    ):
        body = await r.json()
    assert r.status == 200
    assert body["result"] == ["OK\n", 'ACK [5@0] {} unknown command "bad"\n']
    assert await c.reply() == ["changed: mixer", "OK"]
    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/_sim/state") as r:
        state = await r.json()
    fam = state["family_state"]
    assert fam["volume"] == 9
    assert fam["mpd_port"] == dev.mpd_port
    assert [x["role"] for x in fam["connections"]] == ["idle"]
