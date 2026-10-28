"""End-to-end: a real Music Assistant server drives simulated DLNA/UPnP renderers.

Skipped unless ``MA_REPO`` points at a Music Assistant checkout with its own ``.venv``.
Each scenario runs in a subprocess with MA's interpreter and
``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095), with a throwaway
  data dir; its mDNS, SSDP search and provider discovery are switched off. The device is
  handed to the dlna provider's discovery entry point (``_device_discovered``) with the
  sim's UDN and description URL, as an SSDP answer would. MA's GENA callback is its own
  stream server (``/notify`` on 127.0.0.1).
- ``smoke`` (eversolo): register; play (the sim's sink receives MA's stream); pause,
  seek, volume, each read back on both sides; stop.
- ``bug6775`` (lumin_teac, ``range_required=silent``): the renderer probes MA's stream
  with Range requests and needs 206. MA answers 200, so the renderer says PLAYING and
  counts time but plays nothing (MA#6775, open on dev).
- ``bug6762`` (eversolo, ``volume_before_play_ignored``): a volume set while the player
  is stopped is overridden by the amp when playback starts, and MA does not re-apply it
  (MA#6762, fix in open PR 6944).

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/dlna_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/dlna_ma_smoke.py smoke
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


def _run_scenario(name: str, timeout: float = 300) -> dict[str, Any]:
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

    def test_ma_drives_dlna_renderer():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["backend"] == "DLNAPlayer", r
        assert r["subscribed"], r
        assert r["playing"]["device"] == "playing", r
        assert r["playing"]["ma"] == "playing", r
        assert r["playing"]["sink_played_s"] > 1, r
        assert r["paused"]["device"] == "paused", r
        assert r["paused"]["ma"] == "paused", r
        assert abs(r["seek"]["ma_elapsed"] - 30) < 4, r
        assert r["seek"]["device"] == "playing", r
        assert r["volume"]["device"] == 33, r
        assert r["volume"]["ma"] == 33, r
        assert r["stopped"]["device"] in ("stopped", "no_media"), r

    def test_bug_6775_strict_range_renderer_plays_audio():
        r = _run_scenario("bug6775")
        assert r["registered"], r
        assert r["ma"] == "playing", r  # MA believes it plays...
        assert r["probes"], r
        assert r["sink_played_s"] > 1, (
            "the renderer reports PLAYING but received no audio: MA's stream server answers "
            "a Range request with 200 instead of 206 (#6775)",
            r,
        )

    def test_bug_6762_volume_set_before_play_sticks():
        r = _run_scenario("bug6762")
        assert r["registered"], r
        assert r["reverted"], r  # the sim's amp really put its own level back
        assert r["after"]["device"] == 45, (
            "volume set while stopped is lost once playback starts (#6762)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------

PROFILES = {
    "smoke": ("dlna/eversolo", {}),
    "bug6775": ("dlna/lumin_teac", {}),
    "bug6762": ("dlna/eversolo", {"volume_before_play_ignored": True}),
}


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
    import re
    import shutil
    import tempfile
    import time

    from music_assistant.constants import DEFAULT_PROVIDERS
    from music_assistant.controllers.discovery.controller import DiscoveryController
    from music_assistant.mass import MusicAssistant

    from seamer.simkit.scenario import Scene

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    # keep the throwaway server off the LAN: no _mass._tcp registration, no mDNS
    # browsing, no SSDP search (the device is handed to the provider below)
    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]

    web_port, stream_port = free_port(), free_port()
    assert not {web_port, stream_port} & FORBIDDEN_PORTS

    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-dlna-{name}-"))
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
                    "dlna": {
                        "type": "player",
                        "domain": "dlna",
                        "instance_id": "dlna",
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
    logging.getLogger("music_assistant.providers.dlna").setLevel(logging.DEBUG)
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
            dev = await scene.device(profile, **knobs)
            location = f"{dev.base_url}/description.xml"
            log(f"device {dev.udn} at {location}")
            await mass.start()
            prov = mass.get_provider("dlna")
            assert prov is not None, "dlna provider did not load"
            pid = dev.udn
            await prov._device_discovered(dev.udn, location)  # type: ignore[attr-defined]

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 30)
            result["registered"] = ok
            if not ok:
                return result
            result["backend"] = type(player()).__name__
            # dev MA registers a DLNA renderer as a protocol player and, after a grace
            # period, wraps it in a Universal Player; the queue lives on that wrapper
            await until(lambda: player().protocol_parent_id is not None, 40)
            qid = player().protocol_parent_id or pid
            await until(lambda: mass.player_queues.get(qid) is not None, 15)
            await until(lambda: bool(dev.gena.subs), 10)
            result["queue_id"] = qid
            result["wrapper"] = type(mass.players.get_player(qid)).__name__

            def player() -> Any:  # what MA's UI shows for the queue's player
                return mass.players.get_player_state(qid)

            result["subscribed"] = sorted({s.service for s in dev.gena.subs.values()})
            log(f"player registered: {player().name} ({result['backend']} in {result['wrapper']})")
            await scene.media_server()
            track = scene.media.track(1, 300.0)
            url = scene.media_url(track.path)
            pid = qid

            if name == "smoke":
                await _smoke(mass, dev, pid, url, player, until, log, result)
            elif name == "bug6775":
                await _bug6775(mass, dev, pid, url, player, until, log, result)
            elif name == "bug6762":
                await _bug6762(mass, dev, pid, url, player, until, log, result)
            result["soap_actions"] = sorted(
                {
                    m.group(1)
                    for e in scene.rec.events
                    if e.kind == "cmd" and str(e.data.get("path", "")).endswith("/control")
                    for m in [re.search(r"<u:(\w+)", str(e.data.get("body", "")))]
                    if m
                }
            )
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


def _sink(dev) -> dict[str, Any]:
    snap = dev.sink.snapshot()
    return {k: snap.get(k) for k in ("url", "bytes", "played_s", "decoder")}


async def _start(mass, dev, pid, url, until, log, result) -> bool:
    await mass.player_queues.play_media(pid, url)
    ok = await until(lambda: dev.lens.state().value == "playing", 30)
    if not ok:
        result["error"] = f"device never reported PLAYING ({dev.lens.state().value})"
    return ok


async def _smoke(mass, dev, pid, url, player, until, log, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    if not await _start(mass, dev, pid, url, until, log, result):
        return
    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    await asyncio.sleep(3)
    sink = _sink(dev)
    result["playing"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
        "sink_bytes": sink["bytes"],
        "sink_played_s": sink["played_s"],
        "sink_decoder": sink["decoder"],
        "stream_url": sink["url"],
        "stream_content_type": dev.stream_ctype,
        "stream_chunked": dev.stream_chunked,
    }
    log(f"playing: {result['playing']}")

    await mass.player_queues.pause(pid)
    await until(
        lambda: (
            dev.transport.state.value == "paused"
            and player().playback_state == PlaybackState.PAUSED
        ),
        10,
    )
    result["paused"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
    }
    log(f"paused: {result['paused']}")
    await mass.player_queues.play(pid)
    await until(lambda: dev.transport.state.value == "playing", 10)

    await mass.player_queues.seek(pid, 30)
    await until(lambda: dev.transport.state.value == "playing", 15)
    await asyncio.sleep(2)
    queue = mass.player_queues.get(pid)
    result["seek"] = {
        "target": 30,
        "ma_elapsed": round(queue.corrected_elapsed_time, 1) - 2,
        "device": dev.transport.state.value,
        "device_reported_s": round(dev.lens.position() or 0.0, 1),
    }
    log(f"seek: {result['seek']}")

    await mass.players.cmd_volume_set(pid, 33)
    await until(lambda: dev.volume == 33 and player().volume_level == 33, 10)
    result["volume"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"volume: {result['volume']}")

    await mass.player_queues.stop(pid)
    await until(lambda: dev.transport.state.value not in ("playing", "paused"), 10)
    result["stopped"] = {"device": dev.transport.state.value, "ma": str(player().playback_state)}


async def _bug6775(mass, dev, pid, url, player, until, log, result) -> None:
    import asyncio

    if not await _start(mass, dev, pid, url, until, log, result):
        return
    await asyncio.sleep(6)
    result["ma"] = str(player().playback_state.value)
    result["ma_elapsed"] = round(mass.player_queues.get(pid).corrected_elapsed_time, 1)
    result["device_reported"] = dev.lens.state().value
    result["device_reported_s"] = round(dev.lens.position() or 0.0, 1)
    result["fake_play"] = dev.fake_play is not None
    result["probes"] = dev.range_probes
    sink = _sink(dev)
    result["sink_played_s"] = sink["played_s"] or 0.0
    result["sink_bytes"] = sink["bytes"] or 0
    log(f"6775: {result}")
    await mass.player_queues.stop(pid)


async def _bug6762(mass, dev, pid, url, player, until, log, result) -> None:
    import asyncio

    await mass.players.cmd_volume_set(pid, 45)
    await until(lambda: dev.volume == 45, 10)
    result["before"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"before play: {result['before']}")
    if not await _start(mass, dev, pid, url, until, log, result):
        return
    await asyncio.sleep(6)
    result["after"] = {"device": dev.volume, "ma": player().volume_level}
    result["reverted"] = any(
        e.kind == "event" and e.name == "volume_reverted" for e in dev.rec.rec.events
    )
    log(f"after play: {result['after']} reverted={result['reverted']}")
    await mass.player_queues.stop(pid)


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
