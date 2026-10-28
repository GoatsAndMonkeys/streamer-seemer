"""End-to-end: a real Music Assistant server streams to simulated AirPlay receivers.

Skipped unless ``MA_REPO`` points at a Music Assistant checkout with its own ``.venv``.
Each scenario runs in a subprocess with MA's interpreter and
``PYTHONPATH=$MA_REPO:<this repo>``:

- MA runs in-process, loopback only, on free high ports (never 8095), with a throwaway
  data dir. Its mDNS registration/browsing, SSDP and provider discovery are off, and the
  DACP ``_dacp._tcp`` advert is not registered.
- MA's ``get_cli_binary()`` has no override, so it is monkeypatched (in the helpers,
  stream, provider and pairing modules, which import it by name) to return the fake
  ``cliairplay`` written by :func:`seamer.families.airplay.fake_cli.install`. MA spawns
  it exactly as it spawns the real binary (PTP daemon included).
- The receiver is handed to the provider's mDNS callback as an ``_airplay._tcp`` service
  with the profile's TXT record (the matching ``_raop._tcp`` record is answered from
  ``async_find_mdns_service``). Loopback addresses are filtered by MA, so
  ``get_primary_ip_address_from_zeroconf`` returns ``127.0.0.1`` here.
- The device's DACP target is MA's DACP server socket.

Scenarios:

- ``smoke`` (wiim_pro_plus, drop-out off): register; play a tone track (the sim's sink
  hears MA's PCM); pause, resume, seek, volume, each read back on both sides; stop.
  The queue belongs to the Universal Player MA wraps the AirPlay protocol player in.
  Resume is tried with ``player_queues.play`` first: on dev that never reaches the
  AirPlay player (``ProtocolBackedPlayer.play`` only forwards to a Chromecast/DLNA
  player with an external source), so the run falls back to ``players.cmd_play``.
- ``bug6664`` (sonos_era100, short volume tick): two quick volume changes. The Sonos
  applies them on its tick and echoes both, 30 ms apart, after MA's 2 s grace. MA adopts
  the stale echo and re-sends it, so the user's level is lost (MA#6664, open on dev).
- ``bug5018`` (wiim_pro_plus): volume changed on the speaker itself, reported over DACP,
  in a session, between sessions, and after TEARDOWN with the session's id (MA#5018).

Run from this repo::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/airplay_ma_smoke.py

or one scenario directly (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/airplay_ma_smoke.py smoke
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

    def test_ma_drives_airplay_receiver():
        r = _run_scenario("smoke")
        assert r["registered"], r
        assert r["backend"] == "GenericAirPlayPlayer", r
        assert r["playing"]["device"] == "playing", r
        assert r["playing"]["ma"] == "playing", r
        assert r["playing"]["sink_bytes"] > 0, r
        assert r["playing"]["heard_track"] == 1, r
        assert r["paused"]["device"] == "paused", r
        assert r["paused"]["ma"] == "paused", r
        assert r["resumed"]["device"] == "playing", r
        assert abs(r["seek"]["ma_elapsed"] - 30) < 4, r
        assert r["seek"]["device"] == "playing", r
        assert r["volume"]["device"] == 33, r
        assert r["volume"]["ma"] == 33, r
        assert r["stopped"]["device"] in ("stopped", "no_media"), r
        assert r["heard_s"] > 5, r

    def test_bug_queue_play_resumes_paused_airplay():
        r = _run_scenario("smoke")
        assert r["paused"]["device"] == "paused", r
        assert r["queue_play_resumed"], (
            "player_queues/play on a paused Universal Player queue never reaches the "
            "AirPlay protocol player (ProtocolBackedPlayer.play is a no-op)",
            r,
        )

    def test_bug_6664_sonos_volume_echo_keeps_user_level():
        r = _run_scenario("bug6664")
        assert r["echoes_sent"] >= 2, r  # the sim really echoed the queued levels
        assert r["resent_by_ma"] == 0, (
            "MA re-sent Sonos' delayed volume echoes, so the level bounces every tick (#6664)",
            r,
        )
        lost = "MA adopted Sonos' delayed volume echo and lost the user's level (#6664)"
        assert r["final"]["ma"] == 50, (lost, r)
        assert r["final"]["device"] == 50, (lost, r)

    def test_bug_5018_external_volume_is_reflected():
        r = _run_scenario("bug5018")
        assert r["in_session"]["ma"] == 55, ("in-session DACP volume ignored", r)
        assert r["between_sessions"]["ma"] == 25, (
            "a volume change on the speaker between sessions is not reflected (#5018)",
            r,
        )
        assert r["after_teardown"]["ma"] == r["after_teardown"]["device"], (
            "the post-TEARDOWN volume report was dropped (#5018)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------

PROFILES = {
    "smoke": ("airplay/wiim_pro_plus", {"starve_after_ms": {"ms": 0}}),
    "bug6664": (
        "airplay/sonos_era100",
        # the real tick is 45-65 s; 3-4 s keeps the test short and still lands after
        # MA's 2 s echo grace
        {"volume_apply_tick_s": {"min": 3.0, "max": 4.0, "step": 0}, "anchor_400": False},
    ),
    "bug5018": (
        "airplay/wiim_pro_plus",
        {
            "starve_after_ms": {"ms": 0},
            "dacp_report_idle": True,
            "dacp_after_teardown_ms": 1500,
        },
    ),
}


def _patch_ma(fake_bin: str) -> None:
    import music_assistant.providers.airplay.helpers as ap_helpers
    import music_assistant.providers.airplay.pairing as ap_pairing
    import music_assistant.providers.airplay.provider as ap_provider
    import music_assistant.providers.airplay.stream as ap_stream
    from music_assistant.controllers.discovery.controller import DiscoveryController

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    # keep the throwaway server off the LAN
    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]
    ap_provider.AirPlayProvider._register_dacp_service = _noop  # type: ignore[method-assign]

    # MA has no binary override: point every importer of get_cli_binary at the fake
    async def _fake_binary() -> str:
        return fake_bin

    for mod in (ap_helpers, ap_stream, ap_provider, ap_pairing):
        if hasattr(mod, "get_cli_binary"):
            mod.get_cli_binary = _fake_binary  # type: ignore[attr-defined]
    # MA skips loopback addresses; the sim lives on 127.0.0.1
    ap_provider.get_primary_ip_address_from_zeroconf = lambda *_a, **_k: "127.0.0.1"  # type: ignore[attr-defined]
    # the builtin Sendspin provider always binds 8927; move it off the user's MA port
    import music_assistant.providers.sendspin.provider as ss_provider
    from aiosendspin.server.server import SendspinServer
    from zeroconf import IPVersion
    from zeroconf.asyncio import AsyncZeroconf

    ss_provider.SENDSPIN_SERVER_PORT = free_port()  # type: ignore[attr-defined]
    SendspinServer._start_mdns_advertising = _noop  # type: ignore[method-assign]
    SendspinServer._start_mdns_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._create_aiozc = lambda self, config: AsyncZeroconf(  # type: ignore[method-assign]
        interfaces=["127.0.0.1"], ip_version=IPVersion.V4Only
    )


def _service_infos(dev: Any) -> tuple[Any, Any]:
    from zeroconf.asyncio import AsyncServiceInfo

    name = str(dev.identity["name"])
    addr = [socket.inet_aton("127.0.0.1")]
    server = f"seamer-{dev.id}.local."
    airplay = AsyncServiceInfo(
        "_airplay._tcp.local.",
        f"{name}._airplay._tcp.local.",
        port=dev.port,
        properties=dev.txt_airplay(),
        addresses=addr,
        server=server,
    )
    raop = AsyncServiceInfo(
        "_raop._tcp.local.",
        f"{dev.raop_service_name()}._raop._tcp.local.",
        port=dev.port,
        properties=dev.txt_raop(),
        addresses=addr,
        server=server,
    )
    return airplay, raop


async def _scenario(name: str) -> dict[str, Any]:
    import asyncio
    import logging
    import shutil
    import tempfile
    import time

    from music_assistant.constants import DEFAULT_PROVIDERS
    from music_assistant.mass import MusicAssistant
    from zeroconf import ServiceStateChange

    from seamer.families.airplay import fake_cli
    from seamer.simkit.scenario import Scene

    web_port, stream_port = free_port(), free_port()
    assert not {web_port, stream_port} & FORBIDDEN_PORTS
    work = Path(tempfile.mkdtemp(prefix=f"seamer-ma-airplay-{name}-"))
    fake_bin = fake_cli.install(work / "bin")
    _patch_ma(str(fake_bin))

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
                    "airplay": {
                        "type": "player",
                        "domain": "airplay",
                        "instance_id": "airplay",
                        "enabled": True,
                        "values": {"log_level": "DEBUG"},
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
    for _n in (
        "music_assistant.AirPlay",
        "music_assistant.airplay",
        "music_assistant.player_queues",
    ):
        logging.getLogger(_n).setLevel(logging.DEBUG)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
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
            airplay_info, raop_info = _service_infos(dev)

            async def _find(service_type: str, *_a: Any, **_k: Any) -> Any:
                return raop_info if service_type.startswith("_raop") else airplay_info

            await mass.start()
            mass.discovery.async_find_mdns_service = _find  # type: ignore[method-assign]
            prov = mass.get_provider("airplay")
            assert prov is not None, "airplay provider did not load"
            prov._dacp_info = None  # never registered (see _patch_ma)
            dacp_port = prov._dacp_server.sockets[0].getsockname()[1]
            await dev.sim_action("set_dacp_target", {"host": "127.0.0.1", "port": dacp_port})
            log(f"device {dev.device_id()} on {dev.base_url}, MA DACP port {dacp_port}")
            await prov.on_mdns_service_state_change(
                airplay_info.name, ServiceStateChange.Added, airplay_info
            )
            pid = "ap" + dev.device_id().replace(":", "").lower()

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 30)
            result["registered"] = ok
            result["player_id"] = pid
            if not ok:
                return result
            # the AirPlay player is a protocol player: the Universal Player wrapper that
            # MA creates for it (about 15 s later) owns the queue
            await until(lambda: player().protocol_parent_id is not None, 40)
            ap_pid, pid = pid, str(player().protocol_parent_id or pid)
            result["queue_id"] = pid
            await until(lambda: mass.player_queues.get(pid) is not None, 10)

            def player() -> Any:
                return mass.players.get_player(ap_pid)

            result["backend"] = type(player()).__name__
            log(f"player registered: {player().display_name} ({result['backend']})")
            await scene.media_server()
            track = scene.media.track(1, 300.0)
            url = scene.media_url(track.path)
            await mass.player_queues.play_media(pid, url)
            playing = await until(lambda: dev.transport.state.value == "playing", 30)
            if not playing:
                result["error"] = "device never reached PLAYING"
                result["device"] = dev.status()
                return result

            if name == "smoke":
                await _smoke(mass, dev, pid, player, until, log, result)
            elif name == "bug6664":
                await _bug6664(mass, dev, pid, player, until, log, result)
            elif name == "bug5018":
                await _bug5018(mass, dev, pid, url, player, until, log, result)
            heard = [e for e in scene.rec.events if e.kind == "sink" and e.name == "heard"]
            result["heard_s"] = round(
                sum(float(e.data.get("tracks", {}).get("1", 0.0)) for e in heard), 1
            )
            result["wire_ops"] = sorted(
                {
                    str(e.data.get("path", "")).removeprefix("/_wire/")
                    for e in scene.rec.events
                    if e.kind == "cmd" and str(e.data.get("path", "")).startswith("/_wire/")
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
    await asyncio.sleep(4)
    sink = dev.sink.snapshot()
    result["playing"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
        "sink_bytes": sink["bytes"],
        "sink_played_s": sink["played_s"],
        "sink_decoder": sink["decoder"],
        "heard_track": sink["track"],
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
    await asyncio.sleep(1)
    # resume the way player_queues/play (and play_pause) does it
    await mass.player_queues.play(pid)
    await until(lambda: dev.transport.state.value == "playing", 3)
    result["queue_play_resumed"] = dev.transport.state.value == "playing"
    log(f"player_queues.play resumed the speaker: {result['queue_play_resumed']}")
    if not result["queue_play_resumed"]:
        await mass.players.cmd_play(pid)  # players/cmd/play reaches the AirPlay player
    await until(
        lambda: (
            dev.transport.state.value == "playing"
            and player().playback_state == PlaybackState.PLAYING
        ),
        15,
    )
    result["resumed"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
    }
    log(f"resumed: {result['resumed']}")

    await mass.player_queues.seek(pid, 30)
    await asyncio.sleep(1)
    await until(lambda: dev.transport.state.value == "playing", 15)
    await asyncio.sleep(2)
    queue = mass.player_queues.get(pid)
    result["seek"] = {
        "target": 30,
        "ma_elapsed": round(queue.corrected_elapsed_time, 1) - 2,
        "device": dev.transport.state.value,
        "sink_track": dev.sink.snapshot()["track"],
    }
    log(f"seek: {result['seek']}")

    await mass.players.cmd_volume_set(pid, 33)
    await until(lambda: dev.volume == 33 and player().volume_level == 33, 10)
    result["volume"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"volume: {result['volume']}")

    await mass.player_queues.stop(pid)
    await until(lambda: dev.transport.state.value not in ("playing", "paused"), 15)
    result["stopped"] = {
        "device": dev.transport.state.value,
        "ma": str(player().playback_state.value),
    }
    log(f"stopped: {result['stopped']}")


async def _bug6664(mass, dev, pid, player, until, log, result) -> None:
    import asyncio

    def requested() -> list[Any]:
        return [e.data.get("level") for e in dev.rec.rec.events if e.name == "volume_requested"]

    await asyncio.sleep(4)  # past the start-of-stream volume push and its grace
    result["start"] = {"device": dev.volume, "ma": player().volume_level}
    sent_before, req_before = len(dev.dacp_sent), len(requested())
    await mass.players.cmd_volume_set(pid, 40)
    await asyncio.sleep(0.6)
    await mass.players.cmd_volume_set(pid, 50)  # the user's final choice
    # each 3-4 s tick applies the queue and echoes every level, 30 ms apart; watch 5 ticks
    await asyncio.sleep(20)
    echoes = dev.dacp_sent[sent_before:]
    result["echoes_sent"] = len(echoes)
    result["echoes"] = [e["command"].rsplit("=", 1)[1] for e in echoes]
    result["requested_by_ma"] = requested()[req_before:]
    result["resent_by_ma"] = len(result["requested_by_ma"]) - 2  # beyond the user's two
    result["device_levels"] = [
        e.data.get("new") for e in dev.rec.rec.events if e.kind == "state" and e.name == "volume"
    ]
    result["final"] = {"device": dev.volume, "ma": player().volume_level}
    log(
        f"6664: final {result['final']} MA requests {result['requested_by_ma']} "
        f"device levels {result['device_levels']}"
    )
    await mass.player_queues.stop(pid)


async def _bug5018(mass, dev, pid, url, player, until, log, result) -> None:
    import asyncio

    await asyncio.sleep(4)  # past the start-of-stream volume push and its grace
    result["active_remote"] = dev.status()["session"]["active_remote"]

    await dev.sim_action("press", {"button": "volume", "level": 55})
    await until(lambda: player().volume_level == 55, 5)
    result["in_session"] = {"device": dev.volume, "ma": player().volume_level}
    log(f"in session: {result['in_session']}")

    await mass.player_queues.stop(pid)
    await until(lambda: dev.session is None, 15)
    # the post-TEARDOWN report (Denon-style, 1.5 s) with the session's id
    await asyncio.sleep(3)
    result["after_teardown"] = {
        "device": dev.volume,
        "ma": player().volume_level,
        "dacp": dev.dacp_sent[-1],
    }
    log(f"after teardown: {result['after_teardown']}")

    await dev.sim_action("press", {"button": "volume", "level": 25})
    await until(lambda: player().volume_level == 25, 5)
    result["between_sessions"] = {
        "device": dev.volume,
        "ma": player().volume_level,
        "dacp": dev.dacp_sent[-1],
    }
    log(f"between sessions: {result['between_sessions']}")


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
