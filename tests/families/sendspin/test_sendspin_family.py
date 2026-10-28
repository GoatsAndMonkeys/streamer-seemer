"""Sendspin family: profiles, both wire generations, the sink, commands, goodbyes, faults."""

from __future__ import annotations

import asyncio
import random

import aiohttp
import pytest

pytest.importorskip("aiosendspin.server")

from aiosendspin.models.types import GoodbyeReason
from aiosendspin.server.server import ClientDisconnectedEvent

from seamer.families import get_family
from seamer.families.sendspin.clock import SkewedClock
from seamer.families.sendspin.sink import (
    StreamFormat,
    chunk_duration_us,
    flac_block_size,
    opus_packet_us,
)
from seamer.simkit.clock import VirtualClock
from seamer.simkit.scenario import Scene
from seamer.simkit.transport import TState

PROFILES = {
    "esphome_s3_psram",
    "esphome_legacy",
    "voice_pe_26_9",
    "esp32_plain",
    "python_cli_pi_zero2w",
    "cpp_cli",
}


# -- pure helpers ---------------------------------------------------------------------


def test_flac_block_size_codes():
    assert flac_block_size(bytes([0xFF, 0xF8, 0x10, 0x00])) == 192
    assert flac_block_size(bytes([0xFF, 0xF8, 0xC0, 0x00])) == 4096
    assert flac_block_size(bytes([0xFF, 0xF8, 0x30, 0x00])) == 1152
    # code 6: 8-bit (n-1) after the 1-byte coded frame number
    assert flac_block_size(bytes([0xFF, 0xF8, 0x60, 0x00, 0x05, 0xFF])) == 256
    # code 7: 16-bit (n-1)
    assert flac_block_size(bytes([0xFF, 0xF8, 0x70, 0x00, 0x05, 0x01, 0x1F])) == 288
    assert flac_block_size(b"\x00\x00\x00\x00") is None


def test_opus_packet_durations():
    assert opus_packet_us(bytes([(31 << 3) | 0])) == 20_000  # CELT FB 20 ms, one frame
    assert opus_packet_us(bytes([(31 << 3) | 1])) == 40_000  # two frames
    assert opus_packet_us(bytes([(16 << 3) | 3, 4])) == 10_000  # CELT 2.5 ms x 4
    assert opus_packet_us(b"") is None


def test_pcm_chunk_duration():
    fmt = StreamFormat("pcm", 48000, 2, 16)
    assert chunk_duration_us(fmt, b"\x00" * 4 * 480) == 10_000


async def test_skewed_clock_runs_fast_and_stays_monotonic():
    base = VirtualClock()
    clk = SkewedClock(base, skew_ppm=100, offset_ms=5, jitter_us=200, rng=random.Random(1))
    t0 = clk.ideal_us()
    await base.advance(10.0)
    assert clk.ideal_us() - t0 == pytest.approx(10_001_000, abs=1)
    reads = [clk.now_us() for _ in range(200)]
    assert reads == sorted(reads)
    assert len(set(reads)) == len(reads)


def test_profiles_load_and_carry_quirk_tagged_knobs():
    fam = get_family("sendspin")
    assert set(fam.profiles()) >= PROFILES
    assert fam.default_port == 8928


# -- G2: the real aiosendspin client ---------------------------------------------------


async def test_g2_handshake_hello_and_audio(scene: Scene, controller):
    dev = await scene.device("sendspin/esphome_s3_psram")
    c = await controller.dial(dev)
    assert dev.status()["connected"]
    assert dev.server_name == "seamer-test"
    info = c.info
    assert info.player_support.buffer_capacity == 621_312
    codecs = {f.codec.value for f in info.player_support.supported_formats}
    assert codecs == {"flac", "opus", "pcm"}
    assert c.connection_security is not None  # Noise session
    await controller.stream_pcm(c, 2.0)
    await scene.wait_for(lambda: dev.fake_sink.metrics.played_chunks > 5, 5, "audio played")
    m = dev.fake_sink.metrics
    assert set(m.header_sizes) == {13}
    assert 0 < m.buffer_high_water <= 621_312
    assert dev.transport.state == TState.PLAYING
    await c.group.stop()
    await scene.wait_for(lambda: dev.transport.state != TState.PLAYING, 5, "stopped")


async def test_g0_legacy_wire(scene: Scene, controller):
    dev = await scene.device("sendspin/esphome_legacy")
    assert dev.client_id == "aabbcc000002"
    c = await controller.dial(dev)
    assert c.connection_security is None  # plain G0
    await scene.wait_for(lambda: dev.puppet.filter.is_synchronized, 10, "time sync")
    await controller.stream_pcm(c, 1.5)
    await scene.wait_for(lambda: dev.fake_sink.metrics.played_chunks > 3, 5, "audio played")
    assert set(dev.fake_sink.metrics.header_sizes) == {9}
    await c.group.stop()


