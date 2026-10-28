"""BluOS wire format: framing, documents per profile, refusals, volume, discovery."""

import asyncio

import pytest
from bluos_util import FAST, PROLOG, events, get

from seamer.families import discover, get_family
from seamer.families.bluos.device import fmt_db
from seamer.simkit.scenario import Scene

PROFILES = ["node_n130", "powernode", "pulse_flex_2i", "nad_c700", "bluos3"]


def test_family_and_profiles_load():
    assert "bluos" in discover()
    fam = get_family("bluos")
    assert set(fam.profiles()) == set(PROFILES)
    assert fam.default_port == 11000
    for name in PROFILES:
        prof = fam.load_profile(name)
        mac = prof.identity["mac"]
        assert mac.startswith("00:00:5E:00:53:"), mac


async def test_stopped_status_golden(scene: Scene):
    dev = await scene.device("bluos/node_n130")
    r = await get(dev, "/Status")
    assert r.status == 200
    assert r.content_type == "text/xml; charset=utf-8"
    assert r.text.startswith(PROLOG)
    sync = dev.sync_stat
    etag = r.xml.get("etag")
    assert len(etag) == 32
    assert r.body == (
        f'<status etag="{etag}"><canMovePlayback>true</canMovePlayback><canSeek>0</canSeek>'
        "<cursor>0</cursor><db>-56</db><indexing>0</indexing><mid>0</mid><mode>1</mode>"
        "<mute>0</mute><pid>1</pid><prid>0</prid><repeat>0</repeat><secs>0</secs>"
        "<shuffle>0</shuffle><sid>5</sid><sleep></sleep><song>0</song><state>stop</state>"
        f"<syncStat>{sync}</syncStat><volume>30</volume></status>"
    )


async def test_sync_status_golden_node(scene: Scene):
    dev = await scene.device("bluos/node_n130")
    r = await get(dev, "/SyncStatus")
    s = dev.sync_stat
    assert r.body == (
        f'<SyncStatus etag="{s}" syncStat="{s}" version="4.16.22" id="127.0.0.1:{dev.port}" '
        'db="-56" volume="30" name="Living Room Node" model="N130" modelName="NODE" '
        'class="streamer" icon="/images/players/N130_nt.png" brand="Bluesound" '
        'schemaVersion="34" initialized="true" mac="00:00:5E:00:53:01">'
        "<pairWithSub></pairWithSub><bluetoothOutput></bluetoothOutput></SyncStatus>"
    )


@pytest.mark.parametrize(
    ("profile", "schema", "has_version", "children"),
    [
        ("node_n130", "34", True, ["pairWithSub", "bluetoothOutput"]),
        ("powernode", "34", True, ["bluetoothOutput"]),
        ("pulse_flex_2i", "34", True, ["zoneOptions", "pairWithSub"]),
        ("nad_c700", "36", True, ["bluetoothOutput"]),
        ("bluos3", "32", False, ["pairWithSub", "bluetoothOutput"]),
    ],
)
async def test_sync_status_per_profile(scene: Scene, profile, schema, has_version, children):
    dev = await scene.device(f"bluos/{profile}")
    x = (await get(dev, "/SyncStatus")).xml
    assert x.get("schemaVersion") == schema
    assert ("version" in x.attrib) is has_version
    assert ("class" in x.attrib) is has_version
    assert [c.tag for c in x] == children
    # pyblu's parse_sync_status needs all of these
    for key in ("etag", "id", "mac", "name", "icon", "brand", "model", "modelName", "db"):
        assert key in x.attrib, key
    int(x.get("volume"))
    float(x.get("db"))


def test_db_mapping_per_profile():
    assert fmt_db(-56.0) == "-56"
    assert fmt_db(-34.5) == "-34.5"
    assert fmt_db(-0.0) == "0"


@pytest.mark.parametrize(
    ("profile", "level", "db"),
    [("node_n130", 50, "-40"), ("pulse_flex_2i", 50, "-24.5"), ("nad_c700", 100, "-20.1")],
)
async def test_volume_levels_and_db(scene: Scene, profile, level, db):
    dev = await scene.device(f"bluos/{profile}")
    r = await get(dev, "/Volume", level=level)
    assert r.xml.text == str(level)
    assert r.xml.get("db") == db
    assert list(r.xml.attrib) == ["db", "offsetDb", "mute", "etag", "source"]
    st = await get(dev, "/Status")
    assert st.field("volume") == str(level)
    assert st.field("db") == db


