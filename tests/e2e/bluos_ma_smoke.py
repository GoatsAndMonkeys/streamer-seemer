"""End-to-end: a real Music Assistant server drives a simulated BluOS player.

Skipped unless ``MA_REPO`` points at a Music Assistant tree. MA's interpreter is
``$MA_PYTHON`` or ``$MA_REPO/.venv/bin/python`` (Python 3.14 with ``pyblu``). Each scenario
runs in a subprocess with ``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095), with a throwaway
  data dir; its own mDNS registration/browsing and UPnP scans are off. The sim's
  ``_musc._tcp`` announcement (``device.mdns_txt()``, so the ``mac`` TXT key is the
  player id) is handed to the bluesound provider's mDNS callback, as zeroconf would.
  The provider's idle poll interval is cut to 3 s so a run gets going quickly; the
  playback polling MA does is untouched.
- ``smoke`` (node_n130): register; play a tone track (the sim's sink receives MA's
  stream); pause, resume, seek, volume (both directions), each read back on both sides;
  stop.
- ``bug6872`` (node_n130, 5 x 240 s album, ~25 min): the player buffers ahead and drops
  what it has not played when MA closes the stream. On dev MA closes 8 s after the last
  byte, so the end of the last track is cut (MA#6872). Opt-in: ``BLUOS_E2E_LONG=1``.
- ``bug_state`` (``inject_state`` = empty ``<state></state>``, bl2:B43): MA's
  ``PLAYBACK_STATE_MAP[state]`` raises KeyError in every poll, so it stops tracking the
  player (volume changed on the device never reaches MA).
- ``bug4398`` (``status_fail``, bl2:B47): one HTTP 503 on /Status escapes pyblu as a raw
  ``ClientResponseError`` and MA logs "Error while requesting latest state from player"
  (MA#4398); one /Status that hangs past pyblu's 5 s timeout marks the player
  unavailable until the next poll.
- ``bug_inputs_image`` (``inputs_missing_attr = "image"``, bss:B21): pyblu's
  ``parse_inputs`` raises KeyError, so the player never registers.

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/bluos_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/bluos_ma_smoke.py smoke
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

    def test_ma_drives_bluos_player():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["playing"]["device"] == "playing", r
        assert r["playing"]["ma"] == "playing", r
        assert r["playing"]["sink_played_s"] > 1, r
        assert r["paused"]["device"] == "paused", r
        assert r["paused"]["ma"] == "paused", r
        assert r["paused"]["bluos_state"] == "pause", r
        assert r["resumed"]["device"] == "playing", r
        assert abs(r["seek"]["ma_elapsed"] - 30) < 4, r
        assert r["seek"]["device"] == "playing", r
        assert r["seek"]["streams_opened"] >= 2, r
        assert r["volume"]["device"] == 33, r
        assert r["volume"]["ma"] == 33, r
        assert r["volume_from_device"]["ma"] == 55, r
        assert r["stopped"]["device"] in ("stopped", "no_media"), r
        assert r["stopped"]["bluos_state"] == "stop", r

    @pytest.mark.skipif(
        not os.environ.get("BLUOS_E2E_LONG"), reason="~25 min; set BLUOS_E2E_LONG=1"
    )
    def test_bug_6872_album_end_not_cut():
        r = _run_scenario("bug6872", timeout=2400)
        assert r["finished"], r
        # ~0.9 s is a fixed sim residual (ffmpeg holds a little decoded audio
        # until EOF); a real #6872 cut drops several seconds.
        assert r["dropped_s"] < 1.5, (
            "MA closed the stream while the player still had the album's end buffered (#6872)",
            r,
        )

    def test_bug_unknown_state_keeps_tracking():
        r = _run_scenario("bug_state")
        assert r["injected"], r
        assert r["ma_volume_after"] == 55, (
            "an unknown <state> broke MA's poll (KeyError), so it stopped tracking the player",
            r,
        )

    def test_bug_4398_one_failed_poll_keeps_player():
        r = _run_scenario("bug4398")
        assert r["failed_polls"] >= 2, r
        assert r["http_503"]["logged"] is None, (
            "one HTTP 503 on /Status escaped as 'Error while requesting latest state' (#4398)",
            r,
        )
        assert not r["went_unavailable"], (
            "one timed-out /Status poll made MA mark the player unavailable (#4398)",
            r,
        )

    def test_bug_inputs_without_image_still_registers():
        r = _run_scenario("bug_inputs_image")
        assert r["registered"], (
            "an input without image= in /RadioBrowse made pyblu raise, so MA never "
            "registered the player (bss:B21)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------

PROFILES: dict[str, tuple[str, dict[str, Any]]] = {
    "smoke": ("bluos/node_n130", {}),
    "bug6872": ("bluos/node_n130", {}),
    "bug_state": ("bluos/node_n130", {}),
    "bug4398": ("bluos/node_n130", {}),
    "bug_inputs_image": ("bluos/node_n130", {"inputs_missing_attr": "image"}),
}


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
    import shutil
    import tempfile
    import time
    from ipaddress import ip_address

    import music_assistant.providers.bluesound.player as blu_player
    import music_assistant.providers.bluesound.provider as blu_provider
    from music_assistant.controllers.discovery.controller import DiscoveryController
    from music_assistant.mass import MusicAssistant
    from zeroconf import IPVersion, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceInfo

    from seamer.simkit.scenario import Scene

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    # keep the throwaway server off the LAN: no _mass._tcp registration, no mDNS
    # browsing, no UPnP search (the device's announcement is handed over below)
    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]
    # the sim lives on loopback, which the zeroconf address helper skips
    blu_provider.get_primary_ip_address_from_zeroconf = (  # type: ignore[assignment]
        lambda info, prefer_ipv6=False: next(
            (str(a) for a in info.ip_addresses_by_version(IPVersion.V4Only)), None
        )
    )
    blu_player.IDLE_POLL_INTERVAL = 3  # only to get going quickly

    web_port, stream_port = free_port(), free_port()
    assert not {web_port, stream_port} & FORBIDDEN_PORTS

    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-bluos-{name}-"))
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
            }
        )
    )
    logging.basicConfig(
        level=logging.INFO,
        filename=work / "server.log",
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("music_assistant.providers.bluesound").setLevel(logging.DEBUG)
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

    def log_count(text: str) -> int:
        try:
            return (work / "server.log").read_text(errors="replace").count(text)
        except OSError:
            return 0

    def log_first(text: str) -> str | None:
        try:
            lines = (work / "server.log").read_text(errors="replace").splitlines()
        except OSError:
            return None
        return next((ln.split(" ", 2)[-1][:300] for ln in lines if text in ln), None)

    profile, knobs = PROFILES[name]
    result: dict[str, Any] = {"scenario": name, "profile": profile, "knobs": knobs}
    mass = MusicAssistant(str(data_dir), str(work / "cache"), safe_mode=True)
    try:
        async with Scene(work / "scene", name=name) as scene:
            dev = await scene.device(profile, **knobs)
            pid = dev.mac
            log(f"device {dev.id} {dev.base_url} mac {pid}")
            await mass.start()
            await mass.config.create_builtin_provider_config("bluesound")
            conf = (await mass.config.get_provider_configs(provider_domain="bluesound"))[0]
            await mass.load_provider_config(conf)
            prov = mass.get_provider("bluesound")
            assert prov is not None, "bluesound provider did not load"
            info = AsyncServiceInfo(
                "_musc._tcp.local.",
                f"{dev.name}._musc._tcp.local.",
                port=dev.port,
                addresses=[ip_address("127.0.0.1").packed],
                properties=dev.mdns_txt(),
            )
            try:
                await prov.on_mdns_service_state_change(  # type: ignore[attr-defined]
                    info.name, ServiceStateChange.Added, info
                )
            except Exception as err:
                result["discovery_error"] = f"{type(err).__name__}: {err}"
                log(f"discovery raised {result['discovery_error']}")

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 20)
            result["registered"] = ok
            if not ok:
                return result
            log(f"player registered: {player().display_name}")

            await scene.media_server()
            if name == "bug6872":
                n = int(os.environ.get("BLUOS_6872_TRACKS", "5"))
                tracks = scene.media.album(n, float(os.environ.get("BLUOS_6872_SECONDS", "240")))
            else:
                tracks = [scene.media.track(1, 300.0)]
            urls = [scene.media_url(t.path) for t in tracks]
            await mass.player_queues.play_media(pid, urls)
            playing = await until(lambda: dev.transport.state.value == "playing", 30)
            if not playing:
                result["error"] = "device never reached PLAYING"
                return result

            if name == "smoke":
                await _smoke(mass, dev, pid, player, until, log, result)
            elif name == "bug6872":
                await _bug6872(mass, dev, pid, tracks, until, log, result)
            elif name == "bug_state":
                await _bug_state(dev, player, until, log, log_count, result)
                result["logged"] = log_first("Error while requesting latest state")
            elif name == "bug4398":
                await _bug4398(dev, player, log, log_first, result)
            result["bluos_commands"] = sorted(
                {
                    str(e.data.get("path", ""))
                    for e in scene.rec.events
                    if e.kind == "cmd" and str(e.data.get("path", "")).startswith("/")
                }
            )
            result["poll_errors_logged"] = log_count("Error while requesting latest state")
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


async def _bluos_state(dev: Any) -> str:
    import re

    import aiohttp

    async with aiohttp.ClientSession() as s, s.get(f"{dev.base_url}/Status") as r:
        m = re.search(r"<state>([^<]*)</state>", await r.text())
        return m.group(1) if m else "?"


async def _smoke(mass, dev, pid, player, until, log, result) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    await until(lambda: player().playback_state == PlaybackState.PLAYING, 15)
    await asyncio.sleep(3)
    sink = dev.sink.snapshot()
    result["playing"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
        "bluos_state": await _bluos_state(dev),
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
        "bluos_state": await _bluos_state(dev),
    }
    log(f"paused: {result['paused']}")
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
        "device_reported_s": round(dev.lens.position() or 0.0, 1),
    }
    log(f"seek: {result['seek']}")

    await mass.players.cmd_volume_set(pid, 33)
    await until(lambda: dev.volume == 33 and player().volume_level == 33, 10)
    result["volume"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"volume: {result['volume']}")
    dev.set_level(55)  # turned on the device (app or knob): MA must read it back
    await until(lambda: player().volume_level == 55, 15)
    result["volume_from_device"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"volume from device: {result['volume_from_device']}")

    await mass.player_queues.stop(pid)
    await until(lambda: dev.transport.state.value not in ("playing", "paused"), 10)
    await until(lambda: player().playback_state == PlaybackState.IDLE, 10)
    result["stopped"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
        "bluos_state": await _bluos_state(dev),
    }
    log(f"stopped: {result['stopped']}")


def _count(dev: Any, kind: str, name: str) -> int:
    return sum(1 for e in dev.rec.rec.events if e.kind == kind and e.name == name)


def _fired(dev: Any, knob: str) -> int:
    return sum(
        1
        for e in dev.rec.rec.events
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    )


async def _bug6872(mass, dev, pid, tracks, until, log, result) -> None:
    import asyncio
    import time

    album_s = sum(t.seconds for t in tracks)
    queue = mass.player_queues.get(pid)
    t_start = time.monotonic()
    next_report = 0.0
    heard: dict[str, Any] | None = None
    while time.monotonic() - t_start < album_s + 300:
        done = [
            e.data
            for e in dev.rec.rec.events
            if e.kind == "sink" and e.name == "heard" and e.data["played_s"] > album_s / 2
        ]
        if done:
            heard = done[-1]
            break
        snap = dev.sink.snapshot()
        if os.environ.get("BLUOS_6872_TRACE") and snap["received_s"] > album_s - 10:
            log(
                f"  trace bytes={snap['bytes']} received={snap['received_s']} state={snap['state']}"
            )
        if time.monotonic() >= next_report:
            log(
                f"t={time.monotonic() - t_start:6.0f}s received={snap['received_s']:7.1f}s "
                f"played={snap['played_s']:7.1f}s lead={snap['fill_s']:5.1f}s "
                f"queue_index={queue.current_index}"
            )
            next_report = time.monotonic() + 60
        await asyncio.sleep(0.5)
    result["finished"] = heard is not None
    result["album_s"] = album_s
    if heard is None:
        return
    result["heard"] = heard
    result["dropped_s"] = round(float(heard["dropped_s"]) + float(heard["unplayed_s"]), 1)
    result["played_s"] = round(float(heard["played_s"]), 1)
    result["end_reason"] = heard.get("reason")
    log(f"stream ended: {heard}")
    if os.environ.get("BLUOS_6872_TRACE"):
        tail = [e for e in dev.rec.rec.events if e.kind in ("sink", "cmd", "state")][-40:]
        for e in tail:
            log(f"  {e.t:9.2f} {e.src} {e.kind} {e.name} {e.data}")
    await asyncio.sleep(20)  # let the queue notice the end
    result["queue_state"] = str(queue.state)


async def _bug_state(dev, player, until, log, log_count, result) -> None:
    import asyncio

    await asyncio.sleep(4)
    errors_before = log_count("Error while requesting latest state")
    dev.knobs.set_live("inject_state", {"p": 1.0, "value": ""})
    dev.set_level(55)
    await until(lambda: _fired(dev, "inject_state") >= 3, 15)
    await until(lambda: player().volume_level == 55, 12)
    result["injected"] = _fired(dev, "inject_state") > 0
    result["ma_volume_after"] = player().volume_level
    result["ma_state"] = str(player().playback_state.value)
    result["poll_errors_during"] = log_count("Error while requesting latest state") - errors_before
    log(f"after empty <state>: {result}")
    dev.knobs.set_live("inject_state", {"p": 0.0})


async def _bug4398(dev, player, log, log_first, result) -> None:
    """One failed /Status poll, twice: an HTTP 503, then a request that hangs past
    pyblu's 5 s timeout. Each failure is a single poll; the next one would succeed."""
    import asyncio
    import time

    async def one_failure(knob: dict[str, Any], watch_s: float) -> dict[str, Any]:
        before = _fired(dev, "status_fail")
        went, unavailable_s = False, 0.0
        dev.knobs.set_live("status_fail", {"p": 1.0, **knob})
        deadline = time.monotonic() + 15
        while _fired(dev, "status_fail") == before and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        dev.knobs.set_live("status_fail", {"p": 0.0})
        t_end = time.monotonic() + watch_s
        while time.monotonic() < t_end:
            if not player().available:
                went = True
                unavailable_s += 0.05
            await asyncio.sleep(0.05)
        return {
            "failed_polls": _fired(dev, "status_fail") - before,
            "went_unavailable": went,
            "unavailable_s": round(unavailable_s, 1),
            "available_at_end": player().available,
        }

    await asyncio.sleep(4)
    result["http_503"] = await one_failure({"mode": "http", "status": 503}, 8)
    result["http_503"]["logged"] = log_first("Error while requesting latest state")
    log(f"one 503 on /Status: {result['http_503']}")
    result["hang"] = await one_failure({"mode": "timeout", "hold_s": 8.0}, 15)
    log(f"one hung /Status: {result['hang']}")
    result["failed_polls"] = result["http_503"]["failed_polls"] + result["hang"]["failed_polls"]
    result["went_unavailable"] = (
        result["http_503"]["went_unavailable"] or result["hang"]["went_unavailable"]
    )


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
