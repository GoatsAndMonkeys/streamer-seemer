"""Sendspin bug hunt: one scenario per known MA bug, against real MA dev.

Run one scenario in MA's interpreter (prints ``RESULT {json}``)::

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/hunt/scenarios.py <name>

or several, three times each, from this repo's venv::

    MA_REPO=... .venv/bin/python tests/e2e/hunt/run_hunt.py --repeat 3 [names...]

Every result has ``bug_present`` (True when the bug showed), ``valid`` (the harness
control worked, so a False ``bug_present`` means something) and an ``evidence`` dict.
Quirk IDs (SSnn) refer to the simresearch dossier; issue numbers to
music-assistant/support.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import itertools

from _harness import Hunt, run

PY_CLI = "sendspin/python_cli_pi_zero2w"
S3 = "sendspin/esphome_s3_psram"
G0 = "sendspin/esphome_legacy"
PLAIN = "sendspin/esp32_plain"

SCENARIOS: dict[str, Any] = {}
OPTIONS: dict[str, dict[str, Any]] = {}


def scenario(name: str, **opts: Any) -> Any:
    def deco(fn: Any) -> Any:
        SCENARIOS[name] = fn
        OPTIONS[name] = opts
        return fn

    return deco


async def _redial_after(h: Hunt, dev: Any, reason: str, timeout: float = 60) -> dict[str, Any]:
    before = dev.sessions
    t = time.monotonic()
    await dev.restart(reason, 3.0)
    ok = await h.until(lambda: dev.sessions > before and dev.connected, timeout)
    return {
        "redialed": ok,
        "after_s": round(time.monotonic() - t, 1) if ok else None,
        "sessions": dev.sessions,
        "ws_attempts": dev.ws_attempts,
        "player_listed": h.player(dev.client_id) is not None,
        "player_available": bool(h.player(dev.client_id) and h.player(dev.client_id).available),
    }


async def _playing(h: Hunt, dev: Any, vid: str, chunks: int = 20, timeout: float = 30) -> bool:
    return await h.until(
        lambda: dev.fake_sink.metrics.played_chunks > chunks and h.ma_state(vid) == "playing",
        timeout,
    )


async def _group(h: Hunt, leader: str, members: list[str]) -> list[str]:
    # the universal players appear one by one; can_group_with fills in after both exist
    await h.until(lambda: set(members) <= set(h.player(leader).state.can_group_with or []), 30)
    await h.mass.players.cmd_set_members(leader, members)
    await h.until(lambda: set(members) <= set(h.player(leader).state.group_members or []), 15)
    return list(h.player(leader).state.group_members or [])


# -- SS26 / #6888: no redial after goodbye shutdown -----------------------------------


@scenario("ss26_shutdown_manual_ip")
async def ss26_shutdown_manual_ip(h: Hunt) -> None:
    """python-cli restarts (Ctrl-C / systemd stop sends goodbye 'shutdown'); the
    device is configured by manual IP, as in #6888."""
    dev = await h.device(PY_CLI)
    await h.start_ma()
    assert await h.registered(dev), "device never registered"
    control = await _redial_after(h, dev, "restart")
    test = await _redial_after(h, dev, "shutdown", 60)
    h.result.update(
        valid=control["redialed"],
        bug_present=not test["redialed"],
        evidence={"control_restart": control, "shutdown": test},
    )


@scenario("ss26_shutdown_mdns_update", mdns_emulation=True)
async def ss26_shutdown_mdns_update(h: Hunt) -> None:
    """Same, discovered over mDNS. While the device is down its record stays in the
    cache (an ESP that reboots without an mDNS goodbye; python-cli killed before it
    can unregister); once back it re-announces, which zeroconf reports as Updated.
    The spec counts an mDNS update of a known service as a reconnect prompt."""
    dev = await h.device(PY_CLI, manual=False)
    await h.start_ma()
    h.mdns_announce(dev, "Added")
    assert await h.registered(dev), "device never registered via mDNS"
    control = await _mdns_cycle(h, dev, "restart", "Updated")
    test = await _mdns_cycle(h, dev, "shutdown", "Updated")
    h.result.update(
        valid=control["redialed"],
        bug_present=not test["redialed"],
        evidence={
            "control_restart": control,
            "shutdown_then_mdns_updated": test,
            "log": h.server_log("Not redialing", "after its goodbye on an mDNS update"),
        },
    )


