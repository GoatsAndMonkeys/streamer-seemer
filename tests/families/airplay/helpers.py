"""Test helpers: a direct ``/_wire`` client, a DACP receiver and a wait helper."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp


async def wire(dev: Any, op: str, **body: Any) -> dict[str, Any]:
    """Call one ``/_wire/<op>`` on the device, as the fake helper does."""
    if op != "connect" and "session" not in body and dev.session is not None:
        body["session"] = dev.session.id
    async with (
        aiohttp.ClientSession() as s,
        s.post(f"{dev.base_url}/_wire/{op}", json=body) as r,
    ):
        return await r.json()


async def connect(dev: Any, **body: Any) -> dict[str, Any]:
    body.setdefault("protocol", "auto")
    body.setdefault("active_remote", "1111")
    body.setdefault("dacp_id", "A1B2C3D4E5F60708")
    return await wire(dev, "connect", **body)


async def until(pred: Any, timeout: float = 5.0, poll: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(poll)
    return bool(pred())


class DacpReceiver:
    """MA's side of DACP: records ``GET /ctrl-int/1/...`` requests and answers 204."""

    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.got: list[dict[str, Any]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def __aenter__(self) -> DacpReceiver:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.read(1024)  # MA reads the request with one read(1024)
        head = raw.decode(errors="replace").split("\r\n")
        headers = dict(h.split(": ", 1) for h in head[1:] if ": " in h)
        self.got.append(
            {
                "t": time.monotonic(),
                "path": head[0].split(" ")[1],
                "active_remote": headers.get("Active-Remote"),
                "dacp_id": headers.get("DACP-ID"),
            }
        )
        writer.write(f"HTTP/1.1 {self.status} No Content\r\nContent-Length: 0\r\n\r\n".encode())
        await writer.drain()
        writer.close()

    def volumes(self) -> list[float]:
        return [
            float(g["path"].rsplit("=", 1)[1]) for g in self.got if "device-volume" in g["path"]
        ]

    async def attach(self, dev: Any) -> DacpReceiver:
        await dev.sim_action("set_dacp_target", {"host": "127.0.0.1", "port": self.port})
        return self
