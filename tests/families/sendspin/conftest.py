"""An in-process aiosendspin server that dials simulated Sendspin devices (as MA does)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("aiosendspin.server")

from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server.audio import AudioFormat
from aiosendspin.server.server import SendspinServer


class Controller:
    """A minimal MA stand-in: dial, stream PCM, send commands, record events."""

    def __init__(self, server: SendspinServer) -> None:
        self.server = server
        self.events: list[Any] = []
        server.add_event_listener(lambda _s, ev: self.events.append(ev))

    async def dial(self, dev: Any, *, timeout: float = 10.0) -> Any:
        if dev.wire_gen == "G2":
            await self.server.trust_unpaired(dev.client_id)
        self.server.connect_to_client(f"ws://127.0.0.1:{dev.port}/sendspin")
        return await self.wait_client(dev, timeout=timeout)

    async def wait_client(self, dev: Any, *, sessions: int = 1, timeout: float = 10.0) -> Any:
        """Wait until the server holds the client and the device saw ``sessions`` sessions."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            c = self.server.get_client(dev.client_id)
            if (
                c is not None
                and c.is_connected
                and c.info_or_none is not None
                and dev.connected
                and dev.sessions >= sessions
            ):
                return c
            await asyncio.sleep(0.02)
        raise AssertionError(f"{dev.client_id} never connected")

    async def stream_pcm(
        self, client: Any, seconds: float, *, chunk_s: float = 0.1, rate: int = 48000
    ) -> None:
        stream = client.group.start_stream()
        fmt = AudioFormat(sample_rate=rate, bit_depth=16, channels=2)
        frames = int(rate * chunk_s)
        # noise, so FLAC cannot shrink it and the byte budget is exercised
        pcm = random.Random(7).randbytes(4 * frames)
        for _ in range(round(seconds / chunk_s)):
            stream.prepare_audio(pcm, fmt)
            await stream.commit_audio()
            await asyncio.sleep(chunk_s)

    def of_type(self, cls: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, cls)]


@pytest_asyncio.fixture
async def controller() -> AsyncIterator[Controller]:
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=Identity.generate(),
        server_name="seamer-test",
        pairing_store=InMemoryServerPairingStore(),
        allow_unencrypted=True,
    )
    try:
        yield Controller(server)
    finally:
        await server.close()
