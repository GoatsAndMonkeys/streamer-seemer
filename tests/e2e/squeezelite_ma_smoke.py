"""End-to-end: a real Music Assistant server drives simulated squeezelite players.

Skipped unless ``MA_REPO`` points at a Music Assistant checkout with its own ``.venv``
(Python 3.14 with ``aioslimproto``). Each scenario runs in a subprocess with MA's
interpreter and ``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095), with a throwaway
  data dir; mDNS and provider discovery are off. The squeezelite provider gets a free
  slimproto port (its UDP discovery binds that same port, not 3483) and
  ``cli_telnet_port``/``cli_json_port`` = 0, so no CLI listeners start. The sim player
  connects straight to the control port (like ``squeezelite -s host:port``).
- ``smoke`` (squeezelite_linux): MA accepts the player; play a tone track (the sim's sink
  receives MA's stream); pause, resume, seek, volume, each read back on both sides; stop.
- ``bug6359`` (squeezelite_esp32 + ``stmd_early_s``): a small-buffer player reports
  STMd (decoder ready) seconds into a track; MA must not cut that track short or stop
  after it (support#6359/#6666/#6614: "stops after the first track(s)").
- ``bug6160`` (squeezelite_linux): pause for 40 s, resume, let the track end. MA stops a
  queue paused for 30 s by design and resumes from the paused position (support#6614);
  it must then advance to the next track at the end (support#6160: "after a long pause,
  no auto-advance").
- ``shutdown_hang`` (squeezelite_linux): the player disconnects, then MA stops. Found
  while writing this test, no support issue: aioslimproto's reader marks the client
  disconnected before calling ``disconnect()``, which then returns early without closing
  the writer, so ``SlimServer.stop()`` waits forever in ``Server.wait_closed()``.
- ``bug3704`` (squeezelite_linux + ``seek_elapsed_from_zero=False``): firmware that keeps
  counting elapsed across the flush of a seek; MA's elapsed time after a seek must stay
  near the target (support#3704).

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/squeezelite_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/squeezelite_ma_smoke.py smoke
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
FORBIDDEN_PORTS = {8095, 8097, 8927, 8928, 3483, 9000, 9090}


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

    def test_ma_drives_squeezelite_player():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["playing"]["device"] == "playing", r
        assert r["playing"]["ma"] == "playing", r
        assert r["playing"]["sink_bytes"] > 0, r
        assert r["playing"]["sink_played_s"] > 1, r
        assert r["paused"]["device"] == "paused", r
        assert r["paused"]["ma"] == "paused", r
        assert r["resumed"]["device"] == "playing", r
        assert abs(r["seek"]["ma_elapsed"] - 30) < 4, r
        assert abs(r["seek"]["device_elapsed"] - 30) < 4, r
        assert r["volume"]["device"] == 33, r
        assert r["volume"]["ma"] == 33, r
        assert r["stopped"]["device"] in ("stopped", "no_media"), r
        assert r["ma_stop_completed"], r

    def test_bug_6359_early_stmd_does_not_cut_the_track():
        r = _run_scenario("bug6359")
        assert r["early_stmd_sent"], r  # the sim really sent STMd early
        assert r["heard"]["1"] > r["track_s"] - 2, (
            "MA started the next track on an early STMd and cut track 1 short (#6359)",
            r,
        )
        assert r["heard"].get("2", 0) > 3, ("playback stopped after the first track", r)

    def test_bug_6160_advance_after_long_pause():
        r = _run_scenario("bug6160")
        # MA stops a queue paused for 30 s on purpose (player_queues watch_pause) and
        # resumes it from the paused position: that is the #6614 behaviour, now by design
        assert r["after_30s"]["ma"] in ("paused", "idle"), r
        assert r["resumed"]["device"] == "playing", r
        assert 18 < r["heard"]["1"] < 23, ("resume did not continue track 1 where it was", r)
        assert r["advanced"], ("no auto-advance at track end after a long pause (#6160)", r)

    def test_ma_stops_after_player_disconnect():
        r = _run_scenario("shutdown_hang")
        assert r["unregistered"], r
        assert r["ma_stop_completed"], (
            "MA's stop hangs in SlimServer.stop() after a player disconnected",
            r,
        )

    def test_bug_3704_seek_offset():
        r = _run_scenario("bug3704")
        assert r["device_carried_ms"] > 5000, r  # the sim reported old+new elapsed
        assert abs(r["ma_elapsed"] - r["target"]) < 4, (
            "MA's elapsed after a seek adds the previous stream's time (#3704)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------

PROFILES: dict[str, tuple[str, dict[str, Any]]] = {
    "smoke": ("squeezelite/squeezelite_linux", {}),
    "bug6359": ("squeezelite/squeezelite_esp32", {"stmd_early_s": 3.0}),
    "bug6160": ("squeezelite/squeezelite_linux", {}),
    "bug3704": ("squeezelite/squeezelite_linux", {"seek_elapsed_from_zero": False}),
    "shutdown_hang": ("squeezelite/squeezelite_linux", {}),
}


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
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
    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]

    web_port, stream_port, slim_port = free_port(), free_port(), free_port()
    assert not {web_port, stream_port, slim_port} & FORBIDDEN_PORTS

    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-slim-{name}-"))
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
                    "squeezelite": {
                        "type": "player",
                        "domain": "squeezelite",
                        "instance_id": "squeezelite",
                        "enabled": True,
                        "values": {
                            "port": slim_port,
                            "cli_telnet_port": 0,
                            "cli_json_port": 0,
                            "discovery": False,
                        },
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
    logging.getLogger("music_assistant.providers.squeezelite").setLevel(logging.DEBUG)
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
    stopped = False

    async def stop_mass(timeout: float) -> bool:
        """Stop MA, giving up after ``timeout`` (see ``shutdown_hang``)."""
        nonlocal stopped
        stopped = True
        task = asyncio.ensure_future(mass.stop())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        return bool(done)

    try:
        async with Scene(work / "scene", name=name) as scene:
            await mass.start()
            assert mass.get_provider("squeezelite") is not None, "squeezelite did not load"
            dev = await scene.device(profile, **knobs)
            dev.connect("127.0.0.1", slim_port)
            pid = dev.mac
            log(f"player {pid} -> slimproto 127.0.0.1:{slim_port}")

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 30)
            result["registered"] = ok
            if not ok:
                return result
            result["backend"] = type(player()).__name__
            await scene.media_server()

            ctx = {
                "mass": mass,
                "dev": dev,
                "pid": pid,
                "player": player,
                "until": until,
                "log": log,
                "scene": scene,
                "result": result,
            }
            if name == "shutdown_hang":
                # the player goes away first (powered off, squeezelite restarted) ...
                await dev.disconnect()
                result["unregistered"] = await until(lambda: player() is None, 10)
                # ... then MA shuts down or reloads the provider
                result["ma_stop_completed"] = await stop_mass(20)
                log(f"MA stop completed: {result['ma_stop_completed']}")
                return result
            await {
                "smoke": _smoke,
                "bug6359": _bug6359,
                "bug6160": _bug6160,
                "bug3704": _bug3704,
            }[name](**ctx)
            result["rx_frames"] = sorted({e.name for e in scene.rec.events if e.kind == "cmd"})
            # stop MA while the player is still connected: aioslimproto then closes the
            # socket itself (after a player-side close it hangs, see shutdown_hang)
            result["ma_stop_completed"] = await stop_mass(60)
    finally:
        if not stopped:
            await stop_mass(60)
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


def _ma_state(player) -> str:
    return str(player().playback_state.value)


def _heard(dev) -> dict[str, float]:
    out: dict[str, float] = {}
    for e in dev.rec.rec.events:
        if e.kind == "sink" and e.name == "heard":
            for k, v in e.data.get("tracks", {}).items():
                out[k] = round(out.get(k, 0.0) + float(v), 2)
    if dev.sink is not None:
        for k, v in dev.sink.snapshot()["heard"].items():
            if not dev.sink._summary_sent:  # still playing: add the live sink
                out[k] = round(out.get(k, 0.0) + float(v), 2)
    return out


async def _play_tracks(mass, scene, pid, *secs: float) -> list[str]:
    from music_assistant_models.enums import QueueOption

    urls = [scene.media_url(scene.media.track(i + 1, s).path) for i, s in enumerate(secs)]
    await mass.player_queues.play_media(pid, urls, option=QueueOption.REPLACE)
    return urls


async def _smoke(mass, dev, pid, player, until, log, scene, result) -> None:
    import asyncio

    await _play_tracks(mass, scene, pid, 300.0)
    await until(lambda: dev.transport.state.value == "playing", 30)
    await until(lambda: _ma_state(player) == "playing", 15)
    await asyncio.sleep(3)
    sink = dev.sink.snapshot()
    result["playing"] = {
        "device": dev.transport.state.value,
        "ma": _ma_state(player),
        "sink_bytes": sink["bytes"],
        "sink_played_s": sink["played_s"],
        "stream_url": sink["url"],
    }
    log(f"playing: {result['playing']}")

    await mass.player_queues.pause(pid)
    await until(lambda: dev.transport.state.value == "paused" and _ma_state(player) == "paused", 10)
    result["paused"] = {"device": dev.transport.state.value, "ma": _ma_state(player)}
    log(f"paused: {result['paused']}")
    await mass.player_queues.play(pid)
    await until(
        lambda: dev.transport.state.value == "playing" and _ma_state(player) == "playing", 10
    )
    result["resumed"] = {"device": dev.transport.state.value, "ma": _ma_state(player)}

    await mass.player_queues.seek(pid, 30)
    await until(lambda: dev.transport.state.value == "playing", 15)
    await asyncio.sleep(2)
    queue = mass.player_queues.get(pid)
    result["seek"] = {
        "target": 30,
        "ma_elapsed": round(queue.corrected_elapsed_time - 2, 1),
        # MA seeks by restarting the stream at the target: device elapsed counts from 0
        "device_elapsed": round(30 + dev.elapsed_ms() / 1000 - 2, 1),
        "device": dev.transport.state.value,
    }
    log(f"seek: {result['seek']}")

    await mass.players.cmd_volume_set(pid, 33)
    await until(lambda: dev.volume == 33 and player().volume_level == 33, 10)
    result["volume"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"volume: {result['volume']}")

    await mass.player_queues.stop(pid)
    await until(lambda: dev.transport.state.value not in ("playing", "paused"), 10)
    result["stopped"] = {"device": dev.transport.state.value, "ma": _ma_state(player)}
    log(f"stopped: {result['stopped']}")


async def _bug6359(mass, dev, pid, player, until, log, scene, result) -> None:
    track_s = 15.0
    await _play_tracks(mass, scene, pid, track_s, 15.0)
    result["track_s"] = track_s
    await until(lambda: dev.transport.state.value == "playing", 30)
    tl = scene.rec.timeline
    await until(lambda: tl().first(kind="event", name="early_stmd") is not None, 10)
    result["early_stmd_sent"] = tl().first(kind="event", name="early_stmd") is not None
    # long enough for both tracks if nothing is cut
    await until(lambda: _heard(dev).get("2", 0) > 5, 2 * track_s + 15)
    result["heard"] = _heard(dev)
    result["device"] = dev.transport.state.value
    result["ma"] = _ma_state(player)
    log(f"heard {result['heard']}")


async def _bug6160(mass, dev, pid, player, until, log, scene, result) -> None:
    import asyncio

    await _play_tracks(mass, scene, pid, 20.0, 30.0)
    await until(lambda: dev.transport.state.value == "playing", 30)
    await asyncio.sleep(5)
    await mass.player_queues.pause(pid)
    await until(lambda: dev.transport.state.value == "paused", 10)
    await asyncio.sleep(32)
    result["after_30s"] = {"device": dev.transport.state.value, "ma": _ma_state(player)}
    log(f"after 32 s paused: {result['after_30s']}")
    await asyncio.sleep(8)
    await mass.player_queues.play(pid)
    await until(lambda: dev.transport.state.value == "playing", 10)
    result["resumed"] = {"device": dev.transport.state.value, "ma": _ma_state(player)}
    result["advanced"] = await until(lambda: _heard(dev).get("2", 0) > 3, 35)
    result["heard"] = _heard(dev)
    queue = mass.player_queues.get(pid)
    result["ma_index"] = queue.current_index
    result["final"] = {"device": dev.transport.state.value, "ma": _ma_state(player)}
    log(f"advanced={result['advanced']} heard={result['heard']} final={result['final']}")


async def _bug3704(mass, dev, pid, player, until, log, scene, result) -> None:
    import asyncio

    await _play_tracks(mass, scene, pid, 300.0)
    await until(lambda: dev.transport.state.value == "playing", 30)
    await asyncio.sleep(10)
    target = 100
    await mass.player_queues.seek(pid, target)
    await until(lambda: dev.stream is not None and dev.stream.carry_ms > 0, 10)
    await until(lambda: dev.transport.state.value == "playing", 15)
    await asyncio.sleep(4)
    queue = mass.player_queues.get(pid)
    result["target"] = target
    result["device_carried_ms"] = dev.stream.carry_ms if dev.stream else 0
    result["device_elapsed_ms"] = dev.elapsed_ms()
    result["ma_elapsed"] = round(queue.corrected_elapsed_time - 4, 1)
    log(f"seek to {target}: ma {result['ma_elapsed']} device {result['device_elapsed_ms']}")


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    # a hung MA stop leaves tasks that would block interpreter exit
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