@scenario("ss26_shutdown_mdns_readd", mdns_emulation=True)
async def ss26_shutdown_mdns_readd(h: Hunt) -> None:
    """Control for the mDNS path: the device withdraws its record on shutdown and
    re-adds it when back (Removed, then Added)."""
    dev = await h.device(PY_CLI, manual=False)
    await h.start_ma()
    h.mdns_announce(dev, "Added")
    assert await h.registered(dev), "device never registered via mDNS"
    test = await _mdns_cycle(h, dev, "shutdown", "Added", withdraw=True)
    h.result.update(valid=True, bug_present=not test["redialed"], evidence={"shutdown": test})


async def _mdns_cycle(
    h: Hunt, dev: Any, reason: str, change: str, withdraw: bool = False
) -> dict[str, Any]:
    before = dev.sessions
    task = asyncio.ensure_future(dev.restart(reason, 4.0))
    await h.until(lambda: dev._down, 5, poll=0.05)
    if withdraw:
        h.mdns_announce(dev, "Removed")
    await task
    t = time.monotonic()
    h.mdns_announce(dev, change)
    ok = await h.until(lambda: dev.sessions > before and dev.connected, 40)
    return {
        "redialed": ok,
        "after_s": round(time.monotonic() - t, 1) if ok else None,
        "sessions": dev.sessions,
        "ws_attempts": dev.ws_attempts,
        "player_listed": h.player(dev.client_id) is not None,
    }


# -- SS27 / #6887: another_server -----------------------------------------------------


@scenario("ss27_another_server")
async def ss27_another_server(h: Hunt) -> None:
    dev = await h.device(S3)
    await h.start_ma()
    assert await h.registered(dev)
    vid = await h.visible(dev)
    await dev.goodbye("another_server")
    await asyncio.sleep(5)
    listed = h.player(vid) is not None
    played = dev.fake_sink.metrics.played_chunks
    err = await h.play(vid)
    reclaimed = await h.until(
        lambda: dev.sessions > 1 and dev.fake_sink.metrics.played_chunks > played + 5, 30
    )
    h.result.update(
        valid=True,
        bug_present=not (listed and reclaimed),
        evidence={"listed_after_goodbye": listed, "reclaimed_on_play": reclaimed, "err": err},
    )


# -- SS36: a player that never reports available --------------------------------------


@scenario("ss36_available_never")
async def ss36_available_never(h: Hunt) -> None:
    """The device keeps reporting available:false (no sync yet, or broken output).
    aiosendspin must not stream to it; MA should then not show it as playing."""
    dev = await h.device(S3, available_mode="never")
    ctl = await h.device(S3, mac_address="aa:bb:cc:00:00:31")
    await h.start_ma()
    assert await h.registered(ctl)
    await h.until(lambda: h.player(dev.client_id) is not None, 20)
    p = h.player(dev.client_id)
    vid = await h.visible(dev, 25) if p is not None else dev.client_id
    err = await h.play(vid)
    await asyncio.sleep(12)
    v = h.player(vid)
    ev = {
        "protocol_available": bool(p and p.available),
        "visible_available": bool(v and v.available),
        "play_error": err,
        "ma_state": h.ma_state(vid),
        "queue_state": str(h.mass.player_queues.get(vid).state.value)
        if h.mass.player_queues.get(vid)
        else None,
        "device_chunks": dev.fake_sink.metrics.chunks,
        "device_streams": dev.fake_sink.metrics.streams,
    }
    # silent failure: MA says playing while the device got nothing (or got audio it
    # said it could not play) and no error reached the user
    silent = ev["ma_state"] == "playing" and err is None
    streamed_anyway = ev["device_chunks"] > 0
    h.result.update(
        valid=True,
        bug_present=silent or streamed_anyway,
        evidence={**ev, "silent_playing": silent, "streamed_to_unavailable": streamed_anyway},
    )


