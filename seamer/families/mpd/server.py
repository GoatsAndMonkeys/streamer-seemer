"""The MPD line-protocol server: connections, greeting, idle/noidle, command lists.

This module follows MPD's ``client/ClientProcess.cxx`` closely:

- a line must start with a lower-case letter or the connection is closed;
- ``noidle`` while idling answers ``OK``; a stray ``noidle`` is ignored (MP2);
- any other line while idling closes the connection;
- ``command_list_begin``/``command_list_ok_begin`` ... ``command_list_end`` run the
  buffered commands in order (``list_OK`` after each with ok_begin), stop at the first
  error (``ACK [errno@index]``) and end with ``OK``;
- idle flags accumulate per client; ``idle`` returns at once when a subscribed flag is
  pending, and every notify clears all pending flags (``Client::IdleNotify``).

The command table and handlers live in :mod:`.commands`; the device owns the state.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .protocol import (
    PERM_ALL,
    PERM_NONE,
    SUBSYSTEMS,
    Ack,
    ProtocolError,
    TokenizeError,
    ack_line,
    parse_permissions,
    tokenize,
)

if TYPE_CHECKING:
    from .device import MpdDevice

_conn_ids = itertools.count(1)


@dataclass
class Conn:
    """One client connection (or an internal "external client" with no socket)."""

    id: int
    peer: str
    perms: int
    writer: asyncio.StreamWriter | None = None
    idle_waiting: bool = False
    idle_mask: frozenset[str] = frozenset()
    pending: set[str] = field(default_factory=set)
    last_active: float = 0.0
    role: str = "unknown"  # "command" / "idle" (python-mpd2 usage) / "external"
    cmd_list: list[str] | None = None
    list_ok: bool = False
    closed: bool = False
    binary_limit: int = 8192
    lines: int = 0

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "peer": self.peer,
            "role": self.role,
            "idle": self.idle_waiting,
            "idle_mask": sorted(self.idle_mask),
            "pending": sorted(self.pending),
            "perms": self.perms,
            "lines": self.lines,
        }


class Reply:
    """Output of one command: ``key: value`` lines plus optional binary payload."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def add(self, key: str, value: object) -> None:
        self.chunks.append(f"{key}: {value}\n".encode())

    def binary(self, data: bytes) -> None:
        self.chunks.append(f"binary: {len(data)}\n".encode() + data + b"\n")

    def data(self) -> bytes:
        return b"".join(self.chunks)


class CloseConnection(Exception):
    """A handler asks to drop the connection without a reply (``close``)."""


class EnterIdle(Exception):
    def __init__(self, mask: frozenset[str]) -> None:
        super().__init__("idle")
        self.mask = mask


