"""The Chromecast device: a CASTV2 receiver that really fetches the media it is given.

Surfaces:

- **CASTV2** (TLS, length-prefixed protobuf; :mod:`.castv2`) on ``cast_port``:
  ``tp.connection``, ``tp.heartbeat``, ``receiver`` (GET_STATUS, LAUNCH, STOP,
  SET_VOLUME, GET_APP_AVAILABILITY), ``media`` on the app's ``transportId`` (LOAD,
  QUEUE_LOAD, PLAY, PAUSE, STOP, SEEK, QUEUE_INSERT/UPDATE/NEXT/PREV/REMOVE,
  GET_STATUS, SET_VOLUME), ``multizone`` (GET_STATUS, GET_CASTING_GROUPS) and the MA
  custom namespace.
- **HTTP** (the main server, 8008 on a real device): ``/setup/eureka_info`` and
  ``/setup/icon.png``; the same on HTTPS (8443) when started.
- **mDNS** ``_googlecast._tcp`` with the TXT records pychromecast parses.

The media session's queue is mirrored onto the TransportMachine (``current`` = the
current item, ``next_slot`` = the item after it), so EOF, byte caps and errors drive the
real advance logic. Wire shapes follow cast_airplay2.md section 2a.

Device options (``build_device(..., **options)``): ``cast_port`` (default 8009 when the
main port is 8008, else any free port), ``eureka_https_port`` (8443 / free port;
``None`` disables it), ``mdns`` (advertise over zeroconf).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import ipaddress
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from ...simkit.device import SimDevice
from ...simkit.kits.discovery import MdnsAdvertiser
from ...simkit.transport import MediaSlot, ReportLens, TState
from . import eureka
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)
from . import protocol as p
from .castv2 import CastConnection, CastServer

# Cast receiver DetailedErrorCode values used here
ERR_DECODE = 102  # MEDIA_DECODE (C30)
ERR_NETWORK = 103  # MEDIA_NETWORK
ERR_SRC_NOT_SUPPORTED = 104  # MEDIA_SRC_NOT_SUPPORTED (LOAD_FAILED, research 2a)

IDLE_APP_ID = "E8C28D3C"  # Backdrop: the idle screen of video devices

KNOWN_APPS: dict[str, tuple[str, list[str]]] = {
    p.APP_MEDIA_RECEIVER: (
        "Default Media Receiver",
        [p.NS_MEDIA, p.NS_CAC, p.NS_DEBUGOVERLAY],
    ),
    p.APP_MA: ("Music Assistant", [p.NS_MEDIA, p.NS_CAC, p.NS_DEBUGOVERLAY, p.NS_MA]),
    p.APP_SENDSPIN: ("Sendspin", [p.NS_SENDSPIN, p.NS_DEBUGOVERLAY]),
    "84912283": ("DashCast", [p.NS_MEDIA, "urn:x-cast:es.offd.dashcast"]),
}

DEFAULT_APP_ERROR_ON = ("GET_STATUS", "QUEUE_UPDATE", "SEEK")


@dataclass
class CastApp:
    app_id: str
    display_name: str
    session_id: str
    transport_id: str
    namespaces: list[str]
    foreign: bool = False  # launched by another sender (C40)
    idle_screen: bool = False

    @property
    def status_text(self) -> str:
        return self.display_name

    def to_json(self) -> dict[str, Any]:
        return {
            "appId": self.app_id,
            "displayName": self.display_name,
            "appType": "WEB",
            "isIdleScreen": self.idle_screen,
            "launchedFromCloud": False,
            "universalAppId": self.app_id,
            "namespaces": [{"name": n} for n in self.namespaces],
            "sessionId": self.session_id,
            "statusText": self.status_text,
            "transportId": self.transport_id,
        }


@dataclass
class MediaSession:
    id: int
    items: list[dict[str, Any]] = field(default_factory=list)
    current_item_id: int | None = None
    idle_reason: str | None = None
    forced_state: str | None = None  # BUFFERING forever, IDLE during a C30 error
    extended: dict[str, Any] | None = None
    starting: bool = False  # play requested, transport not yet LOADING
    load_request_id: int | None = None  # requestId of the LOAD until audio starts
    foreign: bool = False
    stream_level: float = 1.0
    stream_muted: bool = False
    media_version: int = 0
    media_sent_version: int = -1

    def item(self, item_id: int | None) -> dict[str, Any] | None:
        return next((i for i in self.items if i["itemId"] == item_id), None)

    def index(self, item_id: int | None) -> int:
        return next((n for n, i in enumerate(self.items) if i["itemId"] == item_id), -1)

    def neighbour(self, step: int) -> dict[str, Any] | None:
        idx = self.index(self.current_item_id)
        if idx < 0 or not 0 <= idx + step < len(self.items):
            return None
        return self.items[idx + step]


class CastLens(ReportLens):
    """currentTime as the device reports it (C3: occasional jumps)."""

    def current_time(self) -> float:
        pos = self.position() or 0.0
        if self.knobs.fires("current_time_jump", truth=round(pos, 3)):
            pos = max(0.0, pos + float(self.knobs.param("current_time_jump", "by_s", -5.0)))
        return round(pos, 3)


def _private_name(host: str) -> bool:
    """A name Google DNS cannot resolve (C13); IP literals are fine."""
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    host = host.lower().rstrip(".")
    return "." not in host or host.endswith((".local", ".lan", ".home", ".internal"))


def _mime(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


class ChromecastDevice(SimDevice):
    family = "chromecast"
    lens_cls = CastLens
    lens: CastLens

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        ident = self.identity
        self.friendly_name: str = ident.get("name", self.id)
        self.model_name: str = ident.get("model", "Google Nest Mini")
        self.cast_type: str = ident.get("cast_type", "audio")
        raw = str(ident.get("uuid", "")).replace("-", "").lower()
        self.uuid_hex = raw if len(raw) == 32 else hashlib.md5(self.id.encode()).hexdigest()
        self.volume_level = float(ident.get("volume", 0.3))
        self.muted = False
        self._applied_level = self.volume_level  # what the amp really does (C8)
        self.app: CastApp | None = None
        self.media: MediaSession | None = None
        self._session_ids = 0
        self._transport_ids = 4
        self._item_ids = 0
        self._loads = 0
        self._launch_failed_once = False
        self._last_sig: Any = None
        self._bg: set[asyncio.Future[Any]] = set()
        self._timers: dict[str, Any] = {}
        self._started_at = 0.0
        self._https: Any = None
        self._mdns: MdnsAdvertiser | None = None
        self._tls_fail_server: asyncio.base_events.Server | None = None
        self._range_session: aiohttp.ClientSession | None = None
        self._cap_knob: str | None = None
        self.cast = CastServer(
            self._handle, on_open=self._on_open, on_close=self._on_close, on_send=self._on_tx
        )
        self.cast.accept_filter = lambda: not self.knobs.get("offline")
        self.transport.add_listener(self._on_tm)
        assert self.sink is not None
        self.sink.add_listener(self._on_sink)
        self.sink.cap_policy = self._cap_policy

    def ident_list(self, key: str) -> list[dict[str, Any]]:
        """A list-valued identity key (the profile loader stores identity values as str)."""
        raw = self.identity.get(key) or []
        if isinstance(raw, str):
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return []
        return [dict(x) for x in raw if isinstance(x, Mapping)]

    # -- lifecycle -------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app()
        eureka.add_routes(app, self)
        return app

    async def on_start(self) -> None:
        self._started_at = self.clock.monotonic()
        opts = self.ctx.options
        real_ports = self.ctx.port == 8008
        await self.cast.start(self.ctx.host, int(opts.get("cast_port", 8009 if real_ports else 0)))
        self.rec.log("cast_listening", host=self.cast.host, port=self.cast.port)
        https_port = opts.get("eureka_https_port", 8443 if real_ports else 0)
        if https_port is not None:
            await self._start_https(int(https_port))
        if opts.get("mdns"):
            await self._start_mdns()
        mean = float(self.knobs.param("spontaneous_error", "mean_s", 0.0) or 0.0)
        if mean > 0:
            self._spawn(self._random_loop("spontaneous_error", mean, self._spontaneous_error))
        pause_mean = self.knobs.get("spontaneous_pause_mean_s")
        if pause_mean > 0:
            self._spawn(self._random_loop("spontaneous_pause", pause_mean, self._device_pause))
        if self.knobs.get("device_ping_s") > 0:
            self._spawn(self._device_ping_loop())
        if self.knobs.get("leader_moves_after_s") > 0:
            self._later("leader_move", self.knobs.get("leader_moves_after_s"), self._leader_move)

    async def on_stop(self) -> None:
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        for task in list(self._bg):
            task.cancel()
        for task in list(self._bg):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._mdns is not None:
            with contextlib.suppress(Exception):
                await self._mdns.stop()
        if self._tls_fail_server is not None:
            self._tls_fail_server.close()
        if self._range_session is not None:
            await self._range_session.close()
        await self.cast.stop()

    async def _start_https(self, port: int) -> None:
        mode = self.knobs.get("eureka_info")
        if mode == "refused":
            return
        if mode == "tls_fail":  # C37: the TLS handshake fails on 8443

            async def _reject(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
                w.write(b"\x15\x03\x03\x00\x02\x02\x28")  # TLS alert: handshake_failure
                with contextlib.suppress(Exception):
                    await w.drain()
                await p.close_writer(w)

            self._tls_fail_server = await asyncio.start_server(_reject, self.ctx.host, port)
            self.eureka_https_port = self._tls_fail_server.sockets[0].getsockname()[1]
            return
        app = web.Application()
        eureka.add_routes(app, self)
        self._https = await self.serve(app, port, ssl_context=p.server_ssl_context())
        self.eureka_https_port = self._https.port

    async def _start_mdns(self) -> None:
        if not MdnsAdvertiser.available():
            self.rec.log("mdns_unavailable")
            return
        self._mdns = MdnsAdvertiser(
            "_googlecast._tcp.local.",
            eureka.mdns_instance_name(self),
            self.cast.port,
            [self.cast.host],
            eureka.txt_records(self),
        )
        try:
            await self._mdns.start()
        except Exception as err:  # zeroconf refuses some interfaces (loopback)
            self.rec.log("mdns_failed", error=repr(err))
            self._mdns = None

    # -- small helpers -----------------------------------------------------------------

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def _later(self, name: str, delay_s: float, fn: Any, *args: Any) -> None:
        old = self._timers.pop(name, None)
        if old is not None:
            old.cancel()

        def _fire() -> None:
            self._timers.pop(name, None)
            self._spawn(fn(*args))

        self._timers[name] = self.clock.call_later(delay_s, _fire)

    def _cancel(self, name: str) -> None:
        t = self._timers.pop(name, None)
        if t is not None:
            t.cancel()

    def _dict_fired(self, name: str, **context: Any) -> None:
        d = self.knobs.defs[name]
        self.rec.knob("fired", knob=name, quirks=list(d.quirks), confidence=d.confidence, **context)

    async def _random_loop(self, name: str, mean_s: float, action: Any) -> None:
        rnd = self.rng.stream(name)
        while True:
            await self.clock.sleep(rnd.expovariate(1.0 / mean_s))
            await action()

    # -- frame I/O -----------------------------------------------------------------------

    def _on_open(self, conn: CastConnection) -> None:
        self.rec.event("cast_connected", conn=conn.id, peer=conn.peer)

    def _on_close(self, conn: CastConnection, reason: str) -> None:
        self.rec.event("cast_disconnected", conn=conn.id, reason=reason)

    def _on_tx(self, conn: CastConnection, msg: p.CastMessage) -> None:
        if msg.namespace == p.NS_HEARTBEAT:
            return
        data = msg.json()
        self.rec.resp(
            f"cast:{data.get('type', '?')}",
            conn=conn.id,
            ns=msg.namespace.rsplit(".", 1)[-1],
            sender=msg.source_id,
            dst=msg.destination_id,
            payload=data,
        )

    async def _reply(
        self, conn: CastConnection, msg: p.CastMessage, payload: dict[str, Any]
    ) -> None:
        await conn.send_json(msg.destination_id, msg.source_id, msg.namespace, payload)

    async def _broadcast(
        self, source: str, ns: str, payload: dict[str, Any], exclude: CastConnection | None = None
    ) -> None:
        for conn in list(self.cast.connections):
            if conn is not exclude and conn.has_channel_to(source):
                await conn.send_json(source, p.BROADCAST_ID, ns, payload)

    async def _handle(self, conn: CastConnection, msg: p.CastMessage) -> None:
        if msg.payload_utf8 is None:  # deviceauth (binary): pychromecast never sends it
            self.rec.cmd("cast:binary", ns=msg.namespace, conn=conn.id)
            return
        data = msg.json()
        mtype = str(data.get("type", ""))
        ns = msg.namespace
        if ns == p.NS_HEARTBEAT:
            await self._heartbeat(conn, msg, mtype)
            return
        self.rec.cmd(
            f"cast:{mtype}",
            conn=conn.id,
            ns=ns.rsplit(".", 1)[-1],
            sender=msg.source_id,
            dst=msg.destination_id,
            payload=data,
        )
        if ns == p.NS_CONNECTION:
            if mtype == "CONNECT":
                conn.channels.add((msg.source_id, msg.destination_id))
            elif mtype == "CLOSE":
                conn.channels.discard((msg.source_id, msg.destination_id))
            return
        delay = self.knobs.get("latency_s")
        if delay > 0:
            jitter = self.knobs.get("jitter_s")
            if jitter:
                delay += self.rng.uniform("cast_jitter", 0, jitter)
            await self.clock.sleep(delay)
        if ns == p.NS_RECEIVER and msg.destination_id == p.PLATFORM_ID:
            await self._receiver(conn, msg, mtype, data)
        elif ns == p.NS_MULTIZONE:
            await self._multizone(conn, msg, mtype, data)
        elif self.app is not None and msg.destination_id == self.app.transport_id:
            if ns not in self.app.namespaces:
                supported = ", ".join(sorted(n for n in self.app.namespaces))
                self.rec.log(
                    "namespace_refused",
                    ns=ns,
                    text=f"Namespace {ns} is not supported by current app. "
                    f"Supported are {supported}",
                )
            elif ns == p.NS_MEDIA:
                await self._media_cmd(conn, msg, mtype, data)
            # MA / sendspin custom namespaces: recorded above, no reply needed

    async def _heartbeat(self, conn: CastConnection, msg: p.CastMessage, mtype: str) -> None:
        if mtype != "PING":
            return
        stop_after = self.knobs.get("heartbeat_stop_after_s")
        if stop_after and self.clock.monotonic() - self._started_at >= stop_after:
            if not getattr(self, "_hb_stop_logged", False):
                self._hb_stop_logged = True
                self._dict_fired("heartbeat_stop_after_s")
            return
        await self._reply(conn, msg, {"type": "PONG"})

    async def _device_ping_loop(self) -> None:
        while True:
            await self.clock.sleep(self.knobs.get("device_ping_s"))
            for conn in list(self.cast.connections):
                for sender in conn.senders_for(p.PLATFORM_ID):
                    await conn.send_json(p.PLATFORM_ID, sender, p.NS_HEARTBEAT, {"type": "PING"})

    # -- receiver namespace -------------------------------------------------------------

    def receiver_status(self) -> dict[str, Any]:
        level = 1.0 if self.knobs.get("control_type") == "fixed" else self.volume_level
        status: dict[str, Any] = {
            "volume": {
                "controlType": self.knobs.get("control_type"),
                "level": round(level, 4),
                "muted": self.muted,
                "stepInterval": self.knobs.get("step_interval"),
            }
        }
        if self.app is not None:
            status["applications"] = [self.app.to_json()]
        elif self.cast_type == "cast":
            backdrop = CastApp(IDLE_APP_ID, "Backdrop", self._backdrop_session, "", [])
            backdrop.idle_screen = True
            status["applications"] = [backdrop.to_json()]
        if self.cast_type == "cast":  # audio devices and groups leave these out
            status["isActiveInput"] = True
            status["isStandBy"] = False
        return status

    @property
    def _backdrop_session(self) -> str:
        return str(uuid.UUID(hashlib.md5(f"{self.id}:backdrop".encode()).hexdigest()))

    async def _send_receiver_status(
        self, conn: CastConnection | None = None, msg: p.CastMessage | None = None, rid: int = 0
    ) -> None:
        body = {"type": "RECEIVER_STATUS", "status": self.receiver_status()}
        if conn is not None and msg is not None:
            await self._reply(conn, msg, {**body, "requestId": rid})
        await self._broadcast(p.PLATFORM_ID, p.NS_RECEIVER, {**body, "requestId": 0}, conn)

    async def _receiver(
        self, conn: CastConnection, msg: p.CastMessage, mtype: str, data: dict[str, Any]
    ) -> None:
        rid = int(data.get("requestId", 0) or 0)
        if mtype == "GET_STATUS":
            await self._reply(
                conn,
                msg,
                {"type": "RECEIVER_STATUS", "requestId": rid, "status": self.receiver_status()},
            )
        elif mtype == "LAUNCH":
            await self._launch(conn, msg, rid, str(data.get("appId", "")))
        elif mtype == "STOP":
            delay = self.knobs.get("quit_ack_delay_s")
            if delay > 0:
                self._dict_fired("quit_ack_delay_s", delay_s=delay)
                await self.clock.sleep(delay)
            await self._quit_app("sender_stop")
            await self._send_receiver_status(conn, msg, rid)
        elif mtype == "SET_VOLUME":
            self._set_volume(data.get("volume") or {})
            await self._send_receiver_status(conn, msg, rid)
        elif mtype == "GET_APP_AVAILABILITY":
            avail = {
                a: "APP_AVAILABLE" if self._launch_problem(a) is None else "APP_UNAVAILABLE"
                for a in data.get("appId", [])
            }
            await self._reply(
                conn,
                msg,
                {"requestId": rid, "responseType": "GET_APP_AVAILABILITY", "availability": avail},
            )
        else:
            await self._reply(
                conn,
                msg,
                {"type": "INVALID_REQUEST", "requestId": rid, "reason": "INVALID_COMMAND"},
            )

    def _launch_problem(self, app_id: str) -> str | None:
        errors = self.knobs.get("launch_error") or {}
        if app_id in errors:
            return str(errors[app_id])
        if app_id not in KNOWN_APPS:
            return "NOT_FOUND"
        return None

    async def _launch(
        self, conn: CastConnection, msg: p.CastMessage, rid: int, app_id: str
    ) -> None:
        mode = self.knobs.get("launch")
        if mode == "never":
            self._dict_fired("launch", mode=mode, app=app_id)
            return
        if self.knobs.get("launch_fail_once") and not self._launch_failed_once:
            self._launch_failed_once = True
            self.knobs.fires("launch_fail_once", app=app_id)
            await self._reply(conn, msg, {"type": "LAUNCH_ERROR", "requestId": rid})
            return
        problem = self._launch_problem(app_id)
        if problem is not None:
            if app_id in (self.knobs.get("launch_error") or {}):
                self._dict_fired("launch_error", app=app_id, reason=problem)
            body: dict[str, Any] = {"type": "LAUNCH_ERROR", "requestId": rid}
            if problem != "silent":
                body["reason"] = problem
            await self._reply(conn, msg, body)
            return
        if self.app is None or self.app.app_id != app_id:
            await self._quit_app("launch", send_status=False)
            self._start_app(app_id)
        if mode == "broadcast_only":
            self._dict_fired("launch", mode=mode, app=app_id)
            await self._send_receiver_status(None, None)
            for c in list(self.cast.connections):  # the requester too, but with requestId 0
                if c.has_channel_to(p.PLATFORM_ID) and c is conn:
                    await c.send_json(
                        p.PLATFORM_ID,
                        p.BROADCAST_ID,
                        p.NS_RECEIVER,
                        {
                            "type": "RECEIVER_STATUS",
                            "requestId": 0,
                            "status": self.receiver_status(),
                        },
                    )
            return
        await self._send_receiver_status(conn, msg, rid)

    def _start_app(self, app_id: str, foreign: bool = False) -> CastApp:
        name, namespaces = KNOWN_APPS.get(app_id, (app_id, [p.NS_MEDIA]))
        self._transport_ids += 1
        self.app = CastApp(
            app_id=app_id,
            display_name=name,
            session_id=str(uuid.UUID(int=self.rng.stream("session").getrandbits(128), version=4)),
            transport_id=f"web-{self._transport_ids}",
            namespaces=list(namespaces),
            foreign=foreign,
        )
        self._loads = 0
        if self._applied_level != self.volume_level:  # C8: the idle SET_VOLUME is lost
            self._dict_fired(
                "ignore_idle_volume", reported=self.volume_level, real=self._applied_level
            )
            self.volume_level = self._applied_level
        self.rec.state(
            "app", app=app_id, session=self.app.session_id, transport=self.app.transport_id
        )
        return self.app

    async def _quit_app(self, reason: str, send_status: bool = True) -> None:
        app = self.app
        if app is None:
            return
        self._cancel("idle_app")
        self.app = None
        self.media = None
        self._last_sig = None
        await self.transport.stop()
        self.transport.next_slot = None
        self.rec.state("app", app=None, reason=reason, was=app.app_id)
        for conn in list(self.cast.connections):  # CLOSE the app's virtual connections
            for sender in conn.senders_for(app.transport_id):
                await conn.send_json(app.transport_id, sender, p.NS_CONNECTION, {"type": "CLOSE"})
                conn.channels.discard((sender, app.transport_id))
        if send_status:
            await self._send_receiver_status()

    def _set_volume(self, vol: Mapping[str, Any]) -> None:
        if "muted" in vol:
            self.muted = bool(vol["muted"])
        if "level" not in vol:
            return
        level = max(0.0, min(1.0, float(vol["level"])))
        if self.knobs.get("control_type") == "fixed":
            self._dict_fired("control_type", ignored_level=level)
            return
        idle_mode = self.knobs.get("ignore_idle_volume")
        if self.app is None and idle_mode == "ignored":
            self._dict_fired("ignore_idle_volume", mode=idle_mode, level=level)
            return
        self.volume_level = level
        if self.app is None and idle_mode == "not_applied":
            return  # reported, but the old level comes back at launch
        self._applied_level = level

    # -- multizone ------------------------------------------------------------------------

    async def _multizone(
        self, conn: CastConnection, msg: p.CastMessage, mtype: str, data: dict[str, Any]
    ) -> None:
        rid = int(data.get("requestId", 0) or 0)
        if mtype == "GET_STATUS":
            members = self.ident_list("members") or [
                {"uuid": self.uuid_hex, "name": self.friendly_name}
            ]
            devices = [
                {"deviceId": eureka.dashed(m["uuid"]), "name": m.get("name", "")} for m in members
            ]
            await self._reply(
                conn,
                msg,
                {
                    "type": "MULTIZONE_STATUS",
                    "requestId": rid,
                    "status": {
                        "devices": devices,
                        "playbackSession": {
                            "appAllowsGrouping": True,
                            "isVideoContent": False,
                            "streamTransferSupported": True,
                        },
                    },
                },
            )
        elif mtype == "GET_CASTING_GROUPS":
            groups = eureka.multizone_body(self)["groups"]
            await self._reply(
                conn, msg, {"type": "CASTING_GROUPS", "requestId": rid, "groups": groups}
            )

    # -- media status ---------------------------------------------------------------------

    def player_state(self) -> str:
        ms = self.media
        if ms is None:
            return "IDLE"
        if ms.forced_state:
            return ms.forced_state
        if ms.foreign and self.transport.owner == "ma":
            return "IDLE"
        st = self.lens.state()
        if st == TState.PLAYING:
            return "PLAYING"
        if st == TState.PAUSED:
            return "PAUSED"
        if st in (TState.LOADING, TState.BUFFERING) or ms.starting:
            return "BUFFERING"
        return "IDLE"

    def _item_json(self, item: dict[str, Any]) -> dict[str, Any]:
        media = item["media"]
        small: dict[str, Any] = {"contentId": media.get("contentId")}
        if "customData" in media:
            small["customData"] = media["customData"]
        return {
            "itemId": item["itemId"],
            "orderId": item["orderId"],
            "autoplay": item.get("autoplay", True),
            "media": small,
        }

    def media_entry(self) -> dict[str, Any] | None:
        ms = self.media
        if ms is None:
            return None
        state = self.player_state()
        entry: dict[str, Any] = {"mediaSessionId": ms.id, "playerState": state}
        if state == "IDLE":
            if ms.idle_reason and not self.knobs.fires("omit_idle_reason"):
                entry["idleReason"] = ms.idle_reason
            if ms.extended is not None:
                entry["extendedStatus"] = ms.extended
            else:
                entry["currentTime"] = self.lens.current_time()
            return entry
        entry = {
            "mediaSessionId": ms.id,
            "playbackRate": 1,
            "playerState": state,
            "currentTime": self.lens.current_time(),
            "supportedMediaCommands": self.knobs.get("supported_media_commands"),
            "volume": {"level": ms.stream_level, "muted": ms.stream_muted},
            "currentItemId": ms.current_item_id,
            "repeatMode": "REPEAT_OFF",
            "items": [self._item_json(i) for i in ms.items],
        }
        cur = ms.item(ms.current_item_id)
        sparse = self.knobs.get("status_sparse_media")
        if cur is not None and not (sparse and ms.media_sent_version == ms.media_version):
            media = dict(cur["media"])
            media.setdefault("duration", None)
            entry["media"] = media
            ms.media_sent_version = ms.media_version
        elif cur is not None:
            self.knobs.fires("status_sparse_media")
        return entry

    def _sig(self) -> Any:
        ms = self.media
        if ms is None:
            return None
        return (
            ms.id,
            self.player_state(),
            ms.idle_reason,
            ms.current_item_id,
            ms.media_version,
            json.dumps(ms.extended, sort_keys=True),
            len(ms.items),
        )

    def _media_status_body(self, rid: int) -> dict[str, Any]:
        entry = self.media_entry()
        return {"type": "MEDIA_STATUS", "requestId": rid, "status": [entry] if entry else []}

    async def _send_media_status(
        self,
        conn: CastConnection | None = None,
        msg: p.CastMessage | None = None,
        rid: int = 0,
        *,
        force: bool = False,
    ) -> None:
        if self.app is None:
            return
        sig = self._sig()
        if conn is not None and msg is not None:
            await self._reply(conn, msg, self._media_status_body(rid))
        elif not force and sig == self._last_sig:
            return
        if sig != self._last_sig or force:
            await self._broadcast(
                self.app.transport_id, p.NS_MEDIA, self._media_status_body(0), conn
            )
        self._last_sig = sig

    def _status_changed(self) -> None:
        """Push a requestId-0 MEDIA_STATUS soon (coalesced, deduplicated)."""
        if getattr(self, "_flush_pending", False):
            return
        self._flush_pending = True

        async def _flush() -> None:
            await asyncio.sleep(0)
            self._flush_pending = False
            await self._send_media_status()
            self._idle_app_check()

        self._spawn(_flush())

    def _idle_app_check(self) -> None:
        timeout = self.knobs.get("idle_app_timeout_s")
        if not timeout or self.app is None:
            return
        if self.player_state() == "IDLE":
            if "idle_app" not in self._timers:
                self._later("idle_app", timeout, self._idle_app_quit)
        else:
            self._cancel("idle_app")

    async def _idle_app_quit(self) -> None:
        if self.app is not None and self.player_state() == "IDLE":
            self._dict_fired("idle_app_timeout_s", app=self.app.app_id)
            await self._quit_app("idle_timeout")

    # -- media namespace --------------------------------------------------------------------

    async def _media_cmd(
        self, conn: CastConnection, msg: p.CastMessage, mtype: str, data: dict[str, Any]
    ) -> None:
        rid = int(data.get("requestId", 0) or 0)
        on = self.knobs.param("app_error_rate", "on", DEFAULT_APP_ERROR_ON)
        if mtype in on and self.knobs.fires("app_error_rate", request=mtype):
            await self._reply(conn, msg, {"type": "ERROR", "reason": "APP_ERROR", "requestId": rid})
            return
        if mtype in ("LOAD", "QUEUE_LOAD"):
            await self._load(conn, msg, rid, data, queue=mtype == "QUEUE_LOAD")
            return
        if mtype == "GET_STATUS":
            await self._reply(conn, msg, self._media_status_body(rid))
            self._last_sig = self._sig()
            return
        ms = self.media
        sid = data.get("mediaSessionId")
        if ms is None or (sid is not None and sid != ms.id):
            await self._reply(
                conn,
                msg,
                {"type": "INVALID_REQUEST", "requestId": rid, "reason": "INVALID_MEDIA_SESSION_ID"},
            )
            return
        state = self.player_state()
        tm = self.transport
        if mtype == "PLAY":
            meta = (data.get("customData") or {}).get("metadata")
            if meta is not None:  # MA flow mode: metadata refresh
                cur = ms.item(ms.current_item_id)
                if cur is not None:
                    cur["media"]["metadata"] = meta
                    ms.media_version += 1
            if state == "PAUSED":
                await tm.play()
            elif state == "IDLE" and meta is None:
                await self._invalid_state(conn, msg, rid)
                return
        elif mtype == "PAUSE":
            if state == "IDLE":
                await self._invalid_state(conn, msg, rid)
                return
            await tm.pause()
        elif mtype == "STOP":
            await self._end_session("CANCELLED")
            if self.knobs.fires("empty_status_after_stop"):
                await self._reply(
                    conn, msg, {"type": "MEDIA_STATUS", "requestId": rid, "status": []}
                )
                self._last_sig = self._sig()
                return
        elif mtype == "SEEK":
            if state == "IDLE":
                await self._invalid_state(conn, msg, rid)
                return
            pos = data.get("currentTime")
            if pos is None and "relativeTime" in data:
                pos = self.transport.truth_position() + float(data["relativeTime"])
            resume = data.get("resumeState")
            stay_paused = resume == "PLAYBACK_PAUSE" or (state == "PAUSED" and resume is None)
            await tm.seek(max(0.0, float(pos or 0.0)))
            ms.starting = True
            await asyncio.sleep(0)  # let the reopen reach BUFFERING
            if stay_paused:
                await tm.pause()
        elif mtype == "QUEUE_INSERT":
            self._queue_insert(ms, data.get("items") or [], data.get("insertBefore"))
            if state == "IDLE" and ms.idle_reason == "FINISHED" and data.get("currentItemId"):
                await self._play_item(ms.item(data["currentItemId"]))
        elif mtype in ("QUEUE_UPDATE", "QUEUE_NEXT", "QUEUE_PREV"):
            jump = {"QUEUE_NEXT": 1, "QUEUE_PREV": -1}.get(mtype, data.get("jump"))
            target = None
            if data.get("currentItemId") is not None:
                target = ms.item(data["currentItemId"])
            elif jump:
                target = ms.neighbour(int(jump))
            for upd in data.get("items") or []:
                item = ms.item(upd.get("itemId"))
                if item is not None and "media" in upd:
                    item["media"].update(upd["media"])
            if target is not None:
                await self._play_item(target)
            elif jump:  # past either end: the session ends
                await self._end_session("FINISHED")
            self._mirror_next()
        elif mtype == "QUEUE_REMOVE":
            ids = set(data.get("itemIds") or [])
            ms.items = [
                i for i in ms.items if i["itemId"] not in ids or i["itemId"] == ms.current_item_id
            ]
            self._renumber(ms)
            self._mirror_next()
        elif mtype == "SET_VOLUME":
            vol = data.get("volume") or {}
            if "level" in vol:
                ms.stream_level = max(0.0, min(1.0, float(vol["level"])))
            if "muted" in vol:
                ms.stream_muted = bool(vol["muted"])
        elif mtype not in ("QUEUE_REORDER", "EDIT_TRACKS_INFO", "SET_PLAYBACK_RATE"):
            await self._reply(
                conn,
                msg,
                {"type": "INVALID_REQUEST", "requestId": rid, "reason": "INVALID_COMMAND"},
            )
            return
        await self._send_media_status(conn, msg, rid)

    async def _invalid_state(self, conn: CastConnection, msg: p.CastMessage, rid: int) -> None:
        await self._reply(conn, msg, {"type": "INVALID_PLAYER_STATE", "requestId": rid})

    def _new_item(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._item_ids += 1
        return {
            "itemId": self._item_ids,
            "orderId": 0,
            "autoplay": raw.get("autoplay", True),
            "media": dict(raw.get("media") or {}),
        }

    @staticmethod
    def _renumber(ms: MediaSession) -> None:
        for n, item in enumerate(ms.items):
            item["orderId"] = n

    def _queue_insert(self, ms: MediaSession, raw_items: list[Any], before: Any) -> None:
        new = [self._new_item(r) for r in raw_items]
        idx = ms.index(before) if before is not None else -1
        if idx < 0:
            ms.items.extend(new)
        else:
            ms.items[idx:idx] = new
        self._renumber(ms)
        self._mirror_next()

    def _mirror_next(self) -> None:
        """Keep TransportMachine.next_slot equal to the queue item after the current one."""
        ms = self.media
        nxt = ms.neighbour(1) if ms is not None and not ms.foreign else None
        if nxt is None or not nxt.get("autoplay", True):
            self.transport.next_slot = None
            return
        self.transport.next_slot = self._slot(nxt)

    def _slot(self, item: dict[str, Any]) -> MediaSlot:
        media = item["media"]
        meta = media.get("metadata") or {}
        dur = media.get("duration")
        return MediaSlot(
            uri=str(media.get("contentId", "")),
            meta=json.dumps(meta),
            duration_s=float(dur) if isinstance(dur, (int, float)) else None,
            title=meta.get("title"),
            extra={"item_id": item["itemId"]},
        )

    async def _load(
        self,
        conn: CastConnection,
        msg: p.CastMessage,
        rid: int,
        data: dict[str, Any],
        queue: bool,
    ) -> None:
        raw_items = (
            data.get("items")
            if queue
            else [{"media": data.get("media") or {}, "autoplay": data.get("autoplay", True)}]
        )
        if not raw_items:
            await self._reply(
                conn, msg, {"type": "INVALID_REQUEST", "requestId": rid, "reason": "INVALID_PARAMS"}
            )
            return
        old = self.media
        if old is not None and self.player_state() != "IDLE":
            await self.transport.stop()
            old.idle_reason, old.forced_state, old.starting = "INTERRUPTED", "IDLE", False
            await self._send_media_status(force=True)
        self._session_ids += 1
        ms = MediaSession(id=self._session_ids)
        ms.items = [self._new_item(r) for r in raw_items]
        self._renumber(ms)
        start = ms.items[min(int(data.get("startIndex", 0) or 0), len(ms.items) - 1)]
        ms.current_item_id = start["itemId"]
        ms.load_request_id = rid
        self.media = ms
        self._last_sig = None
        self._loads += 1
        content_id = str(start["media"].get("contentId", ""))
        url = urlsplit(content_id)
        if self.knobs.get("public_dns_only") and _private_name(url.hostname or ""):
            self.knobs.fires("public_dns_only", host=url.hostname)
            await self._load_failed(conn, msg, rid, start["itemId"])
            return
        reject = self.knobs.get("reject_http_media")
        if reject != "off" and url.scheme == "http":
            self._dict_fired("reject_http_media", mode=reject, url=content_id)
            if reject == "load_failed":
                await self._load_failed(conn, msg, rid, start["itemId"])
                return
            ms.forced_state = "BUFFERING"  # silent: nothing plays, no error
            await self._send_media_status(conn, msg, rid)
            return
        offset = float(data.get("currentTime", 0) or 0)
        await self._play_item(start, offset)
        kill = self.knobs.get("kill_control_socket_after_flow_restart_s")
        if kill > 0 and self._loads >= 2:
            self._later("kill_socket", kill, self._kill_sockets, self._loads)
        takeover_at = self.knobs.param("foreign_takeover", "at_s", 0)
        if takeover_at and self._session_ids == 1:
            self._later("foreign_takeover", float(takeover_at), self._foreign_takeover)
        await self._send_media_status(conn, msg, rid)

    async def _load_failed(
        self, conn: CastConnection, msg: p.CastMessage, rid: int, item_id: int
    ) -> None:
        ms = self.media
        await self._reply(
            conn,
            msg,
            {
                "type": "LOAD_FAILED",
                "requestId": rid,
                "itemId": item_id,
                "detailedErrorCode": ERR_SRC_NOT_SUPPORTED,
            },
        )
        if ms is not None:
            ms.idle_reason, ms.forced_state = "ERROR", None
        await self._send_media_status(force=True)

    async def _play_item(self, item: dict[str, Any] | None, offset_s: float = 0.0) -> None:
        ms = self.media
        if ms is None or item is None:
            return
        ms.current_item_id = item["itemId"]
        ms.idle_reason = ms.forced_state = ms.extended = None
        ms.starting = True
        slot = self._slot(item)
        tm = self.transport
        await tm.set_uri(
            slot.uri, slot.meta, slot.duration_s, title=slot.title, item_id=item["itemId"]
        )
        self._mirror_next()
        await tm.play()
        if offset_s > 0:
            await tm.seek(offset_s)
        self._status_changed()

    async def _end_session(self, reason: str) -> None:
        ms = self.media
        if ms is None:
            return
        ms.starting = False
        ms.forced_state = None
        ms.idle_reason = reason
        await self.transport.stop()

    # -- truth -> protocol ------------------------------------------------------------------

    def _on_tm(self, name: str, data: dict[str, Any]) -> None:
        ms = self.media
        if ms is None:
            return
        if name == "state":
            if data["new"] in (TState.LOADING, TState.BUFFERING, TState.PLAYING, TState.PAUSED):
                ms.starting = False
            self._status_changed()
        elif name == "advanced":
            cur = self.transport.current
            item_id = cur.extra.get("item_id") if cur is not None else None
            if item_id is not None and ms.item(item_id) is not None:
                ms.current_item_id = item_id
                ms.idle_reason = ms.extended = None
                ms.starting = True
            self._mirror_next()
            self._status_changed()
        elif name == "track_ended":
            reason = str(data.get("reason"))
            will_advance = (
                self.transport.next_slot is not None
                and self.knobs.get("auto_advance")
                and reason != "error"
            )
            if will_advance:
                return
            ms.starting = False
            if reason == "error":
                ms.idle_reason = "ERROR"
                self._spawn(self._broadcast_error(ERR_NETWORK, ms.current_item_id))
            else:
                self._last_item_ended(ms)

    def _last_item_ended(self, ms: MediaSession) -> None:
        mode = self.knobs.get("idle_on_eof")
        if mode == "stay_buffering":
            self._dict_fired("idle_on_eof", mode=mode)
            ms.forced_state = "BUFFERING"
        elif mode == "ERROR":
            self._dict_fired("idle_on_eof", mode=mode)
            ms.idle_reason = "ERROR"
        else:
            ms.idle_reason = "FINISHED"
        self._status_changed()

    async def _broadcast_error(self, code: int, item_id: int | None) -> None:
        if self.app is None:
            return
        body = {"type": "ERROR", "requestId": 0, "detailedErrorCode": code, "itemId": item_id}
        await self._broadcast(self.app.transport_id, p.NS_MEDIA, body)

    def _on_sink(self, name: str, data: dict[str, Any]) -> None:
        ms = self.media
        if ms is None or self.transport.owner != "ma":
            return
        if name == "opened":
            cur = ms.item(ms.current_item_id)
            declared = _mime((cur or {}).get("media", {}).get("contentType"))
            served = _mime(data.get("content_type"))
            if (
                self.knobs.get("strict_content_type")
                and declared
                and served != declared
                and self.knobs.fires("strict_content_type", declared=declared, served=served)
            ):
                self._spawn(self._fail_current(ERR_SRC_NOT_SUPPORTED))
                return
            cut = self.knobs.get("range_rerequest_after_bytes")
            if cut > 0 and self.transport.current is not None:
                self._spawn(self._range_rerequest(self.transport.current.uri, cut))
        elif name == "first_audio":
            ms.load_request_id = None
        elif name == "cap_hit" and self._cap_knob is not None:
            self._dict_fired(self._cap_knob, **data)
        elif name == "eof":
            cut_ms = self.knobs.get("clip_tail_cut_ms")
            if cut_ms > 0 and self.transport.next_slot is None:
                unplayed = float(data.get("unplayed_s", 0.0))
                self._later(
                    "clip_tail", max(0.0, unplayed - cut_ms / 1000), self._clip_tail, cut_ms
                )

    def _cap_policy(
        self, _status: int, headers: Mapping[str, str], byte_cap: int, time_cap: float
    ) -> tuple[int, float]:
        """C9/C10/C33: one cap for responses with a Content-Length, another without."""
        self._cap_knob = None
        if "Content-Length" in headers:
            cut = self.knobs.get("content_length_cutoff_bytes")
            if cut > 0:
                byte_cap = min(byte_cap, cut) if byte_cap else cut
                self._cap_knob = "content_length_cutoff_bytes"
                self.rec.log("cap_armed", knob="content_length_cutoff_bytes", bytes=cut)
        else:
            secs = self.knobs.get("no_content_length_stop_after_s")
            if secs > 0:
                time_cap = min(time_cap, secs) if time_cap else secs
                self._cap_knob = "no_content_length_stop_after_s"
                self.rec.log("cap_armed", knob="no_content_length_stop_after_s", seconds=secs)
        return byte_cap, time_cap

    async def _fail_current(self, code: int) -> None:
        """The current item cannot play: LOAD_FAILED (while loading) or ERROR, then IDLE/ERROR."""
        ms = self.media
        if ms is None or self.app is None:
            return
        item_id = ms.current_item_id
        rid = ms.load_request_id
        await self.transport.stop()
        ms.starting = False
        ms.idle_reason, ms.forced_state = "ERROR", None
        if rid is not None:
            body = {
                "type": "LOAD_FAILED",
                "requestId": rid,
                "itemId": item_id,
                "detailedErrorCode": code,
            }
            await self._broadcast(self.app.transport_id, p.NS_MEDIA, body)
        else:
            await self._broadcast_error(code, item_id)
        await self._send_media_status(force=True)

    async def _range_rerequest(self, url: str, after: int) -> None:
        """X5: past N bytes, ask again with ``Range: bytes=N-``; a 200 drops the item."""
        sink = self.sink
        assert sink is not None
        while sink.active and sink.bytes_read < after:
            await self.clock.sleep(0.05)
        if not sink.active or self.transport.current is None or self.transport.current.uri != url:
            return
        offset = after  # the device asks for exactly what it has consumed so far
        if self._range_session is None:
            self._range_session = aiohttp.ClientSession()
        try:
            async with self._range_session.get(url, headers={"Range": f"bytes={offset}-"}) as resp:
                status = resp.status
                content_range = resp.headers.get("Content-Range")
        except (aiohttp.ClientError, OSError) as err:
            self.rec.log("range_rerequest", url=url, error=repr(err))
            return
        self._dict_fired(
            "range_rerequest_after_bytes", offset=offset, status=status, content_range=content_range
        )
        if status != 206:
            await self._fail_current(ERR_NETWORK)

    async def _clip_tail(self, cut_ms: int) -> None:
        ms = self.media
        if ms is None or self.transport.state not in (TState.PLAYING, TState.BUFFERING):
            return
        self._dict_fired("clip_tail_cut_ms", cut_ms=cut_ms)
        await self.transport.stop()
        self._last_item_ended(ms)

    async def _kill_sockets(self, load_no: int) -> None:
        n = self.cast.reset_all()
        self._dict_fired("kill_control_socket_after_flow_restart_s", load=load_no, reset=n)

    # -- device-side events (knobs and sim actions) -------------------------------------------

    async def _spontaneous_error(self) -> bool:
        """C30: ERROR x dup, IDLE/ERROR + extendedStatus LOADING, then the next item."""
        ms = self.media
        if ms is None or self.app is None or self.player_state() not in ("PLAYING", "BUFFERING"):
            return False
        prm = self.knobs.get("spontaneous_error") or {}
        code = int(prm.get("code", ERR_DECODE))
        dup = int(prm.get("dup", 2))
        gap = float(prm.get("gap_ms", 30)) / 1000
        then = prm.get("then", "load_next")
        item_id = ms.current_item_id
        nxt = ms.neighbour(1)
        self._dict_fired("spontaneous_error", item=item_id, code=code, then=then)
        for n in range(dup):
            await self._broadcast_error(code, item_id)
            if n < dup - 1:
                await self.clock.sleep(gap)
        await self.clock.sleep(0.003)
        ms.idle_reason, ms.forced_state, ms.starting = "ERROR", "IDLE", False
        if nxt is not None and then == "load_next":
            ms.extended = {
                "playerState": "LOADING",
                "loadingItemId": nxt["itemId"],
                "media": nxt["media"],
            }
        await self._send_media_status(force=True)
        await self.transport.stop()
        if nxt is not None and then == "load_next":
            await self.clock.sleep(self.knobs.get("next_item_fetch_delay_ms") / 1000)
            if self.media is ms:
                await self._play_item(nxt)
        else:
            ms.forced_state = None
        return True

    async def _device_pause(self) -> bool:
        """C21: the device pauses by itself (voice, touch, another sender)."""
        if self.media is None or self.player_state() != "PLAYING":
            return False
        if self.knobs.get("spontaneous_pause_mean_s"):
            self._dict_fired("spontaneous_pause_mean_s")
        await self.transport.pause()
        return True

    async def _foreign_takeover(
        self, app_id: str | None = None, content_id: str | None = None, title: str | None = None
    ) -> bool:
        """C40: off (+0), app idle (+2 s), BUFFERING (+7 s), PLAYING the foreign stream (+10 s)."""
        app_id = app_id or self.knobs.param("foreign_takeover", "app", p.APP_MEDIA_RECEIVER)
        content_id = content_id or self.knobs.param(
            "foreign_takeover", "content_id", "https://media.example.invalid/foreign.mp3"
        )
        title = title or self.knobs.param("foreign_takeover", "title", "Foreign stream")
        self._dict_fired("foreign_takeover", app=app_id)
        await self._quit_app("foreign_takeover")
        await self.clock.sleep(2.0)
        self._start_app(app_id, foreign=True)
        await self._send_receiver_status()
        await self.clock.sleep(5.0)
        self._session_ids += 1
        ms = MediaSession(id=self._session_ids, foreign=True, forced_state="BUFFERING")
        self._item_ids += 1
        ms.items = [
            {
                "itemId": self._item_ids,
                "orderId": 0,
                "autoplay": True,
                "media": {
                    "contentId": content_id,
                    "contentType": "audio/mpeg",
                    "streamType": "BUFFERED",
                    "metadata": {"metadataType": 3, "title": title},
                },
            }
        ]
        ms.current_item_id = self._item_ids
        self.media = ms
        await self._send_media_status(force=True)
        await self.clock.sleep(3.0)
        if self.media is ms:
            ms.forced_state = None
            self.transport.take_over("foreign", title=title)
            await self._send_media_status(force=True)
        return True

    async def _leader_move(self, new_host: str | None = None) -> int:
        """C25: the group's leader changes; sockets are reset and the endpoint moves."""
        port = self.cast.port
        host = new_host or self.cast.host
        n = self.cast.reset_all()
        await self.cast.stop_listening()
        self._dict_fired("leader_moves_after_s", reset=n, new_host=host)
        await self.clock.sleep(self.knobs.get("leader_move_gap_s"))
        await self.cast.start(host, port)
        self.rec.log("cast_listening", host=host, port=port, moved=True)
        return n

    async def _press_next(self) -> bool:
        """A button on the MA receiver app: player_command next over the MA namespace."""
        if self.app is None or p.NS_MA not in self.app.namespaces:
            return False
        await self._broadcast(
            self.app.transport_id, p.NS_MA, {"type": "player_command", "command": "next"}
        )
        return True

    # -- hooks -----------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        ms = self.media
        return {
            "cast_host": self.cast.host,
            "cast_port": self.cast.port,
            "eureka_https_port": getattr(self, "eureka_https_port", None),
            "uuid": self.uuid_hex,
            "app": self.app.to_json() if self.app else None,
            "player_state": self.player_state(),
            "media_session": None
            if ms is None
            else {
                "id": ms.id,
                "current_item_id": ms.current_item_id,
                "idle_reason": ms.idle_reason,
                "forced_state": ms.forced_state,
                "items": [i["media"].get("contentId") for i in ms.items],
            },
            "volume": {
                "level": self.volume_level,
                "applied": self._applied_level,
                "muted": self.muted,
            },
            "connections": len(self.cast.connections),
            "output_delay_ms": self.knobs.get("output_delay_ms"),
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "spontaneous_error":
            return await self._spontaneous_error()
        if name == "foreign_takeover":
            return await self._foreign_takeover(
                payload.get("app"), payload.get("content_id"), payload.get("title")
            )
        if name == "device_pause":
            return await self._device_pause()
        if name == "press_next":
            return await self._press_next()
        if name == "leader_move":
            return await self._leader_move(payload.get("host"))
        if name == "quit_app":
            await self._quit_app("device")
            return True
        raise KeyError(name)