# -- SS24 / SS44: volume echo with a stepped DAC ---------------------------------------


@scenario("ss24_volume_steps_echo")
async def ss24_volume_steps_echo(h: Hunt) -> None:
    """The device's volume has 16 hardware steps, so 33 comes back as 31. A loop shows
    as MA re-sending the volume; a stale UI shows as MA keeping 33."""
    a = await h.device(PY_CLI, volume_steps=16)
    b = await h.device(PY_CLI, volume_steps=16, mac_address="aa:bb:cc:00:00:41")
    cmds_a, cmds_b = h.count_commands(a), h.count_commands(b)
    await h.start_ma()
    assert await h.registered(a)
    assert await h.registered(b)
    va, vb = await h.visible(a), await h.visible(b)
    await h.mass.players.cmd_volume_set(va, 33)
    await asyncio.sleep(10)
    single = {
        "commands": len(cmds_a),
        "device": a.volume,
        "ma": h.player(va).state.volume_level,
    }
    members = await _group(h, va, [vb])
    await asyncio.sleep(2)
    group_before = getattr(h.player(va).state, "group_volume", None)
    n_a, n_b = len(cmds_a), len(cmds_b)
    await h.mass.players.cmd_group_volume(va, 47)
    await asyncio.sleep(10)
    group = {
        "members": members,
        "group_volume_before": group_before,
        "cmd_log_b": cmds_b[n_b:],
        "commands_a": len(cmds_a) - n_a,
        "commands_b": len(cmds_b) - n_b,
        "device_a": a.volume,
        "device_b": b.volume,
        "ma_a": h.player(va).state.volume_level,
        "ma_b": h.player(vb).state.volume_level,
        "ma_group": h.player(va).state.group_volume
        if hasattr(h.player(va).state, "group_volume")
        else None,
    }
    loop = single["commands"] > 2 or group["commands_a"] > 3 or group["commands_b"] > 3
    h.result.update(
        valid=a.volume != 33,  # the step knob took effect
        bug_present=loop,
        evidence={"single": single, "group": group, "cmd_log_a": cmds_a[:20]},
    )


# -- SS44 / #6334: group volume unmutes a muted member --------------------------------


@scenario("ss44_group_volume_unmutes")
async def ss44_group_volume_unmutes(h: Hunt) -> None:
    """Two current ESPHome speakers (volume does not clear mute on the device) in a
    group; the member is muted, then the group volume changes."""
    a = await h.device(S3)
    b = await h.device(S3, mac_address="aa:bb:cc:00:00:51")
    cmds_b = h.count_commands(b)
    await h.start_ma()
    assert await h.registered(a)
    assert await h.registered(b)
    va, vb = await h.visible(a), await h.visible(b)
    members = await _group(h, va, [vb])
    await h.mass.players.cmd_volume_mute(vb, True)
    await h.until(lambda: b.muted, 10)
    muted_ok = b.muted
    n = len(cmds_b)
    await h.mass.players.cmd_group_volume(va, 64)
    await asyncio.sleep(5)
    ev = {
        "members": members,
        "muted_before": muted_ok,
        "device_b_muted_after": b.muted,
        "ma_b_muted_after": h.player(vb).state.volume_muted,
        "device_b_volume": b.volume,
        "commands_to_b": cmds_b[n:],
    }
    h.result.update(
        valid=muted_ok,
        bug_present=muted_ok and (not b.muted or not ev["ma_b_muted_after"]),
        evidence=ev,
    )