@pytest.mark.parametrize(
    ("echo", "expect_echo"), [("immediate", True), ("delayed", True), ("missing", False)]
)
async def test_volume_command_and_state_echo(scene: Scene, controller, echo, expect_echo):
    dev = await scene.device("sendspin/esphome_s3_psram", state_echo=echo, state_echo_delay_ms=50)
    c = await controller.dial(dev)
    player = c.role("player@v1")
    await scene.wait_for(lambda: player.volume == 50, 5, "initial state")
    player.set_volume(23)
    await scene.wait_for(lambda: dev.volume == 23, 5, "device applied volume")
    if expect_echo:
        await scene.wait_for(lambda: player.volume == 23, 5, "server saw echo")
    else:
        await asyncio.sleep(0.3)
        assert player.volume == 50  # the server never hears back (SS24)
        scene.expect.eventually(kind="knob", name="state_echo")


@pytest.mark.parametrize("reason", ["shutdown", "another_server", "restart", "user_request"])
async def test_goodbye_reason_reaches_server(scene: Scene, controller, reason):
    dev = await scene.device("sendspin/python_cli_pi_zero2w")
    await controller.dial(dev)
    await dev.goodbye(reason)
    await scene.wait_for(
        lambda: controller.of_type(ClientDisconnectedEvent), 5, "server saw disconnect"
    )
    assert controller.of_type(ClientDisconnectedEvent)[-1].goodbye_reason == GoodbyeReason(reason)
    assert dev.goodbyes == [reason]


async def test_restart_relistens_on_same_port(scene: Scene, controller):
    dev = await scene.device("sendspin/esphome_s3_psram", restart_down_s=0.3)
    await controller.dial(dev)
    port = dev.port
    await dev.restart("restart")
    assert dev.restarts == 1
    assert dev.status()["ws_url"].endswith(f":{port}/sendspin")
    # the server re-dials after a "restart" goodbye; the same identity is admitted again
    await controller.wait_client(dev, sessions=2, timeout=20)
    assert dev.sessions >= 2


async def test_origin_header_is_rejected(scene: Scene):
    dev = await scene.device("sendspin/esphome_s3_psram")
    async with aiohttp.ClientSession() as s:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await s.ws_connect(f"ws://127.0.0.1:{dev.port}/sendspin", origin="http://192.0.2.10")
        assert err.value.status == 403
    assert dev.rejected == {"origin": 1}


async def test_accept_and_hang_never_says_hello(scene: Scene):
    dev = await scene.device("sendspin/esphome_s3_psram", fault_mode="accept_and_hang")
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"ws://127.0.0.1:{dev.port}/sendspin")
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive(timeout=0.5)
        await ws.close()
    assert dev.sessions == 0


async def test_never_consume_overruns_the_budget(scene: Scene, controller):
    dev = await scene.device(
        "sendspin/esphome_s3_psram",
        fault_mode="never_consume",
        buffer_capacity=100_000,
        formats="pcm:48000:16:2",
    )
    c = await controller.dial(dev)
    await controller.stream_pcm(c, 2.0, chunk_s=0.1)
    m = dev.fake_sink.metrics
    assert m.dropped_overrun > 0
    assert m.buffer_high_water <= 100_000
    assert m.played_chunks == 0
    await c.group.stop()


async def test_overrun_close_policy_drops_the_session(scene: Scene, controller):
    dev = await scene.device(
        "sendspin/esphome_s3_psram",
        fault_mode="never_consume",
        buffer_capacity=60_000,
        formats="pcm:48000:16:2",
        ring_overrun_policy="close_connection",
    )
    c = await controller.dial(dev)
    await controller.stream_pcm(c, 1.0, chunk_s=0.1)
    await scene.wait_for(lambda: "restart" in dev.goodbyes, 5, "overrun closed the session")
    scene.expect.eventually(kind="knob", name="ring_overrun_policy")


async def test_opus_decode_stutter_on_plain_esp32(scene: Scene, controller):
    dev = await scene.device("sendspin/esp32_plain")
    c = await controller.dial(dev)
    await controller.stream_pcm(c, 4.0)
    await scene.wait_for(lambda: dev.fake_sink.metrics.chunks > 20, 5, "chunks arrived")
    assert dev.fake_sink.fmt is not None
    assert dev.fake_sink.fmt.codec == "opus"
    # SS5: without PSRAM the Opus decoder falls behind and chunks miss their slot
    assert dev.fake_sink.metrics.dropped_decode > 0
    await c.group.stop()


async def test_sim_api_actions(scene: Scene, controller):
    dev = await scene.device("sendspin/cpp_cli")
    c = await controller.dial(dev)
    player = c.role("player@v1")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{dev.base_url}/_sim/action/set_volume", json={"volume": 77}) as r:
            assert r.status == 200
        async with s.get(f"{dev.base_url}/_sim/state") as r:
            state = await r.json()
    await scene.wait_for(lambda: player.volume == 77, 5, "server saw local volume")
    assert "sink" in str(state)