async def test_volume_relative_abs_db_and_mute(scene: Scene):
    dev = await scene.device("bluos/node_n130")
    await get(dev, "/Volume", level=50)
    r = await get(dev, "/Volume", db=-4)
    assert r.xml.text == "45"
    r = await get(dev, "/Volume", abs_db=-20)
    assert r.xml.text == "75"
    r = await get(dev, "/Volume", mute=1)
    assert r.xml.text == "0"
    assert r.xml.get("mute") == "1"
    assert r.xml.get("db") == "-100"
    assert r.xml.get("muteVolume") == "75"
    st = await get(dev, "/Status")
    assert (st.field("mute"), st.field("volume"), st.field("muteVolume")) == ("1", "0", "75")
    sync = (await get(dev, "/SyncStatus")).xml
    assert sync.get("volume") == "75"
    assert sync.get("mute") == "1"
    r = await get(dev, "/Volume", mute=0)
    assert (r.xml.text, r.xml.get("mute")) == ("75", "0")


async def test_refusals_are_http_200_error_bodies(scene: Scene):
    dev = await scene.device("bluos/node_n130")
    r = await get(dev, "/RemoveSlave", slave="127.0.0.1", port=dev.port)
    assert (r.status, r.body) == (200, "<error>no slave available as new master</error>")
    r = await get(dev, "/Save", name="x")
    assert (r.status, r.body) == (200, "<error>empty</error>")
    r = await get(dev, "/Preset", id=99)
    assert (r.status, r.body) == (200, "<error>Preset not found</error>")
    r = await get(dev, "/Nope")
    assert r.status == 404


async def test_presets_and_inputs_documents(scene: Scene):
    dev = await scene.device("bluos/node_n130")
    r = await get(dev, "/Presets")
    assert r.body.startswith('<presets prid="0">\n  <preset id="1" name="Classical Radio"')
    assert r.body.endswith("\n</presets>")
    r = await get(dev, "/RadioBrowse", service="Capture")
    items = r.xml.findall("item")
    assert [i.get("id") for i in items] == ["input2", "input1"]
    assert items[0].get("URL") == "Capture%3Ahw%3Aimxspdif%2C0%2F1%2F25%2F2%3Fid%3Dinput2"
    assert list(items[0].attrib) == [
        "playerName",
        "text",
        "inputType",
        "id",
        "URL",
        "image",
        "type",
    ]
    soundbar = await scene.device("bluos/node_n130", device_class="soundbar")
    ids = [i.get("id") for i in (await get(soundbar, "/RadioBrowse", service="Capture")).xml]
    assert ids[0] == "input4"


async def test_input_status_fields(scene: Scene):
    dev = await scene.device("bluos/powernode")
    assert (await get(dev, "/Play", url="Capture:bluez:bluetooth")).body == "<state>stream</state>"
    st = await get(dev, "/Status")
    assert st.field("service") == "Capture"
    assert st.field("inputId") == "input5"
    assert st.field("serviceType") == "AudioInputs"
    assert st.field("settingsGroupId") == "capture-input5"
    assert int(st.field("secs")) >= 3600
    assert st.xml.find("actions/action").get("name") == "back"
    await get(dev, "/Play", inputTypeIndex="coax-1")
    assert (await get(dev, "/Status")).field("inputId") == "input3"


async def test_preset_plays_service_and_steps(scene: Scene):
    dev = await scene.device("bluos/node_n130")
    await get(dev, "/Preset", id=2)
    st = await get(dev, "/Status")
    assert (st.field("service"), st.field("title1"), st.field("state")) == (
        "TuneIn",
        "Morning Jazz",
        "stream",
    )
    assert st.field("volume") == "20"  # a preset carries its volume
    await get(dev, "/Preset", id="+1")
    assert (await get(dev, "/Status")).field("title1") == "Classical Radio"


