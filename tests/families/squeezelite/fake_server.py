"""A small slimproto server for tests, modelled on aioslimproto 3.3 (what MA runs).

It does what MA's client object does on the wire: after HELO it sends vers, setd 0xFE,
setd 0, aude and audg; ``play`` flushes (strm f + q) and sends strm s with the HTTP
request; on RESP it answers codc + cont (ignoring a RESP that arrives before the new
stream's STMc); STMd with an enqueued url promotes it (strm s without flush).
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct
from typing import Any
from urllib.parse import urlparse

from seamer.families.squeezelite import protocol as P

CODC = {"audio/wav": b"p1321", "audio/x-wav": b"p1321", "audio/flac": b"f????"}


class FakeSlimServer:
    def __init__(self, heartbeat_s: float = 0.0, volume_curve: int = 0) -> None:
        self.heartbeat_s = heartbeat_s
        self.curve = P.CURVES[volume_curve]
        self.frames: list[tuple[str, bytes]] = []  # (op or STAT event, payload)
        self.helos: list[bytes] = []
        self.names: list[bytes] = []
        self.port = 0
        self.next_url: str | None = None
        self.awaiting_stmc = False
        self.autoplay = True
        self.auto_unpause = True  # answer STMl with strm u (MA does unpause_at)
        self._server: asyncio.base_events.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._changed = asyncio.Event()
        self.heartbeats = 0
        self.connections = 0
        self.closed_after_bye = False

    async def start(self) -> FakeSlimServer:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()

    # -- observation -----------------------------------------------------------------

    def events(self) -> list[str]:
        return [op for op, _ in self.frames]

    def count(self, name: str) -> int:
        return self.events().count(name)

    async def wait(self, name: str, n: int = 1, timeout: float = 10.0) -> bytes:
        """Wait until ``name`` (opcode or STAT event) has been received ``n`` times."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while self.count(name) < n:
            self._changed.clear()
            left = end - loop.time()
            if left <= 0:
                raise AssertionError(f"timed out waiting for {name} x{n}; got {self.events()}")
            try:
                await asyncio.wait_for(self._changed.wait(), left)
            except TimeoutError:
                pass
        return [p for op, p in self.frames if op == name][n - 1]

    def last_stat(self, name: str = "STMt") -> dict[str, int]:
        payload = [p for op, p in self.frames if op == name][-1]
        f = P.STAT_STRUCT.unpack(payload[4:51])
        keys = [
            "num_crlf",
            "mas_init",
            "mas_mode",
            "buf_size",
            "buf_full",
            "rx_h",
            "rx_l",
            "signal",
            "jiffies",
            "out_size",
            "out_full",
            "elapsed_s",
            "voltage",
            "elapsed_ms",
            "server_ts",
        ]
        return dict(zip(keys, f, strict=True))

    # -- commands (aioslimproto-style) -----------------------------------------------

    def send(self, op: bytes, data: bytes = b"") -> None:
        assert self._writer is not None, "no player connected"
        self._writer.write(P.server_frame(op, data))

    def strm(self, cmd: str, **kw: Any) -> None:
        self.send(b"strm", P.build_strm(cmd, **kw))

    def play(self, url: str, *, autostart: bool = True, flush: bool = True) -> None:
        if flush:
            self.strm("f")
            self.strm("q")
            self.next_url = None
        u = urlparse(url)
        path = u.path + (f"?{u.query}" if u.query else "")
        req = (
            f"GET {path} HTTP/1.0\r\nHost: {u.hostname}:{u.port}\r\nConnection: close\r\n"
            "Accept: */*\r\nCache-Control: no-cache\r\nRange: bytes=0-\r\n\r\n"
        ).encode()
        self.autoplay = autostart
        self.awaiting_stmc = True
        self.strm(
            "s",
            autostart="3" if autostart else "2",
            codec="?????",
            threshold_kb=200,
            output_threshold=20,
            server_port=u.port or 80,
            server_ip=int(ipaddress.ip_address(u.hostname or "127.0.0.1")),
            flags=0x20 if u.scheme == "https" else 0,
            request=req,
        )

    def enqueue(self, url: str) -> None:
        self.next_url = url

    def pause(self) -> None:
        self.strm("p")

    def unpause(self, at_jiffies: int = 0) -> None:
        self.strm("u", replay_gain=at_jiffies)

    def stop(self) -> None:
        self.strm("q")

    def volume(self, level: int) -> None:
        old = P.old_gain(level)
        new = P.new_gain(level, self.curve)
        self.send(b"audg", struct.pack("!LLBBLL", old, old, 1, 255, new, new))

    def mute(self, muted: bool) -> None:
        self.send(b"aude", struct.pack("2B", 0 if muted else 1, 0))

    # -- connection ------------------------------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self.connections += 1
        buf = b""
        hb: asyncio.Task[Any] | None = None
        try:
            while True:
                data = await reader.read(64)
                if not data:
                    break
                buf += data
                frames, buf = P.parse_client_frames(buf)
                for op, payload in frames:
                    if op == "BYE!":
                        self.frames.append((op, payload))
                        self.closed_after_bye = True
                        writer.close()
                        return
                    if op == "HELO" and self.heartbeat_s and hb is None:
                        hb = asyncio.create_task(self._heartbeat())
                        self._tasks.add(hb)
                    self._on_frame(op, payload)
                self._changed.set()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if hb is not None:
                hb.cancel()
            self._changed.set()

    async def _heartbeat(self) -> None:
        while True:
            self.heartbeats += 1
            self.strm("t", replay_gain=self.heartbeats)
            await asyncio.sleep(self.heartbeat_s)

    def _on_frame(self, op: str, payload: bytes) -> None:
        if op == "STAT":
            ev = payload[:4].decode("latin-1")
            self.frames.append((ev, payload))
            if ev == "STMc":
                self.awaiting_stmc = False
            elif ev in ("STMd", "STMu") and self.next_url:
                url, self.next_url = self.next_url, None
                self.play(url, autostart=True, flush=False)
            elif ev == "STMl" and not self.autoplay and self.auto_unpause:
                self.unpause()
            return
        self.frames.append((op, payload))
        if op == "HELO":
            self.helos.append(payload)
            self.send(b"vers", b"7.9")
            self.send(b"setd", b"\xfe")
            self.send(b"setd", b"\x00")
            self.send(b"aude", b"\x01\x01")
            self.volume(20)
        elif op == "SETD":
            self.names.append(payload)
        elif op == "RESP":
            if self.awaiting_stmc:
                return
            headers = payload.decode("latin-1").lower()
            ctype = ""
            for line in headers.split("\r\n"):
                if line.startswith("content-type:"):
                    ctype = line.split(":", 1)[1].strip()
            self.send(b"codc", CODC.get(ctype, b"m????"))
            self.send(b"cont", struct.pack("!IBH", 0, 0, 0))


class FakeDiscovery(asyncio.DatagramProtocol):
    """UDP discovery responder like aioslimproto's (E TLV reply, D legacy reply)."""

    def __init__(self, name: str = "Seamer Test Server", ipad: str = "192.0.2.10") -> None:
        self.name = name
        self.ipad = ipad
        self.requests: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.requests.append(data)
        assert self.transport is not None
        if data[:1] == b"e":
            vals = {"NAME": self.name, "IPAD": self.ipad, "JSON": "0", "VERS": "7.999.999"}
            out = b"E" + b"".join(
                k.encode() + bytes([len(v)]) + v.encode() for k, v in vals.items()
            )
            self.transport.sendto(out, addr)
        elif data[:1] == b"d":
            self.transport.sendto(b"D" + self.ipad.encode().ljust(16, b"\x00"), addr)

    @classmethod
    async def start(cls, **kw: Any) -> tuple[FakeDiscovery, int]:
        loop = asyncio.get_running_loop()
        _, proto = await loop.create_datagram_endpoint(
            lambda: cls(**kw), local_addr=("127.0.0.1", 0)
        )
        assert proto.transport is not None
        return proto, proto.transport.get_extra_info("sockname")[1]