class MpdServer:
    def __init__(self, dev: MpdDevice) -> None:
        self.dev = dev
        self.rec = dev.rec
        self.knobs = dev.knobs
        self.clock = dev.clock
        self.conns: dict[int, Conn] = {}
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._watchdog: asyncio.Task[None] | None = None
        self.port = 0
        self.refused = 0

    # -- lifecycle ---------------------------------------------------------------

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._accept, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._watchdog = asyncio.create_task(self._timeouts())

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
        if self._server is not None:
            self._server.close()
        for conn in list(self.conns.values()):
            self.close(conn, "shutdown")
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    def default_perms(self) -> int:
        text = self.knobs.get("default_permissions")
        if text:
            return parse_permissions(text)
        return PERM_NONE if self.knobs.get("password") else PERM_ALL

    # -- connections -------------------------------------------------------------

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        peer = writer.get_extra_info("peername")
        peer_s = f"{peer[0]}:{peer[1]}" if peer else "?"
        try:
            if self.knobs.get("offline"):
                self.rec.event("mpd_refused", peer=peer_s, reason="offline")
                writer.close()
                return
            limit = self.knobs.get("max_connections")
            if len(self.conns) >= limit:
                # MPD: "Max connections reached" -- accept, then close with no greeting
                self.refused += 1
                self.rec.knob(
                    "fired",
                    knob="max_connections",
                    quirks=list(self.knobs.defs["max_connections"].quirks),
                    confidence=self.knobs.defs["max_connections"].confidence,
                    peer=peer_s,
                    open=len(self.conns),
                )
                self.rec.event("mpd_refused", peer=peer_s, reason="max_connections")
                writer.close()
                return
            conn = Conn(id=next(_conn_ids), peer=peer_s, perms=self.default_perms(), writer=writer)
            conn.last_active = self.clock.monotonic()
            self.conns[conn.id] = conn
            self.rec.event("mpd_connect", conn=conn.id, peer=peer_s, open=len(self.conns))
            delay = self.knobs.get("greeting_delay_s")
            if delay > 0:
                self.rec.knob(
                    "fired",
                    knob="greeting_delay_s",
                    quirks=list(self.knobs.defs["greeting_delay_s"].quirks),
                    confidence="high",
                    delay_s=delay,
                )
                await self.clock.sleep(delay)
            self._write(conn, f"OK MPD {self.dev.protocol_version}\n".encode())
            await self._serve(conn, reader)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            for c in list(self.conns.values()):
                if c.writer is writer:
                    self.close(c, "eof")

    async def _serve(self, conn: Conn, reader: asyncio.StreamReader) -> None:
        while not conn.closed:
            try:
                raw = await reader.readline()
            except (ConnectionError, ValueError):
                return
            if not raw:
                return
            if not raw.endswith(b"\n"):
                return  # EOF in the middle of a line
            conn.last_active = self.clock.monotonic()
            conn.lines += 1
            line = raw[:-1].decode("utf-8", errors="replace")
            if line.endswith("\r"):
                line = line[:-1]
            await self.process_line(conn, line)

    def close(self, conn: Conn, reason: str) -> None:
        if conn.closed:
            return
        conn.closed = True
        conn.idle_waiting = False
        self.conns.pop(conn.id, None)
        if conn.writer is not None:
            with contextlib.suppress(Exception):
                conn.writer.close()
        self.rec.event(
            "mpd_disconnect", conn=conn.id, role=conn.role, reason=reason, open=len(self.conns)
        )

    def _write(self, conn: Conn, data: bytes) -> None:
        if conn.closed or conn.writer is None:
            return
        try:
            conn.writer.write(data)
        except Exception:
            self.close(conn, "write_error")

    def drop(self, which: str) -> list[int]:
        """Close connections by role (``command``/``idle``/``all``); returns their ids."""
        hit = [c for c in list(self.conns.values()) if which == "all" or c.role == which]
        for c in hit:
            self.close(c, f"dropped:{which}")
        return [c.id for c in hit]

    async def _timeouts(self) -> None:
        while True:
            await self.clock.sleep(0.5)
            limit = self.knobs.get("connection_timeout_s")
            if limit <= 0:
                continue
            now = self.clock.monotonic()
            for conn in list(self.conns.values()):
                if conn.idle_waiting and not self.knobs.get("timeout_idle_too"):
                    continue
                if now - conn.last_active >= limit:
                    if conn.idle_waiting:
                        self.rec.knob(
                            "fired",
                            knob="timeout_idle_too",
                            quirks=list(self.knobs.defs["timeout_idle_too"].quirks),
                            confidence=self.knobs.defs["timeout_idle_too"].confidence,
                            conn=conn.id,
                        )
                    self.close(conn, "timeout")

    # -- idle --------------------------------------------------------------------

    def idle_add(self, *subsystems: str) -> None:
        """Raise idle flags on every client (MPD's ``idle_add``)."""
        self.rec.event("mpd_idle_add", subsystems=list(subsystems))
        for conn in list(self.conns.values()):
            conn.pending.update(subsystems)
            if conn.idle_waiting and self._wanted(conn):
                self._idle_notify(conn)

    @staticmethod
    def _wanted(conn: Conn) -> set[str]:
        mask = conn.idle_mask or frozenset(SUBSYSTEMS)
        return conn.pending & mask

    def _idle_notify(self, conn: Conn) -> None:
        flags = self._wanted(conn)
        conn.pending.clear()
        conn.idle_waiting = False
        out = "".join(f"changed: {s}\n" for s in SUBSYSTEMS if s in flags) + "OK\n"
        self.rec.resp("idle", conn=conn.id, changed=[s for s in SUBSYSTEMS if s in flags])
        self._write(conn, out.encode())

    def _enter_idle(self, conn: Conn, mask: frozenset[str]) -> None:
        conn.idle_mask = mask
        if not mask:
            conn.role = "idle" if conn.role != "external" else conn.role
        elif mask == {"database"} and conn.role != "external":
            conn.role = "command"  # python-mpd2's keepalive on a command connection
        conn.idle_waiting = True
        if self._wanted(conn):
            self._idle_notify(conn)

    # -- line processing -----------------------------------------------------------

    async def process_line(self, conn: Conn, line: str) -> None:
        knobs = self.knobs
        if knobs.enabled("drop_connection"):
            which = knobs.param("drop_connection", "which", "command")
            if (which in ("all", conn.role)) and knobs.fires(
                "drop_connection", conn=conn.id, role=conn.role, line=line[:40]
            ):
                self.close(conn, "drop_connection")
                return
        if not line or not ("a" <= line[0] <= "z"):
            self.rec.event("mpd_malformed", conn=conn.id, line=line[:80])
            self.close(conn, "malformed")
            return
        if line == "noidle":
            if conn.idle_waiting:
                conn.idle_waiting = False
                conn.pending.clear()
                self.rec.cmd("noidle", conn=conn.id)
                self._write(conn, b"OK\n")
            else:
                self._stray_noidle(conn)
            return
        if conn.idle_waiting:
            self.rec.event("mpd_command_during_idle", conn=conn.id, line=line[:80])
            self.close(conn, "command_during_idle")
            return
        if conn.cmd_list is not None:
            if line == "command_list_end":
                cmds, conn.cmd_list = conn.cmd_list, None
                await self._run_list(conn, cmds, conn.list_ok)
            else:
                conn.cmd_list.append(line)
            return
        if line in ("command_list_begin", "command_list_ok_begin"):
            conn.cmd_list = []
            conn.list_ok = line == "command_list_ok_begin"
            return
        await self._latency()
        out = await self._run_one(conn, 0, line)
        if out is None:
            return
        reply, ok = out
        self._write(conn, reply + (b"OK\n" if ok else b""))

    def _stray_noidle(self, conn: Conn) -> None:
        mode = self.knobs.get("stray_noidle")
        self.rec.event("mpd_stray_noidle", conn=conn.id, reply=mode)
        if mode == "ignore":
            return
        d = self.knobs.defs["stray_noidle"]
        self.rec.knob("fired", knob="stray_noidle", quirks=list(d.quirks), confidence=d.confidence)
        if mode == "ok":
            self._write(conn, b"OK\n")
        else:
            self._write(conn, ack_line(Ack.UNKNOWN, 0, "", 'unknown command "noidle"').encode())

    async def _latency(self) -> None:
        delay = self.knobs.get("latency_s")
        jitter = self.knobs.get("jitter_s")
        if jitter:
            delay += self.knobs.rng.uniform("jitter", 0.0, jitter)
        if delay > 0:
            await self.clock.sleep(delay)
        if self.knobs.fires("hang", proto="mpd"):
            await self.clock.sleep(float(self.knobs.param("hang", "hold_s", 60.0)))

    async def _run_list(self, conn: Conn, cmds: list[str], list_ok: bool) -> None:
        self.rec.cmd("command_list", conn=conn.id, n=len(cmds), ok_mode=list_ok)
        await self._latency()
        out = bytearray()
        for i, line in enumerate(cmds):
            res = await self._run_one(conn, i, line, in_list=True)
            if res is None:  # closed
                return
            reply, ok = res
            out += reply
            if not ok:
                self._write(conn, bytes(out))
                return
            if list_ok:
                out += b"list_OK\n"
        self._write(conn, bytes(out) + b"OK\n")

    async def _run_one(
        self, conn: Conn, index: int, line: str, *, in_list: bool = False
    ) -> tuple[bytes, bool] | None:
        """Run one command. Returns (bytes, ok) or None when the connection closed or
        the client entered idle."""
        from .commands import COMMANDS

        try:
            args = tokenize(line)
        except TokenizeError as err:
            return ack_line(Ack.ARG, index, "", str(err)).encode(), False
        if not args:
            return ack_line(Ack.UNKNOWN, index, "", "No command given").encode(), False
        name, params = args[0], args[1:]
        self.rec.cmd(name, conn=conn.id, args=params, proto="mpd", role=conn.role)
        cmd = COMMANDS.get(name)
        if cmd is None or not self.dev.supports(name):
            msg = f'unknown command "{name}"'
            self.rec.resp(name, conn=conn.id, ack=int(Ack.UNKNOWN), msg=msg)
            return ack_line(Ack.UNKNOWN, index, "", msg).encode(), False
        try:
            if cmd.perm & conn.perms != cmd.perm:
                raise ProtocolError(Ack.PERMISSION, f'you don\'t have permission for "{name}"')
            nargs = len(params)
            lo, hi = cmd.min_args, cmd.max_args
            if lo == hi and nargs != lo:
                raise ProtocolError(Ack.ARG, f'wrong number of arguments for "{name}"')
            if nargs < lo:
                raise ProtocolError(Ack.ARG, f'too few arguments for "{name}"')
            if hi >= 0 and nargs > hi:
                raise ProtocolError(Ack.ARG, f'too many arguments for "{name}"')
            reply = Reply()
            await cmd.handler(self.dev, conn, params, reply)
        except ProtocolError as err:
            shown = name if err.command is None else err.command
            self.rec.resp(name, conn=conn.id, ack=int(err.code), msg=err.message)
            return ack_line(err.code, index, shown, err.message).encode(), False
        except EnterIdle as idle:
            if in_list:
                # MPD cannot idle inside a command list; treat it as an argument error
                return ack_line(Ack.ARG, index, name, "idle in command list").encode(), False
            self._enter_idle(conn, idle.mask)
            return None
        except CloseConnection:
            self.close(conn, "close")
            return None
        self.rec.resp(name, conn=conn.id, ok=True)
        return reply.data(), True
