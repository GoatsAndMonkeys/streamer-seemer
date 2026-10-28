"""End-to-end: a real Music Assistant server drives a simulated Chromecast.

Skipped unless ``MA_REPO`` points at a Music Assistant checkout with its own ``.venv``
(it has pychromecast). Each scenario runs in a subprocess with MA's interpreter and
``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095), with a throwaway
  data dir; its mDNS, SSDP search and provider discovery, the cast provider's LAN
  ``CastBrowser`` and Sendspin's mDNS are switched off.
- Discovery workaround: the sim is not found over mDNS (that would put it on the LAN).
  A ``CastInfo`` for it is put into the chromecast provider's ``CastBrowser.devices``
  and the provider's own discovery callback (``_on_chromecast_discovered``) is called,
  so MA runs the same eureka_info probes and pychromecast connect as for a real device.
  pychromecast's dial module has fixed ports (8008/8443); they are pointed at the
  sim's HTTP/HTTPS ports.
- ``smoke`` (nest_mini): register; play a tone track (the sim's sink receives MA's
  flow stream); pause, seek, volume, each read back on both sides; stop.
- ``bug6658`` (nest_mini): a three-track queue; in the last track the device sends the
  C30/C46 error shape (ERROR 102 twice, then IDLE/ERROR). MA must not go back and play
  earlier tracks again (#6658/#6895).
- ``x5`` (nest_mini + ``range_rerequest_after_bytes``): the device asks MA's stream
  again with ``Range: bytes=N-`` (Samsung, #6775/#6106). MA answers 200 from the start,
  the device drops the item.
- ``bug6695`` (cast_group + its two member speakers): an announcement on a Cast group
  whose last 1.5 s are never played (C28/C42). Does the announcement's audio survive the
  cut? (Without members known to MA, MA plays nothing on the group.)

Known MA bugs are reported as xfail: the test passes again once MA is fixed.

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/chromecast_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/chromecast_ma_smoke.py smoke
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
MA_REPO = os.environ.get("MA_REPO")
FORBIDDEN_PORTS = {8095, 8097, 8927, 8928}


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS and port > 20000:
            return port


# -- pytest side (seamer's venv) ------------------------------------------------------


def _run_scenario(name: str, timeout: float = 420) -> dict[str, Any]:
    assert MA_REPO is not None
    python = os.environ.get("MA_PYTHON") or str(Path(MA_REPO) / ".venv" / "bin" / "python")
    env = dict(os.environ, PYTHONPATH=f"{MA_REPO}{os.pathsep}{REPO}")
    proc = subprocess.run(
        [python, str(Path(__file__).resolve()), name],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    if not lines:
        raise AssertionError(
            f"scenario {name} gave no result (exit {proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n--- stderr ---\n{proc.stderr[-4000:]}"
        )
    result = json.loads(lines[-1].removeprefix("RESULT "))
    print(json.dumps(result, indent=1))
    return result


if "pytest" in sys.modules:
    import pytest

    pytestmark = pytest.mark.skipif(not MA_REPO, reason="set MA_REPO to a Music Assistant tree")

    def test_ma_drives_chromecast():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["backend"] == "ChromecastPlayer", r
        assert r["eureka_probes"], r  # MA asked the sim's eureka_info (cast type, MAC)
        assert r["playing"]["device"] == "PLAYING", r
        assert r["playing"]["ma"] == "playing", r
        assert r["playing"]["sink_bytes"] > 0, r
        assert r["playing"]["sink_played_s"] > 1, r
        assert r["playing"]["app"] in ("C35B0678", "CC1AD845"), r
        assert r["paused"]["device"] == "PAUSED", r
        assert r["paused"]["ma"] == "paused", r
        assert abs(r["seek"]["ma_elapsed"] - 30) < 4, r
        assert r["seek"]["device"] == "PLAYING", r
        assert r["volume"]["device"] == pytest.approx(0.33, abs=0.011), r
        assert r["volume"]["ma"] == 33, r
        assert r["stopped"]["device"] == "IDLE", r
        assert r["stopped"]["ma"] in ("idle", "PlaybackState.IDLE"), r

    @pytest.mark.parametrize("scenario", ["bug6658", "bug6658_idle"])
    def test_bug_6658_no_replay_after_error_near_the_end(scenario):
        r = _run_scenario(scenario)
        assert r["error_sent"], r  # the sim really sent the C30 error shape
        assert r["in_last_track_at_error"], r
        assert not r["replayed"], (
            "after an IDLE/ERROR in the last track MA went back to earlier tracks (#6658)",
            r,
        )
        if r["ma_index_after"] < r["ma_index_at_error"]:
            # open MA bug: the queue pointer jumps back to item 0 and the rest of the last
            # track is never played (no new LOAD, MA idle)
            pytest.xfail(f"MA reset the queue to item {r['ma_index_after']} (#6658): {r}")

    def test_x5_range_rerequest():
        r = _run_scenario("x5")
        assert r["range"] is not None, r  # the sim re-requested with Range
        # MA's stream server has no Range support: it answers 200 from the start, and a
        # Samsung-like receiver drops the item (X5). A 206 would mean MA added Range.
        if r["range"]["status"] != 206:
            assert r["idle_reason"] == "ERROR", r
            pytest.xfail(f"MA ignored Range, the device dropped the item (X5): {r}")

    def test_bug_6695_group_announcement_tail():
        r = _run_scenario("bug6695")
        assert r["registered"], r
        assert len(r["group_members"]) == 2, r
        assert r["clip_fired"], r
        if r["announcement_heard_s"] < r["announcement_s"] - 0.3:
            pytest.xfail(f"the end of the announcement was cut on the Cast group (#6695): {r}")


# -- scenario side (MA's venv) --------------------------------------------------------

PROFILES: dict[str, tuple[str, dict[str, Any]]] = {
    "smoke": ("chromecast/nest_mini", {}),
    # C46: the C30 shape (ERROR 102 x2, IDLE/ERROR + LOADING next.mp3, GET next.mp3)
    # 80 s before the end of the last track, while MA still generates the flow
    "bug6658": ("chromecast/nest_mini", {}),
    # the same error, but the device stays IDLE/ERROR, after MA's flow buffer completed
    # (MA's resume-on-idle path)
    "bug6658_idle": ("chromecast/nest_mini", {"spontaneous_error": {"then": "idle"}}),
    "x5": ("chromecast/nest_mini", {"range_rerequest_after_bytes": 1_500_000}),
    "bug6695": ("chromecast/cast_group", {}),
}
# member speakers simulated next to a group (their uuids are in cast_group's members)
MEMBERS: dict[str, tuple[str, ...]] = {"bug6695": ("nest_mini", "google_home_speaker")}


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
    import shutil
    import tempfile
    import time
    import uuid

    import pychromecast.dial
    from music_assistant.constants import DEFAULT_PROVIDERS
    from music_assistant.controllers.discovery.controller import DiscoveryController
    from music_assistant.mass import MusicAssistant
    from pychromecast.models import CastInfo, HostServiceInfo

    from seamer.families import build_device
    from seamer.simkit.scenario import Scene

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    # keep the throwaway server off the LAN: no _mass._tcp registration, no mDNS
    # browsing, no SSDP search (the device is handed to the provider below)
    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]
    # the cast provider's own discover_players() starts pychromecast's CastBrowser,
    # which would browse the LAN for real Cast devices and connect to them
    from music_assistant.providers.chromecast.provider import ChromecastProvider

    ChromecastProvider.discover_players = _noop  # type: ignore[method-assign]
    # the Sendspin provider is builtin: it binds 8927 (the user's MA owns that port) and
    # advertises itself over mDNS
    import music_assistant.providers.sendspin.provider as sendspin_provider
    from aiosendspin.server.server import SendspinServer

    sendspin_provider.SENDSPIN_SERVER_PORT = free_port()  # type: ignore[misc]
    SendspinServer._start_mdns_advertising = _noop  # type: ignore[method-assign]
    SendspinServer._start_mdns_discovery = _noop  # type: ignore[method-assign]

    web_port, stream_port = free_port(), free_port()
    assert not {web_port, stream_port} & FORBIDDEN_PORTS

    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-cast-{name}-"))
    data_dir = work / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(
        json.dumps(
            {
                "default_providers_setup": sorted(n for n, _ in DEFAULT_PROVIDERS),
                "core": {
                    "webserver": {
                        "domain": "webserver",
                        "values": {"bind_port": web_port, "bind_ip": "127.0.0.1"},
                    },
                    "streams": {
                        "domain": "streams",
                        "values": {
                            "bind_port": stream_port,
                            "bind_ip": "127.0.0.1",
                            "publish_ip": "127.0.0.1",
                        },
                    },
                    "discovery": {
                        "domain": "discovery",
                        "values": {"zeroconf_interfaces": "default", "upnp_network_scan": False},
                    },
                },
                "providers": {
                    "chromecast": {
                        "type": "player",
                        "domain": "chromecast",
                        "instance_id": "chromecast",
                        "enabled": True,
                        "values": {},
                    }
                },
            }
        )
    )
    logging.basicConfig(
        level=logging.INFO,
        filename=work / "server.log",
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("music_assistant.providers.chromecast").setLevel(logging.DEBUG)
    logging.getLogger("music_assistant.player_queues").setLevel(logging.DEBUG)
    t0 = time.monotonic()

    def log(msg: str) -> None:
        print(f"[{name} {time.monotonic() - t0:6.1f}s] {msg}", flush=True)

    async def until(pred: Any, timeout: float, poll: float = 0.1) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(poll)
        return bool(pred())

    profile, knobs = PROFILES[name]
    result: dict[str, Any] = {"scenario": name, "profile": profile, "knobs": knobs}
    mass = MusicAssistant(str(data_dir), str(work / "cache"))
    try:
        async with Scene(work / "scene", name=name) as scene:
            # pychromecast types any device whose cast port is not 8009 as a group
            # (dial.get_cast_type), so a single speaker needs the real port
            cast_port = 0 if "group" in profile else (8009 if _port_free(8009) else 0)
            result["cast_port_8009"] = cast_port == 8009
            dev: Any = build_device(
                "chromecast",
                profile.partition("/")[2],
                recorder=scene.rec,
                clock=scene.clock,
                seed=scene.seed,
                device_id="cast1",
                overrides=knobs or None,
                cast_port=cast_port,
            )
            scene.devices.append(dev)
            await dev.start()
            http_port = int(dev.base_url.rsplit(":", 1)[1])
            # pychromecast probes eureka_info on fixed ports; aim them at the sim
            pychromecast.dial.FORMAT_BASE_URL_HTTP = f"http://{{}}:{http_port}"  # type: ignore[attr-defined]
            pychromecast.dial.FORMAT_BASE_URL_HTTPS = f"https://{{}}:{dev.eureka_https_port}"  # type: ignore[attr-defined]
            log(f"device {dev.uuid_hex} cast {dev.cast.port} http {http_port}")
            await mass.start()
            prov: Any = mass.get_provider("chromecast")
            assert prov is not None, "chromecast provider did not load"
            cast_uuid = uuid.UUID(dev.uuid_hex)
            pid = str(cast_uuid)
            # what CastBrowser would hold after mDNS: no cast type / manufacturer, so MA
            # fills them in over HTTP like it does for a freshly found device
            prov.browser.devices[cast_uuid] = CastInfo(
                {HostServiceInfo("127.0.0.1", dev.cast.port)},
                cast_uuid,
                dev.model_name,
                dev.friendly_name,
                "127.0.0.1",
                dev.cast.port,
                # mDNS carries no cast type; MA asks eureka_info, except when the sim
                # could not get port 8009 and pychromecast would call it a group
                None if cast_port == 8009 or "group" in profile else dev.cast_type,
                None if cast_port == 8009 or "group" in profile else dev.identity["manufacturer"],
            )
            await asyncio.to_thread(prov._on_chromecast_discovered, cast_uuid, "")
            # a Cast group's announcement goes to its members when MA knows none of
            # them, so the group's member speakers are simulated as well
            for i, member in enumerate(MEMBERS.get(name, ()), start=2):
                mdev: Any = build_device(
                    "chromecast",
                    member,
                    recorder=scene.rec,
                    clock=scene.clock,
                    seed=scene.seed,
                    device_id=f"cast{i}",
                )
                scene.devices.append(mdev)
                await mdev.start()
                muuid = uuid.UUID(mdev.uuid_hex)
                prov.browser.devices[muuid] = CastInfo(
                    {HostServiceInfo("127.0.0.1", mdev.cast.port)},
                    muuid,
                    mdev.model_name,
                    mdev.friendly_name,
                    "127.0.0.1",
                    mdev.cast.port,
                    mdev.cast_type,
                    mdev.identity["manufacturer"],
                )
                await asyncio.to_thread(prov._on_chromecast_discovered, muuid, "")
                log(f"member {mdev.friendly_name} cast {mdev.cast.port}")

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 30)
            result["registered"] = ok
            if not ok:
                return result
            result["backend"] = type(player()).__name__
            result["eureka_probes"] = sorted(
                {
                    json.dumps(e.data.get("query", {}), sort_keys=True)
                    for e in scene.rec.events
                    if e.kind == "cmd" and e.name == "eureka_info"
                }
            )
            log(f"player registered: {player().display_name} ({result['backend']})")
            if name in MEMBERS:
                await until(lambda: len(list(mass.players.iter_group_members(player()))) >= 2, 20)
                result["group_members"] = [
                    m.display_name for m in mass.players.iter_group_members(player())
                ]
            await scene.media_server()

            if name == "smoke":
                await _smoke(mass, scene, dev, pid, player, until, log, result)
            elif name.startswith("bug6658"):
                await _bug6658(mass, scene, dev, pid, until, log, result, name == "bug6658")
            elif name == "x5":
                await _x5(mass, scene, dev, pid, until, log, result)
            elif name == "bug6695":
                await _bug6695(mass, scene, dev, pid, until, log, result)
            result["media_commands"] = sorted(
                {
                    str(e.name).removeprefix("cast:")
                    for e in scene.rec.events
                    if e.kind == "cmd" and str(e.name).startswith("cast:")
                }
            )
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _fired(dev: Any, knob: str) -> list[Any]:
    return [
        e
        for e in dev.rec.rec.events
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    ]


async def _smoke(mass, scene, dev, pid, player, until, log, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    track = scene.media.track(1, 300.0)
    await mass.player_queues.play_media(pid, scene.media_url(track.path))
    await until(lambda: dev.player_state() == "PLAYING", 30)
    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    await asyncio.sleep(3)
    sink = dev.sink.snapshot()
    result["playing"] = {
        "device": dev.player_state(),
        "ma": str(player().playback_state.value),
        "app": dev.app.app_id if dev.app else None,
        "sink_bytes": sink["bytes"],
        "sink_played_s": sink["played_s"],
        "sink_decoder": sink["decoder"],
        "stream_url": sink["url"],
    }
    log(f"playing: {result['playing']}")

    await mass.player_queues.pause(pid)
    await until(
        lambda: dev.player_state() == "PAUSED" and player().playback_state == PlaybackState.PAUSED,
        10,
    )
    result["paused"] = {"device": dev.player_state(), "ma": str(player().playback_state.value)}
    log(f"paused: {result['paused']}")
    await mass.player_queues.play(pid)
    await until(lambda: dev.player_state() == "PLAYING", 10)

    await mass.player_queues.seek(pid, 30)
    await asyncio.sleep(0.5)
    await until(lambda: dev.player_state() == "PLAYING", 15)
    await asyncio.sleep(2)
    queue = mass.player_queues.get(pid)
    result["seek"] = {
        "target": 30,
        "ma_elapsed": round(queue.corrected_elapsed_time - 2, 1),
        "device": dev.player_state(),
        "device_reported_s": round(dev.lens.position() or 0.0, 1),
    }
    log(f"seek: {result['seek']}")

    await mass.players.cmd_volume_set(pid, 33)
    await until(lambda: abs(dev.volume_level - 0.33) < 0.011 and player().volume_level == 33, 10)
    result["volume"] = {"device": dev.volume_level, "ma": player().volume_level}
    log(f"volume: {result['volume']}")

    await mass.player_queues.stop(pid)
    await until(lambda: dev.player_state() == "IDLE", 10)
    await until(lambda: player().playback_state == PlaybackState.IDLE, 10)
    result["stopped"] = {
        "device": dev.player_state(),
        "idle_reason": dev.media.idle_reason if dev.media else None,
        "ma": str(player().playback_state.value),
    }
    log(f"stopped: {result['stopped']}")


async def _bug6658(mass, scene, dev, pid, until, log, result, early: bool) -> None:
    """C46: the C30 error shape in the last track (early: 80 s before its end)."""
    import asyncio
    import time

    lengths = (20.0, 20.0, 90.0) if early else (25.0, 25.0, 25.0)
    tracks = [scene.media.track(i, n) for i, n in zip((1, 2, 3), lengths, strict=True)]
    await mass.player_queues.play_media(pid, [scene.media_url(t.path) for t in tracks])
    queue = mass.player_queues.get(pid)
    await until(lambda: dev.player_state() == "PLAYING", 30)

    heard: list[int] = []

    def note() -> None:
        trk = dev.sink.snapshot()["track"]
        if trk is not None and (not heard or heard[-1] != trk):
            heard.append(trk)

    # play into the third track, past MA's end of flow generation
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        note()
        if heard and heard[-1] == 3 and (early or mass.player_queues.flow_stream_finished(pid)):
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(10 if early else 2)
    note()
    result["in_last_track_at_error"] = bool(heard) and heard[-1] == 3
    result["flow_finished_at_error"] = mass.player_queues.flow_stream_finished(pid)
    result["ma_index_at_error"] = queue.current_index
    loads_before = sum(1 for e in scene.rec.events if e.kind == "cmd" and e.name == "cast:LOAD")
    log(f"heard {heard}; MA index {queue.current_index}; sending the C30 error")
    result["error_sent"] = await dev.sim_action("spontaneous_error", {})

    # watch for MA replaying (resume-on-idle polls for 60 s, then waits 1 s)
    samples = []
    deadline = time.monotonic() + 70  # resume-on-idle polls 60 s, then waits 1 s
    while time.monotonic() < deadline:
        note()
        samples.append((queue.current_index, dev.player_state()))
        await asyncio.sleep(0.25)
    loads_after = sum(1 for e in scene.rec.events if e.kind == "cmd" and e.name == "cast:LOAD")
    result["heard_tracks"] = heard
    result["heard_s"] = dev.sink.snapshot()["heard"]
    result["last_track_s"] = lengths[-1]
    result["loads_after_error"] = loads_after - loads_before
    result["ma_index_after"] = queue.current_index
    result["device_after"] = dev.player_state()
    result["ma_state_after"] = str(queue.state.value)
    result["ma_indexes_seen_after"] = sorted({i for i, _ in samples if i is not None})
    result["next_mp3_fetched"] = any(
        "next.mp3" in str(e.data.get("url", "")) for e in scene.rec.events if e.kind == "sink"
    )
    after_error = heard[heard.index(3) :] if 3 in heard else heard
    result["replayed"] = any(t < 3 for t in after_error) or (
        result["loads_after_error"] > 0 and min(result["ma_indexes_seen_after"] or [2]) < 2
    )
    log(f"after: {result}")


async def _x5(mass, scene, dev, pid, until, log, result) -> None:
    import asyncio

    track = scene.media.track(1, 300.0)
    await mass.player_queues.play_media(pid, scene.media_url(track.path))
    await until(lambda: dev.player_state() == "PLAYING", 30)
    ok = await until(lambda: bool(_fired(dev, "range_rerequest_after_bytes")), 120, 0.25)
    fired = _fired(dev, "range_rerequest_after_bytes")
    result["range"] = dict(fired[-1].data) if ok else None
    await asyncio.sleep(3)
    queue = mass.player_queues.get(pid)
    result["device_after"] = dev.player_state()
    result["idle_reason"] = dev.media.idle_reason if dev.media else None
    result["ma_state_after"] = str(queue.state.value)
    result["stream_url"] = dev.sink.snapshot()["url"]
    log(f"x5: {result}")


async def _bug6695(mass, scene, dev, pid, until, log, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    announce = scene.media.track(5, 4.0)
    result["announcement_s"] = 4.0
    played_before = dev.sink.snapshot()["heard"].get(5, 0.0)
    await mass.players.play_announcement(pid, scene.media_url(announce.path))
    await until(lambda: dev.player_state() in ("PLAYING", "BUFFERING"), 20)
    await asyncio.sleep(1)
    await until(lambda: mass.players.get_player(pid).playback_state != PlaybackState.PLAYING, 30)
    await asyncio.sleep(2)
    heard = dev.sink.snapshot()["heard"]
    result["sink_heard"] = heard
    result["announcement_heard_s"] = round(heard.get(5, heard.get("5", 0.0)) - played_before, 2)
    result["clip_fired"] = bool(_fired(dev, "clip_tail_cut_ms"))
    result["device_after"] = dev.player_state()
    log(f"bug6695: {result}")


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
