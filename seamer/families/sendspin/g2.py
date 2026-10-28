"""G2 endpoint: the real aiosendspin ``SendspinClient`` wired to the simulated device.

The client does the hard parts exactly as ESPHome 2026.10.0b2+ and the Linux clients
do: ``client/init``, the Noise KKpsk2 handshake (server is the initiator), admission
rules for several servers, time sync, and the 13-byte audio header. The device only
supplies the clock (:class:`SkewedClock`), the formats and buffer figures, and reacts
to the listener callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiosendspin.client import client as _client_mod
from aiosendspin.client import connection as _conn_mod
from aiosendspin.client.client import SendspinClient
from aiosendspin.models.core import DeviceInfo, GroupUpdateServerPayload, StreamStartMessage
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, GoodbyeReason, PlayerCommand, Roles, UndefinedField
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.session import NoiseCipherSuite
from aiosendspin.noise.trust_store import InMemoryClientPairingStore

if TYPE_CHECKING:
    from .device import SendspinDevice

G2_HEADER_LEN = 13
ROLE_MAP = {"player": Roles.PLAYER, "controller": Roles.CONTROLLER, "metadata": Roles.METADATA}
COMMAND_MAP = {c.value: c for c in PlayerCommand}

# The fake sink never decodes, so let the client accept Opus streams as raw chunks
# (aiosendspin itself only decodes PCM/FLAC and refuses Opus at hello and stream/start).
_RAW_CODECS = (AudioCodec.PCM, AudioCodec.FLAC, AudioCodec.OPUS)
_client_mod.DECODABLE_CODECS = _RAW_CODECS  # type: ignore[attr-defined]
_conn_mod.DECODABLE_CODECS = _RAW_CODECS  # type: ignore[misc]


class G2Endpoint:
    """One persistent client per device (identity and pairing records survive restarts)."""

    def __init__(self, dev: SendspinDevice, identity: Identity) -> None:
        self.dev = dev
        self.identity = identity
        self.rec = dev.rec.child("g2")
        self.store = InMemoryClientPairingStore()
        self.client: SendspinClient | None = None
        self._ws: web.WebSocketResponse | None = None

    @property
    def client_id(self) -> str:
        return self.identity.peer_id

    async def setup(self) -> None:
        d = self.dev
        formats = [
            SupportedAudioFormat(
                codec=AudioCodec(f.codec),
                channels=f.channels,
                sample_rate=f.sample_rate,
                bit_depth=f.bit_depth,
            )
            for f in d.formats
        ]
        support = ClientHelloPlayerSupport(
            supported_formats=formats, buffer_capacity=int(d.knobs.get("buffer_capacity"))
        )
        roles = [ROLE_MAP[r] for r in d.roles if r in ROLE_MAP]
        mac = d.knobs.get("mac_address") or None
        client = SendspinClient(
            self.identity,
            d.name,
            roles,
            pairing_store=self.store,
            device_info=DeviceInfo(
                product_name=d.identity.get("product_name"),
                manufacturer=d.identity.get("manufacturer"),
                software_version=d.identity.get("software_version"),
                mac_address=mac,
            ),
            player_support=support if Roles.PLAYER in roles else None,
            output_delay_ms=d.output_delay_ms,
            required_lead_time_ms=float(d.knobs.get("required_lead_time_ms")),
            min_buffer_ms=float(d.knobs.get("min_buffer_ms")),
            initial_volume=d.volume,
            initial_muted=d.muted,
            state_supported_commands=[
                COMMAND_MAP[c] for c in d.supported_commands if c in COMMAND_MAP
            ],
            clock=d.dev_clock,
            cipher_suite=(
                NoiseCipherSuite.CHACHAPOLY
                if d.knobs.get("suite") == "chachapoly"
                else NoiseCipherSuite.AESGCM
            ),
        )
        await client.set_unpaired_access(enabled=bool(d.knobs.get("unpaired_access")))
        client.add_stream_start_listener(self._on_stream_start)
        client.add_stream_end_listener(self._on_stream_end)
        client.add_stream_clear_listener(self._on_stream_clear)
        client.add_audio_chunk_listener(self._on_chunk)
        client.add_server_command_listener(self._on_command)
        client.add_group_update_listener(self._on_group)
        client.add_metadata_listener(self._on_metadata)
        client.add_disconnect_listener(self._on_disconnect)
        self.client = client

    # -- session ---------------------------------------------------------------------

    async def serve(self, ws: web.WebSocketResponse) -> None:
        assert self.client is not None
        self._ws = ws
        watcher = asyncio.create_task(self._watch_admission(ws))
        try:
            await self.client.attach_websocket(ws)
        finally:
            watcher.cancel()
            if self._ws is ws:
                self._ws = None

    async def _watch_admission(self, ws: web.WebSocketResponse) -> None:
        """attach_websocket only returns at close; report the session once it is admitted."""
        assert self.client is not None
        never = self.dev.knobs.get("available_mode") == "never"
        while not ws.closed:
            conn = getattr(self.client, "_admitted_connection", None)
            if never and conn is not None and conn._reported_available:
                # before the time filter converges, so available:true never goes out (SS36)
                conn._reported_available = False
            info = self.client.server_info
            if self.client.connected and info is not None:
                self.dev.on_session_up(info.server_id, info.name)
                return
            await asyncio.sleep(0.02)

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    async def send_state(self) -> None:
        if self.client is None or not self.client.connected:
            return
        d = self.dev
        with contextlib.suppress(RuntimeError, ConnectionError):
            await self.client.send_player_state(
                available=d.knobs.get("available_mode") != "never",
                volume=d.volume,
                muted=d.muted,
            )
        self.rec.report("client/state", volume=d.volume, muted=d.muted)

    async def goodbye(self, reason: str) -> None:
        if self.client is None:
            return
        if reason == "none":
            self.abort()
            return
        await self.client.disconnect(GoodbyeReason(reason))

    def abort(self) -> None:
        ws = self._ws
        req = ws._req if ws is not None else None
        if req is not None and req.transport is not None:
            req.transport.abort()

    def play_time(self, server_ts: int) -> int:
        assert self.client is not None
        return self.client.compute_play_time(server_ts)

    # -- listeners -------------------------------------------------------------------

    def _on_stream_start(self, msg: StreamStartMessage) -> None:
        player = msg.payload.player
        if player is None:
            return
        self.dev.spawn(
            self.dev.on_stream_start(
                player.codec.value, player.sample_rate, player.channels, player.bit_depth
            )
        )

    def _on_stream_end(self, roles: list[str] | None) -> None:
        self.dev.spawn(self.dev.on_stream_end(roles))

    def _on_stream_clear(self, roles: list[str] | None) -> None:
        self.dev.on_stream_clear(roles)

    def _on_chunk(self, ts: int, data: bytes, _fmt: Any, _send_ahead: int) -> None:
        self.dev.note_server_activity()
        self.dev.on_audio(ts, data, G2_HEADER_LEN, self.play_time(ts))

    def _on_command(self, payload: Any) -> None:
        player = payload.player
        if player is None:
            return
        self.dev.spawn(
            self.dev.on_command(
                player.command.value, player.volume, player.mute, player.output_delay_ms
            )
        )

    def _on_group(self, payload: GroupUpdateServerPayload) -> None:
        state = payload.playback_state
        self.dev.on_group_update(state.value if state is not None else None)

    def _on_metadata(self, payload: Any) -> None:
        meta = getattr(payload, "metadata", None)
        if meta is None or isinstance(meta, UndefinedField):
            return
        title = getattr(meta, "title", None)
        progress = getattr(meta, "progress", None)
        pos = getattr(progress, "track_progress", None) if progress is not None else None
        self.dev.on_metadata(None if isinstance(title, UndefinedField) else title, pos)

    def _on_disconnect(self) -> None:
        self.dev.on_session_down()
