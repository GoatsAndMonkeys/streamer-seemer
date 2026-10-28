"""End-to-end: a real Music Assistant server drives simulated WiiM / LinkPlay devices.

Skipped unless ``MA_REPO`` points at a Music Assistant checkout with its own ``.venv``
(Python 3.14 with the ``wiim`` SDK). Each scenario runs in a subprocess with MA's
interpreter and ``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095), with a throwaway
  data dir; its mDNS, SSDP search and provider discovery are switched off. The device is
  handed to the wiim provider's discovery entry point (``_discover_device``) with the
  sim's description URL, so MA probes it and routes it by manufacturer/model as it does
  with a device found on the LAN. The SDK's HTTP API endpoint is pointed at the sim's
  HTTPS port (real units use 443).
- ``smoke`` (wiim_pro): register; play a tone track (the sim's sink receives MA's
  stream); pause, seek, volume, each read back on both sides; stop.
- ``bug6867`` (wiim_pro + ``stale_prev_position``): after a seek MA's elapsed time jumps
  ahead to where the previous stream was (MA#6867, open on dev).
- ``bug6897`` (wiim_amp + ``input_switch_without_upnp_event``): the TV turns on (HDMI
  ARC) while MA plays; MA should stop claiming the player for its queue (MA#6897, open
  on dev).

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/linkplay_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/linkplay_ma_smoke.py smoke
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

    def test_ma_drives_wiim_device():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["backend"] == "WiimPlayer", r
        assert r["playing"]["device"] == "playing", r
        assert r["playing"]["ma"] == "playing", r
        assert r["playing"]["sink_bytes"] > 0, r
        assert r["paused"]["device"] == "paused", r
        assert r["paused"]["ma"] == "paused", r
        assert abs(r["seek"]["ma_elapsed"] - 30) < 4, r
        assert r["seek"]["device"] == "playing", r
        assert r["volume"]["device"] == 33, r
        assert r["volume"]["ma"] == 33, r
        assert r["stopped"]["device"] in ("stopped", "no_media"), r

    def test_bug_6867_no_jump_ahead_after_seek():
        r = _run_scenario("bug6867")
        assert r["stale_served"], r  # the sim really served the stale position
        assert r["worst_ahead_s"] < 3, (
            "MA's elapsed time jumped to the previous stream's position after a seek (#6867)",
            r,
        )

    def test_bug_6897_tv_input_releases_the_queue():
        r = _run_scenario("bug6897")
        assert r["device"]["mode"] == "49", r  # the sim is on HDMI, HTTP says so
        assert r["after"]["active_source"] != r["queue_id"], (
            "MA still shows its queue as the source 20 s after the TV took the input (#6897)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------

PROFILES = {
    "smoke": ("linkplay/wiim_pro", {}),
    "bug6867": (
        "linkplay/wiim_pro",
        # reporter's captures: ~0.8 s STOPPED, then TRANSITIONING at the old position
        {
            "stale_prev_position": {"p": 1.0, "window_s": 0.6},
            "play_start_delay_s": 0.8,
            "load_delay_s": 0.4,
        },
    ),
    "bug6897": ("linkplay/wiim_amp", {"input_switch_without_upnp_event": True}),
}


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
    import shutil
    import tempfile
    import time

    import wiim.discovery
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

    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-wiim-{name}-"))
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
                    "wiim": {
                        "type": "player",
                        "domain": "wiim",
                        "instance_id": "wiim",
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
    logging.getLogger("music_assistant.providers.wiim").setLevel(logging.DEBUG)
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
            api_port = int(dev.api_url.rsplit(":", 1)[1])
            orig = wiim.discovery.async_create_http_api_endpoint

            async def _endpoint(host: str | None, **_k: Any) -> Any:
                return await orig(host, protocol="https", port=api_port)

            wiim.discovery.async_create_http_api_endpoint = _endpoint  # type: ignore[assignment]
            location = f"{dev.base_url}/description.xml"
            log(f"device {dev.udn} UPnP {dev.base_url}, API {dev.api_url}")
            await mass.start()
            prov = mass.get_provider("wiim")
            assert prov is not None, "wiim provider did not load"
            pid = f"wiim_{dev.udn}"
            await prov._discover_device("127.0.0.1", dev.identity["name"], (location,))  # type: ignore[attr-defined]

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 30)
            result["registered"] = ok
            if not ok:
                return result
            result["backend"] = type(player()).__name__
            log(f"player registered: {player().display_name} ({result['backend']})")
            await scene.media_server()
            track = scene.media.track(1, 300.0)
            url = scene.media_url(track.path)
            await mass.player_queues.play_media(pid, url)
            playing = await until(lambda: dev.transport.state.value == "playing", 30)
            result["queue_id"] = pid
            if not playing:
                result["error"] = "device never reached PLAYING"
                return result

            if name == "smoke":
                await _smoke(mass, dev, pid, player, until, log, result)
            elif name == "bug6867":
                await _bug6867(mass, dev, pid, until, log, result)
            elif name == "bug6897":
                await _bug6897(mass, dev, pid, log, result)
            result["httpapi_commands"] = sorted(
                {
                    str(e.data.get("query", {}).get("command", "")).split(":")[0]
                    for e in scene.rec.events
                    if e.kind == "cmd" and e.data.get("path") == "/httpapi.asp"
                }
            )
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


async def _smoke(mass, dev, pid, player, until, log, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    await asyncio.sleep(3)
    sink = dev.sink.snapshot()
    result["playing"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
        "sink_bytes": sink["bytes"],
        "sink_played_s": sink["played_s"],
        "sink_decoder": sink["decoder"],
        "stream_url": sink["url"],
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


async def _bug6867(mass, dev, pid, until, log, result) -> None:
    import asyncio
    import time

    queue = mass.player_queues.get(pid)
    stale_before = sum(1 for _ in _fired(dev, "stale_prev_position"))
    rows = []
    for played, target in ((8, 120), (12, 200), (6, 90)):
        await asyncio.sleep(played)
        t_seek = time.time()
        await mass.player_queues.seek(pid, target)
        worst = 0.0
        while time.time() - t_seek < 3:
            worst = max(worst, queue.corrected_elapsed_time - (target + time.time() - t_seek))
            await asyncio.sleep(0.02)
        rows.append({"played_before": played, "target": target, "max_ahead_s": round(worst, 1)})
        log(f"seek {rows[-1]}")
        await until(lambda: dev.transport.state.value == "playing", 15)
    result["seeks"] = rows
    result["worst_ahead_s"] = max(r["max_ahead_s"] for r in rows)
    result["stale_served"] = sum(1 for _ in _fired(dev, "stale_prev_position")) > stale_before


def _fired(dev, knob: str):
    return (
        e
        for e in dev.rec.rec.events
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    )


async def _bug6897(mass, dev, pid, log, result) -> None:
    import asyncio

    def sample() -> dict[str, Any]:
        state = mass.players.get_player_state(pid)
        media = state.current_media
        return {
            "active_source": state.active_source,
            "playback_state": str(state.playback_state),
            "media_title": media.title if media else None,
        }

    await asyncio.sleep(6)
    result["before"] = sample()
    log(f"before TV on: {result['before']}")
    await dev.switch_source("hdmi")
    await asyncio.sleep(20)
    result["after"] = sample()
    st = dev.player_status()
    result["device"] = {"mode": st["mode"], "status": st["status"], "owner": dev.transport.owner}
    log(f"20 s after TV on: {result['after']} device {result['device']}")


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
