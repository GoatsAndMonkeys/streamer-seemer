"""The squeezelite device: a slimproto *client* that connects out to the server.

Unlike the HTTP families, the protocol surface here is an outgoing TCP connection to
the server's slimproto port (3483 by default) plus UDP discovery. The device's own
HTTP listener only carries the ``/_sim`` control API.

Flow (squeezelite ``slimproto.c`` / ``stream.c``):

- connect, send HELO (capabilities), answer ``vers``/``setd``/``aude``/``audg``
- ``strm s``: STMf, open the HTTP stream (embedded request; ``server_ip`` 0 = the
  slimproto server), STMc. RESP with the response headers. With autostart >= 2 the
  body is held until ``cont``; ``cont`` subtracts 2 from autostart. Autostart 1 plays
  as soon as the buffer is ready; autostart 0 sends STMl and waits for ``strm u``.
- STMs when audio starts, STMd (+DSCO) when the stream is fully read, STMu when the
  output drained with nothing queued, STMo on an output underrun, STMt as heartbeat.
- ``strm s`` while the previous stream is fully read but still playing is gapless:
  it queues the next track (the transport's next slot).

Truth (what is heard) is the TransportMachine + HttpSink as in every family.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import ipaddress
import struct
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from ...simkit.device import SimDevice
from ...simkit.transport import TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)
from . import protocol as P

OUTPUT_BUFFER_BYTES = 3_528_000  # squeezelite default output buffer (20 s of 44.1k/16/2)
PCM_BPS = 176_400


@dataclass
class Stream:
    """One ``strm s`` request and what the device has said about it."""

    strm: P.Strm
    url: str
    autostart: int
    held: bool = False  # body/output held (autostart >= 2 until cont, 0 until strm u)
    content_type: str | None = None
    resp: bytes | None = None
    eof: bool = False
    stml_sent: bool = False
    stms_at: float | None = None
    stmd_sent: bool = False
    decode_error: bool = False
    silent: bool = False  # flac_sync_silent: read but never played
    carry_ms: int = 0  # seek_elapsed_from_zero=False: elapsed of the flushed stream
    jump_ms: int = 0
    paused_at: float | None = None
    paused_ms: float = 0.0
    timers: list[Any] = field(default_factory=list)


def _as_list(value: Any) -> list[str]:
    """A profile list; identity values may arrive stringified ("['a', 'b']" or "a,b")."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value).strip()
    if text.startswith("["):
        with contextlib.suppress(ValueError, SyntaxError):
            return [str(v) for v in ast.literal_eval(text)]
    return [p.strip() for p in text.split(",") if p.strip()]


