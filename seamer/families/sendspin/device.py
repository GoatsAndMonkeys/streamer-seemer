"""The Sendspin device: an ESPHome/Voice PE speaker or a Linux client that MA dials.

Direction (SS41): the device listens for a WebSocket at ``/sendspin`` (default port
8928) and the server connects to it, found by mDNS ``_sendspin._tcp`` or entered by hand
(MA's "manual discovery IPs"). Audio is pushed to the device; it never fetches anything.

Per connection the device speaks one wire generation (knob ``wire_gen``):

- **G2** (:mod:`.g2`): the real aiosendspin ``SendspinClient`` (Noise, 13-byte header).
- **G0** (:mod:`.legacy`): the from-scratch :class:`WirePuppet` (plain text, 9 bytes).

Both feed the same :class:`FakeSink` (byte budget, decode cost, DAC drift, underruns),
driven by the device's own :class:`SkewedClock`, and the same transport truth:
``stream/start`` = playing, ``stream/end`` = paused or stopped (from ``group/update``),
``stream/clear`` = seek/flush.

Sim actions (``POST /_sim/action/<name>``):

- ``goodbye`` {reason?}: send ``client/goodbye`` (or drop if ``none``) and close.
- ``restart`` {reason?, down_s?}: goodbye, go off the network for ``down_s`` (also
  withdrawing mDNS unless ``mdns_on_goodbye=keep``), then listen again on the same port
  with the same identity. Runs in the background; returns at once.
- ``drop``: close the TCP connection without a goodbye.
- ``set_volume`` {volume} / ``set_mute`` {muted}: a local change (hardware button),
  reported to the server.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from aiohttp import WSMsgType, web
from aiosendspin.noise.keys import Identity

from ...simkit.device import SimDevice
from ...simkit.kits.discovery import MdnsAdvertiser
from ...simkit.kits.http import HttpServer
from ...simkit.transport import ReportLens, TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)
from .clock import SkewedClock
from .g2 import G2Endpoint
from .legacy import WirePuppet
from .sink import FakeSink, StreamFormat

SERVICE_TYPE = "_sendspin._tcp.local."
WS_PATH = "/sendspin"
STREAM_URI = "sendspin://stream"


@dataclass(frozen=True)
class FormatSpec:
    codec: str
    sample_rate: int
    bit_depth: int
    channels: int


def parse_formats(spec: str) -> list[FormatSpec]:
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        codec, rate, bits, ch = item.split(":")
        out.append(FormatSpec(codec.lower(), int(rate), int(bits), int(ch)))
    return out


def _csv(spec: str) -> list[str]:
    return [s.strip() for s in spec.split(",") if s.strip()]


class SendspinDevice(SimDevice):
    family = "sendspin"
    lens_cls = ReportLens
    use_sink = False

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        k = self.knobs
        self.name = self.identity.get("name", self.id)
        self.formats = parse_formats(k.get("formats"))
        self.roles = _csv(k.get("roles"))
        self.supported_commands = _csv(k.get("supported_commands"))
        self.volume = int(k.get("initial_volume"))
        self.muted = False
        self.output_delay_ms = float(k.get("output_delay_ms"))
        self.dev_clock = SkewedClock(
            self.clock,
            skew_ppm=float(k.get("clock_skew_ppm")),
            offset_ms=float(k.get("clock_offset_ms")),
            jitter_us=int(k.get("clock_jitter_us")),
            rng=self.rng.stream("clock_jitter"),
        )
        self.fake_sink = FakeSink(
            self.dev_clock.now_us,
            k,
            self.rec.child("sink"),
            on_overrun_close=self._on_overrun_close,
            on_underrun=self._on_underrun,
        )
        self.identity_key = Identity.from_private_bytes(self.rng.stream("identity").randbytes(32))
        self.g2: G2Endpoint | None = None
        self.puppet: WirePuppet | None = None
        self.player_error = False
        self.blackholed = False
        self.server_id: str | None = None
        self.server_name: str | None = None
        self.group_state: str | None = None
        self.title: str | None = None
        self.sessions = 0
        self.goodbyes: list[str] = []
        self.restarts = 0
        self.ws_attempts = 0
        self.rejected: dict[str, int] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._tick_task: asyncio.Task[None] | None = None
        self._last_rx = self.clock.monotonic()
        self._last_filter_count = 0
        self._mdns: MdnsAdvertiser | None = None
        self._live_req: web.Request | None = None
        self._down = False

    # -- identity ----------------------------------------------------------------------

    @property
    def wire_gen(self) -> str:
        return str(self.knobs.get("wire_gen"))

    @property
    def client_id(self) -> str:
        if self.wire_gen == "G0" and self.knobs.get("client_id_mode") == "mac":
            return str(self.knobs.get("mac_address")).replace(":", "").lower()
        return self.identity_key.peer_id

    def device_info_dict(self) -> dict[str, Any]:
        info = {
            key: self.identity[key]
            for key in ("product_name", "manufacturer", "software_version")
            if key in self.identity
        }
        if self.knobs.get("mac_address"):
            info["mac_address"] = self.knobs.get("mac_address")
        return info

    # -- protocol surface --------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app()
        app.router.add_get(WS_PATH, self._ws_handler)
        app.router.add_get("/", self._root)
        return app

    async def _root(self, _req: web.Request) -> web.Response:
        return web.Response(status=404, text="sendspin device: WebSocket at /sendspin")

    async def _ws_handler(self, req: web.Request) -> web.StreamResponse:
        self.ws_attempts += 1
        if self.knobs.get("reject_origin_header") and req.headers.get("Origin"):
            self._reject("origin")
            raise web.HTTPForbidden(text="Origin not allowed")
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(req)
        self._live_req = req
        self._last_rx = self.clock.monotonic()
        fault = self.knobs.get("fault_mode")
        self.rec.event("ws_open", remote=req.remote, wire=self.wire_gen, fault=fault)
        if fault == "accept_and_hang":
            # upgraded, then silence: never send client/init or client/hello (SS28)
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
            self.rec.event("ws_closed", hung=True)
            return ws
        if fault == "blackhole":
            self.clock.call_later(float(self.knobs.get("blackhole_after_s")), self._blackhole)
        try:
            if self.wire_gen == "G0":
                puppet = WirePuppet(self, ws)
                self.puppet = puppet
                try:
                    await puppet.run()
                finally:
                    if self.puppet is puppet:
                        self.puppet = None
                    self.on_session_down()
            else:
                assert self.g2 is not None
                await self.g2.serve(ws)
        finally:
            if self._live_req is req:
                self._live_req = None
            self.rec.event("ws_closed", close_code=ws.close_code)
        return ws

    def _reject(self, why: str) -> None:
        self.rejected[why] = self.rejected.get(why, 0) + 1
        self.rec.event("ws_rejected", reason=why)

    def _blackhole(self) -> None:
        """Stop reading and answering, keeping the socket open (SS29)."""
        req = self._live_req
        if req is None or req.transport is None:
            return
        self.blackholed = True
        transport = req.transport
        transport.pause_reading()
        with contextlib.suppress(AttributeError, TypeError):
            transport.write = lambda _data: None  # type: ignore[method-assign]
        self.rec.event("blackhole", after_s=self.knobs.get("blackhole_after_s"))

    async def hold_blackhole(self, ws: web.WebSocketResponse) -> None:
        while not ws.closed:
            await asyncio.sleep(0.5)

    # -- callbacks from the wire endpoints ---------------------------------------------

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def note_server_activity(self) -> None:
        self._last_rx = self.clock.monotonic()

    def on_session_up(self, server_id: str | None, name: str | None) -> None:
        self.sessions += 1
        self.server_id = server_id
        self.server_name = name
        self.player_error = False
        self.rec.event("session_up", server_id=server_id, server_name=name, wire=self.wire_gen)

    def on_session_down(self) -> None:
        if self.fake_sink.active:
            self.fake_sink.end()
        self.rec.event("session_down", server_id=self.server_id)
        self.server_id = None
        if self.transport.state in (TState.PLAYING, TState.BUFFERING, TState.LOADING):
            self.spawn(self.transport.stop())

    async def on_stream_start(self, codec: str, rate: int, channels: int, bits: int) -> None:
        fmt = StreamFormat(codec, rate, channels, bits)
        self.rec.event("stream/start", **fmt.__dict__)
        self.fake_sink.start(fmt)
        tm = self.transport
        if tm.state not in (TState.PLAYING, TState.BUFFERING, TState.LOADING):
            if tm.state is not TState.PAUSED or tm.current is None:
                await tm.set_uri(STREAM_URI, title=self.title)
            await tm.play()

    async def on_stream_end(self, roles: list[str] | None) -> None:
        if roles is not None and "player" not in roles:
            return
        self.rec.event("stream/end", roles=roles, group_state=self.group_state)
        self.fake_sink.end()
        if self.group_state == "stopped":
            await self.transport.stop()
        else:
            await self.transport.pause()

    def on_stream_clear(self, roles: list[str] | None) -> None:
        if roles is not None and "player" not in roles:
            return
        self.rec.event("stream/clear", roles=roles)
        self.fake_sink.clear()

    def on_group_update(self, playback_state: str | None) -> None:
        if playback_state is None or playback_state == self.group_state:
            return
        self.group_state = playback_state
        self.rec.event("group/update", playback_state=playback_state)
        if playback_state == "stopped" and self.transport.state is TState.PAUSED:
            self.spawn(self.transport.stop())

    def on_metadata(self, title: str | None, progress_ms: int | None) -> None:
        if title:
            self.title = title
            cur = self.transport.current
            if cur is not None:
                cur.title = title
        if progress_ms is None or self.transport.current is None:
            return
        pos = progress_ms / 1000
        truth = self.transport.truth_position()
        if abs(truth - pos) > 1.5:
            self.rec.event("metadata_seek", truth=round(truth, 3), progress=pos)
            self.spawn(self.transport.seek(pos))

    async def on_command(
        self, command: str | None, volume: int | None, mute: bool | None, delay_ms: Any
    ) -> None:
        self.rec.cmd("server/command", command=command, volume=volume, mute=mute, delay=delay_ms)
        if command == "volume" and volume is not None:
            self.volume = max(0, min(100, int(volume)))
            if self.knobs.get("volume_unmutes"):
                self.muted = False
        elif command == "mute" and mute is not None:
            self.muted = bool(mute)
        elif command in ("set_output_delay", "set_static_delay") and delay_ms is not None:
            self.output_delay_ms = float(delay_ms)
        else:
            self.rec.log("command_ignored", command=command)
            return
        self.rec.state("player", volume=self.volume, muted=self.muted, delay=self.output_delay_ms)
        await self._echo_state()

    async def _echo_state(self) -> None:
        mode = self.knobs.get("state_echo")
        if mode == "missing":
            self.rec.knob("state_echo", mode="missing")
            return
        if mode == "delayed":
            await self.clock.sleep(float(self.knobs.get("state_echo_delay_ms")) / 1000)
        await self.send_state()

    async def send_state(self) -> None:
        if self.puppet is not None:
            await self.puppet.send_state()
        elif self.g2 is not None:
            await self.g2.send_state()

    def on_audio(self, server_ts: int, payload: bytes, header_len: int, play_at: int) -> None:
        self.fake_sink.on_chunk(server_ts, payload, header_len, play_at)

    def _on_overrun_close(self) -> None:
        self.rec.knob("ring_overrun_policy", action="close_connection")
        self.spawn(self.goodbye("restart"))

    def _on_underrun(self) -> None:
        if self.wire_gen == "G0" and self.knobs.get("legacy_state_field"):
            self.player_error = True
            self.spawn(self.send_state())

    # -- connection control ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        if self.puppet is not None:
            return not self.puppet.ws.closed and self.puppet.server_info is not None
        return self.g2 is not None and self.g2.connected

    async def goodbye(self, reason: str | None = None) -> None:
        reason = reason or str(self.knobs.get("goodbye_reason"))
        self.goodbyes.append(reason)
        self.rec.event("client/goodbye", reason=reason)
        if self.puppet is not None:
            if reason == "none":
                self.puppet.abort()
            else:
                await self.puppet.goodbye(reason)
        elif self.g2 is not None:
            await self.g2.goodbye(reason)

    def drop(self) -> None:
        self.rec.event("drop")
        if self.puppet is not None:
            self.puppet.abort()
        elif self.g2 is not None:
            self.g2.abort()

    async def restart(self, reason: str | None = None, down_s: float | None = None) -> None:
        """Goodbye, go dark, come back on the same port with the same identity."""
        down = float(self.knobs.get("restart_down_s") if down_s is None else down_s)
        self.restarts += 1
        if self.connected:
            await self.goodbye(reason)
        self._down = True
        port = self.port
        self.rec.event("restart_down", down_s=down)
        if self._mdns is not None and self.knobs.get("mdns_on_goodbye") == "withdraw":
            await self._mdns.stop()
        if self.server is not None:
            await self.server.stop()
        await self.clock.sleep(down)
        app = self.build_app()
        app.add_routes(self._sim_routes())
        self.server = HttpServer(app, self.ctx.host, port, self.ctx.ssl_context)
        await self.server.start()
        if self._mdns is not None:
            if self.knobs.get("mdns_on_goodbye") == "withdraw":
                await self._mdns.start()
            self.rec.event("mdns", action="announce")
        self._down = False
        self.rec.event("restart_up", port=port)

    # -- lifecycle ---------------------------------------------------------------------

    async def on_start(self) -> None:
        if self.wire_gen == "G2":
            self.g2 = G2Endpoint(self, self.identity_key)
            await self.g2.setup()
        elif self.knobs.get("client_id_mode") == "key":
            self.rec.log("g0_key_id", note="G0 with client_id_mode=key uses the key ID")
        self._tick_task = asyncio.create_task(self._tick_loop())
        if self.knobs.get("advertise_mdns"):
            if MdnsAdvertiser.available():
                self._mdns = MdnsAdvertiser(
                    SERVICE_TYPE,
                    self.name,
                    self.port,
                    [self.host],
                    {"path": WS_PATH, "name": self.name},
                )
                await self._mdns.start()
                self.rec.event("mdns", action="register", port=self.port)
            else:
                self.rec.log("mdns_unavailable")

    async def on_stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
        if self.connected:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.goodbye("shutdown"), 2)
        for task in list(self._tasks):
            task.cancel()
        if self._mdns is not None:
            with contextlib.suppress(Exception):
                await self._mdns.stop()

    async def _tick_loop(self) -> None:
        while True:
            await self.clock.sleep(0.02)
            self.fake_sink.tick()
            self._check_liveness()

    def _check_liveness(self) -> None:
        if self.g2 is not None and self.g2.client is not None:
            conn = getattr(self.g2.client, "_admitted_connection", None)
            filt = getattr(conn, "_time_filter", None)
            count = getattr(filt, "count", 0) if filt is not None else 0
            if count != self._last_filter_count:
                self._last_filter_count = count
                self.note_server_activity()
        limit = float(self.knobs.get("liveness_timeout_s"))
        if limit <= 0 or not self.connected:
            return
        if self.clock.monotonic() - self._last_rx > limit:
            self.rec.knob("liveness_timeout_s", silent_s=limit)
            self._last_rx = self.clock.monotonic()
            self.spawn(self.goodbye("restart"))

    # -- /_sim -------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "wire_gen": self.wire_gen,
            "client_id": self.client_id,
            "connected": self.connected,
            "server_id": self.server_id,
            "server_name": self.server_name,
            "volume": self.volume,
            "muted": self.muted,
            "output_delay_ms": self.output_delay_ms,
            "group_state": self.group_state,
            "sessions": self.sessions,
            "ws_attempts": self.ws_attempts,
            "goodbyes": list(self.goodbyes),
            "restarts": self.restarts,
            "down": self._down,
            "rejected": dict(self.rejected),
            "sink": self.fake_sink.snapshot(),
            "ws_url": f"ws://{self.host}:{self.port}{WS_PATH}" if self.server else None,
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "goodbye":
            await self.goodbye(payload.get("reason"))
            return self.goodbyes[-1]
        if name == "restart":
            self.spawn(self.restart(payload.get("reason"), payload.get("down_s")))
            return "restarting"
        if name == "drop":
            self.drop()
            return "dropped"
        if name == "set_volume":
            self.volume = max(0, min(100, int(payload["volume"])))
            await self.send_state()
            return self.volume
        if name == "set_mute":
            self.muted = bool(payload["muted"])
            await self.send_state()
            return self.muted
        raise KeyError(name)