# -- SS29 / #6190: silent half-open socket --------------------------------------------


@scenario("ss29_blackhole_group")
async def ss29_blackhole_group(h: Hunt) -> None:
    """A group of two plays; member A's link dies without FIN (stops reading and
    answering). B must keep playing and MA's loop must not freeze."""
    a = await h.device(S3, fault_mode="blackhole", blackhole_after_s=25.0)
    b = await h.device(S3, mac_address="aa:bb:cc:00:00:61")
    tap_b = h.tap(b)
    await h.start_ma()
    assert await h.registered(a)
    assert await h.registered(b)
    va, vb = await h.visible(a), await h.visible(b)
    members = await _group(h, va, [vb])
    await h.play(va, 300)  # outlasts the 130 s window, so track end is not a gap
    await _playing(h, b, va)
    lag = h.loop_lag_monitor()  # after play: the sim renders the test track in-loop
    await h.until(lambda: a.blackholed, 40)
    t_bh = time.monotonic()
    u0, d0 = b.fake_sink.metrics.underruns, b.fake_sink.metrics.dropped_late
    s0 = a.sessions
    timeline: list[tuple[float, Any]] = []

    def snap() -> tuple[Any, ...]:
        pa = h.player(a.client_id)
        return (
            bool(pa and pa.available),
            h.ma_state(va),
            h.ma_state(vb),
            a.sessions - s0,
            b.fake_sink.metrics.played_chunks > 0 and b.sessions,
        )

    t_det = None
    t_end = t_bh + 120
    while time.monotonic() < t_end:
        cur = snap()
        if not timeline or timeline[-1][1] != cur:
            timeline.append((round(time.monotonic() - t_bh, 1), cur))
        if t_det is None and (not cur[0] or cur[3] > 0):
            t_det = round(time.monotonic() - t_bh, 1)
        await asyncio.sleep(0.2)
    detected = t_det is not None
    gaps = _max_gap_ms([r for r in tap_b if r[2] >= t_bh])
    ev = {
        "members": members,
        "detected_unavailable_after_s": t_det,
        "max_loop_lag_ms": lag["max_lag_ms"],
        "b_underruns": b.fake_sink.metrics.underruns - u0,
        "b_dropped_late": b.fake_sink.metrics.dropped_late - d0,
        "b_max_arrival_gap_ms": gaps,
        "b_still_playing": h.ma_state(vb) == "playing" or h.ma_state(va) == "playing",
        "log": h.server_log("Slow send_bytes", "No PONG", "Late binary"),
        "timeline (t, [a_avail, leader, b, a_new_sessions, b_session])": timeline[:30],
    }
    hurt_b = ev["b_underruns"] > 0 or ev["b_dropped_late"] > 0 or (gaps or 0) > 1000
    h.result.update(
        valid=a.blackholed,
        bug_present=hurt_b or lag["max_lag_ms"] > 1000 or not detected,
        evidence=ev,
    )


def _max_gap_ms(rows: list[tuple[int, float, float, int]]) -> float | None:
    if len(rows) < 2:
        return None
    arr = [r[2] for r in rows]
    return round(max(b - a for a, b in itertools.pairwise(arr)) * 1000, 1)


# -- the ESP 60 s silence watchdog while idle ------------------------------------------


@scenario("ss55_idle_60s_watchdog")
async def ss55_idle_60s_watchdog(h: Hunt) -> None:
    """sendspin-cpp drops a connection after 60 s with nothing from the server. An idle
    player (nothing playing) must still hear from MA often enough, G2 and G0 alike."""
    g2 = await h.device(S3, liveness_timeout_s=60.0)
    g0 = await h.device(G0, liveness_timeout_s=60.0)
    await h.start_ma()
    assert await h.registered(g2)
    assert await h.registered(g0)
    s2, s0 = g2.sessions, g0.sessions
    await asyncio.sleep(150)
    ev = {
        "g2_goodbyes": g2.goodbyes,
        "g0_goodbyes": g0.goodbyes,
        "g2_new_sessions": g2.sessions - s2,
        "g0_new_sessions": g0.sessions - s0,
    }
    h.result.update(valid=True, bug_present=bool(g2.goodbyes or g0.goodbyes), evidence=ev)


