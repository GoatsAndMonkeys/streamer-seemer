"""WirePuppet: a from-scratch G0 (pre-Noise) Sendspin client for one WebSocket.

G0 is what ESPHome 2026.5 to 2026.10.0b1 and shipped Voice PE 26.9.x speak (SS18, SS53):

- unencrypted text frames from the start: ``client/hello`` carrying ``client_id``
  (lowercase MAC without colons, SS1), ``version: 1`` and ``player@v1_support``;
- the server replies ``server/hello`` (no ``server/activate``);
- ``client/state`` with the top-level ``state: "synchronized"`` and ``static_delay_ms``;
- ``client/time`` / ``server/time`` text messages for clock sync;
- binary audio with the 9-byte header ``[4][int64 BE server timestamp]``.

aiosendspin's own client can no longer speak this, so the puppet does it by hand. It
also gives the device hooks for faults the real client would never commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from aiosendspin.client.time_sync import SendspinTimeFilter

if TYPE_CHECKING:
    from .device import SendspinDevice

AUDIO_CHUNK = 4
G0_HEADER_LEN = 9


class WirePuppet:
    def __init__(self, dev: SendspinDevice, ws: web.WebSocketResponse) -> None:
        self.dev = dev
        self.ws = ws
        self.rec = dev.rec.child("g0")
        self.filter = SendspinTimeFilter()
        self.server_info: dict[str, Any] | None = None
        self._time_task: asyncio.Task[None] | None = None
        self._closing = False
        self.time_replies = 0

    # -- outbound --------------------------------------------------------------------

    async def send(self, type_: str, payload: dict[str, Any]) -> None:
        if self.ws.closed:
            return
        await self.ws.send_str(json.dumps({"type": type_, "payload": payload}))

    def hello_payload(self) -> dict[str, Any]:
        d = self.dev
        player_support = {
            "supported_formats": [
                {
                    "codec": f.codec,
                    "channels": f.channels,
                    "sample_rate": f.sample_rate,
                    "bit_depth": f.bit_depth,
                }
                for f in d.formats
            ],
            "buffer_capacity": int(d.knobs.get("buffer_capacity")),
            "supported_commands": d.supported_commands,
        }
        payload: dict[str, Any] = {
            "client_id": d.client_id,
            "name": d.name,
            "version": 1,
            "supported_roles": [f"{r}@v1" for r in d.roles],
            "device_info": d.device_info_dict(),
        }
        if "player" in d.roles:
            payload["player@v1_support"] = player_support
        return payload

    async def send_state(self) -> None:
        d = self.dev
        payload: dict[str, Any] = {}
        if d.knobs.get("legacy_state_field"):
            payload["state"] = "error" if d.player_error else "synchronized"
        if "player" in d.roles:
            payload["player"] = {
                "volume": d.volume,
                "muted": d.muted,
                "static_delay_ms": round(d.output_delay_ms),
            }
        await self.send("client/state", payload)
        self.rec.report("client/state", **payload)

    async def goodbye(self, reason: str) -> None:
        self._closing = True
        await self.send("client/goodbye", {"reason": reason})
        await self.ws.close()

    # -- clock -----------------------------------------------------------------------

    def play_time(self, server_ts: int) -> int:
        if self.filter.is_synchronized:
            client_ts = self.filter.compute_client_time(server_ts)
        else:
            client_ts = self.dev.dev_clock.now_us() + 500_000
        return client_ts - round(self.dev.output_delay_ms * 1000)

    async def _time_loop(self) -> None:
        with contextlib.suppress(ConnectionError, RuntimeError):
            while not self.ws.closed:
                await self.send("client/time", {"client_transmitted": self.dev.dev_clock.now_us()})
                interval = 0.2 if not self.filter.is_synchronized else 2.0
                await asyncio.sleep(interval)

    def _on_server_time(self, p: dict[str, Any]) -> None:
        received = self.dev.dev_clock.now_us()
        offset = (
            (p["server_received"] - p["client_transmitted"]) + (p["server_transmitted"] - received)
        ) / 2
        delay = (
            (received - p["client_transmitted"]) - (p["server_transmitted"] - p["server_received"])
        ) / 2
        self.filter.update(round(offset), round(delay), received)

    # -- the session -----------------------------------------------------------------

    async def run(self) -> None:
        """Hello, state, then the message loop until the socket closes."""
        await self.send("client/hello", self.hello_payload())
        self.rec.report("client/hello", client_id=self.dev.client_id)
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    self._on_binary(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
                if self.dev.blackholed:
                    await self.dev.hold_blackhole(self.ws)
                    break
        finally:
            if self._time_task is not None:
                self._time_task.cancel()

    async def _on_text(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            self.rec.log("bad_json", data=data[:200])
            return
        type_ = msg.get("type")
        p = msg.get("payload") or {}
        self.dev.note_server_activity()
        if type_ == "server/hello":
            self.server_info = p
            self.rec.event("server/hello", server=p)
            self.dev.on_session_up(p.get("server_id"), p.get("name"))
            await self.send_state()
            self._time_task = asyncio.create_task(self._time_loop())
        elif type_ == "server/time":
            self.time_replies += 1
            self._on_server_time(p)
        elif type_ == "stream/start":
            player = p.get("player")
            if player:
                await self.dev.on_stream_start(
                    player["codec"],
                    player["sample_rate"],
                    player["channels"],
                    player["bit_depth"],
                )
        elif type_ == "stream/end":
            await self.dev.on_stream_end(p.get("roles"))
        elif type_ == "stream/clear":
            self.dev.on_stream_clear(p.get("roles"))
        elif type_ == "server/command":
            player = p.get("player")
            if player:
                await self.dev.on_command(
                    player.get("command"),
                    player.get("volume"),
                    player.get("mute"),
                    player.get("static_delay_ms", player.get("output_delay_ms")),
                )
        elif type_ == "group/update":
            self.dev.on_group_update(p.get("playback_state"))
        elif type_ == "server/state":
            meta = p.get("metadata") or {}
            progress = meta.get("progress") or {}
            self.dev.on_metadata(meta.get("title"), progress.get("track_progress"))
        else:
            self.rec.log("unhandled", type=type_)

    def _on_binary(self, data: bytes) -> None:
        self.dev.note_server_activity()
        if len(data) < G0_HEADER_LEN or data[0] != AUDIO_CHUNK:
            self.rec.log("binary_ignored", kind=data[0] if data else None, size=len(data))
            return
        (server_ts,) = struct.unpack(">q", data[1:G0_HEADER_LEN])
        payload = data[G0_HEADER_LEN:]
        self.dev.on_audio(server_ts, payload, G0_HEADER_LEN, self.play_time(server_ts))

    def abort(self) -> None:
        self._closing = True
        transport = self.ws._req.transport if self.ws._req is not None else None
        if transport is not None:
            transport.abort()
