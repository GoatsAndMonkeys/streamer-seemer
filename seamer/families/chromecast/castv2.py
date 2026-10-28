"""The CASTV2 TLS server: connections, virtual channels and frame I/O.

A sender opens one TLS socket and multiplexes *virtual connections* over it: it sends
``CONNECT`` from its sender id (``sender-0``) to a destination (``receiver-0``, or an
app's ``transportId``) before talking to it. Broadcasts from a destination go to every
socket that holds a virtual connection to that destination.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import socket
import ssl
import struct
from collections.abc import Awaitable, Callable
from typing import Any

from . import protocol as p

Handler = Callable[["CastConnection", p.CastMessage], Awaitable[None]]

_conn_ids = itertools.count(1)


class CastConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_send: Callable[[CastConnection, p.CastMessage], None] | None = None,
    ) -> None:
        self.id = next(_conn_ids)
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.peer = f"{peer[0]}:{peer[1]}"
        # (sender id, destination id) pairs opened with CONNECT
        self.channels: set[tuple[str, str]] = set()
        self.closed = False
        self._on_send = on_send
        self._lock = asyncio.Lock()

    def senders_for(self, dest: str) -> list[str]:
        return sorted({s for s, d in self.channels if d == dest})

    def has_channel_to(self, dest: str) -> bool:
        return any(d == dest for _s, d in self.channels)

    async def send(self, msg: p.CastMessage) -> bool:
        if self.closed:
            return False
        if self._on_send is not None:
            self._on_send(self, msg)
        try:
            async with self._lock:
                self.writer.write(p.frame(msg))
                await self.writer.drain()
        except (ConnectionError, OSError, RuntimeError):
            self.closed = True
            return False
        return True

    async def send_json(self, source: str, dest: str, ns: str, payload: dict[str, Any]) -> bool:
        return await self.send(p.json_message(source, dest, ns, payload))

    def reset(self) -> None:
        """Abort the socket (the peer sees ECONNRESET, like a real device dropping it)."""
        self.closed = True
        transport = self.writer.transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        transport.abort()

    async def close(self) -> None:
        self.closed = True
        await p.close_writer(self.writer)


class CastServer:
    """``asyncio.start_server`` with TLS; one task per connection reading frames."""

    def __init__(
        self,
        handler: Handler,
        *,
        on_open: Callable[[CastConnection], None] | None = None,
        on_close: Callable[[CastConnection, str], None] | None = None,
        on_send: Callable[[CastConnection, p.CastMessage], None] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.handler = handler
        self.on_open = on_open
        self.on_close = on_close
        self.on_send = on_send
        self.ssl_context = ssl_context or p.server_ssl_context()
        self.connections: set[CastConnection] = set()
        self.host = "127.0.0.1"
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.accepting = True  # False: accept and immediately reset (device unreachable)
        self.accept_filter: Callable[[], bool] | None = None  # checked per new socket

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start(self, host: str, port: int = 0) -> None:
        self._server = await asyncio.start_server(
            self._serve, host, port, ssl=self.ssl_context, reuse_address=True
        )
        self.host = host
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop_listening(self) -> None:
        srv, self._server = self._server, None
        if srv is not None:
            srv.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(srv.wait_closed(), 2.0)

    def reset_all(self) -> int:
        conns = list(self.connections)
        for conn in conns:
            conn.reset()
        return len(conns)

    async def stop(self) -> None:
        await self.stop_listening()
        for conn in list(self.connections):
            await conn.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        conn = CastConnection(reader, writer, self.on_send)
        if not self.accepting or (self.accept_filter is not None and not self.accept_filter()):
            conn.reset()
            self._tasks.discard(task)
            return
        self.connections.add(conn)
        if self.on_open:
            self.on_open(conn)
        reason = "eof"
        try:
            while not conn.closed:
                msg = await p.read_frame(reader)
                await self.handler(conn, msg)
        except asyncio.IncompleteReadError:
            reason = "eof"
        except p.FrameError as err:
            reason = f"bad_frame: {err}"
        except (ConnectionError, OSError, ssl.SSLError) as err:
            reason = repr(err)
        except asyncio.CancelledError:
            reason = "server_stop"
        finally:
            self.connections.discard(conn)
            if not conn.closed:
                await conn.close()
            if self.on_close:
                self.on_close(conn, reason)
            self._tasks.discard(task)