# -- SS53 / SS18: mixed G0/G2 group sync ----------------------------------------------


@scenario("ss53_mixed_g0_g2_sync")
async def ss53_mixed_g0_g2_sync(h: Hunt) -> None:
    """An ESPHome 2026.10 (G2) and a 2026.9 (G0) speaker in one group, as during the
    weeks-long fleet transition. Ground truth: for every server timestamp both play,
    compare the true host times."""
    a = await h.device(S3)
    b = await h.device(G0)
    ta, tb = h.tap(a), h.tap(b)
    await h.start_ma()
    assert await h.registered(a)
    assert await h.registered(b)
    va, vb = await h.visible(a), await h.visible(b)
    members = await _group(h, va, [vb])
    await h.play(va)
    await _playing(h, a, va)
    await asyncio.sleep(30)
    ev = _pair_sync(ta, tb, settle_s=5)
    ev.update(
        members=members,
        a_header=sorted(set(a.fake_sink.metrics.header_sizes)),
        b_header=sorted(set(b.fake_sink.metrics.header_sizes)),
        a_played=a.fake_sink.metrics.played_chunks,
        b_played=b.fake_sink.metrics.played_chunks,
        b_dropped_late=b.fake_sink.metrics.dropped_late,
    )
    valid = ev["common"] > 50
    h.result.update(
        valid=valid,
        bug_present=valid and (ev["p95_abs_ms"] > 5 or b.fake_sink.metrics.played_chunks < 50),
        evidence=ev,
    )


def _pair_sync(
    ta: list[tuple[int, float, float, int]],
    tb: list[tuple[int, float, float, int]],
    settle_s: float = 5,
    after: float = 0.0,
) -> dict[str, Any]:
    """|true play time A - B| over server timestamps both devices received."""
    if not ta or not tb:
        return {"common": 0, "p95_abs_ms": None, "max_abs_ms": None}
    start = max(ta[0][2], tb[0][2], after) + settle_s
    ma = {r[0]: r[1] for r in ta if r[2] >= start}
    mb = {r[0]: r[1] for r in tb if r[2] >= start}
    common = sorted(set(ma) & set(mb))
    diffs = [(ma[t] - mb[t]) * 1000 for t in common]
    if not diffs:
        return {"common": 0, "p95_abs_ms": None, "max_abs_ms": None}
    absd = sorted(abs(d) for d in diffs)
    return {
        "common": len(common),
        "median_ms": round(statistics.median(diffs), 2),
        "p95_abs_ms": round(absd[int(0.95 * (len(absd) - 1))], 2),
        "max_abs_ms": round(absd[-1], 2),
    }


# -- SS30 / SS6: Wi-Fi stall of one group member --------------------------------------


