"""Bug hunt: known WiiM / LinkPlay / Arylic issues replayed against real Music Assistant.

Same harness as ``tests/e2e/linkplay_ma_smoke.py`` (MA in a subprocess with its own
interpreter, loopback only, free high ports, throwaway data dir, discovery switched
off). Each test asserts the *correct* behaviour, so a failure on MA dev means the bug
reproduces. Skipped unless ``MA_REPO`` is set.

Official WiiM path (wiim provider -> ``WiimPlayer``, wiim SDK over UPnP + HTTPS):

- ``art_unknow`` (lq:Q13, L80): the WiiM app plays its own stream with no cover; the
  device reports ``albumArtURI`` ``unknow``. MA should not publish it as the art URL.
- ``art_selfsigned`` (L58, L61, ``self_hosted_art``): AirPlay plays; the cover is on the
  device's self-signed HTTPS listener with no Content-Type. MA should publish an art URL
  a browser can load (proxied), not the raw device URL.
- ``advance_drop`` (lq:Q35, MA#6557, ``drop_advance_event``): the device advances to the
  SetNext URI without any AVTransport event; MA's queue should follow by polling.
- ``advance_noevents`` (MA#5900, MA#5634: a firewall blocked GENA, ``notify_delivery``
  drop_silently): no NOTIFY at all; MA's queue should still follow the natural advance.

Generic LinkPlay path (wiim provider -> ``LinkPlayPlayer`` shell via pywiim, playback
over MA's DLNA provider), profile ``arylic_a31`` (http only, no SetNext, no events,
RelTime 0, Play after pause may fault 701):

- ``arylic_register`` (MA#4374): an HTTP-only module registers as a LinkPlay player.
- ``arylic_resume`` (MA#6425): pause / resume three times.
- ``arylic_noflow_advance`` (MA#6425 follow-up): flow mode off, two tracks; MA must start
  track 2 itself because the device refuses SetNext.
- ``arylic_flow_drift`` (MA#6425 follow-up): flow mode, two tracks, then a pause; MA's
  current item and elapsed time should match what the sink actually plays.
- ``arylic_long_pause`` (MA#6756 comment): flow mode, resume after a 40 s pause vs a
  5 s pause; time from command to audio.
- ``arylic_evented_resume`` (control for the two above): the same A31 with state events
  on, so MA sees PAUSED at once; queue play() must resume the device.

The official-path scenarios point every pywiim client (including the WiiM player's
command client used for group reads on each poll) at the sim's API port; without that,
each poll stalls ~14 s on a dead default port and polling-based results are wrong.

Run::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/hunt/linkplay_hunt.py

or one scenario (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/hunt/linkplay_hunt.py art_unknow
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


if "pytest" in sys.modules:
    import pytest

    pytestmark = pytest.mark.skipif(not MA_REPO, reason="set MA_REPO to a Music Assistant tree")

    def test_q13_unknow_art_not_published():
        r = _run_scenario("art_unknow")
        assert r["device_art"] == "unknow", r  # the sim really sent the sentinel
        assert r["ma"]["title"], r  # MA picked up the app's stream
        art = r["ma"]["image_url"] or ""
        assert not art.rstrip("/").lower().endswith(("unknow", "unknown", "un_known")), (
            "MA publishes the device's 'unknow' placeholder as the cover URL (lq:Q13)",
            r,
        )

    def test_l58_selfsigned_device_art_is_loadable():
        r = _run_scenario("art_selfsigned")
        assert r["device_art"].startswith("https://"), r
        assert r["ma"]["active_source"] == "airplay", r
        assert r["published_fetch"]["ok"], (
            "MA hands out the device's self-signed HTTPS cover URL; a verifying client "
            "(browser) cannot load it (L58)",
            r,
        )

    def test_q35_advance_without_event_followed():
        r = _run_scenario("advance_drop")
        assert r["dropped"], r
        assert r["device_advanced"], r
        assert r["ma_followed_s"] is not None, r
        assert r["ma_followed_s"] < 12, r

    def test_5900_advance_without_any_notify_followed():
        r = _run_scenario("advance_noevents")
        assert r["device_advanced"], r
        assert r["ma_followed_s"] is not None, r
        assert r["ma_followed_s"] < 12, r

    def test_4374_http_only_linkplay_registers():
        r = _run_scenario("arylic_register")
        assert r["registered"], r
        assert r["backend"] == "LinkPlayPlayer", r

    def test_6425_arylic_resume_after_pause():
        r = _run_scenario("arylic_resume")
        # MA reloads the stream (SetAVTransportURI + Play) on resume, so the device's
        # 701 refusal of a bare Play after pause never comes into play.
        assert all(x["resumed"] for x in r["cycles"]), r

    def test_6425_arylic_no_flow_next_track_starts():
        r = _run_scenario("arylic_noflow_advance")
        assert r["device_tracks_started"] >= 2, (
            "the device stopped after track 1 and MA never started track 2 (#6425)",
            r,
        )
        assert r["ma_index"] == 1, r

    def test_6425_arylic_flow_mode_position_tracks_device():
        r = _run_scenario("arylic_flow_drift")
        assert not r["index_mismatches"], r
        assert r["worst_error_s"] is not None, r
        assert r["worst_error_s"] < 3, (
            "MA's elapsed time drifts from what the device plays in flow mode (#6425)",
            r,
        )

    def test_6425_evented_linkplay_resumes_from_pause():
        r = _run_scenario("arylic_evented_resume")
        assert r["device_paused"], r
        assert r["ma_saw_paused"], r
        assert r["resumed"], (
            "queue play() on a paused protocol-backed LinkPlay shell never reaches the device",
            r,
        )

    def test_6756_resume_after_long_pause_is_fast():
        r = _run_scenario("arylic_long_pause")
        assert r["short"]["to_audio_s"] is not None, r
        assert r["long"]["to_audio_s"] is not None, r
        assert r["long"]["to_audio_s"] < 3, r


# -- scenario side (MA's venv) --------------------------------------------------------

TRACK_S = 25.0

SCENARIOS: dict[str, tuple[str, dict[str, Any], bool]] = {
    # name: (profile, knobs, generic LinkPlay over DLNA)
    "art_unknow": ("linkplay/wiim_pro", {}, False),
    "art_selfsigned": ("linkplay/wiim_pro", {"self_hosted_art": True}, False),
    "advance_drop": ("linkplay/wiim_mini", {"drop_advance_event": 1.0}, False),
    "advance_noevents": ("linkplay/wiim_mini", {"notify_delivery": "drop_silently"}, False),
    "arylic_register": ("linkplay/arylic_a31", {}, True),
    "arylic_resume": ("linkplay/arylic_a31", {"resume_701": {"p": 0.5}}, True),
    "arylic_noflow_advance": ("linkplay/arylic_a31", {"resume_701": False}, True),
    "arylic_flow_drift": ("linkplay/arylic_a31", {"resume_701": False}, True),
    "arylic_long_pause": ("linkplay/arylic_a31", {"resume_701": False}, True),
    # control: same A31 but evented, so MA sees PAUSED at once
    "arylic_evented_resume": (
        "linkplay/arylic_a31",
        {"resume_701": False, "no_state_events": False},
        True,
    ),
}
FLOW = {
    "arylic_flow_drift": True,
    "arylic_long_pause": True,
    "arylic_noflow_advance": False,
    "arylic_evented_resume": True,
}


def _fired(dev: Any, knob: str) -> int:
    return sum(
        1
        for e in dev.rec.rec.events
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    )


def _seturi_count(c: Any) -> int:
    return sum(1 for e in c.scene.rec.events if e.kind == "cmd" and e.name == "set_uri")


def _faults(scene: Any, code: int, action: str | None = None) -> int:
    return sum(
        1
        for e in scene.rec.events
        if e.name == "upnp_fault"
        and e.data.get("code") == code
        and (action is None or e.data.get("action") == action)
    )


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

    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]

    profile, knobs, generic = SCENARIOS[name]
    web_port, stream_port = free_port(), free_port()
    assert not {web_port, stream_port} & FORBIDDEN_PORTS
    work = Path(tempfile.mkdtemp(prefix=f"seamer-hunt-{name}-"))
    data_dir = work / "data"
    data_dir.mkdir()
    providers: dict[str, Any] = {
        "wiim": {"type": "player", "domain": "wiim", "instance_id": "wiim", "enabled": True}
    }
    if generic:
        providers["dlna"] = {
            "type": "player",
            "domain": "dlna",
            "instance_id": "dlna",
            "enabled": True,
        }
    # sendspin would try the user's port 8927; keep it off
    providers["sendspin"] = {
        "type": "player",
        "domain": "sendspin",
        "instance_id": "sendspin",
        "enabled": False,
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
        "music_assistant.providers.wiim",
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
            api_port = int(dev.api_url.rsplit(":", 1)[1])
            orig = wiim.discovery.async_create_http_api_endpoint

            async def _endpoint(host: str | None, **_k: Any) -> Any:
                return await orig(host, protocol="https", port=api_port)

            wiim.discovery.async_create_http_api_endpoint = _endpoint  # type: ignore[assignment]
            # pywiim probes https/http itself; point every client (the generic shell and
            # the official player's command client for group reads) at the sim's API port.
            # Without this, each official-path poll waits ~14 s on a dead default port.
            import music_assistant.providers.wiim.linkplay_player as wlp
            import music_assistant.providers.wiim.player as wpl
            import music_assistant.providers.wiim.provider as wprov
            import pywiim

            real_client = pywiim.WiiMClient

            def _client(host: str, **kw: Any) -> Any:
                kw.setdefault("port", api_port)
                return real_client(host, **kw)  # probes https, then http

            for mod in (wprov, wpl, wlp):
                mod.WiiMClient = _client  # type: ignore[attr-defined,assignment,misc]
            location = f"{dev.base_url}/description.xml"
            log(f"device {dev.udn} UPnP {dev.base_url}, API {dev.api_url}")
            await mass.start()
            prov = mass.get_provider("wiim")
            assert prov is not None, "wiim provider did not load"
            pid = f"wiim_{dev.udn}"
            if generic:
                dprov = mass.get_provider("dlna")
                assert dprov is not None, "dlna provider did not load"
                await dprov._device_discovered(dev.udn, location)  # type: ignore[attr-defined]
            await prov._discover_device("127.0.0.1", dev.identity["name"], (location,))  # type: ignore[attr-defined]

            def player() -> Any:
                return mass.players.get_player(pid)

            ok = await until(lambda: player() is not None and player().available, 30)
            result["registered"] = ok
            if not ok:
                result["players"] = [p.player_id for p in mass.players.all_players()]
                return result
            result["backend"] = type(player()).__name__
            log(f"player registered: {player().display_name} ({result['backend']})")
            if generic and name in FLOW:
                await _set_flow(mass, [pid, dev.udn], FLOW[name], result)
            ctx = Ctx(mass, scene, dev, pid, player, until, log, result)
            await globals()[f"_s_{name}"](ctx)
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


class Ctx:
    def __init__(self, mass, scene, dev, pid, player, until, log, result) -> None:
        self.mass, self.scene, self.dev, self.pid = mass, scene, dev, pid
        self.player, self.until, self.log, self.result = player, until, log, result

    def state(self) -> dict[str, Any]:
        st = self.mass.players.get_player_state(self.pid)
        m = st.current_media
        return {
            "active_source": st.active_source,
            "playback_state": str(st.playback_state.value),
            "title": m.title if m else None,
            "image_url": m.image_url if m else None,
        }

    async def tracks(self, n: int, seconds: float = TRACK_S) -> list[str]:
        await self.scene.media_server()
        return [self.scene.media_url(self.scene.media.track(i + 1, seconds).path) for i in range(n)]

    def queue(self) -> Any:
        return self.mass.player_queues.get(self.pid)


async def _set_flow(mass: Any, pids: list[str], on: bool, result: dict[str, Any]) -> None:
    from music_assistant.constants import CONF_FLOW_MODE

    for p in pids:
        with __import__("contextlib").suppress(KeyError):
            mass.config.set_raw_player_config_value(p, CONF_FLOW_MODE, on)
        if (pl := mass.players.get_player(p)) is not None:
            pl.__dict__.pop("flow_mode", None)  # cached_property
    result["flow_mode"] = {p: getattr(mass.players.get_player(p), "flow_mode", None) for p in pids}


# -- official WiiM path ---------------------------------------------------------------


async def _s_art_unknow(c: Ctx) -> None:
    import asyncio

    await c.dev.sim_action("switch_source", {"source": "app", "title": "Morning Radio"})
    await c.until(lambda: c.state()["title"] == "Morning Radio", 40)
    await asyncio.sleep(1)
    c.result["device_art"] = c.dev.artwork_for(c.dev.transport.current)
    c.result["ma"] = c.state()
    c.log(f"MA: {c.result['ma']}")


async def _s_art_selfsigned(c: Ctx) -> None:
    import asyncio

    import aiohttp

    await c.dev.sim_action("switch_source", {"source": "airplay", "title": "Phone"})
    await c.until(lambda: c.state()["active_source"] == "airplay", 40)
    await asyncio.sleep(1)
    c.result["device_art"] = c.dev.artwork_for(c.dev.transport.current)
    c.result["ma"] = st = c.state()
    url = st["image_url"] or ""
    if url.startswith("/"):
        url = c.mass.webserver.base_url.rstrip("/") + url
    fetch: dict[str, Any] = {"url": url, "ok": False}
    try:
        async with aiohttp.ClientSession() as s, s.get(url, timeout=10) as r:
            body = await r.read()
            fetch.update(
                ok=r.status == 200 and body[:2] == b"\xff\xd8",
                status=r.status,
                content_type=r.headers.get("Content-Type"),
            )
    except Exception as err:
        fetch["error"] = f"{type(err).__name__}: {err}"[:300]
    c.result["published_fetch"] = fetch
    c.log(f"MA: {st} fetch {fetch}")


async def _advance(c: Ctx, knob: str | None) -> None:
    import time

    urls = await c.tracks(2, 20.0)
    await c.mass.player_queues.play_media(c.pid, urls)
    await c.until(lambda: c.dev.transport.tracks_started >= 1, 30)
    t_play = time.monotonic()
    ok = await c.until(lambda: c.dev.transport.tracks_started >= 2, 40)
    c.result["device_advanced"] = ok
    t_adv = time.monotonic()
    c.result["advance_after_s"] = round(t_adv - t_play, 1)
    import asyncio

    timeline: list[dict[str, Any]] = []
    followed_at = None
    while time.monotonic() - t_adv < 30:
        idx, title = c.queue().current_index, c.state()["title"]
        if not timeline or (timeline[-1]["idx"], timeline[-1]["title"]) != (idx, title):
            timeline.append({"t": round(time.monotonic() - t_adv, 1), "idx": idx, "title": title})
        if idx == 1 and followed_at is None:
            followed_at = time.monotonic() - t_adv
        if followed_at is not None and time.monotonic() - t_adv > followed_at + 3:
            break
        await asyncio.sleep(0.2)
    c.result["timeline"] = timeline
    c.result["ma_followed_s"] = round(followed_at, 1) if followed_at is not None else None
    c.result["ma_index"] = c.queue().current_index
    c.result["ma_state"] = c.state()
    c.result["setnext_calls"] = sum(
        1 for e in c.scene.rec.events if e.kind == "cmd" and e.name == "set_next"
    )
    if knob:
        c.result["dropped"] = _fired(c.dev, knob) > 0 or c.dev._avt_muted
    c.log(f"advance: {c.result}")


async def _s_advance_drop(c: Ctx) -> None:
    await _advance(c, "drop_advance_event")


async def _s_advance_noevents(c: Ctx) -> None:
    await _advance(c, None)


# -- generic LinkPlay over DLNA ---------------------------------------------------------


async def _s_arylic_register(c: Ctx) -> None:
    c.result["api_scheme"] = c.dev.api_url.split(":", 1)[0]
    c.result["players"] = sorted(
        f"{p.player_id}:{type(p).__name__}" for p in c.mass.players.all_players()
    )


async def _play1(c: Ctx, n: int = 1, seconds: float = TRACK_S) -> list[str]:
    from music_assistant_models.enums import PlaybackState

    urls = await c.tracks(n, seconds)
    await c.mass.player_queues.play_media(c.pid, urls if n > 1 else urls[0])
    ok = await c.until(lambda: c.dev.transport.state.value == "playing", 30)
    await c.until(lambda: c.player().playback_state == PlaybackState.PLAYING, 10)
    c.result["started"] = ok
    c.result["players"] = sorted(
        f"{p.player_id}:{type(p).__name__}" for p in c.mass.players.all_players()
    )
    return urls


async def _s_arylic_resume(c: Ctx) -> None:
    import asyncio

    await _play1(c, 1, 300.0)
    cycles = []
    for _ in range(3):
        await asyncio.sleep(4)
        await c.mass.player_queues.pause(c.pid)
        paused = await c.until(lambda: c.dev.transport.state.value == "paused", 10)
        await asyncio.sleep(3)
        err = None
        seturi0 = _seturi_count(c)
        try:
            await c.mass.player_queues.play(c.pid)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:200]
        resumed = await c.until(lambda: c.dev.transport.state.value == "playing", 10)
        cycles.append(
            {
                "paused": paused,
                "resumed": resumed,
                "error": err,
                "reloaded": _seturi_count(c) > seturi0,
            }
        )
        c.log(f"cycle {cycles[-1]}")
    c.result["cycles"] = cycles
    c.result["faults_701"] = _faults(c.scene, 701, "Play")


async def _s_arylic_noflow_advance(c: Ctx) -> None:
    # tracks longer than the DLNA provider's 30 s poll, as real tracks are
    await _play1(c, 2, 45.0)
    await c.until(lambda: c.dev.transport.tracks_started >= 2, 70)
    await c.until(lambda: c.queue().current_index == 1, 10)
    c.result["device_tracks_started"] = c.dev.transport.tracks_started
    c.result["device_state"] = c.dev.transport.state.value
    c.result["ma_index"] = c.queue().current_index
    c.result["ma_state"] = c.state()
    c.result["setnext_calls"] = sum(
        1 for e in c.scene.rec.events if e.kind == "cmd" and e.name == "set_next"
    )
    c.log(f"noflow: {c.result}")


def _flow_sample(c: Ctx, label: str) -> dict[str, Any]:
    """MA's queue index/elapsed next to what the sink actually plays.

    Index truth is the tone the sink hears (track N -> queue index N-1). Elapsed truth is
    the stream position minus the earlier tracks; that only holds for the first,
    uninterrupted flow stream, so samples taken after a reload carry ``err=None``.
    """
    q = c.queue()
    aud = c.dev.sink.audible if c.dev.sink else None
    track = aud.track if aud else None
    t_idx = (track - 1) if track else None
    ma = q.corrected_elapsed_time
    err = None
    if aud is not None and t_idx is not None and c.dev.transport.tracks_started == 1:
        truth = aud.position_s - t_idx * TRACK_S
        err = round(abs(ma - truth), 1) if q.current_index == t_idx else 99.0
    return {
        "at": label,
        "sink_track_idx": t_idx,
        "ma_idx": q.current_index,
        "ma_elapsed": round(ma, 1),
        "stream_pos": round(aud.position_s, 1) if aud else None,
        "err": err,
    }


async def _s_arylic_flow_drift(c: Ctx) -> None:
    import asyncio
    import time

    from music_assistant_models.enums import PlaybackState

    t = time.monotonic()
    urls = await c.tracks(2, TRACK_S)
    await c.mass.player_queues.play_media(c.pid, urls)
    await c.until(lambda: c.dev.transport.state.value == "playing", 30)
    t_dev = time.monotonic() - t
    await c.until(lambda: c.player().playback_state == PlaybackState.PLAYING, 40)
    c.result["ma_playing_lag_s"] = round(time.monotonic() - t - t_dev, 1)
    samples = []
    for label, wait in (("t+8", 8), ("t+20", 12), ("track2+6", 0), ("track2+12", 6)):
        if label == "track2+6":
            await c.until(lambda: (c.dev.sink.audible.position_s or 0) >= TRACK_S + 6, 40)
        else:
            await asyncio.sleep(wait)
        samples.append(_flow_sample(c, label))
    # pause/resume: does MA still point at the track the device plays?
    await c.mass.player_queues.pause(c.pid)
    await c.until(lambda: c.dev.transport.state.value == "paused", 10)
    await asyncio.sleep(5)
    await c.mass.player_queues.play(c.pid)
    await c.until(lambda: c.dev.transport.state.value == "playing", 10)
    await asyncio.sleep(3)
    samples.append(_flow_sample(c, "resumed+3"))
    c.result["samples"] = samples
    errs = [s["err"] for s in samples if s["err"] is not None]
    c.result["worst_error_s"] = max(errs) if errs else None
    c.result["index_mismatches"] = [
        s["at"]
        for s in samples
        if s["sink_track_idx"] is not None and s["ma_idx"] != s["sink_track_idx"]
    ]
    c.log(f"flow: {samples}")


async def _s_arylic_long_pause(c: Ctx) -> None:
    import asyncio
    import time

    await _play1(c, 1, 300.0)

    async def cycle(pause_s: float) -> dict[str, Any]:
        await asyncio.sleep(6)
        started = c.dev.transport.tracks_started
        await c.mass.player_queues.pause(c.pid)
        await c.until(lambda: c.dev.transport.state.value == "paused", 10)
        await asyncio.sleep(pause_s)
        b0 = c.dev.sink.snapshot()["bytes"]
        q = c.queue()
        before = {"queue_state": str(q.state), "player_state": str(c.player().playback_state)}
        n_cmds = sum(1 for e in c.scene.rec.events if e.kind == "cmd")
        t = time.monotonic()
        err = None
        try:
            await c.mass.player_queues.play(c.pid)
        except Exception as e:  # recorded, not raised: the timing is the finding
            err = f"{type(e).__name__}: {e}"[:200]
        ok = await c.until(
            lambda: (
                c.dev.transport.state.value == "playing" and c.dev.sink.snapshot()["bytes"] != b0
            ),
            30,
            0.05,
        )
        return {
            "pause_s": pause_s,
            "to_audio_s": round(time.monotonic() - t, 2) if ok else None,
            "reloaded": c.dev.transport.tracks_started > started,
            "before_play": before,
            "error": err,
            "device_cmds_after_play": [e.name for e in c.scene.rec.events if e.kind == "cmd"][
                n_cmds:
            ],
        }

    c.result["short"] = await cycle(5)
    c.log(f"short: {c.result['short']}")
    c.result["long"] = await cycle(40)
    c.log(f"long: {c.result['long']}")


async def _s_arylic_evented_resume(c: Ctx) -> None:
    import asyncio

    from music_assistant_models.enums import PlaybackState

    await _play1(c, 1, 300.0)
    await asyncio.sleep(5)
    await c.mass.player_queues.pause(c.pid)
    c.result["device_paused"] = await c.until(lambda: c.dev.transport.state.value == "paused", 10)
    c.result["ma_saw_paused"] = await c.until(lambda: c.queue().state == PlaybackState.PAUSED, 10)
    await asyncio.sleep(3)
    n_cmds = sum(1 for e in c.scene.rec.events if e.kind == "cmd")
    err = None
    try:
        await c.mass.player_queues.play(c.pid)
    except Exception as e:  # recorded, not raised
        err = f"{type(e).__name__}: {e}"[:200]
    c.result["resumed"] = await c.until(lambda: c.dev.transport.state.value == "playing", 15)
    c.result["error"] = err
    c.result["device_cmds_after_play"] = [
        e.name for e in c.scene.rec.events if e.kind == "cmd" and not e.name.startswith("GET ")
    ][n_cmds:]
    c.log(f"evented resume: {c.result}")


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "art_unknow"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
