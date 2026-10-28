"""Bug hunt: known Bluesound / NAD (BluOS) issues replayed against real Music Assistant.

Same harness as ``tests/e2e/bluos_ma_smoke.py``: MA runs in a subprocess with its own
interpreter (Python 3.14 with ``pyblu``). It is loopback only, uses free high ports and a
throwaway data dir, and has discovery switched off. Each sim's ``_musc._tcp``
announcement is handed to the bluesound provider's mDNS callback, and the idle poll is
cut to 3 s. Each test asserts the *correct* behaviour, so a failure on MA dev means the
bug reproduces. Skipped unless ``MA_REPO`` is set.

- ``inputs_text`` (bss:B21, ha#151728, ``inputs_missing_attr = "text"``): a Capture input
  without ``text``. MA's source list should not show "Input: None".
- ``follower_mute`` (bl2:B29): a follower's /Status is its leader's. Muting only the
  leader must not show the follower muted, and muting only the follower must stick.
- ``input_volume`` (player.py volume guard): on a local input (Optical), a volume change
  made on the device should reach MA.
- ``leader_ungroup`` (bss:B18, bl2:B33): a 3-player group plays an MA queue and the
  leader is ungrouped through MA. Playback should move to a remaining member, with the
  third player still grouped under it.
- ``input_leader_ungroup`` (bl2:B31, bl2:B33): the leader plays an input (the firmware
  refuses ``RemoveSlave`` of itself here). After MA's ungroup, device and MA should agree.
- ``group_volume`` (ha#161607, BL-05): MA's group volume on the leader should reach
  every member.
- ``preset`` (bss:B25, ha#140922): selecting ``preset-2`` should apply the preset's
  volume (20) and show the station.
- ``reboot`` (bl2:R6, ``reboot_downtime_s = 15``): during a reboot MA should mark the
  player unavailable, then bring it back on its own once the player answers again.
- ``long_pause`` (ma#6756 on BluOS): resuming after a 60 s pause should give audio
  within a few seconds, the same as after a 5 s pause.
- ``nad_connect`` (bl2:B44, ma#3488, profile ``nad_c700``: 2-4 s 'connecting'): after
  play, MA's queue should reach PLAYING and stay on item 0, with no IDLE flap that ends it.
- ``bluos3`` (bl2:B51): a BluOS 3 player registers, plays and leads a group.
- ``drop_model`` (bl2:B47, ha#127500, ``drop_model_attrs``): the player still registers
  when its /SyncStatus has no modelName.
- ``wedged_pause`` (bss:B15, ha#188764, ``wedged_pause_state``, low confidence): an app
  takes over a paused player and the status stays 'pause' while audio plays. MA's pause
  should still silence the player.

Run::

    MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/hunt/bluos_hunt.py

or one scenario (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/hunt/bluos_hunt.py preset
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

    def _ok(r: dict[str, Any]) -> dict[str, Any]:
        assert all(r.get("registered", {}).values()), r
        return r

    def test_b21_input_without_text_has_a_name():
        r = _ok(_run_scenario("inputs_text"))
        assert r["input_sources"], r
        assert not any("None" in n for n in r["input_sources"]), (
            "an input without a text attribute shows as 'Input: None' (bss:B21, ha#151728)",
            r,
        )

    def test_b29_follower_mute_is_its_own():
        r = _ok(_run_scenario("follower_mute"))
        assert r["grouped"], r
        assert r["leader_muted"]["device_follower_muted"] is False, r
        assert r["leader_muted"]["ma_follower_muted"] is False, (
            "muting the leader shows the follower muted in MA (bl2:B29)",
            r,
        )
        assert r["follower_muted"]["device_follower_muted"] is True, r
        assert r["follower_muted"]["ma_follower_muted"] is True, (
            "a follower's own mute is overwritten by its leader's (bl2:B29)",
            r,
        )

    def test_volume_on_local_input_reaches_ma():
        r = _ok(_run_scenario("input_volume"))
        assert r["idle_followed"], r
        assert r["on_input"], r
        assert r["input_followed"], (
            "on a local input MA ignores the player's volume (player.py volume guard)",
            r,
        )

    def test_b18_leader_ungroup_hands_over():
        r = _ok(_run_scenario("leader_ungroup"))
        assert r["grouped"], r
        assert r["after"]["new_leader"], r
        assert r["after"]["new_leader_audio"], r
        assert r["after"]["other_follows_new_leader"], r

    def test_b33_input_leader_ungroup_consistent():
        r = _ok(_run_scenario("input_leader_ungroup"))
        assert r["grouped"], r
        assert r["after"]["error"] is None, r
        assert r["after"]["consistent"], r

    def test_161607_group_volume_reaches_members():
        r = _ok(_run_scenario("group_volume"))
        assert r["grouped"], r
        assert all(v != r["before"][k] for k, v in r["after"].items()), r

    def test_b25_preset_applies_volume():
        r = _ok(_run_scenario("preset"))
        assert r["device_source"] == "preset", r
        assert r["device_volume"] == 20, r
        assert r["ma"]["title"], r

    def test_reboot_recovers_by_polling():
        r = _ok(_run_scenario("reboot"))
        assert r["went_unavailable"], r
        assert r["back_by_poll_s"] is not None, (
            "after a reboot MA never polls the player back to available",
            r,
        )
        assert r["played_after"], r

    def test_6756_bluos_resume_after_long_pause():
        r = _ok(_run_scenario("long_pause"))
        assert r["short"]["to_audio_s"] is not None, r
        assert r["long"]["to_audio_s"] is not None, r
        assert r["long"]["to_audio_s"] < 5, r

    def test_3488_slow_connect_keeps_queue():
        r = _ok(_run_scenario("nad_connect"))
        assert r["device_played"], r
        assert r["final"]["queue_state"] == "playing", r
        assert r["final"]["queue_index"] == 0, r
        assert r["device_tracks_started"] == 1, r
        assert not r["queue_idle_flap"], (
            "MA's queue drops to IDLE while the player is still connecting (bl2:B44)",
            r,
        )

    def test_b51_bluos3_registers_plays_groups():
        r = _ok(_run_scenario("bluos3"))
        assert r["played"], r
        assert r["grouped"], r

    def test_127500_no_model_name_registers():
        r = _run_scenario("drop_model")
        assert all(r["registered"].values()), (
            "a player without modelName in /SyncStatus never registers (ha#127500)",
            r,
        )

    def test_b15_pause_works_when_status_is_wedged():
        r = _ok(_run_scenario("wedged_pause"))
        assert r["wedged"], r
        assert r["silenced"], (
            "MA's pause cannot stop a player whose status is wedged on 'pause' (bss:B15)",
            r,
        )


# -- scenario side (MA's venv) --------------------------------------------------------

N130, POWERNODE, FLEX = "bluos/node_n130", "bluos/powernode", "bluos/pulse_flex_2i"
SCENARIOS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "inputs_text": [(N130, {"inputs_missing_attr": "text"})],
    "follower_mute": [(N130, {}), (POWERNODE, {})],
    "input_volume": [(N130, {})],
    "leader_ungroup": [(N130, {}), (POWERNODE, {}), (FLEX, {})],
    "input_leader_ungroup": [(N130, {}), (POWERNODE, {})],
    "group_volume": [(N130, {}), (POWERNODE, {}), (FLEX, {})],
    "preset": [(N130, {})],
    "reboot": [(N130, {"reboot_downtime_s": 15.0})],
    "long_pause": [(N130, {})],
    "nad_connect": [("bluos/nad_c700", {})],
    "bluos3": [("bluos/bluos3", {}), (N130, {})],
    "drop_model": [(N130, {"drop_model_attrs": True})],
    "wedged_pause": [(N130, {"wedged_pause_state": True})],
}


class Ctx:
    def __init__(self, mass, scene, devs, until, log, result, announce) -> None:
        self.mass, self.scene, self.devs = mass, scene, devs
        self.until, self.log, self.result, self.announce = until, log, result, announce
        self.dev = devs[0]
        self.pid = devs[0].mac

    def st(self, dev: Any = None) -> Any:
        return self.mass.players.get_player_state((dev or self.dev).mac)

    def player(self, dev: Any = None) -> Any:
        return self.mass.players.get_player((dev or self.dev).mac)

    def summary(self, dev: Any = None) -> dict[str, Any]:
        st = self.st(dev)
        if st is None:
            return {}
        m = st.current_media
        return {
            "available": st.available,
            "playback_state": str(st.playback_state.value),
            "volume": st.volume_level,
            "muted": st.volume_muted,
            "active_source": st.active_source,
            "group_members": list(st.group_members),
            "synced_to": st.synced_to,
            "title": m.title if m else None,
        }

    def queue(self, dev: Any = None) -> Any:
        return self.mass.player_queues.get((dev or self.dev).mac)

    async def play_track(self, dev: Any = None, secs: float = 300.0) -> bool:
        dev = dev or self.dev
        await self.scene.media_server()
        url = self.scene.media_url(self.scene.media.track(1, secs).path)
        await self.mass.player_queues.play_media(dev.mac, [url])
        return await self.until(lambda: audio(dev) > 0, 30)

    async def group(self, leader: Any, followers: list[Any]) -> bool:
        # can_group_with is filled in on the leader's next state update
        await self.until(
            lambda: all(f.mac in self.st(leader).can_group_with for f in followers), 15
        )
        await self.mass.players.cmd_set_members(
            leader.mac, player_ids_to_add=[f.mac for f in followers]
        )
        return await self.until(
            lambda: (
                all(f.leader is leader for f in followers)
                and all(f.mac in (self.st(leader).group_members or []) for f in followers)
                and all(self.st(f).synced_to == leader.mac for f in followers)
            ),
            20,
        )


def audio(dev: Any) -> int:
    return int(dev.sink.snapshot().get("bytes") or 0)


def played(dev: Any) -> float:
    return float(dev.sink.snapshot().get("played_s") or 0.0)


def topology(devs: list[Any]) -> dict[str, Any]:
    return {
        d.name: {
            "leader": d.leader.name if d.leader else None,
            "followers": [f.name for f in d.followers],
            "state": d.transport.state.value,
            "source": d.source,
            "volume": d.volume,
            "muted": d.muted,
        }
        for d in devs
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

    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController.run_provider_discovery = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]
    blu_provider.get_primary_ip_address_from_zeroconf = (  # type: ignore[assignment]
        lambda info, prefer_ipv6=False: next(
            (str(a) for a in info.ip_addresses_by_version(IPVersion.V4Only)), None
        )
    )
    blu_player.IDLE_POLL_INTERVAL = 3  # only to get going quickly

    web_port, stream_port = free_port(), free_port()
    assert not {web_port, stream_port} & FORBIDDEN_PORTS
    work = Path(tempfile.mkdtemp(prefix=f"seamer-hunt-bluos-{name}-"))
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
                # sendspin would bind the user's port 8927; keep it off
                "providers": {
                    "sendspin": {
                        "type": "player",
                        "domain": "sendspin",
                        "instance_id": "sendspin",
                        "enabled": False,
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
    for lg in ("music_assistant.providers.bluesound", "music_assistant.players"):
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

    specs = SCENARIOS[name]
    result: dict[str, Any] = {"scenario": name, "devices": specs}
    mass = MusicAssistant(str(data_dir), str(work / "cache"), safe_mode=True)
    try:
        async with Scene(work / "scene", name=name) as scene:
            devs = [await scene.device(p, **k) for p, k in specs]
            await mass.start()
            await mass.config.create_builtin_provider_config("bluesound")
            conf = (await mass.config.get_provider_configs(provider_domain="bluesound"))[0]
            await mass.load_provider_config(conf)
            prov = mass.get_provider("bluesound")
            assert prov is not None, "bluesound provider did not load"

            async def announce(dev: Any) -> str | None:
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
                    return f"{type(err).__name__}: {err}"[:300]
                return None

            result["discovery_errors"] = {}
            for d in devs:
                log(f"device {d.name} {d.base_url} mac {d.mac}")
                if err := await announce(d):
                    result["discovery_errors"][d.name] = err
            registered = {}
            for d in devs:
                registered[d.name] = await until(
                    lambda d=d: (p := mass.players.get_player(d.mac)) is not None and p.available,
                    20,
                )
            result["registered"] = registered
            if all(registered.values()):
                ctx = Ctx(mass, scene, devs, until, log, result, announce)
                await globals()[f"_s_{name}"](ctx)
            result["topology"] = topology(devs)
            result["bluos_commands"] = sorted(
                {
                    str(e.data.get("path", ""))
                    for e in scene.rec.events
                    if e.kind == "cmd" and str(e.data.get("path", "")).startswith("/")
                }
            )
            text = (work / "server.log").read_text(errors="replace")
            result["log_errors"] = [
                ln.split(" ", 2)[-1][:300]
                for ln in text.splitlines()
                if " ERROR " in ln or "Traceback" in ln
            ][:10]
    finally:
        await mass.stop()
        if not os.environ.get("SEAMER_KEEP"):
            shutil.rmtree(work, ignore_errors=True)
        else:
            result["work_dir"] = str(work)
    return result


# -- scenarios ------------------------------------------------------------------------


async def _s_inputs_text(c: Ctx) -> None:
    await c.until(
        lambda: any(s.id.startswith("input-") for s in c.player()._attr_source_list or []), 15
    )
    c.result["device_inputs"] = [{k: i.get(k) for k in ("id", "text")} for i in c.dev.inputs]
    c.result["input_sources"] = [
        s.name for s in c.player()._attr_source_list or [] if s.id.startswith("input-")
    ]
    c.log(f"input sources: {c.result['input_sources']}")


async def _s_follower_mute(c: Ctx) -> None:
    import asyncio

    a, b = c.devs
    c.result["grouped"] = await c.group(a, [b])
    if not c.result["grouped"]:
        return

    async def snap() -> dict[str, Any]:
        await asyncio.sleep(8)  # several polls of both players
        return {
            "device_leader_muted": a.muted,
            "device_follower_muted": b.muted,
            "ma_leader_muted": c.st(a).volume_muted,
            "ma_follower_muted": c.st(b).volume_muted,
        }

    await c.mass.players.cmd_volume_mute(a.mac, True)
    c.result["leader_muted"] = await snap()
    await c.mass.players.cmd_volume_mute(a.mac, False)
    await c.until(lambda: not a.muted, 5)
    await c.mass.players.cmd_volume_mute(b.mac, True)
    c.result["follower_muted"] = await snap()
    c.log(f"mute: {c.result['leader_muted']} / {c.result['follower_muted']}")


async def _s_input_volume(c: Ctx) -> None:
    c.dev.set_level(40)
    c.dev.bump_sync()
    c.result["idle_followed"] = await c.until(lambda: c.st().volume_level == 40, 15)
    await c.dev.sim_action("select_input", {"id": "input2"})
    c.result["on_input"] = await c.until(
        lambda: c.dev.source == "input" and (c.st().active_source or "") != "", 15
    )
    c.result["ma_on_input"] = c.summary()
    c.dev.set_level(55)
    c.dev.bump_sync()
    c.result["input_followed"] = await c.until(lambda: c.st().volume_level == 55, 15)
    c.result["ma_after"] = c.summary()
    c.log(f"input volume: {c.result['ma_after']}")


async def _s_leader_ungroup(c: Ctx) -> None:
    import asyncio

    a, b, d = c.devs
    if not await c.play_track(a):
        c.result["error"] = "leader never played"
        return
    c.result["grouped"] = await c.group(a, [b, d])
    if not c.result["grouped"]:
        return
    await asyncio.sleep(3)
    err = None
    try:
        await c.mass.players.cmd_ungroup(a.mac)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]

    def heir() -> Any:
        return next(
            (x for x in (b, d) if x.leader is None and x.transport.state.value == "playing"), None
        )

    await c.until(lambda: heir() is not None, 25)
    new = heir()
    before = audio(new) if new else 0
    await asyncio.sleep(4)
    other = (d if new is b else b) if new else None
    c.result["after"] = {
        "error": err,
        "new_leader": new.name if new else None,
        "new_leader_audio": bool(new and audio(new) > before),
        "other_follows_new_leader": bool(other and other.leader is new),
        "old_leader_state": a.transport.state.value,
        "ma": {x.name: c.summary(x) for x in c.devs},
        "topology": topology(c.devs),
    }
    c.log(f"ungroup leader: {c.result['after']}")


async def _s_input_leader_ungroup(c: Ctx) -> None:
    import asyncio

    a, b = c.devs
    c.result["grouped"] = await c.group(a, [b])
    if not c.result["grouped"]:
        return
    await a.sim_action("select_input", {"id": "input2"})
    await c.until(lambda: c.st(a).active_source not in (None, ""), 10)
    await asyncio.sleep(3)
    err = None
    try:
        await c.mass.players.cmd_ungroup(a.mac)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
    await asyncio.sleep(8)

    def agree() -> bool:
        device_grouped = b.leader is a
        ma_grouped = c.st(b).synced_to == a.mac or b.mac in c.st(a).group_members
        return device_grouped == ma_grouped

    c.result["after"] = {
        "error": err,
        "consistent": agree(),
        "device_follower_leader": b.leader.name if b.leader else None,
        "leader_source": a.source,
        "leader_state": a.transport.state.value,
        "ma": {x.name: c.summary(x) for x in c.devs},
        "refusals": [e.data for e in c.scene.rec.events if e.kind == "event" and "refus" in e.name][
            :5
        ],
    }
    c.log(f"input leader ungroup: {c.result['after']}")


async def _s_group_volume(c: Ctx) -> None:
    import asyncio

    a, b, d = c.devs
    for x, lvl in ((a, 50), (b, 40), (d, 30)):
        await c.mass.players.cmd_volume_set(x.mac, lvl)
    c.result["grouped"] = await c.group(a, [b, d])
    if not c.result["grouped"]:
        return
    await asyncio.sleep(4)
    c.result["before"] = {x.name: x.volume for x in c.devs}
    await c.mass.players.cmd_group_volume(a.mac, 20)
    await asyncio.sleep(6)
    c.result["after"] = {x.name: x.volume for x in c.devs}
    c.result["ma"] = {x.name: c.summary(x)["volume"] for x in c.devs}
    c.result["ma_group_volume"] = getattr(c.st(a), "group_volume", None)
    c.log(f"group volume: {c.result['before']} -> {c.result['after']}")


async def _s_preset(c: Ctx) -> None:
    import asyncio

    await c.mass.players.cmd_volume_set(c.pid, 45)
    await c.until(lambda: c.dev.volume == 45, 5)
    await c.until(lambda: any(s.id == "preset-2" for s in c.player()._attr_source_list), 10)
    await c.mass.players.select_source(c.pid, "preset-2")
    await c.until(lambda: c.dev.source == "preset", 10)
    await asyncio.sleep(5)
    c.result["device_source"] = c.dev.source
    c.result["device_volume"] = c.dev.volume
    c.result["ma"] = c.summary()
    c.log(f"preset: {c.result['ma']} device vol {c.dev.volume}")


async def _s_reboot(c: Ctx) -> None:
    import time

    await c.play_track()
    await c.dev.sim_action("reboot", {})
    t = time.monotonic()
    c.result["went_unavailable"] = await c.until(lambda: not c.st().available, 20)
    c.result["unavailable_after_s"] = round(time.monotonic() - t, 1)
    await c.until(lambda: any(e.name == "rebooted" for e in c.scene.rec.events), 30)
    t = time.monotonic()
    ok = await c.until(lambda: c.st().available, 40)
    c.result["back_by_poll_s"] = round(time.monotonic() - t, 1) if ok else None
    if not ok:
        c.result["reannounce_error"] = await c.announce(c.dev)
        t = time.monotonic()
        ok2 = await c.until(lambda: c.st().available, 15)
        c.result["back_by_reannounce_s"] = round(time.monotonic() - t, 1) if ok2 else None
    c.result["ma_after"] = c.summary()
    c.result["queue_after"] = c.queue().state.value
    await c.play_track()
    ok = await c.until(lambda: c.dev.transport.state.value == "playing", 20)
    before = played(c.dev)
    c.result["played_after"] = ok and await c.until(lambda: played(c.dev) > before + 1, 10)
    c.result["device_after"] = c.dev.transport.snapshot()
    c.log(f"reboot: {c.result}")


async def _s_long_pause(c: Ctx) -> None:
    import asyncio
    import time

    if not await c.play_track():
        c.result["error"] = "never played"
        return

    async def cycle(pause_s: float) -> dict[str, Any]:
        await asyncio.sleep(4)
        await c.mass.player_queues.pause(c.pid)
        paused = await c.until(lambda: c.dev.transport.state.value == "paused", 10)
        await asyncio.sleep(pause_s)
        before = played(c.dev)
        n_ev = len(c.scene.rec.events)
        t = time.monotonic()
        err = None
        try:
            await c.mass.player_queues.play(c.pid)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:200]

        def heard() -> bool:
            # a reload opens a new stream (played_s restarts); a plain resume continues
            fresh = any(
                e.kind == "sink" and e.name == "first_audio" for e in c.scene.rec.events[n_ev:]
            )
            return played(c.dev) > (0.5 if fresh else before + 0.5)

        ok = await c.until(heard, 30, 0.05)
        return {
            "pause_s": pause_s,
            "device_paused": paused,
            "to_audio_s": round(time.monotonic() - t, 2) if ok else None,
            "error": err,
            "ma": c.summary(),
            "device": c.dev.transport.state.value,
            "sink": c.dev.sink.snapshot(),
            "queue_elapsed": c.queue().elapsed_time,
            "after_play": [
                f"{e.kind}:{e.name}:{e.data.get('path') or e.data.get('reason') or ''}"
                for e in c.scene.rec.events[n_ev:]
                if e.kind in ("cmd", "sink", "event")
                and e.data.get("path") not in ("/Status", "/SyncStatus")
            ][:16],
        }

    c.result["short"] = await cycle(5)
    c.result["long"] = await cycle(60)
    c.log(f"pause: {c.result['short']} / {c.result['long']}")


async def _s_nad_connect(c: Ctx) -> None:
    import asyncio
    import time

    await c.scene.media_server()
    urls = [c.scene.media_url(c.scene.media.track(i + 1, 300.0).path) for i in range(3)]
    seen: list[tuple[float, str, str, str]] = []
    t = time.monotonic()

    async def watch() -> None:
        while True:
            q = c.queue()
            row = (
                c.st().playback_state.value,
                q.state.value if q else "-",
                c.dev.lens.state_word(),
            )
            if not seen or seen[-1][1:] != row:
                seen.append((round(time.monotonic() - t, 2), *row))
            await asyncio.sleep(0.1)

    task = asyncio.create_task(watch())
    await c.mass.player_queues.play_media(c.pid, urls)
    c.result["device_played"] = await c.until(lambda: audio(c.dev) > 0, 20)
    await asyncio.sleep(15)
    task.cancel()
    q = c.queue()
    c.result["transitions"] = seen
    states = [row[2] for row in seen]
    first = states.index("playing") if "playing" in states else len(states)
    c.result["queue_idle_flap"] = "idle" in states[first:]
    c.result["device_tracks_started"] = c.dev.transport.tracks_started
    c.result["device_uri"] = c.dev.transport.snapshot()["uri"]
    c.result["final"] = {
        "queue_state": q.state.value,
        "queue_index": q.current_index,
        "player": c.summary(),
    }
    c.log(f"nad: {seen}")


async def _s_bluos3(c: Ctx) -> None:
    old, new = c.devs
    c.result["played"] = await c.play_track(old)
    c.result["grouped"] = await c.group(old, [new])
    c.result["ma"] = {x.name: c.summary(x) for x in c.devs}


async def _s_drop_model(c: Ctx) -> None:
    c.result["ma"] = c.summary()


async def _s_wedged_pause(c: Ctx) -> None:
    import asyncio

    await c.play_track()
    await asyncio.sleep(3)
    await c.mass.player_queues.pause(c.pid)
    await c.until(lambda: c.dev.transport.state.value == "paused", 10)
    await asyncio.sleep(2)
    c.dev.transport.take_over("spotify", title="Phone app")
    await c.until(lambda: c.dev.transport.state.value == "playing", 5)
    await asyncio.sleep(6)
    c.result["wedged"] = bool(getattr(c.dev, "wedged", False))
    c.result["ma_before"] = c.summary()
    err = None
    try:
        await c.mass.players.cmd_pause(c.pid)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
    c.result["silenced"] = await c.until(lambda: c.dev.transport.state.value != "playing", 8)
    c.result["error"] = err
    c.result["ma_after"] = c.summary()
    c.result["device_after"] = {
        "state": c.dev.transport.state.value,
        "status": c.dev.lens.state_word(),
    }
    c.log(f"wedged: {c.result}")


def main() -> int:
    import asyncio

    name = sys.argv[1] if len(sys.argv) > 1 else "preset"
    result = asyncio.run(_scenario(name))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