async def test_queue_transport_skip_back_and_playlist(scene: Scene):
    dev = await scene.device("bluos/node_n130")
    tracks = [{"title": f"T{i}", "artist": "A", "album": "B", "secs": 200} for i in range(3)]
    await dev.sim_action("load_queue", {"tracks": tracks})
    st = await get(dev, "/Status")
    assert (st.field("state"), st.field("title1"), st.field("service")) == (
        "play",
        "T0",
        "LocalMusic",
    )
    assert st.field("totlen") == "200"
    assert st.field("inputId") is None  # bss:B22
    assert (await get(dev, "/Skip")).body == "<id>1</id>"
    assert (await get(dev, "/Back")).body == "<id>0</id>"
    await get(dev, "/Skip")
    await get(dev, "/Skip")
    assert (await get(dev, "/Skip")).body == "<id>0</id>"  # wraps (bss:B14)
    pl = await get(dev, "/Playlist")
    assert pl.xml.get("length") == "3"
    songs = await get(dev, "/Playlist", start=0, end=1)
    assert len(songs.xml.findall("song")) == 2
    r = await get(dev, "/Play", seek=50)
    assert r.body == "<state>play</state>"
    assert dev.transport.truth_position() == pytest.approx(50, abs=1)
    assert (await get(dev, "/Pause")).body == "<state>pause</state>"
    assert (await get(dev, "/Play")).body == "<state>play</state>"
    assert (await get(dev, "/Stop")).body == "<state>stop</state>"
    sh = await get(dev, "/Shuffle", state=1)
    assert sh.xml.get("shuffle") == "1"
    rp = await get(dev, "/Repeat", state=1)
    assert rp.xml.get("repeat") == "1"
    assert (await get(dev, "/Sleep")).body == "<sleep>15</sleep>"
    assert (await get(dev, "/Clear")).xml.get("length") == "0"


async def test_url_stream_status_fields(scene: Scene):
    dev = await scene.device("bluos/node_n130", **FAST)
    await get(dev, "/Play", url="http://192.0.2.9/x.mp3", title="Radio X")
    st = await get(dev, "/Status")
    assert st.field("streamUrl") == "http://192.0.2.9/x.mp3"
    assert st.field("title1") == "Radio X"
    assert st.field("streamFormat") == "MP3 320 kb/s"
    assert st.field("quality") == "320"
    assert st.field("repeat") == "2"
    assert st.field("image") == "/images/streamingNP.png"


async def test_follower_play_url_leaves_group(scene: Scene):
    a = await scene.device("bluos/node_n130", **FAST)
    b = await scene.device("bluos/powernode", **FAST)
    await get(a, "/AddSlave", slave="127.0.0.1", port=b.port)
    assert b.leader is a
    await get(b, "/Play", url="http://192.0.2.9/y.flac")
    assert b.leader is None
    assert a.followers == []


def test_mdns_txt_and_service_type():
    from seamer.families import build_device

    dev = build_device("bluos", "node_n130", host="127.0.0.1", port=0)
    assert dev.mdns_txt() == {
        "mac": "00:00:5E:00:53:01",
        "model": "N130",
        "_objectType": "player",
        "version": "4.16.22",
    }
    assert dev.mdns_service_type() == "_musc._tcp.local."
    assert dev.mdns_addresses() == ["127.0.0.1"]


async def test_reboot_goes_away_and_comes_back(scene: Scene):
    dev = await scene.device("bluos/node_n130", reboot_downtime_s=0.4)
    await get(dev, "/Volume", level=60)
    async with __import__("aiohttp").ClientSession() as s, s.post(dev.base_url + "/reboot") as r:
        assert r.status == 200
        assert "Rebooting" in await r.text()
    await asyncio.sleep(0.2)
    with pytest.raises(Exception):  # noqa: B017  (connection refused)
        await get(dev, "/Status", client_timeout=1)
    await scene.wait_for(lambda: not dev._down, 5, "back up")
    st = await get(dev, "/Status")
    assert st.field("state") == "stop"
    assert st.field("volume") == "60"  # volume survives a reboot


async def test_url_plays_through_the_sink(scene: Scene):
    """A paced stream (as MA sends) plays; the buffered tail is dropped on close (#6872)."""
    dev = await scene.device("bluos/node_n130", **FAST)
    await scene.media_server()
    t = scene.media.track(1, 3.0)
    await get(dev, "/Play", url=scene.media_url(t.path, route="stream", rate=1, chunked=0))
    await scene.wait_for(lambda: events(dev, "first_audio", kind="sink"), 5, "audio")
    assert (await get(dev, "/Status")).field("state") == "stream"
    await scene.wait_for(lambda: events(dev, "heard", kind="sink"), 10, "heard")
    heard = events(dev, "heard", kind="sink")[0]
    assert heard["received_s"] > 2.9
    assert 1.0 < heard["played_s"] < 2.9
    assert heard["reason"] == "dropped"
    assert (await get(dev, "/Status")).field("state") == "stop"
