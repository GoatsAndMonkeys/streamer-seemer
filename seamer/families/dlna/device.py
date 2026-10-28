"""A generic DLNA/UPnP MediaRenderer, the way MA's ``dlna`` provider (async_upnp_client
``DmrDevice``) sees it.

Main server (``ctx.port``): ``/description.xml``, ``/upnp/<svc>/scpd.xml``,
``/upnp/<svc>/control`` (SOAP), ``/upnp/<svc>/event`` (GENA SUBSCRIBE/UNSUBSCRIBE) and
``/_sim``. Services: ``avt``, ``rc``, ``cm`` and, with ``openhome``, ``ohproduct``,
``ohinfo``, ``ohtime``, ``ohvolume``. An SSDP responder answers M-SEARCH on loopback
(``ctx.options["ssdp_port"]``, default ephemeral).

The renderer really fetches the stream (:class:`DlnaSink`); truth is what the sink
plays. Quirks only change what it reports or how it answers.

Sim actions (``POST /_sim/action/<name>``)
------------------------------------------
- ``local_volume {level}``: someone turns the volume on the device itself.
- ``drop_subscriptions``: forget every GENA subscription (as after a reboot).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid as uuidlib
from dataclasses import dataclass
from typing import Any, ClassVar

import aiohttp
from aiohttp import web

from ...simkit.device import SimDevice
from ...simkit.kits.discovery import SsdpResponder, SsdpTarget
from ...simkit.transport import MediaSlot, ReportLens, TransportMachine, TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)
from . import openhome, proto, upnp
from .sink import DlnaSink

PLAYING_LIKE = (TState.PLAYING, TState.BUFFERING, TState.LOADING)
# The LUMIN/TEAC probe sequence from the #6775 log: three ranged GETs before playing
RANGE_PROBES = ("bytes=0-", "bytes=12-", "bytes=16384-")
SEEK_EVENT_MUTE_S = 2.0


@dataclass
class FakePlay:
    """#6775: the device says PLAYING and counts RelTime up, but decodes nothing."""

    started: float
    duration_s: float | None
    base: float = 0.0
    paused_at: float | None = None

    def position(self, now: float) -> float:
        end = self.paused_at if self.paused_at is not None else now
        pos = self.base + (end - self.started)
        return min(pos, self.duration_s) if self.duration_s else pos

    @property
    def state(self) -> TState:
        return TState.PAUSED if self.paused_at is not None else TState.PLAYING


class DlnaLens(ReportLens):
    """Truth -> what the renderer reports (fake play, lingering media, gap state)."""

    dev: DlnaDevice

    def state(self) -> TState:
        d = self.dev
        tm = self.tm
        if d.fake_play is not None:
            return self._differs("state", tm.state, d.fake_play.state)
        if d.linger is not None:
            return self._differs("state", tm.state, d.linger[1])
        if (
            d.advancing
            and tm.state in (TState.LOADING, TState.BUFFERING)
            and d.knobs.get("track_gap_state") == "stopped"
        ):
            return self._differs("state", tm.state, TState.STOPPED)
        return tm.state

    def position(self) -> float | None:
        d = self.dev
        truth = self.tm.truth_position()
        if d.fake_play is not None:
            return self._differs("position", truth, d.fake_play.position(d.clock.monotonic()))
        if d.pending_seek is not None:
            return self._differs("position", truth, d.pending_seek)
        if d.linger is not None:
            return self._differs("position", truth, d.linger[2])
        return super().position()

    def duration(self) -> float | None:
        d = self.dev
        if d.linger is not None:
            return d.linger[0].duration_s
        return super().duration()

    def metadata(self) -> MediaSlot | None:
        d = self.dev
        if d.linger is not None:
            return d.linger[0]
        return self.tm.current


