"""Bug hunt: known DLNA / UPnP renderer issues replayed against real Music Assistant.

Same harness as ``tests/e2e/dlna_ma_smoke.py``: MA runs in a subprocess with its own
interpreter, loopback only, on free high ports (never 8095), with a throwaway data dir
and discovery switched off. The device is handed to the dlna provider with the sim's UDN
and description URL. The queue lives on the Universal Player that wraps the DLNA
protocol player. Each test asserts the *correct* behaviour, so a failure on MA dev means
the bug reproduces. Skipped unless ``MA_REPO`` is set.

Gapless and next-track (flow mode off):

- ``gapless_next`` (control, MA#6459): SetNextAVTransportURI works; the device advances
  by itself and MA's queue index and elapsed time follow it.
- ``setnext_ignored`` (MA#6459, dsh:10-12): the renderer answers SetNext with 200 but
  stops at the end of the track. MA must start track 2 itself.
- ``setnext_playing_only`` (MA#5852, dsh:11, Samsung): SetNext gives 701 unless the
  device is already PLAYING, and the device takes 3 s to load. MA sends SetNext about
  1 s after Play, so it must retry (or start track 2 itself) for track 2 to play.
- ``gap_transitioning`` (dsh:7): a 4 s TRANSITIONING gap between tracks. MA should
  follow the advance without reloading track 2 or skipping to track 3.

Position, pause and play:

- ``reltime_stuck_resume`` (MA#6874): flow mode, RelTime stuck at 0:00:00. After pause
  and resume (players/cmd/play), MA's elapsed time must not jump back.
- ``resume_after_pause`` (MA#6756, linkplay hunt finding 6): plain DLNA, events on.
  Queue play() after a pause must resume the device (players/cmd/play as control).
- ``pause_chunked`` (dsh:6): Pause gives 701 on a chunked stream (MA's flow stream). MA
  must not show PAUSED while the device keeps playing.
- ``play_slow_ack`` (dsh:3, Eversolo): Play answers only once audio plays (about 7 s,
  past the client's 5 s SOAP timeout). Device and MA should both end up PLAYING.

Eventing (GENA) and volume:

- ``gena_short_timeout`` (dsh:42, async_upnp_client#302): the device grants a 30 s
  subscription. Renewals should come at a sane rate, not in a busy loop.
- ``gena_new_sid`` (dsh:43, DL-04): a renewal answers with a new SID. Events on the new
  SID must still reach MA.
- ``gena_lost`` (device reboot, MA#5565): the device forgets its subscriptions. A local
  pause on the device must show up in MA within one poll interval (30 s).
- ``no_rc_local_volume`` (ma:dlna#3474): RenderingControl sends no events. A volume
  change on the device itself must show up in MA within a poll interval or two.

Device quirks:

- ``samsung_tv`` (DL-07, DL-08, MA#6543): HEAD check on SetAVTransportURI and a
  protocolInfo filter. Playback must start and audio must flow.
- ``power_cycle`` (MA#5565): the renderer goes offline, comes back and re-announces
  itself. MA must play to it again without a restart.

Run::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/hunt/dlna_hunt.py

or one scenario (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/hunt/dlna_hunt.py gapless_next
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
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


def _secs(v: float | None) -> float:
    """A measured delay, or infinity when it never happened."""
    return float("inf") if v is None else v


if "pytest" in sys.modules:
    import pytest

    pytestmark = pytest.mark.skipif(not MA_REPO, reason="set MA_REPO to a Music Assistant tree")

    def test_6459_gapless_setnext_followed():
        r = _run_scenario("gapless_next")
        assert r["setnext_calls"] >= 1, r
        assert r["device_advanced"], r
        assert _secs(r["ma_followed_s"]) < 5, r
        assert _secs(r["elapsed_err_s"]) < 3, r

    def test_6459_setnext_ignored_next_track_starts():
        r = _run_scenario("setnext_ignored")
        assert r["ignored_fired"], r
        assert r["device_tracks_started"] >= 2, (
            "the renderer stopped after track 1 and MA never started track 2 (#6459)",
            r,
        )
        assert r["ma_index"] == 1, r

    def test_5852_setnext_701_while_loading_next_track_starts():
        r = _run_scenario("setnext_playing_only")
        assert r["device_tracks_started"] >= 2, (
            "SetNext was refused with 701 while the renderer was still loading; MA never "
            "retried it and never started track 2 itself",
            r,
        )
        assert r["ma_index"] == 1, r

    def test_dsh7_transitioning_gap_followed():
        r = _run_scenario("gap_transitioning")
        assert r["device_advanced"], r
        assert r["ma_index"] == 1, r
        assert r["extra_seturi"] == 0, r

    def test_6874_elapsed_does_not_jump_back_on_resume():
        r = _run_scenario("reltime_stuck_resume")
        assert r["stuck_fired"], r
        assert all(c["ok"] for c in r["cycles"]), (
            "MA's elapsed time jumps back after pause/resume on a renderer whose RelTime "
            "is stuck at 0 (#6874)",
            r,
        )

    def test_queue_play_resumes_paused_dlna():
        r = _run_scenario("resume_after_pause")
        assert all(c["device_paused"] and c["ma_paused"] for c in r["cycles"]), r
        assert all(c["to_playing_s"] is not None for c in r["cycles"]), (
            "queue play() on a paused DLNA Universal Player sends nothing to the device",
            r,
        )

    def test_dsh6_pause_on_chunked_stream():
        r = _run_scenario("pause_chunked")
        assert r["stream_chunked"], r
        assert not r["ma_paused_device_playing"], (
            "Pause was refused (701, chunked stream) yet MA shows PAUSED while the device "
            "keeps playing",
            r,
        )
        assert r["device_after_pause"] != "playing", r

    def test_dsh3_slow_play_ack():
        r = _run_scenario("play_slow_ack")
        assert r["device"] == "playing", r
        assert r["ma"] == "playing", r
        assert r["sink_played_s"] > 3, r

    def test_dsh42_short_gena_timeout_no_busy_loop():
        r = _run_scenario("gena_short_timeout")
        assert r["renewals_in_window"] <= 4, (
            "a 30 s granted subscription makes MA renew in a busy loop",
            r,
        )
        assert r["volume_seen"], r

    def test_dsh43_new_sid_on_renew_events_still_arrive():
        r = _run_scenario("gena_new_sid")
        assert r["renewed_with_new_sid"], r
        assert _secs(r["volume_seen_s"]) < 5, r

    def test_5565_lost_subscription_local_pause_seen():
        r = _run_scenario("gena_lost")
        assert r["dropped"] >= 1, r
        assert _secs(r["pause_seen_s"]) <= 35, (
            "after the renderer lost its subscriptions MA did not see a local pause "
            "within one poll interval",
            r,
        )

    def test_dlna3474_no_rc_events_local_volume_seen():
        r = _run_scenario("no_rc_local_volume")
        assert _secs(r["volume_seen_s"]) <= 65, (
            "a renderer without RenderingControl events changes volume locally; MA never "
            "polls RC while subscribed and keeps showing the old level",
            r,
        )

    def test_samsung_tv_plays():
        r = _run_scenario("samsung_tv")
        assert r["device"] == "playing", r
        assert r["sink_played_s"] > 3, r
        assert r["faults"] == [], r

    def test_5565_power_cycle_plays_again():
        r = _run_scenario("power_cycle")
        assert r["first_play"], r
        assert r["after"]["device"] == "playing", (
            "after the renderer came back MA could not play to it again (#5565)",
            r,
        )
        assert r["after"]["sink_played_s"] > 1, r


# -- scenario side (MA's venv) --------------------------------------------------------

TRACK_S = 25.0
PLAIN = {"play_blocks_until_audio_s": 0.0, "openhome": False}

SCENARIOS: dict[str, tuple[str, dict[str, Any], bool | None]] = {
    # name: (profile, knobs, flow mode; None = MA's default)
    "gapless_next": ("dlna/eversolo", PLAIN, False),
    "setnext_ignored": ("dlna/eversolo", {**PLAIN, "set_next": "ignored"}, False),
    "setnext_playing_only": (
        "dlna/eversolo",
        {**PLAIN, "set_next": "playing_only", "load_delay_s": 3.0},
        False,
    ),
    "gap_transitioning": (
        "dlna/eversolo",
        {**PLAIN, "transition_gap_s": 4.0, "track_gap_state": "transitioning"},
        False,
    ),
    "reltime_stuck_resume": ("dlna/eversolo", {**PLAIN, "reltime": "stuck"}, True),
    "resume_after_pause": ("dlna/eversolo", PLAIN, True),
    "pause_chunked": ("dlna/eversolo", {**PLAIN, "pause_needs_content_length": True}, True),
    "play_slow_ack": (
        "dlna/eversolo",
        {"play_blocks_until_audio_s": 10.0, "load_delay_s": 7.0},
        True,
    ),
    "gena_short_timeout": ("dlna/eversolo", {**PLAIN, "granted_timeout_s": 30}, None),
    "gena_new_sid": (
        "dlna/eversolo",
        {**PLAIN, "granted_timeout_s": 70, "new_sid_on_renew": True},
        None,
    ),
    "gena_lost": ("dlna/eversolo", PLAIN, True),
    "no_rc_local_volume": ("dlna/eversolo", {**PLAIN, "no_rc_events": True}, True),
    "samsung_tv": ("dlna/samsung_tv", {"range_rerequest_after_bytes": 0}, True),
    "power_cycle": ("dlna/eversolo", PLAIN, True),
}


def _events(c: Any, kind: str | None, name: str) -> list[Any]:
    return [e for e in c.scene.rec.events if (kind is None or e.kind == kind) and e.name == name]


def _fired(c: Any, knob: str) -> int:
    return sum(
        1
        for e in c.scene.rec.events
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    )


def _faults(c: Any) -> list[str]:
    return [f"{e.data.get('action')}:{e.data.get('code')}" for e in _events(c, None, "upnp_fault")]


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

    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]

    profile, knobs, flow = SCENARIOS[name]
    web_port, stream_port = free_port(), free_port()
    assert not {web_port, stream_port} & FORBIDDEN_PORTS
    work = Path(tempfile.mkdtemp(prefix=f"seamer-hunt-{name}-"))
    data_dir = work / "data"
    data_dir.mkdir()
    providers: dict[str, Any] = {
        "dlna": {"type": "player", "domain": "dlna", "instance_id": "dlna", "enabled": True},
        # sendspin would try the user's port 8927; keep it off
        "sendspin": {
            "type": "player",
            "domain": "sendspin",
            "instance_id": "sendspin",
            "enabled": False,
        },
    }
    for p in providers.values():
        p["values"] = {}
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
                "providers": providers,
            }
        )
    )
    logging.basicConfig(
        level=logging.INFO,
        filename=work / "server.log",
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    for lg in (
        "music_assistant.providers.dlna",
        "music_assistant.player_queues",
        "music_assistant.players",
    ):
        logging.getLogger(lg).setLevel(logging.DEBUG)
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
            udn = dev.udn
            await prov._device_discovered(udn, location)  # type: ignore[attr-defined]
            ok = await until(
                lambda: (p := mass.players.get_player(udn)) is not None and p.available, 30
            )
            result["registered"] = ok
            if not ok:
                return result
            proto = mass.players.get_player(udn)
            result["backend"] = type(proto).__name__
            await until(lambda: proto.protocol_parent_id is not None, 40)
            qid = proto.protocol_parent_id or udn
            await until(lambda: mass.player_queues.get(qid) is not None, 15)
            await until(lambda: bool(dev.gena.subs), 10)
            result["queue_id"] = qid
            if flow is not None:
                await _set_flow(mass, [qid, udn], flow, result)
            log(f"player registered ({result['backend']}), queue {qid}")
            ctx = Ctx(mass, scene, dev, qid, udn, prov, location, until, log, result)
            await globals()[f"_s_{name}"](ctx)
            result["soap_actions"] = sorted(
                {
                    m.group(1)
                    for e in scene.rec.events
                    if e.kind == "cmd" and str(e.data.get("path", "")).endswith("/control")
                    for m in [re.search(r"<u:(\w+)", str(e.data.get("body", "")))]
                    if m
                }
            )
            result["faults"] = _faults(ctx)
            q = mass.player_queues.get(qid)
            result["queue_flow_mode"] = getattr(q, "flow_mode", None)
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


class Ctx:
    def __init__(self, mass, scene, dev, qid, udn, prov, location, until, log, result) -> None:
        self.mass, self.scene, self.dev, self.qid, self.udn = mass, scene, dev, qid, udn
        self.prov, self.location = prov, location
        self.until, self.log, self.result = until, log, result

    def ui(self) -> Any:
        """What MA's UI shows for the queue's player."""
        return self.mass.players.get_player_state(self.qid)

    def ma_state(self) -> str:
        return str(self.ui().playback_state.value)

    def dev_state(self) -> str:
        return str(self.dev.transport.state.value)

    def queue(self) -> Any:
        return self.mass.player_queues.get(self.qid)

    def seturi(self) -> int:
        return len(_events(self, "cmd", "set_uri"))

    def setnext(self) -> int:
        return len(_events(self, "cmd", "set_next"))

    async def tracks(self, n: int, seconds: float = TRACK_S) -> list[str]:
        await self.scene.media_server()
        return [self.scene.media_url(self.scene.media.track(i + 1, seconds).path) for i in range(n)]

    async def play(self, n: int = 1, seconds: float = TRACK_S) -> bool:
        urls = await self.tracks(n, seconds)
        await self.mass.player_queues.play_media(self.qid, urls if n > 1 else urls[0])
        ok = await self.until(lambda: self.dev_state() == "playing", 30)
        await self.until(lambda: self.ma_state() == "playing", 15)
        self.result["started"] = ok
        return ok


async def _set_flow(mass: Any, pids: list[str], on: bool, result: dict[str, Any]) -> None:
    from music_assistant.constants import CONF_FLOW_MODE

    for p in pids:
        with __import__("contextlib").suppress(KeyError):
            mass.config.set_raw_player_config_value(p, CONF_FLOW_MODE, on)
        if (pl := mass.players.get_player(p)) is not None:
            pl.__dict__.pop("flow_mode", None)  # cached_property
    result["flow_mode"] = {p: getattr(mass.players.get_player(p), "flow_mode", None) for p in pids}


async def _timed(c: Ctx, pred: Any, timeout: float) -> float | None:
    import time

    t = time.monotonic()
    ok = await c.until(pred, timeout, 0.1)
    return round(time.monotonic() - t, 1) if ok else None


# -- gapless / next track ---------------------------------------------------------------


async def _advance(c: Ctx, wait_s: float = 50) -> float | None:
    """Play two tracks; return when the device starts track 2 (seconds after play)."""
    import time

    await c.play(2, TRACK_S)
    t = time.monotonic()
    ok = await c.until(lambda: c.dev.transport.tracks_started >= 2, wait_s)
    c.result["device_advanced"] = ok
    return round(time.monotonic() - t, 1) if ok else None


async def _s_gapless_next(c: Ctx) -> None:
    import asyncio

    await _advance(c)
    c.result["ma_followed_s"] = await _timed(c, lambda: c.queue().current_index == 1, 15)
    await asyncio.sleep(5)
    truth = c.dev.transport.truth_position()
    ma = c.queue().corrected_elapsed_time
    c.result["elapsed"] = {"device": round(truth or 0, 1), "ma": round(ma, 1)}
    c.result["elapsed_err_s"] = round(abs(ma - (truth or 0)), 1)
    c.result["setnext_calls"] = c.setnext()
    c.result["seturi_calls"] = c.seturi()
    c.result["ma_index"] = c.queue().current_index


async def _s_setnext_ignored(c: Ctx) -> None:
    await c.play(2, TRACK_S)
    await c.until(lambda: c.dev.transport.tracks_started >= 2, TRACK_S + 40)
    await c.until(lambda: c.queue().current_index == 1, 10)
    c.result["ignored_fired"] = _fired(c, "set_next") > 0
    c.result["device_tracks_started"] = c.dev.transport.tracks_started
    c.result["device_state"] = c.dev_state()
    c.result["ma_index"] = c.queue().current_index
    c.result["ma_state"] = c.ma_state()
    c.result["setnext_calls"] = c.setnext()
    c.result["seturi_calls"] = c.seturi()


async def _s_setnext_playing_only(c: Ctx) -> None:
    await c.play(2, TRACK_S)
    await c.until(lambda: c.dev.transport.tracks_started >= 2, TRACK_S + 40)
    await c.until(lambda: c.queue().current_index == 1, 10)
    c.result["refused_701"] = _fired(c, "set_next")
    c.result["setnext_calls"] = c.setnext()
    c.result["seturi_calls"] = c.seturi()
    c.result["device_tracks_started"] = c.dev.transport.tracks_started
    c.result["device_state"] = c.dev_state()
    c.result["ma_index"] = c.queue().current_index
    c.result["ma_state"] = c.ma_state()


async def _s_gap_transitioning(c: Ctx) -> None:
    import asyncio

    await _advance(c)
    seturi0 = c.seturi()
    states: list[str] = []
    for _ in range(80):  # 8 s through the gap and into track 2
        s = f"{c.dev_state()}/{c.ma_state()}/{c.queue().current_index}"
        if not states or states[-1] != s:
            states.append(s)
        await asyncio.sleep(0.1)
    await c.until(lambda: c.queue().current_index == 1, 10)
    c.result["timeline"] = states
    c.result["extra_seturi"] = c.seturi() - seturi0
    c.result["ma_index"] = c.queue().current_index
    c.result["device_tracks_started"] = c.dev.transport.tracks_started


# -- position, pause, play ----------------------------------------------------------------


async def _s_reltime_stuck_resume(c: Ctx) -> None:
    import asyncio

    await c.play(1, 300.0)
    cycles = []
    for _ in range(2):
        await asyncio.sleep(10)
        await c.mass.player_queues.pause(c.qid)
        await c.until(lambda: c.dev_state() == "paused", 10)
        await c.until(lambda: c.ma_state() == "paused", 10)
        await asyncio.sleep(1)
        paused_at = c.queue().corrected_elapsed_time
        truth_paused = c.dev.transport.truth_position() or 0
        await asyncio.sleep(3)
        # players/cmd/play reaches the DLNA output; queue play() on a paused Universal
        # Player does not (see resume_after_pause), which would mask this check
        await c.mass.players.cmd_play(c.qid)
        resumed = await c.until(lambda: c.dev_state() == "playing", 10)
        samples = []
        for _ in range(6):  # 3 s after resume
            await asyncio.sleep(0.5)
            samples.append(round(c.queue().corrected_elapsed_time, 1))
        lowest = min(samples)
        cycles.append(
            {
                "ma_paused": round(paused_at, 1),
                "device_paused": round(truth_paused, 1),
                "ma_after_resume": samples,
                "device_after": round(c.dev.transport.truth_position() or 0, 1),
                "resumed": resumed,
                "ma_state": c.ma_state(),
                "ok": resumed and lowest >= paused_at - 1 and samples[-1] > samples[0],
            }
        )
        c.log(f"cycle {cycles[-1]}")
    c.result["cycles"] = cycles
    c.result["stuck_fired"] = c.dev.knobs.get("reltime") == "stuck"


async def _s_resume_after_pause(c: Ctx) -> None:
    import asyncio

    await c.play(1, 300.0)
    cycles = []
    for how in ("queue", "players"):
        await asyncio.sleep(5)
        await c.mass.player_queues.pause(c.qid)
        paused = await c.until(lambda: c.dev_state() == "paused", 10)
        ma_paused = await c.until(lambda: c.queue().state.value == "paused", 10)
        await asyncio.sleep(3)
        n = len(c.scene.rec.events)
        if how == "queue":
            await c.mass.player_queues.play(c.qid)
        else:
            await c.mass.players.cmd_play(c.qid)
        to_play = await _timed(c, lambda: c.dev_state() == "playing", 10)
        cycles.append(
            {
                "via": how,
                "device_paused": paused,
                "ma_paused": ma_paused,
                "to_playing_s": to_play,
                "device_cmds": [
                    e.name
                    for e in list(c.scene.rec.events)[n:]
                    if e.kind == "cmd" and not e.name.startswith("GET")
                ][:8],
            }
        )
        c.log(f"cycle {cycles[-1]}")
        if to_play is None:  # get it playing again for the next cycle
            await c.mass.players.cmd_play(c.qid)
            await c.until(lambda: c.dev_state() == "playing", 10)
    c.result["cycles"] = cycles


async def _s_pause_chunked(c: Ctx) -> None:
    import asyncio

    await c.play(1, 300.0)
    await asyncio.sleep(3)
    c.result["stream_chunked"] = bool(c.dev.stream_chunked or c.dev.stream_length is None)
    err = None
    try:
        await c.mass.player_queues.pause(c.qid)
    except Exception as e:  # recorded: the outcome is the finding
        err = f"{type(e).__name__}: {e}"[:200]
    await asyncio.sleep(4)
    c.result["pause_error"] = err
    c.result["device_after_pause"] = c.dev_state()
    c.result["ma_after_pause"] = c.ma_state()
    c.result["ma_paused_device_playing"] = c.ma_state() == "paused" and c.dev_state() == "playing"
    c.result["pause_701"] = _fired(c, "pause_needs_content_length")
    # a poll later: does MA notice?
    await asyncio.sleep(32)
    c.result["after_poll"] = {"device": c.dev_state(), "ma": c.ma_state()}


async def _s_play_slow_ack(c: Ctx) -> None:
    import asyncio

    urls = await c.tracks(1, 300.0)
    err = None
    try:
        await c.mass.player_queues.play_media(c.qid, urls[0])
    except Exception as e:  # recorded
        err = f"{type(e).__name__}: {e}"[:200]
    await c.until(lambda: c.dev_state() == "playing", 30)
    await asyncio.sleep(12)
    c.result["play_error"] = err
    c.result["device"] = c.dev_state()
    c.result["ma"] = c.ma_state()
    c.result["sink_played_s"] = c.dev.sink.snapshot()["played_s"] or 0.0
    c.result["play_calls"] = sum(
        1
        for e in c.scene.rec.events
        if e.kind == "cmd" and "<u:Play" in str(e.data.get("body", ""))
    )
    c.result["tracks_started"] = c.dev.transport.tracks_started


# -- eventing and volume --------------------------------------------------------------------


async def _local_volume(c: Ctx, level: int, timeout: float) -> float | None:
    await c.dev.sim_action("local_volume", {"level": level})
    return await _timed(c, lambda: c.ui().volume_level == level, timeout)


async def _s_gena_short_timeout(c: Ctx) -> None:
    import asyncio

    n0 = len(_events(c, None, "gena_renew"))
    await asyncio.sleep(20)
    c.result["renewals_in_window"] = len(_events(c, None, "gena_renew")) - n0
    c.result["subscribes"] = len(_events(c, None, "gena_subscribe"))
    c.result["volume_seen"] = await _local_volume(c, 37, 10) is not None


async def _s_gena_new_sid(c: Ctx) -> None:
    import asyncio

    ok = await c.until(lambda: _fired(c, "new_sid_on_renew") > 0, 40)
    c.result["renewed_with_new_sid"] = ok
    await asyncio.sleep(2)
    c.result["live_subs"] = len(c.dev.gena.subs)
    c.result["volume_seen_s"] = await _local_volume(c, 37, 40)
    c.result["renewals"] = len(_events(c, None, "gena_renew"))
    c.result["subscribes"] = len(_events(c, None, "gena_subscribe"))


async def _s_gena_lost(c: Ctx) -> None:
    import asyncio

    await c.play(1, 300.0)
    await asyncio.sleep(3)
    c.result["dropped"] = await c.dev.sim_action("drop_subscriptions", {})
    await asyncio.sleep(1)
    await c.dev.transport.pause()
    c.result["pause_seen_s"] = await _timed(c, lambda: c.ma_state() == "paused", 100)
    c.result["volume_seen_s"] = await _local_volume(c, 37, 70)
    c.result["resubscribed"] = len(c.dev.gena.subs)
    c.result["subscribes"] = len(_events(c, None, "gena_subscribe"))
    c.result["ma_state"] = c.ma_state()


async def _s_no_rc_local_volume(c: Ctx) -> None:
    await c.play(1, 300.0)
    c.result["volume_seen_s"] = await _local_volume(c, 37, 100)
    c.result["ma_volume"] = c.ui().volume_level
    c.result["device_volume"] = c.dev.volume


# -- device quirks --------------------------------------------------------------------------


async def _s_samsung_tv(c: Ctx) -> None:
    import asyncio

    await c.play(1, 300.0)
    await asyncio.sleep(8)
    c.result["device"] = c.dev_state()
    c.result["ma"] = c.ma_state()
    c.result["sink_played_s"] = c.dev.sink.snapshot()["played_s"] or 0.0
    c.result["head_checks"] = [e.data.get("status") for e in _events(c, None, "head_check")]
    c.result["faults"] = _faults(c)


async def _s_power_cycle(c: Ctx) -> None:
    import asyncio

    from seamer.simkit.kits.http import HttpServer

    c.result["first_play"] = await c.play(1, 300.0)
    c.result["first_url"] = c.dev.sink.audible.url
    await asyncio.sleep(3)
    dev = c.dev
    port = dev.port
    # power off: the listener goes away, playback and subscriptions are gone
    await dev.transport.stop()
    dev.gena.subs.clear()
    assert dev.server is not None
    await dev.server.stop()
    c.result["unavailable_s"] = await _timed(
        c, lambda: not c.mass.players.get_player(c.udn).available, 90
    )
    c.result["ma_while_off"] = c.ma_state()
    await asyncio.sleep(5)
    # power on: same port, same UDN; an ssdp:alive hands it to the provider again
    app = dev.build_app()
    app.add_routes(dev._sim_routes())
    dev.server = HttpServer(app, dev.host, port)
    await dev.server.start()
    await c.prov._device_discovered(c.udn, c.location)
    c.result["available_again_s"] = await _timed(
        c, lambda: c.mass.players.get_player(c.udn).available, 60
    )
    await asyncio.sleep(3)
    err = None
    try:
        urls = await c.tracks(1, 300.0)
        await c.mass.player_queues.play_media(c.qid, urls[0])
    except Exception as e:  # recorded
        err = f"{type(e).__name__}: {e}"[:200]
    await c.until(lambda: c.dev_state() == "playing", 30)
    await asyncio.sleep(4)
    c.result["after"] = {
        "error": err,
        "device": c.dev_state(),
        "ma": c.ma_state(),
        "sink_played_s": dev.sink.snapshot()["played_s"] or 0.0,
        "sink_url_changed": dev.sink.audible.url != c.result.get("first_url"),
        "resubscribed": len(dev.gena.subs),
    }


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "gapless_next"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