class SqueezeliteDevice(SimDevice):
    family = "squeezelite"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        ident = self.identity
        self.mac = str(ident.get("mac", "02:00:00:00:00:01")).lower()
        self.name = str(ident.get("name", self.id))
        dev = ident.get("device_id", "squeezeplay")
        self.device_id = int(dev) if str(dev).isdigit() else P.DEVICE_IDS[str(dev)]
        self.revision = int(ident.get("revision", 0))
        # volume / output
        self.volume = 0
        self.gain = 0.0 if self.knobs.get("initial_gain_zero") else 1.0
        self.audg_seen = False
        self.output_on = True
        self.dac_on = True
        # connection
        self.server_addr: tuple[str, int] | None = None
        self.connected = False
        self.connects = 0
        self.helo_count = 0
        self.rx: list[tuple[str, bytes]] = []  # every server frame (tests)
        self.tx: list[tuple[str, str]] = []  # (opcode, STAT event or "") sent
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._conn_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._pending: list[bytes] = []
        self.writes = 0  # socket writes (batch_frames_per_recv coalesces frames)
        self._pending_since = 0.0
        self._last_rx = 0.0
        self._last_stmt = 0.0
        self._stopping = False
        self._bg: set[asyncio.Future[Any]] = set()
        # streams
        self.stream: Stream | None = None
        self.next_stream: Stream | None = None
        self._stale: tuple[int, int] | None = None  # (count, elapsed_ms) after a flush
        self._stale_resp: bytes | None = None
        self._capped = False
        self._carry_ms = 0
        if self.sink is not None:
            self.sink.add_listener(self._on_sink)
        self.transport.add_listener(self._on_transport)

    # -- SimDevice hooks ---------------------------------------------------------

    def build_app(self) -> web.Application:
        return self.make_app()

    async def on_start(self) -> None:
        self._tick_task = asyncio.create_task(self._tick_loop())
        server = self.ctx.options.get("server") or self.knobs.get("server")
        if server:
            host, _, port = str(server).rpartition(":")
            self.connect(host or "127.0.0.1", int(port or P.SLIMPROTO_PORT))

    async def on_stop(self) -> None:
        self._stopping = True
        for task in (self._conn_task, self._tick_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._close_socket()

    def status(self) -> dict[str, Any]:
        st = self.stream
        return {
            "connected": self.connected,
            "server": f"{self.server_addr[0]}:{self.server_addr[1]}" if self.server_addr else None,
            "mac": self.mac,
            "name": self.name,
            "volume": self.volume,
            "gain": round(self.gain, 5),
            "output_on": self.output_on,
            "connects": self.connects,
            "stream": None
            if st is None
            else {"url": st.url, "autostart": st.autostart, "held": st.held, "eof": st.eof},
            "next_stream": self.next_stream.url if self.next_stream else None,
            "elapsed_ms": self.elapsed_ms(),
            "jiffies": self.jiffies(),
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "connect":
            self.connect(payload.get("host", "127.0.0.1"), int(payload.get("port", 3483)))
            return True
        if name == "disconnect":
            await self.disconnect()
            return True
        if name == "bye":
            await self.send_bye()
            return True
        if name == "discover":
            reply, addr = await self.discover(
                payload.get("host", "127.0.0.1"),
                int(payload.get("port", P.SLIMPROTO_PORT)),
                legacy=bool(payload.get("legacy", False)),
            )
            if payload.get("connect"):
                self.connect(reply.get("IPAD") or addr[0], addr[1])
            return {"reply": reply, "addr": list(addr)}
        raise KeyError(name)

    # -- clocks & reported values ------------------------------------------------

    def jiffies(self) -> int:
        k = self.knobs
        ms = self.clock.monotonic() * 1000 * (1 + k.get("drift_ppm") / 1e6)
        return int(ms + k.get("jiffies_offset_ms")) & 0xFFFFFFFF

    def elapsed_ms(self) -> int:
        st = self.stream
        if st is None or self.sink is None or st.stms_at is None:
            ms = 0 if st is None else st.carry_ms
        else:
            ms = int(self.sink.audible.played_s * 1000) + st.carry_ms + st.jump_ms
            if self.knobs.get("heartbeat_while_paused_advances"):
                paused = st.paused_ms
                if st.paused_at is not None:
                    paused += (self.clock.monotonic() - st.paused_at) * 1000
                ms += int(paused)
        bits = max(1, min(32, self.knobs.get("elapsed_ms_bits")))
        return ms & ((1 << bits) - 1)

    def stat_fields(self, server_timestamp: int = 0) -> P.StatFields:
        size = self.knobs.get("stream_buffer_kb") * 1024
        sink = self.sink
        fill = 0
        out_fill = 0
        rx = 0
        if sink is not None and self.stream is not None:
            rx = sink.bytes_read
            out_fill = min(OUTPUT_BUFFER_BYTES, int(sink.buffer.fill_s * PCM_BPS))
            fill = 0 if self.stream.eof else min(size, out_fill // 4)
        return P.StatFields(
            stream_buffer_size=size,
            stream_buffer_fullness=fill,
            bytes_received=rx,
            jiffies=self.jiffies(),
            output_buffer_size=OUTPUT_BUFFER_BYTES,
            output_buffer_fullness=out_fill,
            elapsed_ms=self.elapsed_ms(),
            server_timestamp=server_timestamp,
        )

    # -- capabilities --------------------------------------------------------------

    def capabilities(self) -> str:
        override = self.knobs.get("hello_caps")
        if override:
            return override
        ident = self.identity
        parts = [
            f"Model={ident.get('model', 'squeezelite')}",
            f"ModelName={ident.get('model_name', 'SqueezeLite')}",
        ]
        parts += _as_list(ident.get("caps", []))
        parts.append(f"Firmware={ident.get('firmware', 'v2.0.0-1488')}")
        parts.append(f"MaxSampleRate={ident.get('max_sample_rate', 192000)}")
        if self.knobs.get("can_https"):
            parts.append("CanHTTPS=1")
        parts += _as_list(ident.get("codecs", ["flc", "pcm", "mp3"]))
        return ",".join(parts)

    # -- connection ----------------------------------------------------------------

    def connect(self, host: str, port: int) -> None:
        """(Re)connect to ``host:port`` and keep the connection up until stopped."""
        self.server_addr = (host, port)
        self._stopping = False
        if self._conn_task is not None and not self._conn_task.done():
            self._conn_task.cancel()
        self._conn_task = asyncio.create_task(self._conn_loop())

    async def disconnect(self) -> None:
        """Drop the control connection and stay disconnected."""
        self._stopping = True
        task, self._conn_task = self._conn_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._close_socket()

    async def send_bye(self) -> None:
        """Send BYE!; close the socket unless ``send_bye_keep_socket``."""
        self._send(P.bye(), "")
        self._flush()
        if not self.knobs.fires("send_bye_keep_socket"):
            await self.disconnect()

    def _close_socket(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._writer = None
        self._reader = None
        if self.connected:
            self.connected = False
            self.rec.event("disconnected")

    async def _conn_loop(self) -> None:
        assert self.server_addr is not None
        while not self._stopping:
            host, port = self.server_addr
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError as err:
                self.rec.event("connect_failed", host=host, port=port, error=repr(err))
                await self.clock.sleep(max(0.1, self.knobs.get("reconnect_delay_s")))
                continue
            self._reader, self._writer = reader, writer
            self.connects += 1
            self.connected = True
            self.rec.event("connected", host=host, port=port, attempt=self.connects)
            reason = "eof"
            try:
                delay = self.knobs.get("helo_delay_s")
                if delay > 0:
                    await self.clock.sleep(delay)
                self._send_helo()
                reason = await self._read_loop(reader)
            except (ConnectionError, OSError) as err:
                reason = repr(err)
            finally:
                self._close_socket()
            self.rec.event("connection_lost", reason=reason)
            if self._stopping:
                return
            await self.clock.sleep(max(0.1, self.knobs.get("reconnect_delay_s")))

    def _send_helo(self) -> None:
        sink = self.sink
        frame = P.helo(
            device_id=self.device_id,
            revision=self.revision,
            mac=self.mac,
            uuid=bytes.fromhex(str(self.identity.get("uuid", "")).replace("-", ""))
            if self.identity.get("uuid")
            else b"\x00" * 16,
            reconnect=self.helo_count > 0,
            bytes_received=sink.bytes_read if sink and self.stream else 0,
            capabilities=self.capabilities(),
        )
        self.helo_count += 1
        self._send(frame, "")

    async def _read_loop(self, reader: asyncio.StreamReader) -> str:
        frames = P.ServerFrameReader()
        self._last_rx = self.clock.monotonic()
        silence = self.knobs.get("server_silence_timeout_s")
        while True:
            try:
                if silence > 0:
                    data = await asyncio.wait_for(reader.read(4096), timeout=silence)
                else:
                    data = await reader.read(4096)
            except TimeoutError:
                self.rec.event("server_silence", seconds=silence)
                return "server_silence"
            if not data:
                return "eof"
            self._last_rx = self.clock.monotonic()
            for op, payload in frames.feed(data):
                self.rx.append((op, payload))
                try:
                    await self._handle(op, payload)
                except Exception as err:  # a bad frame must not kill the client
                    self.rec.event("handler_error", op=op, error=repr(err))

    # -- sending -------------------------------------------------------------------

    def _send(self, frame: bytes, event: str) -> None:
        op = frame[:4].decode("latin-1")
        self.tx.append((op, event))
        self.rec.resp(f"tx:{op}{':' + event if event else ''}")
        if self._writer is None:
            return
        self._pending.append(frame)
        if len(self._pending) == 1:
            self._pending_since = self.clock.monotonic()
        if len(self._pending) >= max(1, self.knobs.get("batch_frames_per_recv")):
            self._flush()

    def _flush(self) -> None:
        if self._pending and self._writer is not None:
            data = b"".join(self._pending)
            self._pending.clear()
            self.writes += 1
            with contextlib.suppress(Exception):
                self._writer.write(data)

    def send_stat(self, event: str, server_timestamp: int = 0) -> None:
        fields = self.stat_fields(server_timestamp)
        if event == "STMt" and self._stale is not None:
            count, ms = self._stale
            fields.elapsed_ms = ms
            self._stale = (count - 1, ms) if count > 1 else None
        self._send(P.stat(event, fields), event)
        if event == "STMt":
            self._last_stmt = self.clock.monotonic()

    def send_resp(self, headers: bytes) -> None:
        self._send(P.resp(headers), "")

    # -- server frames -------------------------------------------------------------

    async def _handle(self, op: str, data: bytes) -> None:
        if op == "strm":
            await self._strm(P.parse_strm(data))
        elif op == "cont":
            await self._cont(data)
        elif op == "codc":
            self._codc(data)
        elif op == "aude":
            await self._aude(data)
        elif op == "audg":
            self._audg(data)
        elif op == "setd":
            self._setd(data)
        elif op == "vers":
            self.rec.cmd("vers", version=data.decode("latin-1"))
        elif op == "serv":
            self._serv(data)
        else:
            self.rec.cmd(f"unhandled:{op}", size=len(data))

    async def _strm(self, s: P.Strm) -> None:
        cmd = s.command
        if cmd != "t":
            self.rec.cmd(f"strm:{cmd}", autostart=s.autostart, replay_gain=s.replay_gain)
        tm = self.transport
        if cmd == "t":
            self.send_stat("STMt", server_timestamp=s.replay_gain)
        elif cmd == "q":
            await self._flush_stream()
            self.send_stat("STMf")
        elif cmd == "f":
            if await self._flush_stream():
                self.send_stat("STMf")
        elif cmd == "p":
            if s.replay_gain > 0:  # pause_for: silent, no STAT
                await tm.pause()
                self._mark_paused(True)
                self.clock.call_later(s.replay_gain / 1000, lambda: self._spawn(self._unpause()))
                return
            await tm.pause()
            self._mark_paused(True)
            self.send_stat("STMp")
        elif cmd == "u":
            # START_AT waits in the output thread; the control loop answers at once
            self._spawn(self._unpause(at_jiffies=s.replay_gain))
            self.send_stat("STMr")
        elif cmd == "a":
            self.rec.event("skip_ahead_ignored", ms=s.replay_gain)
        elif cmd == "s":
            await self._strm_start(s)

    async def _strm_start(self, s: P.Strm) -> None:
        self.send_stat("STMf")
        k = self.knobs
        if s.format == "p" and (s.pcm_bits or 16) > k.get("max_pcm_bits"):
            self.rec.event("unsupported_pcm", bits=s.pcm_bits)
            self.send_stat("STMn")
            return
        if s.https and not k.get("can_https"):
            self.rec.event("https_unsupported")
            self.send_stat("STMn")
            return
        host = (
            str(ipaddress.IPv4Address(s.server_ip))
            if s.server_ip
            else (self.server_addr[0] if self.server_addr else "127.0.0.1")
        )
        port = s.server_port or (443 if s.https else 80)
        _, path = s.request_line()
        url = f"{'https' if s.https else 'http'}://{host}:{port}{path}"
        st = Stream(strm=s, url=url, autostart=s.autostart)
        if not k.get("seek_elapsed_from_zero"):
            st.carry_ms = self._carry_ms
        if k.get("codc_after_body_noise"):
            st.autostart = s.autostart - 2 if s.autostart >= 2 else s.autostart
        cur = self.stream
        tm = self.transport
        gapless = (
            cur is not None
            and tm.state in (TState.PLAYING, TState.BUFFERING)
            and self.next_stream is None
        )
        if gapless:
            assert cur is not None
            if not cur.eof:
                # squeezelite replaces the stream: what is not read yet is lost
                self._cap_current()
            self.next_stream = st
            await tm.set_next(url)
            self.rec.event("queued_next", url=url)
        else:
            self.stream, self.next_stream = st, None
            self._cancel_timers()
            await tm.set_next(None)
            await tm.set_uri(url)
            await tm.play()
        self.send_stat("STMc")
        stale, self._stale_resp = self._stale_resp, None
        if stale is not None and k.get("stale_resp_after_flush"):
            self.rec.event("stale_resp")
            self.send_resp(stale)

    def _cap_current(self) -> None:
        if self.sink is None:
            return
        self._capped = True
        self.knobs.set_live("time_cap_s", max(0.01, self.sink.buffer.received_s + 0.01))

    def _uncap(self) -> None:
        if self._capped:
            self._capped = False
            self.knobs.set_live("time_cap_s", 0.0)

    async def _flush_stream(self) -> bool:
        """strm q/f: stop output and stream. True if anything was flushed."""
        st = self.stream
        if st is None and self.next_stream is None:
            return False
        if st is not None:
            ms = self.elapsed_ms()
            self._carry_ms = ms
            if self.knobs.get("stale_stmt_after_flush") > 0:
                self._stale = (self.knobs.get("stale_stmt_after_flush"), ms)
            self._stale_resp = st.resp
        self._cancel_timers()
        self.stream = self.next_stream = None
        self._uncap()
        await self.transport.set_next(None)
        await self.transport.stop()
        return True

    async def _unpause(self, at_jiffies: int = 0) -> None:
        st = self.stream
        tm = self.transport
        if at_jiffies and not self.knobs.get("early_start_before_unpause_at"):
            delay = ((at_jiffies - self.jiffies()) & 0xFFFFFFFF) / 1000
            if 0 < delay < 10:
                await self.clock.sleep(delay)
        if st is not None and st.held:
            self._release(st)
        elif tm.state == TState.PAUSED:
            await tm.play()
            if st is not None and self.knobs.fires("pause_resume_position_jump"):
                st.jump_ms += int(
                    float(self.knobs.param("pause_resume_position_jump", "jump_s", 2.0)) * 1000
                )
        self.output_on = True
        self._mark_paused(False)

    def _mark_paused(self, paused: bool) -> None:
        st = self.stream
        if st is None:
            return
        now = self.clock.monotonic()
        if paused and st.paused_at is None:
            st.paused_at = now
        elif not paused and st.paused_at is not None:
            st.paused_ms += (now - st.paused_at) * 1000
            st.paused_at = None

    def _hold(self, st: Stream) -> None:
        if self.sink is not None and not st.held:
            st.held = True
            self.sink.pause()
            self.rec.event("hold", url=st.url, autostart=st.autostart)

    def _release(self, st: Stream) -> None:
        if st.silent:
            return
        if st.held:
            st.held = False
            self.rec.event("release", url=st.url)
            if self.transport.state == TState.PAUSED:
                self._spawn(self.transport.play())
            elif self.sink is not None:
                self.sink.resume()

    async def _cont(self, data: bytes) -> None:
        metaint, loop, _ = P.parse_cont(data)
        st = self.stream if self.next_stream is None else self.next_stream
        if self.stream is not None and self.stream.resp is not None and self.stream.held:
            st = self.stream
        self.rec.cmd("cont", metaint=metaint, loop=loop)
        if st is None:
            return
        if st.autostart >= 2:
            st.autostart -= 2
        if st.autostart == 1:
            self._release(st)

    def _codc(self, data: bytes) -> None:
        fmt = data[:5].decode("latin-1")
        self.rec.cmd("codc", format=fmt)
        st = self.stream
        if fmt[:1] == "p" and P.PCM_SIZES.get(fmt[1:2], 16) > self.knobs.get("max_pcm_bits"):
            self.rec.event("unsupported_pcm", bits=P.PCM_SIZES.get(fmt[1:2]))
            self.send_stat("STMn")
            return
        if (
            st is not None
            and st.stms_at is not None
            and self.knobs.get("codc_after_body_noise")
            and self.sink is not None
        ):
            lost = self.sink.buffer.drop()
            self.rec.event("decoder_reopen_noise", lost_s=round(lost, 3), format=fmt)

    async def _aude(self, data: bytes) -> None:
        spdif, dac = (data[0], data[1]) if len(data) >= 2 else (1, 1)
        self.rec.cmd("aude", spdif=spdif, dac=dac)
        self.dac_on = bool(dac)
        if not spdif and self.output_on:
            self.output_on = False
            if self.knobs.get("mute_aude_stops_output"):
                await self.transport.pause()
                self._mark_paused(True)
        elif spdif and not self.output_on:
            self.output_on = True  # STOPPED: stays silent until strm u

    def _audg(self, data: bytes) -> None:
        g = P.parse_audg(data)
        self.volume = P.gain_to_volume(g.old_left, g.left)
        self.audg_seen = True
        if self.knobs.get("hardware_volume") or not g.adjust:
            self.gain = 1.0
        else:
            self.gain = g.left / 65536
        self.rec.cmd("audg", volume=self.volume, gain=round(self.gain, 5), adjust=g.adjust)

    def _setd(self, data: bytes) -> None:
        if not data:
            return
        sid = data[0]
        if sid == 0:
            if len(data) > 1:
                self.name = data[1:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
                self.rec.cmd("setd_name", name=self.name)
            else:
                self.rec.cmd("setd_query", id=0)
                self._send(P.setd_name(self.name, self.knobs.get("setd_name_nul")), "")
        elif sid == 0xFE:
            self.rec.cmd("setd_query", id=0xFE)
            width = int(self.identity.get("display_width", 0))
            if width:
                self._send(P.setd_display(width, int(self.identity.get("display_height", 0))), "")

    def _serv(self, data: bytes) -> None:
        ip = struct.unpack("!I", data[:4])[0] if len(data) >= 4 else 0
        self.rec.cmd("serv", ip=ip)
        if ip and self.server_addr:
            self.connect(str(ipaddress.IPv4Address(ip)), self.server_addr[1])

    # -- truth events --------------------------------------------------------------

    def _on_sink(self, name: str, data: dict[str, Any]) -> None:
        st = self.stream
        if st is None or self.sink is None or data.get("url", st.url) != st.url:
            return
        if self.sink.audible.url != st.url:
            return
        if name == "opened":
            ctype = data.get("content_type") or "application/octet-stream"
            st.content_type = ctype
            status = int(data.get("status", 200))
            st.resp = (
                f"HTTP/1.0 {status} OK\r\nContent-Type: {ctype}\r\nConnection: close\r\n\r\n"
            ).encode()
            if st.autostart >= 2 or st.autostart == 0:
                self._hold(st)
            is_flac = (
                "flac" in ctype or st.strm.format == "f" or st.url.split("?")[0].endswith(".flac")
            )
            if is_flac and self.knobs.fires("flac_sync_silent", url=st.url):
                st.silent = True
                self._hold(st)
            self.send_resp(st.resp)
        elif name == "first_audio":
            st.stms_at = self.clock.monotonic()
            self.send_stat("STMs")
            self._arm_stream_timers(st)
            if st.eof:
                self._stream_done(st)
        elif name in ("eof", "cap_hit"):
            if st.eof:
                return
            st.eof = True
            if st.stms_at is not None:  # else: the body is still held; sent with STMs
                self._stream_done(st)
        elif name == "underrun":
            self.send_stat("STMo")
        elif name == "error":
            self.send_stat("STMn")

    def _stream_done(self, st: Stream) -> None:
        """The stream was fully read: DSCO, then STMd (decoder ready for the next one)."""
        self._send(P.dsco(self.knobs.get("dsco_reason")), "")
        if not st.stmd_sent and not st.decode_error:
            st.stmd_sent = True
            self.send_stat("STMd")

    def _arm_stream_timers(self, st: Stream) -> None:
        k = self.knobs
        early = k.get("stmd_early_s")
        if early > 0:
            st.timers.append(self.clock.call_later(early, lambda: self._early_stmd(st)))
        if k.fires("stmn_decode_error", url=st.url):
            at = float(k.param("stmn_decode_error", "at_s", 2.0))
            st.timers.append(self.clock.call_later(at, lambda: self._decode_error(st)))
        if k.fires("stmo_underrun_at", url=st.url):
            at = float(k.param("stmo_underrun_at", "at_s", 3.0))
            stall = float(k.param("stmo_underrun_at", "stall_s", 2.0))
            st.timers.append(self.clock.call_later(at, lambda: self._spawn(self._stall(st, stall))))

    def _early_stmd(self, st: Stream) -> None:
        if st is self.stream and not st.stmd_sent:
            st.stmd_sent = True
            self.rec.event("early_stmd", url=st.url)
            self.send_stat("STMd")

    def _decode_error(self, st: Stream) -> None:
        if st is not self.stream or st.eof:
            return
        st.decode_error = True
        self.rec.event("decode_error", url=st.url)
        self.send_stat("STMn")
        self._cap_current()

    async def _stall(self, st: Stream, stall_s: float) -> None:
        if st is not self.stream or self.transport.state != TState.PLAYING:
            return
        self.rec.event("output_underrun", stall_s=stall_s)
        self.send_stat("STMo")
        await self.transport.pause()
        await self.clock.sleep(stall_s)
        if st is self.stream and self.transport.state == TState.PAUSED:
            await self.transport.play()

    def _cancel_timers(self) -> None:
        for st in (self.stream, self.next_stream):
            if st is not None:
                for t in st.timers:
                    t.cancel()
                st.timers.clear()

    def _on_transport(self, name: str, data: dict[str, Any]) -> None:
        if name == "track_started":
            self._uncap()
        elif name == "advanced":
            if self.next_stream is not None and self.next_stream.url == data.get("uri"):
                self._cancel_timers()
                self.stream, self.next_stream = self.next_stream, None
        elif name == "track_ended" and self.next_stream is None and self.stream is not None:
            self._cancel_timers()
            self.stream = None
            self.send_stat("STMu")

    # -- background ----------------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _tick_loop(self) -> None:
        while True:
            await self.clock.sleep(0.1)
            now = self.clock.monotonic()
            if self._pending and now - self._pending_since >= 0.2:
                self._flush()
            st = self.stream
            if not self.connected:
                continue
            if (
                st is not None
                and st.held
                and st.autostart == 0
                and not st.stml_sent
                and not st.silent
                and self.sink is not None
                and st.resp is not None
                and (
                    st.eof
                    or self.sink.buffer.full  # the stream buffer cannot take more
                    or self.sink.bytes_read
                    >= min(st.strm.threshold_kb, self.knobs.get("stream_buffer_kb")) * 1024
                )
            ):
                st.stml_sent = True
                self.send_stat("STMl")
                if self.knobs.get("early_start_before_unpause_at"):
                    self.rec.event("early_start")
                    self._release(st)
            every = self.knobs.get("stmt_interval_s")
            if (
                every > 0
                and self.transport.state == TState.PLAYING
                and now - self._last_stmt >= every
            ):
                self.send_stat("STMt")

    # -- discovery -----------------------------------------------------------------

    async def discover(
        self, host: str, port: int = P.SLIMPROTO_PORT, *, legacy: bool = False, timeout: float = 3.0
    ) -> tuple[dict[str, str], tuple[str, int]]:
        """Send a discovery datagram to ``host:port``; return the parsed reply and its source."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[bytes, tuple[str, int]]] = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                if not fut.done():
                    fut.set_result((data, addr))

        transport, _ = await loop.create_datagram_endpoint(
            _Proto, local_addr=("127.0.0.1" if host.startswith("127.") else "0.0.0.0", 0)
        )
        try:
            req = (
                P.discovery_request_legacy(self.device_id, self.revision, self.mac)
                if legacy
                else P.discovery_request_tlv()
            )
            self.rec.resp("tx:discovery", legacy=legacy, host=host, port=port)
            transport.sendto(req, (host, port))
            data, addr = await asyncio.wait_for(fut, timeout)
        finally:
            transport.close()
        reply = P.parse_discovery_reply(data)
        self.rec.event("discovered", reply=reply, addr=f"{addr[0]}:{addr[1]}")
        return reply, (addr[0], addr[1])