@scenario("ss30_wifi_stall_group")
async def ss30_wifi_stall_group(h: Hunt) -> None:
    """Member A stops reading its socket for 25 s mid-song (a starved 2.4 GHz link),
    then catches up. B must be unaffected, A must come back in sync."""
    # longer than the buffered lead, so A runs dry and the server's sends back up
    a = await h.device(S3, rx_window_stall_ms=25000.0, rx_window_stall_at_s=30.0)
    b = await h.device(S3, mac_address="aa:bb:cc:00:00:71")
    ta, tb = h.tap(a), h.tap(b)
    await h.start_ma()
    assert await h.registered(a)
    assert await h.registered(b)
    va, vb = await h.visible(a), await h.visible(b)
    members = await _group(h, va, [vb])
    lag = h.loop_lag_monitor()
    await h.play(va)
    await _playing(h, b, va)
    t_stall = await _wait_event(h, a, "rx_stall", 60)
    u0, d0 = b.fake_sink.metrics.underruns, b.fake_sink.metrics.dropped_late
    await asyncio.sleep(25 + 25)
    after = _pair_sync(ta, tb, settle_s=10, after=(t_stall or 0) + 25)
    b_gap = _max_gap_ms([r for r in tb if t_stall and r[2] >= t_stall])
    a_back = [r for r in ta if t_stall and r[2] >= t_stall + 26]
    ev = {
        "members": members,
        "stalled": t_stall is not None,
        "a_sessions": a.sessions,
        "a_chunks_after_stall": len(a_back),
        "a_dropped_late": a.fake_sink.metrics.dropped_late,
        "a_underruns": a.fake_sink.metrics.underruns,
        "b_underruns": b.fake_sink.metrics.underruns - u0,
        "b_dropped_late": b.fake_sink.metrics.dropped_late - d0,
        "b_max_arrival_gap_ms": b_gap,
        "sync_after_recovery": after,
        "max_loop_lag_ms": lag["max_lag_ms"],
        "log": h.server_log("Slow send_bytes", "Late binary", "No PONG", "skipping"),
    }
    b_hurt = ev["b_underruns"] > 0 or ev["b_dropped_late"] > 0 or (b_gap or 0) > 1500
    a_lost = len(a_back) < 50 or (after["p95_abs_ms"] or 1e9) > 5
    h.result.update(valid=t_stall is not None, bug_present=b_hurt or a_lost, evidence=ev)


async def _wait_event(h: Hunt, dev: Any, kind: str, timeout: float) -> float | None:
    path = h.scene.timeline_path

    def seen() -> bool:
        try:
            return f'"{kind}"' in path.read_text()
        except OSError:
            return False

    ok = await h.until(seen, timeout, poll=0.1)
    return time.monotonic() if ok else None


# -- players after an MA restart -------------------------------------------------------


@scenario("ma_restart_players_return")
async def ma_restart_players_return(h: Hunt) -> None:
    """MA restarts (update, container restart) while the speakers stay up. Every
    player must come back without the user touching it."""
    a = await h.device(S3)
    b = await h.device(G0)
    c = await h.device(PY_CLI, mac_address="aa:bb:cc:00:00:81")
    await h.start_ma()
    for d in (a, b, c):
        assert await h.registered(d)
    va = await h.visible(a)
    await h.play(va)
    await _playing(h, a, va)
    out: dict[str, Any] = {}
    for round_, down in (("quick", 0.0), ("down_20s", 20.0)):
        before = {d.client_id: d.sessions for d in (a, b, c)}
        t = time.monotonic()
        await h.restart_ma(down)
        back = {}
        for d in (a, b, c):
            ok = await h.registered(d, 45)
            back[d.knobs.get("wire_gen") + "/" + d.name] = {
                "available": ok,
                "after_s": round(time.monotonic() - t, 1),
                "new_sessions": d.sessions - before[d.client_id],
                "goodbyes_seen": d.ws_attempts,
            }
        out[round_] = back
    vb = await h.visible(a)
    err = await h.play(vb)
    p0 = a.fake_sink.metrics.played_chunks
    plays = await h.until(lambda: a.fake_sink.metrics.played_chunks > p0 + 20, 30)
    out["play_after_restart"] = {"error": err, "plays": plays}
    missing = [k for r in ("quick", "down_20s") for k, v in out[r].items() if not v["available"]]
    h.result.update(valid=True, bug_present=bool(missing) or not plays, evidence=out)


# -- device offline for a while: MA's redial backoff ----------------------------------


