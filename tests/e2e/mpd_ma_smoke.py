"""End-to-end: a real Music Assistant server drives a simulated MPD server.

Skipped unless ``MA_REPO`` points at a Music Assistant tree. MA's interpreter is
``$MA_PYTHON`` or ``$MA_REPO/.venv/bin/python`` (Python 3.14 with ``python-mpd2``). Each
scenario runs in a subprocess with ``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095; its Sendspin
  server is moved off 8927), with a throwaway data dir; its mDNS registration/browsing
  and UPnP scans are off. The ``mpd`` provider
  is configured with the sim's ``127.0.0.1:<port>`` as its one manual host, exactly as a
  user would enter it. MA's python-mpd2 clients talk to the sim over real TCP.
- ``smoke`` (mpd_0_23): register (model ``MPD 0.23.5``); play a track (the sim's sink
  GETs MA's stream URL and decodes it); pause, resume, seek (MA's queue seek: a new
  ``clear``/``add``/``play``, so a new GET), volume both ways (MA -> setvol; another
  client's setvol read back by MA), stop.
- ``mp1`` (MP1): another client pauses / resumes / changes volume. MPD pushes
  ``changed: player``/``changed: mixer`` to MA's idle connection at once, but MA's idle
  loop compares the yielded *list* with strings, so it never refreshes. A pause is only
  noticed by the 5 s poll; a resume while MA thinks the player is paused is never seen.
- ``mp12`` (MP12, pympd#217/#220): only MA's command socket is dropped (NAT or server
  ``connection_timeout``). MA logs "Failed to fetch MPD status" on every poll and every
  command fails, but it never reconnects because only the idle loop triggers that.
- ``mp8`` (MP8): another client stops the player while MA has it paused. MA's play
  sends ``pause 0``, which MPD answers ``OK`` and ignores in ``stop``; MA claims
  PLAYING and nothing plays.

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/mpd_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/mpd_ma_smoke.py smoke
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
FORBIDDEN_PORTS = {8095, 8097, 8927, 8928, 6600}


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

    def test_ma_drives_mpd_server():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["model"] == "MPD 0.23.5", r
        assert r["playing"]["device"] == "playing", r
        assert r["playing"]["ma"] == "playing", r
        assert r["playing"]["mpd_state"] == "play", r
        assert r["playing"]["sink_played_s"] > 1, r
        assert r["playing"]["user_agent"].startswith("Music Player Daemon"), r
        assert r["paused"] == {"device": "paused", "ma": "paused", "mpd_state": "pause"}, r
        assert r["resumed"]["device"] == "playing", r
        assert r["resumed"]["ma"] == "playing", r
        assert r["seek"]["streams_opened"] >= 2, r
        assert abs(r["seek"]["ma_elapsed"] - 30) < 4, r
        assert r["seek"]["device"] == "playing", r
        assert r["volume"] == {"device": 33, "ma": 33}, r
        assert r["volume_from_device"]["ma"] == 55, r
        assert r["stopped"]["mpd_state"] == "stop", r
        assert r["stopped"]["ma"] == "idle", r
        assert {"clear", "add", "play", "pause", "setvol", "status", "stop"} <= set(
            r["mpd_commands"]
        ), r

    def test_mp1_idle_event_refreshes_ma_state():
        r = _run_scenario("mp1")
        assert r["idle_pushed"]["pause"], r  # the sim did push `changed: player`
        assert r["pause_seen_s"] is not None, r
        assert r["pause_seen_s"] < 1.5, (
            "another client paused MPD; MA ignored the idle event (MP1) and only the "
            "5 s poll noticed",
            r,
        )
        assert r["resume_seen"], (
            "another client resumed MPD while MA had it paused; MA never noticed (MP1)",
            r,
        )

    def test_mp12_command_socket_drop_recovers():
        r = _run_scenario("mp12")
        assert r["dropped"], r
        assert r["reconnected"], (
            "MA's command socket died; MA kept logging 'Failed to fetch MPD status' "
            "and failing commands, and never reconnected (MP12)",
            r,
        )

    def test_mp8_play_after_external_stop_plays():
        r = _run_scenario("mp8")
        assert r["externally_stopped"], r
        assert r["after_play"]["device"] == "playing", (
            "MA's play sent `pause 0` to a stopped MPD (a no-op) and claimed PLAYING (MP8)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------

PROFILES: dict[str, tuple[str, dict[str, Any]]] = {
    "smoke": ("mpd/mpd_0_23", {}),
    "mp1": ("mpd/mpd_0_23", {}),
    "mp12": ("mpd/mpd_0_23", {}),
    "mp8": ("mpd/mpd_0_23", {}),
}


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
    import shutil
    import tempfile
    import time

    from music_assistant.controllers.discovery.controller import DiscoveryController
    from music_assistant.mass import MusicAssistant

    from seamer.simkit.scenario import Scene

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    # keep the throwaway server off the LAN
    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]
    # the builtin Sendspin provider binds 8927 (the user's MA); move it to a free port
    import music_assistant.providers.sendspin.provider as ss_provider
    from aiosendspin.server.server import SendspinServer

    ss_provider.SENDSPIN_SERVER_PORT = free_port()  # type: ignore[attr-defined]
    SendspinServer._start_mdns_advertising = _noop  # type: ignore[method-assign]
    SendspinServer._start_mdns_discovery = _noop  # type: ignore[method-assign]

    web_port, stream_port, mpd_port = free_port(), free_port(), free_port()
    assert not {web_port, stream_port, mpd_port} & FORBIDDEN_PORTS

    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-mpd-{name}-"))
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
                },
                "providers": {
                    "mpd": {
                        "type": "player",
                        "domain": "mpd",
                        "instance_id": "mpd",
                        "enabled": True,
                        "values": {"manual_discovery_ip_addresses": [f"127.0.0.1:{mpd_port}"]},
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
    logging.getLogger("music_assistant.providers.mpd").setLevel(logging.DEBUG)
    t0 = time.monotonic()

    def log(msg: str) -> None:
        print(f"[{name} {time.monotonic() - t0:6.1f}s] {msg}", flush=True)

    async def until(pred: Any, timeout: float, poll: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(poll)
        return bool(pred())

    def log_count(text: str) -> int:
        try:
            return (work / "server.log").read_text(errors="replace").count(text)
        except OSError:
            return 0

    profile, knobs = PROFILES[name]
    result: dict[str, Any] = {"scenario": name, "profile": profile, "knobs": knobs}
    # not safe mode: safe mode skips every non-builtin provider, mpd included
    mass = MusicAssistant(str(data_dir), str(work / "cache"))
    try:
        async with Scene(work / "scene", name=name) as scene:
            dev = await scene.device(profile, mpd_port=mpd_port, **knobs)
            pid = f"mpd_127.0.0.1_{mpd_port}"
            log(f"device {dev.id} mpd://127.0.0.1:{dev.mpd_port}")
            await mass.start()

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 20)
            result["registered"] = ok
            if not ok:
                result["log_tail"] = _log_tail(work)
                return result
            result["model"] = player().device_info.model
            result["connections"] = [c["role"] for c in dev.status()["connections"]]
            log(f"player registered: {player().display_name} {result['connections']}")

            await scene.media_server()
            track = scene.media.track(1, 300.0)
            await mass.player_queues.play_media(pid, [scene.media_url(track.path)])
            playing = await until(lambda: dev.transport.state.value == "playing", 30)
            if not playing:
                result["error"] = "device never reached PLAYING"
                result["log_tail"] = _log_tail(work)
                return result
            await asyncio.sleep(2)

            if name == "smoke":
                await _smoke(mass, dev, pid, player, until, log, result)
            elif name == "mp1":
                await _mp1(mass, dev, pid, player, until, log, result)
            elif name == "mp12":
                await _mp12(mass, dev, pid, player, until, log, log_count, result)
            elif name == "mp8":
                await _mp8(mass, dev, pid, player, until, log, result)
            result["mpd_commands"] = sorted(
                {e.name for e in scene.rec.events if e.kind == "cmd" and e.data.get("proto")}
            )
            result["fetch_failures_logged"] = log_count("Failed to fetch MPD status")
            result["idle_loop_disconnects_logged"] = log_count("MPD idle loop disconnected")
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


def _log_tail(work: Path) -> list[str]:
    try:
        lines = (work / "server.log").read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [ln[:300] for ln in lines if "mpd" in ln.lower()][-15:]


def _count(dev: Any, kind: str, name: str) -> int:
    return sum(1 for e in dev.rec.rec.events if e.kind == kind and e.name == name)


def _idle_pushes(dev: Any, since_seq: int, subsystem: str) -> int:
    """`changed:` replies the sim sent to an idling client after ``since_seq``."""
    return sum(
        1
        for e in dev.rec.rec.events
        if e.seq > since_seq
        and e.kind == "resp"
        and e.name == "idle"
        and subsystem in e.data.get("changed", [])
    )


def _status_cmds(dev: Any, since_seq: int) -> int:
    return sum(
        1
        for e in dev.rec.rec.events
        if e.seq > since_seq and e.kind == "cmd" and e.name == "status" and e.data.get("proto")
    )


def _last_seq(dev: Any) -> int:
    return dev.rec.rec.events[-1].seq if dev.rec.rec.events else 0


def _ua(dev: Any) -> str:
    reqs = [e for e in dev.rec.rec.events if e.kind == "event" and e.name == "http_request"]
    return str(reqs[-1].data.get("headers", {}).get("User-Agent", "")) if reqs else ""


async def _smoke(mass, dev, pid, player, until, log, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    sink = dev.sink.snapshot()
    result["playing"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
        "mpd_state": dev.mpd_state(),
        "sink_bytes": sink["bytes"],
        "sink_played_s": sink["played_s"],
        "sink_decoder": sink["decoder"],
        "stream_url": sink["url"],
        "queue": [s["uri"] for s in dev.status()["queue"]],
        "user_agent": _ua(dev),
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
        "mpd_state": dev.mpd_state(),
    }
    log(f"paused: {result['paused']}")
    await asyncio.sleep(1)
    await mass.player_queues.play(pid)
    await until(
        lambda: (
            dev.transport.state.value == "playing"
            and player().playback_state == PlaybackState.PLAYING
        ),
        10,
    )
    result["resumed"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
    }
    log(f"resumed: {result['resumed']}")

    opened_before = _count(dev, "sink", "opened")
    await mass.player_queues.seek(pid, 30)
    await until(lambda: _count(dev, "sink", "opened") > opened_before, 15)
    await until(lambda: dev.transport.state.value == "playing", 15)
    await asyncio.sleep(2)
    queue = mass.player_queues.get(pid)
    result["seek"] = {
        "target": 30,
        "ma_elapsed": round(queue.corrected_elapsed_time, 1) - 2,
        "device": dev.transport.state.value,
        "streams_opened": _count(dev, "sink", "opened"),
        "mpd_elapsed": dev.elapsed(),
    }
    log(f"seek: {result['seek']}")

    await mass.players.cmd_volume_set(pid, 33)
    await until(lambda: dev.volume == 33 and player().volume_level == 33, 10)
    result["volume"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"volume: {result['volume']}")
    # another MPD client (a phone app) turns it up; MA must read it back
    await dev.external("setvol 55")
    await until(lambda: player().volume_level == 55, 12)
    result["volume_from_device"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"volume from device: {result['volume_from_device']}")

    await mass.player_queues.stop(pid)
    await until(lambda: dev.mpd_state() == "stop", 10)
    await until(lambda: player().playback_state == PlaybackState.IDLE, 10)
    result["stopped"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
        "mpd_state": dev.mpd_state(),
    }
    log(f"stopped: {result['stopped']}")


async def _mp1(mass, dev, pid, player, until, log, result) -> None:
    import asyncio
    import time

    from music_assistant_models.enums import PlaybackState

    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    # line up just after one of MA's 5 s polls, so a poll cannot hide the bug
    seq = _last_seq(dev)
    await until(lambda: _status_cmds(dev, seq) > 0, 8, poll=0.01)
    await asyncio.sleep(0.2)

    seq = _last_seq(dev)
    t = time.monotonic()
    reply = await dev.external("pause 1")
    result["external_pause_reply"] = reply
    await until(lambda: player().playback_state == PlaybackState.PAUSED, 10)
    seen = player().playback_state == PlaybackState.PAUSED
    result["pause_seen_s"] = round(time.monotonic() - t, 2) if seen else None
    result["idle_pushed"] = {"pause": _idle_pushes(dev, seq, "player")}
    result["status_after_pause_push"] = _status_cmds(dev, seq)
    log(f"external pause: seen by MA after {result['pause_seen_s']} s")

    # now MA has it paused (needs_poll is False): another client resumes
    seq = _last_seq(dev)
    await dev.external("pause 0")
    await until(lambda: player().playback_state == PlaybackState.PLAYING, 12)
    result["resume_seen"] = player().playback_state == PlaybackState.PLAYING
    result["idle_pushed"]["resume"] = _idle_pushes(dev, seq, "player")
    result["status_after_resume_push"] = _status_cmds(dev, seq)
    result["after_resume"] = {
        "mpd_state": dev.mpd_state(),
        "ma": str(player().playback_state.value),
    }
    log(f"external resume: seen={result['resume_seen']} {result['after_resume']}")

    # volume from another client while MA thinks it is paused: never read back
    seq = _last_seq(dev)
    await dev.external("setvol 21")
    await until(lambda: player().volume_level == 21, 8)
    result["volume_seen"] = player().volume_level == 21
    result["idle_pushed"]["mixer"] = _idle_pushes(dev, seq, "mixer")
    log(f"external setvol 21: MA volume {player().volume_level}")


async def _mp12(mass, dev, pid, player, until, log, log_count, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    before = dev.status()["connections"]
    result["connections_before"] = [(c["id"], c["role"]) for c in before]
    dropped = dev.mpd.drop("command")
    result["dropped"] = dropped
    log(f"dropped command connection(s) {dropped}; open: {result['connections_before']}")
    await until(lambda: log_count("Failed to fetch MPD status") > 0, 8)
    result["fetch_failed_logged"] = log_count("Failed to fetch MPD status") > 0
    try:
        await mass.players.cmd_volume_set(pid, 12)
        result["volume_cmd"] = "ok"
    except Exception as err:
        result["volume_cmd"] = f"{type(err).__name__}: {err}"[:200]
    log(f"volume after drop: {result['volume_cmd']} (device {dev.volume})")
    # MA's RECONNECT_DELAY is 5 s; give it three times that
    known = {c["id"] for c in before}
    await until(
        lambda: any(c["id"] not in known for c in dev.status()["connections"]),
        15,
    )
    after = dev.status()["connections"]
    result["connections_after"] = [(c["id"], c["role"]) for c in after]
    result["reconnected"] = any(c["id"] not in known for c in after)
    result["ma_available"] = player().available
    await asyncio.sleep(0)
    try:
        await mass.players.cmd_volume_set(pid, 13)
        result["volume_cmd_later"] = "ok"
    except Exception as err:
        result["volume_cmd_later"] = f"{type(err).__name__}: {err}"[:200]
    result["device_volume"] = dev.volume
    log(f"after 15 s: reconnected={result['reconnected']} {result['volume_cmd_later']}")


async def _mp8(mass, dev, pid, player, until, log, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    await mass.player_queues.pause(pid)
    await until(lambda: dev.mpd_state() == "pause", 10)
    result["paused"] = {"mpd_state": dev.mpd_state(), "ma": str(player().playback_state.value)}
    await asyncio.sleep(1)
    reply = await dev.external("stop")  # a phone app (or end of stream) stops MPD
    result["externally_stopped"] = dev.mpd_state() == "stop" and reply == "OK\n"
    await asyncio.sleep(2)
    result["ma_after_external_stop"] = str(player().playback_state.value)
    log(f"external stop; MA still thinks {result['ma_after_external_stop']}")
    opened = _count(dev, "sink", "opened")
    await mass.players.cmd_play(pid)
    await asyncio.sleep(0.5)
    result["ma_right_after_play"] = str(player().playback_state.value)
    await until(lambda: dev.transport.state.value == "playing", 6)
    result["after_play"] = {
        "device": dev.transport.state.value,
        "mpd_state": dev.mpd_state(),
        "ma": str(player().playback_state.value),
        "new_streams": _count(dev, "sink", "opened") - opened,
        "pause_noop_fired": sum(
            1
            for e in dev.rec.rec.events
            if e.kind == "knob" and e.data.get("knob") == "pause_in_stop_noop"
        ),
    }
    log(f"after MA play: {result['after_play']} (right after: {result['ma_right_after_play']})")


def main() -> None:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)


if __name__ == "__main__":
    main()
