"""End-to-end: a real Music Assistant server drives simulated Sendspin devices.

Skipped unless ``MA_REPO`` points at a Music Assistant checkout that has its own
``.venv`` (Python 3.14 with aiosendspin). Each scenario runs in a subprocess with MA's
interpreter and ``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095), with a throwaway
  data dir. Its own mDNS (and aiosendspin's) is switched off; the device is reached
  through the provider's "manual discovery IPs" entry, as a user would enter it.
- ``smoke``: MA dials the device, approves it (guest access), and the player plays a
  tone track into the fake sink. Then pause, seek, volume, each read back on both sides.
- ``bug6888``: python-cli restarts with goodbye ``shutdown`` (Ctrl-C / systemd stop).
  MA should dial it again once it is back. Control: the same with ``restart``.
  ``bug6888_user_request`` is the same with goodbye ``user_request``.
- ``bug6887``: the device says goodbye ``another_server``. The player should stay
  listed, not disappear until the provider is reloaded. On MA dev (cdff463ec) this
  passes (held-elsewhere handling plus aiosendspin's reclaim), so it is a regression guard.

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/sendspin_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/sendspin_ma_smoke.py smoke
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

    def test_ma_drives_sendspin_device():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["playing"] == {
            "ok": True,
            "device": "playing",
            "ma": "playing",
            "queue": "playing",
        }
        assert r["played_chunks"] > 0, r
        assert r["header_sizes"] == [13], r
        assert r["paused"]["device"] != "playing", r
        assert r["paused"]["ma"] != "playing", r
        assert r["resumed"], r
        assert r["seek"]["device_new_stream"], r
        assert 29 <= r["seek"]["ma_elapsed"] < 40, r
        assert r["volume"]["device"] == 33, r
        assert r["volume"]["ma"] == 33, r
        assert r["local_volume"]["ma"] == 61, r

    def test_bug_6888_redial_after_shutdown_goodbye():
        r = _run_scenario("bug6888")
        assert r["control_restart"]["redialed"], r  # the harness itself works
        assert r["shutdown"]["redialed"], (
            "MA did not dial the device again after a 'shutdown' goodbye (#6888)",
            r,
        )

    def test_bug_6888_redial_after_user_request_goodbye():
        # same mechanism: only a "restart" goodbye keeps MA's reconnect loop alive
        r = _run_scenario("bug6888_user_request")
        assert r["control_restart"]["redialed"], r
        assert r["user_request"]["redialed"], (
            "MA did not dial the device again after a 'user_request' goodbye (#6888)",
            r,
        )

    def test_bug_6887_player_usable_after_another_server():
        r = _run_scenario("bug6887")
        assert r["before"]["visible_listed"], r
        assert r["after"]["visible_listed"], r
        assert r["reclaim"]["reclaimed"], (
            "after an 'another_server' goodbye MA never takes the device back on play (#6887)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
    import shutil
    import tempfile
    import time

    import music_assistant.providers.sendspin.provider as ss_provider
    from aiosendspin.server.server import SendspinServer
    from music_assistant.controllers.discovery.controller import DiscoveryController
    from music_assistant.mass import MusicAssistant
    from zeroconf import IPVersion
    from zeroconf.asyncio import AsyncZeroconf

    from seamer.simkit.scenario import Scene

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    # keep the throwaway server off the LAN: no MA or aiosendspin mDNS at all
    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]
    DiscoveryController._create_aiozc = lambda self, config: AsyncZeroconf(  # type: ignore[method-assign]
        interfaces=["127.0.0.1"], ip_version=IPVersion.V4Only
    )
    SendspinServer._start_mdns_advertising = _noop  # type: ignore[method-assign]
    SendspinServer._start_mdns_discovery = _noop  # type: ignore[method-assign]

    web_port, stream_port, ss_port = free_port(), free_port(), free_port()
    assert not {web_port, stream_port, ss_port} & FORBIDDEN_PORTS
    ss_provider.SENDSPIN_SERVER_PORT = ss_port  # MA's Sendspin listener (8927 normally)

    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-{name}-"))
    data_dir = work / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(
        json.dumps(
            {
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
                }
            }
        )
    )
    logging.basicConfig(
        level=logging.INFO,
        filename=work / "server.log",
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("music_assistant.providers.sendspin").setLevel(logging.DEBUG)
    t0 = time.monotonic()

    def log(msg: str) -> None:
        print(f"[{name} {time.monotonic() - t0:6.1f}s] {msg}", flush=True)

    async def until(pred: Any, timeout: float, poll: float = 0.25) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(poll)
        return bool(pred())

    profile = {
        "smoke": "sendspin/esphome_s3_psram",
        "bug6888": "sendspin/python_cli_pi_zero2w",
        "bug6888_user_request": "sendspin/python_cli_pi_zero2w",
        "bug6887": "sendspin/esphome_s3_psram",
    }[name]
    profile = os.environ.get("SEAMER_PROFILE", profile)
    result: dict[str, Any] = {"scenario": name, "profile": profile}
    mass = MusicAssistant(str(data_dir), str(work / "cache"), safe_mode=True)
    try:
        async with Scene(work / "scene", name=name) as scene:
            dev = await scene.device(profile, restart_down_s=3.0)
            ws_url = f"ws://127.0.0.1:{dev.port}/sendspin"
            log(f"device {dev.client_id[:12]} at {ws_url}")
            await mass.start()
            prov = mass.get_provider("sendspin")
            assert prov is not None, "sendspin provider not loaded"
            # what a user types into "manual discovery IPs"; saving reloads the provider
            await mass.config.save_provider_config(
                "sendspin",
                {"manual_discovery_ip_addresses": [ws_url]},
                instance_id=prov.instance_id,
            )
            pid = dev.client_id

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 30)
            result["registered"] = ok
            result["sessions_at_register"] = dev.sessions
            if not ok:
                return result
            log(f"player registered: {player().display_name}")

            if name == "smoke":
                await _smoke(mass, scene, dev, pid, player, until, log, result)
            elif name.startswith("bug6888"):
                bad = "user_request" if name.endswith("user_request") else "shutdown"
                for reason, key in (("restart", "control_restart"), (bad, bad)):
                    before = dev.sessions
                    await dev.restart(reason, 3.0)
                    log(f"device back after goodbye {reason}; waiting for MA to dial")
                    redialed = await until(lambda b=before: dev.sessions > b and dev.connected, 45)
                    result[key] = {
                        "redialed": redialed,
                        "sessions": dev.sessions,
                        "ws_attempts": dev.ws_attempts,
                        "player_listed": player() is not None,
                    }
                    log(f"{key}: {result[key]}")
                    if not redialed:
                        break
            elif name == "bug6887":
                vid = await _visible_player(dev, player, until)
                result["visible_player"] = vid
                result["before"] = _listing(mass, pid, vid)
                await dev.goodbye("another_server")
                await asyncio.sleep(5)
                result["after"] = _listing(mass, pid, vid)
                log(f"after another_server: {result['after']}")
                # The other server is done with it; the user presses play in MA. The
                # spec (and aiosendspin's reclaim_client_for_playback) say MA dials the
                # device back with connection reason "playback" and plays.
                played_before = dev.fake_sink.metrics.played_chunks
                result["play_error"] = await _play(mass, scene, vid, until)
                reclaimed = await until(
                    lambda: (
                        dev.sessions > 1 and dev.fake_sink.metrics.played_chunks > played_before + 5
                    ),
                    30,
                )
                result["reclaim"] = {
                    "reclaimed": reclaimed,
                    "sessions": dev.sessions,
                    "ws_attempts": dev.ws_attempts,
                    "played_chunks": dev.fake_sink.metrics.played_chunks,
                    **_listing(mass, pid, vid),
                }
                log(f"play after another_server: {result['reclaim']}")
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


async def _visible_player(dev: Any, player: Any, until: Any) -> str:
    """A device with a MAC registers as a protocol player; MA wraps it in a visible
    "universal" player some seconds later, and the queue lives on that one."""
    proto = player()
    await until(lambda: proto.protocol_parent_id is not None, 40)
    return str(proto.protocol_parent_id or dev.client_id)


def _listing(mass: Any, pid: str, vid: str) -> dict[str, Any]:
    proto, visible = mass.players.get_player(pid), mass.players.get_player(vid)
    return {
        "protocol_registered": proto is not None,
        "protocol_available": bool(proto and proto.available),
        "visible_listed": visible is not None,
        "visible_available": bool(visible and visible.available),
    }


async def _play(mass: Any, scene: Any, queue_id: str, until: Any) -> str | None:
    """Play a 90 s tone track on ``queue_id``; returns the error text, if any."""
    import asyncio

    from music_assistant_models.errors import MusicAssistantError, PlayerUnavailableError

    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 90.0).path)
    await until(lambda: mass.player_queues.get(queue_id) is not None, 15)
    err: str | None = None
    for _ in range(40):  # the queue is created a moment after the player registers
        try:
            await mass.player_queues.play_media(queue_id, url)
            return None
        except PlayerUnavailableError as e:
            err = repr(e)
            await asyncio.sleep(0.25)
        except MusicAssistantError as e:
            return repr(e)
    return err


async def _smoke(mass, scene, dev, pid, player, until, log, result) -> None:
    import asyncio

    pid = await _visible_player(dev, player, until)
    result["visible_player"] = pid

    def player() -> Any:
        return mass.players.get_player(pid)

    result["play_error"] = await _play(mass, scene, pid, until)

    def ma_state() -> str:
        return str(player().state.playback_state.value)

    def ma_volume() -> int | None:
        return player().state.volume_level

    def queue_state() -> str:
        q = mass.player_queues.get(pid)
        return str(q.state.value) if q else "none"

    playing = await until(
        lambda: dev.fake_sink.metrics.played_chunks > 20 and ma_state() == "playing", 30
    )
    m = dev.fake_sink.metrics
    result["playing"] = {
        "ok": playing,
        "device": dev.transport.state.value,
        "ma": ma_state(),
        "queue": queue_state(),
    }
    result["played_chunks"] = m.played_chunks
    result["header_sizes"] = sorted(set(m.header_sizes))
    result["format"] = dev.fake_sink.fmt.__dict__ if dev.fake_sink.fmt else None
    log(f"playing: {m.played_chunks} chunks {result['playing']} fmt={result['format']}")
    await asyncio.sleep(2)

    # MA's Sendspin player has no native pause: pause ends the stream (stream/end with
    # the group stopped) and resume starts a new one at the saved position.
    await mass.player_queues.pause(pid)
    await until(lambda: dev.transport.state.value != "playing" and ma_state() != "playing", 10)
    result["paused"] = {
        "device": dev.transport.state.value,
        "device_sink_active": dev.fake_sink.active,
        "ma": ma_state(),
        "queue": queue_state(),
    }
    log(f"paused: {result['paused']}")

    await mass.player_queues.resume(pid)
    result["resumed"] = await until(lambda: dev.transport.state.value == "playing", 15)
    streams_before = dev.fake_sink.metrics.streams
    played_before = dev.fake_sink.metrics.played_chunks
    await mass.player_queues.seek(pid, 30)
    await until(
        lambda: (
            dev.fake_sink.metrics.streams > streams_before
            and dev.fake_sink.metrics.played_chunks > played_before + 10
        ),
        20,
    )
    queue = mass.player_queues.get(pid)
    result["seek"] = {
        "ma_elapsed": round(queue.corrected_elapsed_time, 2) if queue else None,
        "device_new_stream": dev.fake_sink.metrics.streams > streams_before,
        "device_playing": dev.transport.state.value,
        "streams": dev.fake_sink.metrics.streams,
        "clears": dev.fake_sink.metrics.clears,
    }
    log(f"seek: {result['seek']}")

    await mass.players.cmd_volume_set(pid, 33)
    await until(lambda: dev.volume == 33 and ma_volume() == 33, 10)
    result["volume"] = {"device": dev.volume, "ma": ma_volume()}
    # a local change (hardware button) is reported back to MA
    await dev.sim_action("set_volume", {"volume": 61})
    await until(lambda: ma_volume() == 61, 10)
    result["local_volume"] = {"device": dev.volume, "ma": ma_volume()}
    log(f"volume: {result['volume']} local: {result['local_volume']}")
    await mass.player_queues.stop(pid)
    await until(lambda: dev.transport.state.value != "playing", 10)
    result["stopped"] = {"device": dev.transport.state.value, "ma": ma_state()}
    result["sink"] = dev.fake_sink.metrics.as_dict()


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