@scenario("ss56_offline_redial_gap")
async def ss56_offline_redial_gap(h: Hunt) -> None:
    """A manual-IP speaker loses power (no goodbye) and is back 130 s later. Count MA's
    dial attempts while it is down and measure how long the speaker then sits unused."""
    dev = await h.device(S3)
    await h.start_ma()
    assert await h.registered(dev)
    port = dev.port
    attempts: list[float] = []
    t_down = time.monotonic()

    async def counting(reader: Any, writer: Any) -> None:
        attempts.append(round(time.monotonic() - t_down, 1))
        writer.close()

    await dev.server.stop()
    dev.drop()
    await asyncio.sleep(0.2)
    srv = await asyncio.start_server(counting, "127.0.0.1", port)
    await asyncio.sleep(130)
    srv.close()
    await srv.wait_closed()
    before = dev.sessions
    from seamer.simkit.kits.http import HttpServer

    app = dev.build_app()
    app.add_routes(dev._sim_routes())
    dev.server = HttpServer(app, dev.ctx.host, port, dev.ctx.ssl_context)
    await dev.server.start()
    t_up = time.monotonic()
    ok = await h.until(lambda: dev.sessions > before and dev.connected, 320, poll=0.5)
    gap = round(time.monotonic() - t_up, 1) if ok else None
    ev = {"dial_attempts_while_down_s": attempts, "redial_gap_after_back_s": gap}
    h.result.update(valid=len(attempts) > 0, bug_present=gap is None or gap > 60, evidence=ev)


# -- SS28 / #5897: WebSocket up, hello never sent --------------------------------------


@scenario("ss28_hung_then_recovers")
async def ss28_hung_then_recovers(h: Hunt) -> None:
    """After an OTA the device accepts the WebSocket but never says hello. 40 s later
    its firmware recovers. MA must drop the dead connection and get the player back."""
    dev = await h.device(S3, fault_mode="accept_and_hang")
    await h.start_ma()
    await h.until(lambda: dev.ws_attempts > 0, 20)
    await asyncio.sleep(40)
    hung_attempts = dev.ws_attempts
    dev.knobs.set_live("fault_mode", "none")
    t = time.monotonic()
    ok = await h.registered(dev, 240)
    ev = {
        "ws_attempts_while_hung": hung_attempts,
        "registered_after_recovery": ok,
        "after_s": round(time.monotonic() - t, 1) if ok else None,
        "ws_attempts": dev.ws_attempts,
        "log": h.server_log("unknown-client", "client/hello", "Timed out"),
    }
    h.result.update(valid=True, bug_present=not ok or (ev["after_s"] or 0) > 60, evidence=ev)


# -- SS35 / SS9 / #6297: volume latency with a huge buffer ----------------------------


@scenario("ss35_volume_latency_big_buffer")
async def ss35_volume_latency_big_buffer(h: Hunt) -> None:
    """An S3 fork advertising 5 MiB plays; the user moves the volume slider."""
    big = await h.device(S3, buffer_capacity=5 * 1024 * 1024)
    cmds = h.count_commands(big)
    await h.start_ma()
    assert await h.registered(big)
    vid = await h.visible(big)
    await h.play(vid, seconds=120)
    await _playing(h, big, vid)
    await asyncio.sleep(15)  # let the buffer fill
    lat = []
    for vol in (21, 42, 63):
        n = len(cmds)
        t = time.monotonic()
        await h.mass.players.cmd_volume_set(vid, vol)
        await h.until(lambda n=n: len(cmds) > n, 90, poll=0.02)
        lat.append(round((time.monotonic() - t) * 1000, 1))
        await asyncio.sleep(2)
    ev = {
        "latency_ms": lat,
        "buffer_high_water": big.fake_sink.metrics.buffer_high_water,
        "max_lead_s": None,
    }
    h.result.update(valid=True, bug_present=max(lat) > 1000, evidence=ev)


# -- SS48: static at track end on 24-bit PCM ------------------------------------------