class DlnaDevice(SimDevice):
    family: ClassVar[str] = "dlna"
    lens_cls = DlnaLens
    lens: DlnaLens
    sink: DlnaSink

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        # swap in the DLNA sink (mid-stream Range re-request) and rebuild truth + lens on it
        self.sink = DlnaSink(self.clock, self.rec.child("sink"), self.knobs)
        self.transport = TransportMachine(self.clock, self.rec, self.knobs, self.sink)
        self.lens = DlnaLens(self.transport, self.knobs, self.rng)
        self.lens.dev = self
        ident = self.identity
        seed = f"seamer-dlna:{self.id}:{ident.get('serial', '')}"
        self.udn = "uuid:" + str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, seed))
        self.volume = int(ident.get("volume", "20"))
        self.muted = False
        self.gena = upnp.Gena(self.clock, self.rec.child("gena"), self.knobs, self._full_state)
        self.oh = openhome.OpenHome(self)
        self.ssdp: SsdpResponder | None = None
        self._http: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        # reported-state and quirk state
        self.fake_play: FakePlay | None = None
        self._fake_timer: Any = None
        self.linger: tuple[MediaSlot, TState, float] | None = None  # (slot, state, pos)
        self._linger_timer: Any = None
        self.pending_seek: float | None = None
        self.advancing = False
        self.reported_next: tuple[str, str] = ("", "")
        self.frozen_actions: str | None = None
        self.volume_restore: int | None = None
        self._avt_quiet_until = 0.0
        self.stream_ctype: str | None = None
        self.stream_chunked = False
        self.stream_length: int | None = None
        self.range_probes: list[dict[str, Any]] = []
        self.transport.add_listener(self._on_transport)
        self.sink.add_listener(self._on_sink)

    # -- identity --------------------------------------------------------------------

    @property
    def ip(self) -> str:
        return self.identity.get("ip", "192.0.2.20")

    @property
    def volume_max(self) -> int:
        return int(self.knobs.get("volume_max"))

    def service_keys(self) -> list[str]:
        keys = ["avt", "rc", "cm"]
        if self.knobs.get("openhome"):
            keys += list(openhome.OH_SERVICES)
        return keys

    def actions_for(self, key: str) -> dict[str, upnp.Args]:
        actions = dict(upnp.SERVICES[key][3])
        if key == "avt" and self.knobs.get("set_next") == "absent":
            actions.pop("SetNextAVTransportURI", None)
        return actions

    def var_spec(self, key: str) -> upnp.VarSpec:
        spec = upnp.VarSpec()
        if key == "avt":
            units = self.knobs.get("seek_units")
            spec.allowed["TransportState"] = upnp.TRANSPORT_STATES
            spec.allowed["TransportStatus"] = ("OK", "ERROR_OCCURRED")
            spec.allowed["CurrentPlayMode"] = ("NORMAL",)
            spec.allowed["TransportPlaySpeed"] = ("1",)
            spec.allowed["A_ARG_TYPE_SeekMode"] = (
                ("REL_TIME",) if units == "rel_time_only" else ("TRACK_NR", "REL_TIME", "ABS_TIME")
            )
        elif key == "rc":
            spec.ranges["Volume"] = (0, self.volume_max, 1)
            spec.allowed["A_ARG_TYPE_Channel"] = ("Master",)
            spec.allowed["A_ARG_TYPE_PresetName"] = ("FactoryDefaults",)
        elif key == "cm":
            spec.allowed["A_ARG_TYPE_Direction"] = ("Input", "Output")
            spec.allowed["A_ARG_TYPE_ConnectionStatus"] = (
                "OK",
                "ContentFormatMismatch",
                "InsufficientBandwidth",
                "UnreliableChannel",
                "Unknown",
            )
        return spec

    # -- lifecycle ---------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app()
        app.router.add_get("/description.xml", self._description)
        for key in upnp.SERVICES:
            app.router.add_get(f"/upnp/{key}/scpd.xml", self._scpd)
            app.router.add_post(f"/upnp/{key}/control", self._control)
            app.router.add_route("SUBSCRIBE", f"/upnp/{key}/event", self._subscribe)
            app.router.add_route("UNSUBSCRIBE", f"/upnp/{key}/event", self._unsubscribe)
        return app

    async def on_start(self) -> None:
        # rec=None: the shared SsdpResponder's own recording clashes with Emitter.cmd(src=)
        self.ssdp = SsdpResponder(
            [
                SsdpTarget(
                    f"{self.base_url}/description.xml",
                    self.udn,
                    [
                        "urn:schemas-upnp-org:device:MediaRenderer:1",
                        *(upnp.SERVICES[k][0] for k in self.service_keys()),
                    ],
                )
            ],
            host=self.ctx.options.get("ssdp_host", "127.0.0.1"),
            port=int(self.ctx.options.get("ssdp_port", 0)),
            rec=None,
        )
        await self.ssdp.start()
        self.rec.log("upnp", udn=self.udn, description=f"{self.base_url}/description.xml")

    async def on_stop(self) -> None:
        for timer in (self._fake_timer, self._linger_timer):
            if timer is not None:
                timer.cancel()
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.gena.close()
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self.ssdp is not None:
            await self.ssdp.stop()

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._http

    def status(self) -> dict[str, Any]:
        return {
            "udn": self.udn,
            "volume": self.volume,
            "muted": self.muted,
            "reported_state": proto.UPNP_STATE[self.lens.state()],
            "fake_play": self.fake_play is not None,
            "pending_seek": self.pending_seek,
            "next_uri": self.reported_next[0],
            "stream": {
                "content_type": self.stream_ctype,
                "chunked": self.stream_chunked,
                "content_length": self.stream_length,
            },
            "range_probes": self.range_probes,
            "subscriptions": len(self.gena.subs),
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "local_volume":
            self.set_volume(int(payload["level"]), via="local")
            return self.volume
        if name == "drop_subscriptions":
            n = len(self.gena.subs)
            self.gena.subs.clear()
            return n
        raise KeyError(name)

    # -- truth -> events ------------------------------------------------------------------

    def _on_sink(self, name: str, data: dict[str, Any]) -> None:
        if name == "opened" and not data.get("part"):
            self.stream_ctype = data.get("content_type")
            self.stream_chunked = bool(data.get("chunked"))
            self.stream_length = data.get("content_length")

    def _on_transport(self, name: str, data: dict[str, Any]) -> None:
        if name == "advanced":
            self.advancing = True
            self.reported_next = ("", "")
            if self.knobs.get("track_gap_state") == "stopped":
                self.rec.knob("fired", knob="track_gap_state", quirks=["dsh:7"], value="stopped")
        if name == "track_started" and self.knobs.get("openhome"):
            self.oh.on_track()
        if name == "state":
            new = data["new"]
            if new == TState.PLAYING:
                self.advancing = False
                if self.volume_restore is not None:
                    level, self.volume_restore = self.volume_restore, None
                    self.rec.event("volume_reverted", level=level, quirk="ma:#6762")
                    self.set_volume(level, via="amp")
            if new in (TState.STOPPED, TState.NO_MEDIA):
                self.advancing = False
            self.avt_changed()
            if new == TState.PAUSED and self.knobs.fires("pause_emits_empty_uri_event"):
                self._spawn(self._empty_uri_event())

    async def _empty_uri_event(self) -> None:
        await self.clock.sleep(0.2)
        fields = [
            ("TransportState", {"val": "PAUSED_PLAYBACK"}),
            ("CurrentTrackURI", {"val": ""}),
            ("AVTransportURI", {"val": ""}),
            ("CurrentTransportActions", {"val": "Play,Stop"}),
        ]
        body = upnp.last_change(upnp.EVENT_NS["avt"], fields)
        self.gena.notify("avt", {"LastChange": body}, dedupe=False)

    def avt_changed(self) -> None:
        if self.clock.monotonic() < self._avt_quiet_until:
            self.rec.event("avt_event_suppressed", quirk="dsh:14")
            return
        body = self._avt_last_change()
        if self.knobs.fires("lastchange_malformed"):
            body = body[: len(body) * 2 // 3]
        props = {"LastChange": body}
        if self.knobs.get("duplicate_lastchange_and_vars"):
            state = self.lens.state()
            props.update(
                TransportState=proto.UPNP_STATE[state],
                CurrentTransportActions=self.actions(),
            )
        self.gena.notify("avt", props)
        if self.knobs.get("openhome"):
            self.oh.changed()

    def rc_changed(self) -> None:
        if self.knobs.get("no_rc_events"):
            self.rec.knob("fired", knob="no_rc_events", quirks=["ma:dlna#3474"])
        else:
            self.gena.notify("rc", {"LastChange": self._rc_last_change()}, dedupe=False)
        if self.knobs.get("openhome"):
            self.oh.changed("ohvolume")

    def _rc_last_change(self) -> str:
        ns_key = "avt" if self.knobs.get("rc_lastchange_namespace") == "avt" else "rc"
        return upnp.last_change(
            upnp.EVENT_NS[ns_key],
            [
                ("Volume", {"channel": "Master", "val": str(self.volume)}),
                ("Mute", {"channel": "Master", "val": "1" if self.muted else "0"}),
            ],
        )

    def _didl_out(self, meta: str) -> str:
        if meta and self.knobs.get("didl_malformed"):
            return meta.replace("</DIDL-Lite>", "")  # dsh:22: never closed
        return meta

    def reported_uri(self) -> tuple[str, str]:
        """(uri, DIDL-Lite) the device reports for the current item."""
        slot = self.lens.metadata()
        if slot is None:
            return "", ""
        meta = slot.meta or proto.didl(
            slot.uri, slot.title, slot.duration_s, slot.extra.get("mime")
        )
        return slot.uri, self._didl_out(meta)

    def accurate_actions(self, state: TState) -> str:
        if state == TState.NO_MEDIA:
            return ""
        if state == TState.PLAYING:
            return "Pause,Stop,Seek,Next,Previous"
        if state == TState.PAUSED:
            return "Play,Stop,Seek,Next,Previous"
        if state in (TState.LOADING, TState.BUFFERING):
            return "Stop"
        return "Play,Stop,Seek"

    def actions(self) -> str:
        mode = self.knobs.get("transport_actions")
        if mode == "empty":
            return ""
        if mode == "stale" and self.frozen_actions is not None:
            return self.frozen_actions
        return self.accurate_actions(self.lens.state())

    def _avt_last_change(self) -> str:
        state = self.lens.state()
        uri, meta = self.reported_uri()
        dur = proto.hms(self.lens.duration())
        nxt, nxt_meta = self.reported_next
        fields = [
            ("TransportState", {"val": proto.UPNP_STATE[state]}),
            ("TransportStatus", {"val": "OK"}),
            ("TransportPlaySpeed", {"val": "1"}),
            ("CurrentPlayMode", {"val": "NORMAL"}),
            ("CurrentTransportActions", {"val": self.actions()}),
            ("PlaybackStorageMedium", {"val": "NETWORK" if uri else "NONE"}),
            ("NumberOfTracks", {"val": "1" if uri else "0"}),
            ("CurrentTrack", {"val": "1" if uri else "0"}),
            ("AVTransportURI", {"val": uri}),
            ("AVTransportURIMetaData", {"val": meta}),
            ("CurrentTrackURI", {"val": uri}),
            ("CurrentTrackMetaData", {"val": meta}),
            ("CurrentTrackDuration", {"val": dur}),
            ("CurrentMediaDuration", {"val": dur}),
            ("NextAVTransportURI", {"val": nxt}),
            ("NextAVTransportURIMetaData", {"val": self._didl_out(nxt_meta)}),
        ]
        return upnp.last_change(upnp.EVENT_NS["avt"], fields)

    def sink_protocols(self) -> str:
        if self.knobs.get("protocolinfo_empty_entries"):
            mid = len(upnp.SINK_PROTOCOLS) // 2
            return ",".join([*upnp.SINK_PROTOCOLS[:mid], "", *upnp.SINK_PROTOCOLS[mid:], ""])
        return ",".join(upnp.SINK_PROTOCOLS)

    def _full_state(self, service: str) -> dict[str, str]:
        if service == "avt":
            return {"LastChange": self._avt_last_change()}
        if service == "rc":
            return {"LastChange": self._rc_last_change()}
        if service == "cm":
            return {
                "SourceProtocolInfo": "",
                "SinkProtocolInfo": self.sink_protocols(),
                "CurrentConnectionIDs": "0",
            }
        return self.oh.full_state(service)

    # -- shared commands ------------------------------------------------------------

    def set_volume(self, level: int, via: str) -> None:
        self.volume = max(0, min(self.volume_max, level))
        self.rec.state("volume", level=self.volume, via=via)
        self.rc_changed()

    def set_mute(self, muted: bool, via: str) -> None:
        self.muted = muted
        self.rec.state("mute", muted=muted, via=via)
        self.rc_changed()

    def _end_fake(self, reason: str) -> None:
        if self.fake_play is None:
            return
        pos = self.fake_play.position(self.clock.monotonic())
        self.fake_play = None
        if self._fake_timer is not None:
            self._fake_timer.cancel()
            self._fake_timer = None
        self.rec.event("fake_play_end", reason=reason, reported_position_s=round(pos, 3))
        self.avt_changed()

    def _arm_fake_timer(self) -> None:
        fp = self.fake_play
        if self._fake_timer is not None:
            self._fake_timer.cancel()
            self._fake_timer = None
        if fp is None or fp.paused_at is not None or not fp.duration_s:
            return
        left = max(0.0, fp.duration_s - fp.position(self.clock.monotonic()))
        self._fake_timer = self.clock.call_later(left, lambda: self._end_fake("eof"))

    def _clear_linger(self) -> None:
        if self._linger_timer is not None:
            self._linger_timer.cancel()
            self._linger_timer = None
        if self.linger is not None:
            self.linger = None
            self.avt_changed()

    async def _range_probe(self, uri: str) -> bool:
        """GET with the LUMIN Range sequence; True when every answer is 206."""
        session = await self._session()
        ok = True
        for rng in RANGE_PROBES:
            info: dict[str, Any] = {"range": rng}
            try:
                async with session.get(uri, headers={"Range": rng}) as resp:
                    await resp.content.read(4096)
                    info.update(
                        status=resp.status,
                        content_range=resp.headers.get("Content-Range"),
                        accept_ranges=resp.headers.get("Accept-Ranges"),
                    )
            except (aiohttp.ClientError, OSError, TimeoutError) as err:
                info.update(status=None, error=repr(err))
            self.range_probes.append(info)
            self.rec.event("range_probe", **info)
            if info.get("status") != 206:
                ok = False
                break
        return ok

    async def play_cmd(self) -> None:
        tm = self.transport
        k = self.knobs
        if self.fake_play is not None:
            if self.fake_play.paused_at is not None:
                now = self.clock.monotonic()
                self.fake_play.base = self.fake_play.position(now)
                self.fake_play.started = now
                self.fake_play.paused_at = None
                self._arm_fake_timer()
                self.avt_changed()
            return
        if tm.current is None:
            raise upnp.UpnpFault(701, "Transition not available")
        self._clear_linger()
        mode = k.get("range_required")
        idle = tm.state in (TState.STOPPED, TState.NO_MEDIA)
        if mode != "off" and idle and not await self._range_probe(tm.current.uri):
            if mode == "error":
                self.rec.knob(
                    "fired", knob="range_required", quirks=["dsh:28", "ma:#6775"], mode=mode
                )
                raise upnp.UpnpFault(716, "Resource not found")
            self.rec.knob(
                "fired",
                knob="range_required",
                quirks=["dsh:28", "ma:#6775"],
                mode=mode,
                uri=tm.current.uri,
            )
            self.fake_play = FakePlay(self.clock.monotonic(), tm.current.duration_s)
            self._arm_fake_timer()
            self.avt_changed()
            return
        pending, self.pending_seek = self.pending_seek, None
        if pending is not None:
            if tm.state not in (TState.PAUSED, *PLAYING_LIKE):
                await tm.play()
            await tm.seek(pending)
        else:
            await tm.play()
        cap = float(k.get("play_blocks_until_audio_s"))
        if cap > 0:
            await asyncio.sleep(0)
            t0 = self.clock.monotonic()
            while tm.state in (TState.LOADING, TState.BUFFERING):
                if self.clock.monotonic() - t0 >= cap:
                    break
                await self.clock.sleep(0.02)
            self.rec.knob(
                "fired",
                knob="play_blocks_until_audio_s",
                quirks=["dsh:3", "DL-05"],
                held_s=round(self.clock.monotonic() - t0, 3),
            )

    async def _later(self, delay: float, coro: Any) -> None:
        await self.clock.sleep(delay)
        await coro

    def _settle(self, coro: Any, knob_ctx: dict[str, Any]) -> Any:
        delay = float(self.knobs.get("skip_settle_delay_s"))
        if delay > 0:
            self.rec.knob("fired", knob="skip_settle_delay_s", quirks=["dsh:5"], **knob_ctx)
            self._spawn(self._later(delay, coro))
            return None
        return coro

    # -- HTTP handlers --------------------------------------------------------------

    async def _description(self, _req: web.Request) -> web.Response:
        pres = "/" if self.knobs.fires("relative_presentation_url") else f"{self.base_url}/"
        body = upnp.description(self.identity, self.udn, self.service_keys(), pres)
        return web.Response(text=body, content_type="text/xml", charset="utf-8")

    async def _scpd(self, req: web.Request) -> web.Response:
        key = req.path.split("/")[2]
        if key not in self.service_keys():
            raise web.HTTPNotFound()
        body = upnp.scpd(upnp.SERVICES[key][2], self.actions_for(key), self.var_spec(key))
        return web.Response(text=body, content_type="text/xml", charset="utf-8")

    async def _subscribe(self, req: web.Request) -> web.Response:
        key = req.path.split("/")[2]
        if key not in self.service_keys():
            raise web.HTTPNotFound()
        return await self.gena.subscribe(req, key)

    async def _unsubscribe(self, req: web.Request) -> web.Response:
        return await self.gena.unsubscribe(req, req.path.split("/")[2])

    def _fault(self, code: int, desc: str) -> web.Response:
        status = 200 if self.knobs.fires("soap_fault_http200", code=code) else 500
        return upnp.xml_response(
            upnp.fault_body(code, desc),
            status=status,
            non_utf8=bool(self.knobs.get("non_utf8_bodies")),
        )

    async def _control(self, req: web.Request) -> web.StreamResponse:
        key = req.path.split("/")[2]
        if key not in self.service_keys():
            raise web.HTTPNotFound()
        stype = upnp.SERVICES[key][0]
        action = req.headers.get("SOAPACTION", "").strip('"').split("#")[-1]
        try:
            args = upnp.soap_args(await req.text())
        except Exception:
            return self._fault(402, "Invalid Args")
        if action not in self.actions_for(key):
            return self._fault(401, "Invalid Action")
        if key in ("avt", "rc") and args.get("InstanceID", "0") != "0":
            return self._fault(718, "Invalid InstanceID")
        try:
            if key.startswith("oh"):
                out = await self.oh.handle(key, action, args)
            else:
                out = await getattr(self, f"_{key}_{action}")(args)
        except upnp.UpnpFault as fault:
            self.rec.resp("upnp_fault", action=action, code=fault.code)
            return self._fault(fault.code, fault.desc)
        body = upnp.soap_body(stype, action, out or {})
        if action == "GetMediaInfo" and self.knobs.fires("content_length_off_by_one"):
            return await self._short_body(req, body.encode("utf-8"))
        return upnp.xml_response(body, non_utf8=bool(self.knobs.get("non_utf8_bodies")))

    async def _short_body(self, req: web.Request, raw: bytes) -> web.StreamResponse:
        """dsh:35: Content-Length says one byte more than is sent, then the socket closes."""
        resp = web.StreamResponse(headers={"Content-Type": 'text/xml; charset="utf-8"'})
        resp.content_length = len(raw) + 1
        await resp.prepare(req)
        await resp.write(raw)
        if req.transport is not None:
            req.transport.close()
        return resp

    # -- AVTransport ------------------------------------------------------------------

    async def _avt_SetAVTransportURI(self, a: dict[str, str]) -> None:
        uri = a.get("CurrentURI", "")
        meta = a.get("CurrentURIMetaData", "")
        if not uri:
            raise upnp.UpnpFault(714, "Illegal MIME-type")
        mime = proto.didl_mime(meta)
        if self.knobs.get("protocolinfo_filter") and mime and mime != "*":
            accepted = {p.split(":")[2].split(";")[0].lower() for p in upnp.SINK_PROTOCOLS}
            if mime.split(";")[0].lower() not in accepted:
                self.rec.knob(
                    "fired", knob="protocolinfo_filter", quirks=["dsh:25", "DL-08"], mime=mime
                )
                raise upnp.UpnpFault(714, "Illegal MIME-type")
        if self.knobs.get("head_check_on_set_uri"):
            await self._head_check(uri)
        tm = self.transport
        linger_s = float(self.knobs.get("previous_media_lingers_s"))
        prev = tm.current
        if linger_s > 0 and prev is not None and tm.state in PLAYING_LIKE:
            self.linger = (prev, tm.state, tm.truth_position())
            if self._linger_timer is not None:
                self._linger_timer.cancel()
            self._linger_timer = self.clock.call_later(linger_s, self._clear_linger)
            self.rec.knob(
                "fired", knob="previous_media_lingers_s", quirks=["dsh:2"], previous=prev.uri
            )
        self._end_fake("new_uri")
        self.pending_seek = None
        self.reported_next = ("", "")
        await tm.set_next(None)
        fields = proto.didl_fields(meta)
        await tm.set_uri(
            uri,
            meta=meta,
            duration_s=proto.didl_duration(meta),
            title=fields["title"],
            mime=mime,
        )
        self.frozen_actions = self.accurate_actions(TState.STOPPED)
        if self.knobs.get("transport_actions") == "stale":
            self.rec.knob(
                "fired",
                knob="transport_actions",
                quirks=["dsh:1", "DL-06", "ma:#6425"],
                frozen=self.frozen_actions,
            )
        self.avt_changed()
        if self.knobs.fires("autoplay_on_set_uri", uri=uri):
            await self.play_cmd()

    async def _head_check(self, uri: str) -> None:
        session = await self._session()
        status: int | None = None
        try:
            async with session.head(uri, allow_redirects=True) as resp:
                status = resp.status
        except (aiohttp.ClientError, OSError, TimeoutError):
            status = None
        self.rec.event("head_check", uri=uri, status=status)
        if status is None or status >= 400:
            self.rec.knob("fired", knob="head_check_on_set_uri", quirks=["DL-07"], status=status)
            raise upnp.UpnpFault(716, "Resource not found")

    async def _avt_SetNextAVTransportURI(self, a: dict[str, str]) -> None:
        mode = self.knobs.get("set_next")
        quirks = ["dsh:10", "dsh:11", "dsh:12"]
        if mode == "fault_701":
            self.rec.knob("fired", knob="set_next", quirks=[*quirks, "ma:#5852"], mode=mode)
            raise upnp.UpnpFault(701, "Transition not available")
        if mode == "playing_only" and self.lens.state() != TState.PLAYING:
            self.rec.knob("fired", knob="set_next", quirks=quirks, mode=mode)
            raise upnp.UpnpFault(701, "Transition not available")
        uri = a.get("NextURI", "")
        meta = a.get("NextURIMetaData", "")
        self.reported_next = (uri, meta)
        if mode == "ignored":
            self.rec.knob("fired", knob="set_next", quirks=quirks, mode=mode, uri=uri)
        else:
            fields = proto.didl_fields(meta)
            await self.transport.set_next(
                uri or None,
                meta=meta,
                duration_s=proto.didl_duration(meta),
                title=fields["title"],
                mime=proto.didl_mime(meta),
            )
        self.avt_changed()

    async def _avt_Play(self, _a: dict[str, str]) -> None:
        await self.play_cmd()
        delay = float(self.knobs.get("play_response_delay_s"))
        if delay > 0:
            await self.clock.sleep(delay)

    async def _avt_Pause(self, _a: dict[str, str]) -> None:
        if self.fake_play is not None:
            if self.fake_play.paused_at is None:
                self.fake_play.paused_at = self.clock.monotonic()
                self._arm_fake_timer()
                self.avt_changed()
            return
        tm = self.transport
        if tm.state not in PLAYING_LIKE:
            if tm.state == TState.PAUSED:
                return
            raise upnp.UpnpFault(701, "Transition not available")
        if (self.stream_chunked or self.stream_length is None) and self.knobs.fires(
            "pause_needs_content_length", chunked=self.stream_chunked
        ):
            raise upnp.UpnpFault(701, "Transition not available")
        await tm.pause()

    async def _avt_Stop(self, _a: dict[str, str]) -> None:
        tm = self.transport
        idle = tm.state in (TState.STOPPED, TState.NO_MEDIA)
        quiet = self.fake_play is None and self.linger is None and idle
        if quiet and self.knobs.fires("stop_when_idle_701", state=tm.state.value):
            raise upnp.UpnpFault(701, "Transition not available")
        self._end_fake("stop")
        self._clear_linger()
        self.pending_seek = None
        await tm.stop()
        self.avt_changed()

    async def _avt_Next(self, _a: dict[str, str]) -> None:
        tm = self.transport
        if tm.next_slot is None:
            raise upnp.UpnpFault(701, "Transition not available")
        coro = self._settle(tm.next(), {"action": "Next"})
        if coro is not None:
            await coro

    async def _avt_Previous(self, _a: dict[str, str]) -> None:
        if self.transport.current is None:
            raise upnp.UpnpFault(701, "Transition not available")
        await self.transport.seek(0.0)

    async def _avt_Seek(self, a: dict[str, str]) -> None:
        unit = a.get("Unit", "")
        units = self.knobs.get("seek_units")
        if unit not in ("REL_TIME", "ABS_TIME", "TRACK_NR"):
            raise upnp.UpnpFault(710, "Seek mode not supported")
        if unit != "REL_TIME":
            if units == "rel_time_only":
                raise upnp.UpnpFault(710, "Seek mode not supported")
            if units == "rel_time_silent":
                self.rec.knob("fired", knob="seek_units", quirks=["dsh:13"], unit=unit)
                return
        tm = self.transport
        if tm.current is None and self.fake_play is None:
            raise upnp.UpnpFault(701, "Transition not available")
        if unit == "TRACK_NR":
            if a.get("Target", "").strip() != "1":
                raise upnp.UpnpFault(711, "Illegal seek target")
            target: float | None = 0.0
        else:
            target = proto.parse_hms(a.get("Target", ""))
        dur = self.lens.duration()
        if target is None or (dur and target > dur):
            raise upnp.UpnpFault(711, "Illegal seek target")
        if not self.knobs.get("seek_emits_event"):
            self._avt_quiet_until = self.clock.monotonic() + SEEK_EVENT_MUTE_S
            self.rec.knob("fired", knob="seek_emits_event", quirks=["dsh:14"], value=False)
        if self.fake_play is not None:
            self.fake_play.base = target
            self.fake_play.started = self.clock.monotonic()
            if self.fake_play.paused_at is not None:
                self.fake_play.paused_at = self.fake_play.started
            self._arm_fake_timer()
            return
        if tm.state in (TState.PAUSED, TState.STOPPED):
            mode = self.knobs.get("seek_when_paused") if tm.state == TState.PAUSED else "ok"
            if mode == "error":
                self.rec.knob("fired", knob="seek_when_paused", quirks=["dsh:14"], mode=mode)
                raise upnp.UpnpFault(701, "Transition not available")
            if mode == "ok":
                self.pending_seek = target
                self.avt_changed()
                return
            self.rec.knob("fired", knob="seek_when_paused", quirks=["dsh:14"], mode=mode)
        coro = self._settle(tm.seek(target), {"action": "Seek", "target": target})
        if coro is not None:
            await coro

    async def _avt_GetTransportInfo(self, _a: dict[str, str]) -> dict[str, str]:
        return {
            "CurrentTransportState": proto.UPNP_STATE[self.lens.state()],
            "CurrentTransportStatus": "OK",
            "CurrentSpeed": "1",
        }

    def _rel_time(self) -> str:
        mode = self.knobs.get("reltime")
        if mode == "not_implemented":
            return proto.NOT_IMPLEMENTED
        if mode == "stuck":
            return "0:00:00"
        return proto.hms(self.lens.position())

    async def _avt_GetPositionInfo(self, _a: dict[str, str]) -> dict[str, str]:
        uri, meta = self.reported_uri()
        rel = self._rel_time()
        abs_time = proto.NOT_IMPLEMENTED if self.knobs.get("abs_time_not_implemented") else rel
        return {
            "Track": "1" if uri else "0",
            "TrackDuration": proto.hms(self.lens.duration()),
            "TrackMetaData": meta,
            "TrackURI": uri,
            "RelTime": rel,
            "AbsTime": abs_time,
            "RelCount": "2147483647",
            "AbsCount": "2147483647",
        }

    async def _avt_GetMediaInfo(self, _a: dict[str, str]) -> dict[str, str]:
        uri, meta = self.reported_uri()
        nxt, nxt_meta = self.reported_next
        return {
            "NrTracks": "1" if uri else "0",
            "MediaDuration": proto.hms(self.lens.duration()),
            "CurrentURI": uri,
            "CurrentURIMetaData": meta,
            "NextURI": nxt,
            "NextURIMetaData": self._didl_out(nxt_meta),
            "PlayMedium": "NETWORK" if uri else "NONE",
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }

    async def _avt_GetTransportSettings(self, _a: dict[str, str]) -> dict[str, str]:
        return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}

    async def _avt_GetCurrentTransportActions(self, _a: dict[str, str]) -> dict[str, str]:
        return {"Actions": self.actions()}

    # -- RenderingControl -----------------------------------------------------------------

    @staticmethod
    def _channel(a: dict[str, str]) -> None:
        if a.get("Channel", "Master") != "Master":
            raise upnp.UpnpFault(402, "Invalid Args")

    async def _rc_ListPresets(self, _a: dict[str, str]) -> dict[str, str]:
        return {"CurrentPresetNameList": "FactoryDefaults"}

    async def _rc_SelectPreset(self, a: dict[str, str]) -> None:
        if a.get("PresetName") != "FactoryDefaults":
            raise upnp.UpnpFault(701, "Invalid Name")

    async def _rc_GetVolume(self, a: dict[str, str]) -> dict[str, str]:
        self._channel(a)
        return {"CurrentVolume": str(self.volume)}

    async def _rc_SetVolume(self, a: dict[str, str]) -> None:
        self._channel(a)
        try:
            level = int(a.get("DesiredVolume", ""))
        except ValueError:
            raise upnp.UpnpFault(402, "Invalid Args") from None
        if not 0 <= level <= self.volume_max:
            raise upnp.UpnpFault(402, "Invalid Args")
        if (
            self.transport.state != TState.PLAYING
            and self.knobs.fires(
                "volume_before_play_ignored", level=level, state=self.transport.state.value
            )
            and self.volume_restore is None
        ):
            self.volume_restore = self.volume
        self.set_volume(level, via="upnp")

    async def _rc_GetMute(self, a: dict[str, str]) -> dict[str, str]:
        self._channel(a)
        return {"CurrentMute": "1" if self.muted else "0"}

    async def _rc_SetMute(self, a: dict[str, str]) -> None:
        self._channel(a)
        self.set_mute(a.get("DesiredMute", "0").lower() in ("1", "true", "yes"), via="upnp")

    # -- ConnectionManager --------------------------------------------------------------

    async def _cm_GetProtocolInfo(self, _a: dict[str, str]) -> dict[str, str]:
        if self.knobs.get("protocolinfo_empty_entries"):
            self.rec.knob("fired", knob="protocolinfo_empty_entries", quirks=["dsh:26"])
        return {"Source": "", "Sink": self.sink_protocols()}

    async def _cm_GetCurrentConnectionIDs(self, _a: dict[str, str]) -> dict[str, str]:
        return {"ConnectionIDs": "0"}

    async def _cm_GetCurrentConnectionInfo(self, a: dict[str, str]) -> dict[str, str]:
        if a.get("ConnectionID", "0") != "0":
            raise upnp.UpnpFault(706, "Invalid connection reference")
        return {
            "RcsID": "0",
            "AVTransportID": "0",
            "ProtocolInfo": "",
            "PeerConnectionManager": "",
            "PeerConnectionID": "-1",
            "Direction": "Input",
            "Status": "OK",
        }
