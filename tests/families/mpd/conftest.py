"""A raw MPD client for tests: sends lines, returns replies byte-for-byte."""

from __future__ import annotations

import asyncio

import pytest_asyncio


class RawMpd:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.greeting = ""

    @classmethod
    async def connect(cls, port: int, host: str = "127.0.0.1", greet: bool = True) -> RawMpd:
        reader, writer = await asyncio.open_connection(host, port)
        c = cls(reader, writer)
        if greet:
            c.greeting = (await asyncio.wait_for(reader.readline(), 5)).decode()
        return c

    def send(self, *lines: str) -> None:
        self.writer.write("".join(f"{ln}\n" for ln in lines).encode())

    async def reply(self, timeout: float = 5.0) -> list[str]:
        """Lines up to and including the final ``OK`` or ``ACK ...`` line."""
        out: list[str] = []
        while True:
            raw = await asyncio.wait_for(self.reader.readline(), timeout)
            if not raw:
                raise ConnectionError("closed")
            line = raw.decode().rstrip("\n")
            out.append(line)
            if line == "OK" or line.startswith("ACK "):
                return out

    async def cmd(self, line: str, timeout: float = 5.0) -> list[str]:
        self.send(line)
        return await self.reply(timeout)

    async def kv(self, line: str) -> dict[str, str]:
        lines = await self.cmd(line)
        assert lines[-1] == "OK", lines
        return dict(ln.split(": ", 1) for ln in lines[:-1])

    async def closed(self, timeout: float = 2.0) -> bool:
        try:
            data = await asyncio.wait_for(self.reader.read(), timeout)
        except (TimeoutError, ConnectionError):
            return False
        return data == b""

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


@pytest_asyncio.fixture
async def connect():
    """``await connect(port)`` opens a RawMpd; all are closed at teardown."""
    opened: list[RawMpd] = []

    async def _connect(port: int, greet: bool = True) -> RawMpd:
        c = await RawMpd.connect(port, greet=greet)
        opened.append(c)
        return c

    yield _connect
    for c in opened:
        await c.close()