@scenario("ss48_pcm24_track_end")
async def ss48_pcm24_track_end(h: Hunt) -> None:
    """A python-cli device asks for 24-bit PCM; a short track ends. Every chunk must be
    whole frames and the last ones must not carry garbage (a loud burst)."""
    dev = await h.device(PY_CLI, formats="pcm:48000:24:2", mac_address="aa:bb:cc:00:00:91")
    payloads: list[bytes] = []
    orig = dev.on_audio

    def tap(ts: int, payload: bytes, hl: int, play_at: int) -> None:
        payloads.append(payload)
        orig(ts, payload, hl, play_at)

    dev.on_audio = tap
    await h.start_ma()
    assert await h.registered(dev)
    vid = await h.visible(dev)
    await h.play(vid, seconds=7.37)
    await h.until(lambda: len(payloads) > 5, 30)
    fmt = dict(dev.fake_sink.fmt.__dict__) if dev.fake_sink.fmt else None
    await h.until(lambda: h.ma_state(vid) != "playing", 40)
    await asyncio.sleep(3)
    misaligned = [i for i, p in enumerate(payloads) if len(p) % 6]
    peaks = [_peak24(p) for p in payloads]
    body = sorted(peaks[: max(1, len(peaks) - 4)])
    typical = body[len(body) // 2] if body else 0
    tail = peaks[-4:]
    ev = {
        "format": fmt,
        "chunks": len(payloads),
        "misaligned_chunks": misaligned[:10],
        "typical_peak": round(typical, 3),
        "tail_peaks": [round(x, 3) for x in tail],
        "tail_sizes": [len(p) for p in payloads[-4:]],
    }
    burst = any(x > max(0.6, typical * 1.5) for x in tail)
    h.result.update(
        valid=bool(fmt and fmt["bit_depth"] == 24 and payloads),
        bug_present=bool(misaligned) or burst,
        evidence=ev,
    )


def _peak24(p: bytes) -> float:
    n = len(p) - len(p) % 3
    peak = 0
    for i in range(0, n, 3):
        v = int.from_bytes(p[i : i + 3], "little", signed=True)
        peak = max(peak, abs(v))
    return peak / 8388607


# -- SS5 / SS31 / #6811: Opus on a plain ESP32 ----------------------------------------


@scenario("ss5_opus_plain_esp32")
async def ss5_opus_plain_esp32(h: Hunt) -> None:
    """Plain ESP32 (no fast PSRAM, 96 KiB budget) prefers Opus. Its decoder runs at 0.8x
    real time cost: it keeps up only if MA keeps enough audio ahead of it."""
    dev = await h.device(PLAIN, opus_decode_rt_factor=0.8)
    tap = h.tap(dev)
    await h.start_ma()
    assert await h.registered(dev)
    vid = await h.visible(dev)
    await h.play(vid, seconds=120)
    await _playing(h, dev, vid)
    await asyncio.sleep(40)
    m = dev.fake_sink.metrics
    leads = [(r[1] - r[2]) * 1000 for r in tap[50:]]
    ev = {
        "codec": dev.fake_sink.fmt.codec if dev.fake_sink.fmt else None,
        "played": m.played_chunks,
        "dropped_decode": m.dropped_decode,
        "dropped_late": m.dropped_late,
        "dropped_overrun": m.dropped_overrun,
        "underruns": m.underruns,
        "lead_ms_min": round(min(leads), 1) if leads else None,
        "lead_ms_median": round(statistics.median(leads), 1) if leads else None,
        "buffer_high_water": m.buffer_high_water,
    }
    bad = m.dropped_decode + m.dropped_late + m.underruns
    h.result.update(valid=m.played_chunks > 100, bug_present=bad > 0, evidence=ev)


def main() -> int:
    name = sys.argv[1]
    result = asyncio.run(run(name, SCENARIOS[name], **OPTIONS[name]))
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
