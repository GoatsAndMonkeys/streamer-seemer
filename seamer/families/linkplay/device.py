"""The LinkPlay / WiiM device: UPnP renderer + HTTP API, the way MA's wiim provider sees it.

Ports
-----
- main server (``ctx.port``, plain HTTP): ``/description.xml``, ``/upnp/<svc>/scpd.xml``,
  ``/upnp/<svc>/control`` (SOAP), ``/upnp/<svc>/event`` (GENA), ``/_sim``. On the real
  device this is the UPnP port (49152). ``/httpapi.asp`` here answers like the wrong
  scheme does (``wrong_scheme_reply``, L57).
- HTTPS API listener (``https_port``, self-signed; mutual TLS with
  ``client_cert_required``) and/or plain HTTP API listener (``http_port``), chosen by
  ``http_scheme`` (lq:Q1). URLs: :attr:`LinkPlayDevice.api_urls`.

Sim actions (``POST /_sim/action/<name>``)
------------------------------------------
- ``switch_source {source}``: hdmi, line-in, optical, bluetooth, phono, aux (physical,
  via the CEC-style STOPPED gap) or airplay, spotify, cast, qobuz, tidal, app (streaming).
- ``app_takeover {title?}``: the WiiM app starts its own queue (lq:Q21).
- ``reboot``: restart the boot clock (``boot_status_none_s``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ssl
from typing import Any, ClassVar

from aiohttp import web

from ...simkit.device import SimDevice
from ...simkit.kits.discovery import SsdpResponder, SsdpTarget
from ...simkit.transport import MediaSlot, ReportLens, TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)
from . import proto, upnp
from .certs import certs
from .httpapi import HttpApi

# uuid (24 hex) -> running device, for multiroom JoinGroup between sims in one process
REGISTRY: dict[str, LinkPlayDevice] = {}


class LinkPlayLens(ReportLens):
    """Truth -> what a LinkPlay unit reports, on top of the generic report_* knobs."""

    dev: LinkPlayDevice

    def position(self) -> float | None:
        truth = self.tm.truth_position()
        pos = super().position()
        d = self.dev
        now = self.tm.clock.monotonic()
        loading = self.tm.state in (TState.LOADING, TState.BUFFERING)
        if d.stale_value is not None and loading and now < d.stale_until:
            return self._differs("position", truth, d.stale_value)  # lq:Q17, MA#6867
        dur = self.duration()
        if now < d.end_pos_until and dur:
            return self._differs("position", truth, dur)  # lq:Q16, MA#6839
        if d.masquerade is not None:
            return self._differs("position", truth, d.masquerade_pos)
        return pos

    def duration(self) -> float | None:
        d = self.dev
        if d.masquerade is not None:
            return d.masquerade.duration_s
        if self.tm.owner in proto.PHYSICAL_INPUTS:
            if d.knobs.get("stale_metadata_after_switch") and d.last_ma_slot is not None:
                return d.last_ma_slot.duration_s  # lq:Q23
            return None
        return super().duration()

    def state(self) -> TState:
        d = self.dev
        now = self.tm.clock.monotonic()
        if now < d.false_playing_until and self.tm.current is None:
            return self._differs("state", self.tm.state, TState.PLAYING)  # lq:Q18
        if d.masquerade is not None:
            return self._differs("state", self.tm.state, TState.PAUSED)  # lq:Q21
        return self.tm.state

    def metadata(self) -> MediaSlot | None:
        d = self.dev
        if d.masquerade is not None:
            return d.masquerade
        if (
            self.tm.owner in proto.PHYSICAL_INPUTS
            and d.knobs.get("stale_metadata_after_switch")
            and d.last_ma_slot is not None
        ):
            return d.last_ma_slot
        return self.tm.current


class LinkPlayDevice(SimDevice):
    family: ClassVar[str] = "linkplay"
    lens_cls = LinkPlayLens
    lens: LinkPlayLens

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.lens.dev = self
        ident = self.identity
        base = ident.get("uuid", "FF31F09E1A5020113B0A3918").upper()
        # unique per device id so several sims of one profile can group
        salt = hashlib.sha1(self.id.encode()).hexdigest()[:4].upper()
        self.uuid = base[:20] + salt
        self.udn = "uuid:" + proto.upnp_uuid(self.uuid)
        self.volume = int(ident.get("volume", "30"))
        self.muted = False
        self.loop_mode = 4
        self.output_index = 0
        self.http = HttpApi(self)
        self.gena = upnp.Gena(self.clock, self.rec.child("gena"), self.knobs, self._full_state)
        self.api_servers: dict[str, Any] = {}
        self.ssdp: SsdpResponder | None = None
        self.booted_at = self.clock.monotonic()
        # quirk state
        self.stale_value: float | None = None
        self.stale_until = 0.0
        self.end_pos_until = 0.0
        self.false_playing_until = 0.0
        self.switchmode_empty_until = 0.0
        self.masquerade: MediaSlot | None = None
        self.masquerade_pos = 0.0
        self.last_ma_slot: MediaSlot | None = None
        self.pending_new_uri = False
        self._avt_muted = False
        self._avt_hold_until = 0.0
        self._avt_flush: Any = None
        self._commanded_advance = False
        self._suppress_medium = False
        self._tasks: set[asyncio.Task[Any]] = set()
        # multiroom
        self.leader: LinkPlayDevice | None = None
        self.followers: list[LinkPlayDevice] = []
        self.transport.add_listener(self._on_transport)

    # -- identity --------------------------------------------------------------------

    @property
    def ip(self) -> str:
        return self.identity.get("ip", "192.0.2.10")

    @property
    def source(self) -> str:
        owner = self.transport.owner
        return owner if owner in proto.SOURCES else "ma"

    @property
    def api_url(self) -> str:
        """The preferred HTTP API base URL (https when served)."""
        return self.api_urls.get("https") or self.api_urls["http"]

    @property
    def api_urls(self) -> dict[str, str]:
        return {k: s.base_url for k, s in self.api_servers.items()}

    # -- lifecycle ---------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app()
        app.router.add_get("/description.xml", self._description)
        app.router.add_get("/httpapi.asp", self._wrong_scheme)
        for key in upnp.SERVICES:
            app.router.add_get(f"/upnp/{key}/scpd.xml", self._scpd)
            app.router.add_post(f"/upnp/{key}/control", self._control)
            app.router.add_route("SUBSCRIBE", f"/upnp/{key}/event", self._subscribe)
            app.router.add_route("UNSUBSCRIBE", f"/upnp/{key}/event", self._unsubscribe)
        return app

    def _api_app(self) -> web.Application:
        app = self.make_app()
        app.router.add_get("/httpapi.asp", self.http.handle)
        return app

    async def on_start(self) -> None:
        scheme = self.knobs.get("http_scheme")
        if scheme in ("https_only", "both"):
            ctx = certs().server_context(bool(self.knobs.get("client_cert_required")))
            self.api_servers["https"] = await self.serve(
                self._api_app(), int(self.knobs.get("https_port")), ssl_context=ctx
            )
        if scheme in ("http_only", "both"):
            self.api_servers["http"] = await self.serve(
                self._api_app(), int(self.knobs.get("http_port"))
            )
        # SSDP answers M-SEARCH on loopback; ctx.options["ssdp_port"] (default: ephemeral)
        self.ssdp = SsdpResponder(
            [
                SsdpTarget(
                    f"{self.base_url}/description.xml",
                    self.udn,
                    [
                        "urn:schemas-upnp-org:device:MediaRenderer:1",
                        *(upnp.SERVICES[k][0] for k in self._service_keys(True)),
                    ],
                )
            ],
            host=self.ctx.options.get("ssdp_host", "127.0.0.1"),
            port=int(self.ctx.options.get("ssdp_port", 0)),
            rec=self.rec.child("ssdp"),
        )
        await self.ssdp.start()
        self.booted_at = self.clock.monotonic()
        REGISTRY[self.uuid] = self
        self.rec.log("api", urls=self.api_urls, udn=self.udn)

    async def on_stop(self) -> None:
        REGISTRY.pop(self.uuid, None)
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.gena.close()
        if self.ssdp is not None:
            await self.ssdp.stop()

    def client_ssl_context(self, with_cert: bool = True) -> ssl.SSLContext:
        """A client context for tests (skips verification; mTLS cert when asked)."""
        return certs().client_context(with_cert)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- truth -> events ------------------------------------------------------------------

    def _on_transport(self, name: str, data: dict[str, Any]) -> None:
        now = self.clock.monotonic()
        if name == "state" and data["new"] == TState.PLAYING:
            self.stale_value = None
            for f in self.followers:
                f.mirror_changed()
        if name == "track_started" and self.transport.owner == "ma":
            self.pending_new_uri = False
            end_s = float(self.knobs.get("end_position_after_load_s"))
            if end_s > 0:
                self.end_pos_until = now + end_s
                self.rec.knob("fired", knob="end_position_after_load_s", quirks=["lq:Q16"])
        if name == "advanced":
            if self._commanded_advance:
                self._commanded_advance = False
            elif self.knobs.fires("drop_advance_event", uri=data.get("uri")):
                self._avt_muted = True
            else:
                hold = float(self.knobs.get("advance_event_delay_s"))
                if hold > 0:
                    self._avt_hold_until = now + hold
                    self.rec.knob("fired", knob="advance_event_delay_s", quirks=["lq:Q35"])
        if name in ("state", "advanced"):
            self.avt_changed()

    def clear_event_mute(self) -> None:
        """Any client command makes a muted AVTransport speak again (drop_advance_event)."""
        if self._avt_muted:
            self._avt_muted = False
            self.avt_changed()

    def avt_changed(self) -> None:
        if self.knobs.get("no_state_events"):
            return  # LP-07: only the initial NOTIFY
        if self._avt_muted:
            return
        now = self.clock.monotonic()
        if now < self._avt_hold_until:
            if self._avt_flush is None:
                self._avt_flush = self.clock.call_later(self._avt_hold_until - now, self._flush_avt)
            return
        self.gena.notify("avt", {"LastChange": self._avt_last_change()})

    def _flush_avt(self) -> None:
        self._avt_flush = None
        self.avt_changed()

    def rc_changed(self, slave: tuple[str, str] | None = None) -> None:
        fields = [
            ("Volume", {"channel": "Master", "val": str(self.volume)}),
            ("Mute", {"channel": "Master", "val": "1" if self.muted else "0"}),
        ]
        if slave is not None:
            fields.append(("Slave", {"action": slave[0], "val": slave[1]}))
        self.gena.notify("rc", {"LastChange": upnp.last_change("rc", fields)}, dedupe=False)

    def _pq_last_change(self) -> str:
        tag = self.knobs.get("playqueue_loopmode_tag")
        return upnp.last_change("pq", [(tag, {"val": str(self.loop_mode)})], inst="QueueID")

    def _reported_uri(self) -> tuple[str, str]:
        """(uri, DIDL) as the device reports them."""
        slot = self.lens.metadata()
        if slot is None:
            return "", ""
        if slot.owner in proto.PHYSICAL_INPUTS:
            return "", ""
        if slot.owner != "ma" and not slot.uri.startswith("http"):
            uri = "wiimu_airplay" if slot.owner == "airplay" else slot.uri
            return uri, proto.didl(uri, slot.title)
        title, artist, album = proto.didl_title(slot.meta)
        return slot.uri, proto.didl(
            slot.uri,
            title or slot.title or self.knobs.get("idle_unknown_sentinel"),
            artist,
            album,
            slot.duration_s,
            art=slot.extra.get("art") or self.knobs.get("artwork_sentinel"),
        )

    def _actions(self, state: TState) -> str:
        if state == TState.PLAYING:
            return "Pause,Stop,Seek,Next,Previous"
        if state == TState.PAUSED:
            return "Play,Stop,Seek,Next,Previous"
        return "Play,Stop,Seek,Next,Previous" if self.transport.current else "Play"

    def _avt_last_change(self) -> str:
        state = self.lens.state()
        fields = [
            ("TransportState", {"val": proto.UPNP_STATE[state]}),
            ("CurrentTransportActions", {"val": self._actions(state)}),
        ]
        if self._suppress_medium and self.source in proto.PHYSICAL_INPUTS:
            # lq:Q24 / MA#6897: the input switch moves TransportState only
            return upnp.last_change("avt", fields)
        uri, meta = self._reported_uri()
        dur = self.lens.duration()
        fields += [
            ("PlaybackStorageMedium", {"val": proto.SOURCES[self.source][1]}),
            ("AVTransportURI", {"val": uri}),
            ("CurrentTrackURI", {"val": uri}),
            ("CurrentTrackDuration", {"val": proto.hms(dur)}),
            ("AVTransportURIMetaData", {"val": meta}),
            ("CurrentTrackMetaData", {"val": meta}),
            ("NumberOfTracks", {"val": "1" if uri else "0"}),
            ("CurrentTrack", {"val": "1" if uri else "0"}),
        ]
        return upnp.last_change("avt", fields)

    def _full_state(self, service: str) -> dict[str, str]:
        if service == "avt":
            return {"LastChange": self._avt_last_change()}
        if service == "rc":
            return {
                "LastChange": upnp.last_change(
                    "rc",
                    [
                        ("Volume", {"channel": "Master", "val": str(self.volume)}),
                        ("Mute", {"channel": "Master", "val": "1" if self.muted else "0"}),
                    ],
                )
            }
        if service == "pq":
            return {"LastChange": self._pq_last_change()}
        return {
            "SourceProtocolInfo": "",
            "SinkProtocolInfo": upnp.SINK_PROTOCOLS,
            "CurrentConnectionIDs": "0",
        }

    # -- shared commands (UPnP + HTTP) ------------------------------------------------------

    async def load_and_play(
        self, uri: str, meta: str = "", via: str = "upnp", duration_s: float | None = None
    ) -> None:
        await self.set_uri(uri, meta, duration_s)
        await self.play_cmd()

    async def set_uri(self, uri: str, meta: str, duration_s: float | None = None) -> None:
        tm = self.transport
        prev = tm.truth_position() if tm.current is not None else None
        self._suppress_medium = False
        self.masquerade = None
        title, _artist, _album = proto.didl_title(meta)
        if duration_s is None:
            duration_s = _didl_duration(meta)
        await tm.set_uri(uri, meta=meta, duration_s=duration_s, title=title)
        self.last_ma_slot = tm.current
        self.pending_new_uri = True
        self._prev_stream_pos = prev

    async def play_cmd(self) -> None:
        tm = self.transport
        now = self.clock.monotonic()
        if tm.current is None:
            window = float(self.knobs.get("false_playing_without_uri_s"))
            if window > 0:
                self.false_playing_until = now + window
                self.rec.knob("fired", knob="false_playing_without_uri_s", quirks=["lq:Q18"])
                self.avt_changed()
            await tm.play()
            return
        if not self.pending_new_uri:
            await tm.play()
            return
        delay = float(self.knobs.get("play_start_delay_s"))
        prev = getattr(self, "_prev_stream_pos", None)
        if prev is not None and self.knobs.fires("stale_prev_position", previous_s=prev):
            window = float(self.knobs.param("stale_prev_position", "window_s", 1.0))
            self.stale_value = prev
            self.stale_until = now + delay + window
        if delay > 0:

            async def _later() -> None:
                await self.clock.sleep(delay)
                await tm.play()

            self._spawn(_later())
        else:
            await tm.play()

    async def resume(self) -> None:
        if self.masquerade is not None:
            return
        await self.transport.play()

    def set_volume(self, level: int, via: str) -> None:
        self.volume = max(0, min(100, level))
        self.rec.state("volume", level=self.volume, via=via)
        if via != "http" or self.knobs.get("echo_http_volume_events"):
            self.rc_changed()
        elif via == "http":
            self.rec.knob("fired", knob="echo_http_volume_events", quirks=["L64"], value=False)

    def set_mute(self, muted: bool, via: str) -> None:
        self.muted = muted
        self.rec.state("mute", muted=muted, via=via)
        if via != "http" or self.knobs.get("echo_http_volume_events"):
            self.rc_changed()

    def set_loop_mode(self, mode: int) -> None:
        self.loop_mode = mode
        self.gena.notify("pq", {"LastChange": self._pq_last_change()})

    async def switchmode(self, arg: str) -> str:
        key = proto.SWITCHMODE.get(arg.lower())
        if key is None:
            return str(self.knobs.get("failure_word"))
        noop = [s.strip().lower() for s in str(self.knobs.get("switchmode_silent_noop")).split(",")]
        if arg.lower() in noop:
            self.rec.knob("fired", knob="switchmode_silent_noop", quirks=["lq:Q30"], target=arg)
            return "OK"
        empty = float(self.knobs.get("switchmode_empty_body_s"))
        if empty > 0:
            self.switchmode_empty_until = self.clock.monotonic() + empty
        if key == "ma":
            await self.transport.stop()
            return "OK"
        await self.switch_source(key)
        return "OK"

    async def switch_source(self, source: str, title: str | None = None) -> None:
        if source not in proto.SOURCES or source == "ma":
            raise KeyError(source)
        tm = self.transport
        self.masquerade = None
        if source in proto.PHYSICAL_INPUTS:
            emit = not self.knobs.get("input_switch_without_upnp_event")
            self._suppress_medium = not emit
            if not emit:
                self.rec.knob("fired", knob="input_switch_without_upnp_event", quirks=["lq:Q24"])
            # the device leaves our stream, shows STOPPED, then plays the input (CEC/ARC)
            await tm.stop()
            tm.owner = source
            self.avt_changed()
            gap = float(self.knobs.get("input_switch_stop_s"))

            async def _then() -> None:
                if gap > 0:
                    await self.clock.sleep(gap)
                tm.take_over(source, emit_event=emit, title=title)
                self.avt_changed()

            self._spawn(_then())
            return
        self._suppress_medium = False
        tm.take_over(source, emit_event=True, title=title or source)
        self.avt_changed()

    # -- multiroom ---------------------------------------------------------------------

    def follower_by_ip(self, ip: str) -> LinkPlayDevice | None:
        return next((f for f in self.followers if f.ip == ip), None)

    async def join(self, uuid: str | None, ip: str | None) -> bool:
        leader = None
        if uuid:
            leader = REGISTRY.get(uuid.upper()[:24])
        if leader is None and ip:
            leader = next((d for d in REGISTRY.values() if d.ip == ip), None)
        if leader is None or leader is self:
            return False
        ver = self.knobs.get("wmrm_version").split(".")[0]
        if leader.knobs.get("wmrm_version").split(".")[0] != ver:
            self.rec.event("join_refused", reason="wmrm_version", quirk="lq:Q49")
            return True  # answers OK, never groups

        async def _apply() -> None:
            delay = float(self.knobs.get("role_propagation_delay_s"))
            if delay > 0:
                await self.clock.sleep(delay)
            await self.leave_group()
            first = not leader.followers
            self.leader = leader
            leader.followers.append(self)
            await self.transport.stop()
            self.rec.state("group", role="follower", leader=leader.id)
            leader.rec.state("group", role="leader", followers=[f.id for f in leader.followers])
            leader.rc_changed(slave=("add", self.uuid))
            ltm = leader.transport
            if (
                first
                and leader.knobs.fires("leader_restart_on_first_join")
                and ltm.state == TState.PLAYING
                and ltm.owner == "ma"
            ):
                await ltm.seek(0.0)  # re-fetch from the start (lq:Q46, L71)

        self._spawn(_apply())
        return True

    async def leave_group(self) -> None:
        leader, self.leader = self.leader, None
        if leader is not None and self in leader.followers:
            leader.followers.remove(self)
            leader.rc_changed(slave=("rm", self.uuid))
            self.rec.state("group", role="standalone")

    async def kick(self, ip: str) -> None:
        f = self.follower_by_ip(ip)
        if f is not None:
            await f.leave_group()

    async def ungroup(self) -> None:
        for f in list(self.followers):
            await f.leave_group()
        await self.leave_group()

    def mirror_changed(self) -> None:
        pass  # followers read the leader on demand

    def slave_list(self) -> dict[str, Any]:
        ver = self.knobs.get("wmrm_version")
        if self.knobs.get("slavelist_shape") == "legacy_caps_strings":
            return {
                "slaves": str(len(self.followers)),
                "slave_list": [
                    {
                        "Name": f.identity.get("name", f.id),
                        "UUID": f.uuid,
                        "IP": f.ip,
                        "Volume": str(f.volume),
                        "Mute": "1" if f.muted else "0",
                        "Channel": "0",
                    }
                    for f in self.followers
                ],
            }
        out: dict[str, Any] = {"slaves": len(self.followers), "wmrm_version": ver}
        if self.followers:
            out["slave_list"] = [
                {
                    "name": f.identity.get("name", f.id),
                    "uuid": f.uuid,
                    "ip": f.ip,
                    "version": f.knobs.get("wmrm_version"),
                    "type": "WiiMu-A31",
                    "channel": 0,
                    "volume": f.volume,
                    "mute": 1 if f.muted else 0,
                    "battery_percent": 0,
                    "battery_charging": 0,
                }
                for f in self.followers
            ]
        return out

    # -- HTTP status bodies -------------------------------------------------------------

    def _text(self, value: str | None, field: str) -> str:
        enc = self.knobs.get("metadata_encoding")
        text = value or self.knobs.get("idle_unknown_sentinel")
        if enc == "plain" or (enc == "mixed" and field != "Title"):
            return text if value or enc == "plain" else "unknown"
        return proto.hexenc(text)

    def _ms(self, seconds: float | None) -> str:
        mult = 1_000_000 if self.knobs.get("pos_units") == "us" else 1000
        return str(round((seconds or 0.0) * mult))

    def player_status(self) -> dict[str, Any]:
        if self.leader is not None and self.knobs.get("follower_status") == "mirror_leader":
            body = self.leader.player_status()
            body["type"] = "1"
            body["vol"], body["mute"] = str(self.volume), "1" if self.muted else "0"
            return body
        tm = self.transport
        now = self.clock.monotonic()
        booting = now - self.booted_at < float(self.knobs.get("boot_status_none_s"))
        state = self.lens.state()
        slot = self.lens.metadata()
        status = proto.HTTP_STATUS[state]
        mode, _medium, vendor = proto.SOURCES[self.source]
        if slot is None and self.source == "ma" and state == TState.NO_MEDIA:
            mode = "0"
        if (
            self.source in ("qobuz", "cast")
            and state == TState.PLAYING
            and self.knobs.get("status_stop_while_connect_playing")
        ):
            status = "stop"  # lq:Q20, L69
        if self.leader is not None:
            status, mode = "none", "99"
        if booting:
            status, mode = "none", "-1"  # L53
        title, artist, album = (None, None, None)
        if slot is not None:
            title, artist, album = proto.didl_title(slot.meta)
            title = title or slot.title
        pos = self.lens.position() or 0.0
        dur = self.lens.duration()
        curpos = self._ms(pos)
        if (
            state in (TState.LOADING, TState.BUFFERING)
            and tm.tracks_started >= 1  # a stream ran before this load
            and self.knobs.fires("curpos_negative_on_restart")
        ):
            curpos = "-" + self._ms(1.2)  # LP-03
        body: dict[str, Any] = {
            "type": "1" if self.leader is not None else "0",
            "ch": "0",
            "mode": mode,
            "loop": str(self.loop_mode),
            "eq": "0",
            "vendor": vendor,
            "status": status,
            "curpos": curpos,
            "offset_pts": curpos,
            "totlen": self._ms(dur),
            "Title": self._text(title, "Title"),
            "Artist": self._text(artist, "Artist"),
            "Album": self._text(album, "Album"),
            "alarmflag": "0",
            "plicount": "1" if slot is not None else "0",
            "plicurr": "1" if slot is not None else "0",
            "vol": str(self.volume),
            "mute": "1" if self.muted else "0",
        }
        if self.knobs.get("duplicate_vendor_key"):
            body["Vendor"] = "unknown"  # L51
        return body

    def status_ex(self) -> dict[str, Any]:
        i = self.identity
        uuid = self.uuid if self.knobs.get("uuid_form") == "compact24" else proto.full32(self.uuid)
        a31 = self.knobs.get("netfield_style") == "a31"
        body: dict[str, Any] = {
            "language": "en_us",
            "ssid": i.get("ssid", i.get("name", "WiiM")),
            "hideSSID": "1",
            "firmware": i.get("firmware", "Linkplay.5.0.000000"),
            "build": "release",
            "project": i.get("project", "WiiM_Pro_with_gc4a"),
            "priv_prj": i.get("priv_prj", i.get("project", "")),
            "Release": i.get("release", "20260901"),
            "FW_Release_version": "",
            "group": "1" if self.leader is not None else "0",
            "wmrm_version": self.knobs.get("wmrm_version"),
            "expired": "0",
            "internet": "1",
            "uuid": uuid,
            "MAC": i.get("mac", "02:00:5E:00:00:10"),
            "STA_MAC": "00:00:00:00:00:00",
            "date": "2026:10:28",
            "time": "12:00:00",
            "netstat": "2",
            "essid": proto.hexenc("seamer-test"),
            "apcli0": "" if a31 else self.ip,
            "eth0": "0.0.0.0",
            "hardware": i.get("hardware", "A98"),
            "VersionUpdate": "0",
            "NewVer": "0",
            "mcu_ver": i.get("mcu_ver", "0"),
            "dsp_ver": i.get("dsp_ver", ""),
            "streams_all": "0x7FFFFFFE",
            "streams": "0x7FFFFFFE",
            "region": "unknown",
            "volume_control": "0",
            "external": "0x0",
            "preset_key": "12",
            "plm_support": i.get("plm_support", "0x300006"),
            "lbc_support": "0",
            "WifiChannel": "6",
            "RSSI": "-40",
            "BSSID": "02:00:5E:00:00:01",
            "battery": "0",
            "battery_percent": "0",
            "securemode": "1",
            "auth": "WPAPSK",
            "encry": "AES",
            "upnp_version": "1005",
            "upnp_uuid": self.udn,
            "uart_pass_port": "8899",
            "communication_port": "8819",
            "web_firmware_update_hide": "0",
            "tidal_version": "2.0",
            "service_version": "1.0",
            "privacy_mode": "0",
            "DeviceName": i.get("name", "WiiM"),
            "GroupName": i.get("name", "WiiM"),
            "max_volume": "100",
        }
        if a31:
            body["security_version"] = "2.0"
        else:
            body["security"] = "https/2.0"
            body["security_version"] = "3.0"
            body["security_capabilities"] = {"ver": "1.0", "aes_ver": "1.0"}
        if self.leader is not None:
            body["master_uuid"] = self.leader.uuid
            body["master_ip"] = self.leader.ip
        return body

    # -- UPnP handlers --------------------------------------------------------------------

    def _service_keys(self, for_description: bool) -> list[str]:
        keys = ["avt", "rc", "cm"]
        pq = self.knobs.get("playqueue")
        if pq == "present" or (pq == "hidden" and not for_description):
            keys.append("pq")
        return keys

    async def _description(self, _req: web.Request) -> web.Response:
        body = upnp.description(self.identity, self.udn, self._service_keys(True))
        return web.Response(text=body, content_type="text/xml", charset="utf-8")

    def _actions_for(self, key: str) -> dict[str, Any]:
        actions = dict(upnp.SERVICES[key][3])
        if key == "avt" and not self.knobs.get("getinfoex_supported"):
            actions.pop("GetInfoEx", None)
        return actions

    async def _scpd(self, req: web.Request) -> web.Response:
        key = req.path.split("/")[2]
        if key not in self._service_keys(False):
            raise web.HTTPNotFound()
        body = upnp.scpd(upnp.SERVICES[key][2], self._actions_for(key))
        return web.Response(text=body, content_type="text/xml", charset="utf-8")

    async def _wrong_scheme(self, req: web.Request) -> web.StreamResponse:
        if self.knobs.get("wrong_scheme_reply") == "conn_reset":
            if req.transport is not None:
                req.transport.abort()
            raise web.HTTPServiceUnavailable()
        return web.Response(status=404, text="404: Not Found", content_type="text/plain")

    async def _subscribe(self, req: web.Request) -> web.Response:
        key = req.path.split("/")[2]
        if key not in self._service_keys(False):
            raise web.HTTPNotFound()
        return await self.gena.subscribe(req, key)

    async def _unsubscribe(self, req: web.Request) -> web.Response:
        return await self.gena.unsubscribe(req, req.path.split("/")[2])

    async def _control(self, req: web.Request) -> web.Response:
        key = req.path.split("/")[2]
        if key not in self._service_keys(False):
            raise web.HTTPNotFound()
        stype = upnp.SERVICES[key][0]
        action = req.headers.get("SOAPACTION", "").strip('"').split("#")[-1]
        try:
            args = upnp.soap_args(await req.text())
        except Exception:
            return upnp.soap_fault(402, "Invalid Args")
        if action not in self._actions_for(key):
            return upnp.soap_fault(401, "Invalid Action")
        self.clear_event_mute()
        try:
            out = await getattr(self, f"_{key}_{action}")(args)
        except upnp.UpnpFault as fault:
            self.rec.resp("upnp_fault", action=action, code=fault.code)
            return upnp.soap_fault(fault.code, fault.desc)
        return upnp.soap_response(stype, action, out or {})

    # AVTransport

    async def _avt_SetAVTransportURI(self, a: dict[str, str]) -> None:
        uri = a.get("CurrentURI", "")
        if not uri:
            raise upnp.UpnpFault(714, "Illegal MIME-type")
        await self.set_uri(uri, a.get("CurrentURIMetaData", ""))
        self.avt_changed()

    async def _avt_SetNextAVTransportURI(self, a: dict[str, str]) -> None:
        if not self.knobs.fires("set_next_supported"):
            self.rec.knob("fired", knob="set_next_supported", quirks=["LP-06"], value=False)
            raise upnp.UpnpFault(401, "Invalid Action")
        meta = a.get("NextURIMetaData", "")
        title, _a, _b = proto.didl_title(meta)
        await self.transport.set_next(
            a.get("NextURI") or None, meta=meta, duration_s=_didl_duration(meta), title=title
        )

    async def _avt_Play(self, _a: dict[str, str]) -> None:
        tm = self.transport
        if tm.state == TState.PAUSED and self.knobs.fires("resume_701"):
            raise upnp.UpnpFault(701, "Transition not available")  # LP-08
        if self.masquerade is not None:
            raise upnp.UpnpFault(701, "Transition not available")
        await self.play_cmd()

    async def _avt_Pause(self, _a: dict[str, str]) -> None:
        tm = self.transport
        if tm.state not in (TState.PLAYING, TState.BUFFERING, TState.LOADING):
            if self.knobs.get("pause_in_stop_fault"):
                raise upnp.UpnpFault(701, "Transition not available")  # L65
            return
        await tm.pause()

    async def _avt_Stop(self, _a: dict[str, str]) -> None:
        self.masquerade = None
        await self.transport.stop()
        self.avt_changed()

    async def _avt_Next(self, _a: dict[str, str]) -> None:
        self._commanded_advance = True
        if not await self.transport.next():
            self._commanded_advance = False
            raise upnp.UpnpFault(701, "Transition not available")

    async def _avt_Previous(self, _a: dict[str, str]) -> None:
        await self.transport.seek(0.0)

    async def _avt_Seek(self, a: dict[str, str]) -> None:
        if a.get("Unit") not in ("REL_TIME", "ABS_TIME"):
            raise upnp.UpnpFault(710, "Seek mode not supported")
        target = proto.parse_hms(a.get("Target", ""))
        if target is None:
            raise upnp.UpnpFault(711, "Illegal seek target")
        await self.transport.seek(target)

    async def _avt_GetTransportInfo(self, _a: dict[str, str]) -> dict[str, str]:
        return {
            "CurrentTransportState": proto.UPNP_STATE[self.lens.state()],
            "CurrentTransportStatus": "OK",
            "CurrentSpeed": "1",
        }

    async def _avt_GetPositionInfo(self, _a: dict[str, str]) -> dict[str, str]:
        uri, meta = self._reported_uri()
        return {
            "Track": "1" if uri else "0",
            "TrackDuration": proto.hms(self.lens.duration()),
            "TrackMetaData": meta,
            "TrackURI": uri,
            "RelTime": proto.hms(self.lens.position()),
            "AbsTime": proto.hms(self.lens.position()),
            "RelCount": "2147483647",
            "AbsCount": "2147483647",
        }

    async def _avt_GetMediaInfo(self, _a: dict[str, str]) -> dict[str, str]:
        uri, meta = self._reported_uri()
        nxt = self.transport.next_slot
        return {
            "NrTracks": "1" if uri else "0",
            "MediaDuration": proto.hms(self.lens.duration()),
            "CurrentURI": uri,
            "CurrentURIMetaData": meta,
            "NextURI": nxt.uri if nxt else "",
            "NextURIMetaData": nxt.meta if nxt else "",
            "PlayMedium": proto.SOURCES[self.source][1],
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }

    async def _avt_GetCurrentTransportActions(self, _a: dict[str, str]) -> dict[str, str]:
        return {"Actions": self._actions(self.lens.state())}

    async def _avt_GetInfoEx(self, _a: dict[str, str]) -> dict[str, str]:
        uri, meta = self._reported_uri()
        vendor = proto.SOURCES[self.source][2]
        return {
            "CurrentTransportState": proto.UPNP_STATE[self.lens.state()],
            "Track": "1" if uri else "0",
            "TrackDuration": proto.hms(self.lens.duration()),
            "TrackMetaData": meta,
            "TrackURI": uri,
            "RelTime": proto.hms(self.lens.position()),
            "PlayMedium": proto.SOURCES[self.source][1],
            "TrackSource": vendor,
        }

    # RenderingControl

    async def _rc_GetVolume(self, _a: dict[str, str]) -> dict[str, str]:
        return {"CurrentVolume": str(self.volume)}

    async def _rc_SetVolume(self, a: dict[str, str]) -> None:
        try:
            self.set_volume(int(a.get("DesiredVolume", "")), via="upnp")
        except ValueError:
            raise upnp.UpnpFault(402, "Invalid Args") from None

    async def _rc_GetMute(self, _a: dict[str, str]) -> dict[str, str]:
        return {"CurrentMute": "1" if self.muted else "0"}

    async def _rc_SetMute(self, a: dict[str, str]) -> None:
        self.set_mute(a.get("DesiredMute", "0").lower() in ("1", "true", "yes"), via="upnp")

    # ConnectionManager

    async def _cm_GetProtocolInfo(self, _a: dict[str, str]) -> dict[str, str]:
        return {"Source": "", "Sink": upnp.SINK_PROTOCOLS}

    async def _cm_GetCurrentConnectionIDs(self, _a: dict[str, str]) -> dict[str, str]:
        return {"ConnectionIDs": "0"}

    # PlayQueue

    def _queue_context(self) -> str:
        slot = self.transport.current if self.transport.owner == "ma" else None
        tracks = ""
        if slot is not None:
            tracks = (
                f"<Track1><URL>{_esc(slot.uri)}</URL><Metadata>{_esc(slot.meta)}</Metadata>"
                "<Source>OnlineMusic</Source><Id>1</Id></Track1>"
            )
        n = "1" if slot is not None else "0"
        return (
            '<?xml version="1.0"?><PlayList><ListName>CurrentQueue</ListName><ListInfo>'
            "<QueueVersion>2.0</QueueVersion><Radio>0</Radio><SourceName>Local</SourceName>"
            f"<PicUrl></PicUrl><TotalNumber>{n}</TotalNumber><TrackNumber>{n}</TrackNumber>"
            f"<LastPlayIndex>{n}</LastPlayIndex></ListInfo><Tracks>{tracks}</Tracks></PlayList>"
        )

    async def _pq_BrowseQueue(self, _a: dict[str, str]) -> dict[str, str]:
        return {"QueueContext": self._queue_context()}

    async def _pq_PlayQueueWithIndex(self, a: dict[str, str]) -> None:
        if self.transport.current is None:
            raise upnp.UpnpFault(701, "Transition not available")
        await self.transport.seek(0.0)
        await self.transport.play()

    async def _pq_SetQueueLoopMode(self, a: dict[str, str]) -> None:
        try:
            self.set_loop_mode(int(a.get("LoopMode", "")))
        except ValueError:
            raise upnp.UpnpFault(402, "Invalid Args") from None

    async def _pq_GetQueueLoopMode(self, _a: dict[str, str]) -> dict[str, str]:
        return {"LoopMode": str(self.loop_mode)}

    async def _pq_GetKeyMapping(self, _a: dict[str, str]) -> dict[str, str]:
        return {
            "QueueContext": '<?xml version="1.0"?><KeyList><ListName>KeyList</ListName>'
            "<MaxNumber>12</MaxNumber></KeyList>"
        }

    # -- sim hooks -------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "udn": self.udn,
            "uuid": self.uuid,
            "api_urls": self.api_urls,
            "source": self.source,
            "volume": self.volume,
            "muted": self.muted,
            "loop_mode": self.loop_mode,
            "reported_state": proto.UPNP_STATE[self.lens.state()],
            "leader": self.leader.id if self.leader else None,
            "followers": [f.id for f in self.followers],
            "subscriptions": len(self.gena.subs),
            "ssdp_port": self.ssdp.port if self.ssdp else None,
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "switch_source":
            await self.switch_source(str(payload.get("source", "")), payload.get("title"))
            return self.source
        if name == "app_takeover":
            return await self.app_takeover(payload.get("title"))
        if name == "reboot":
            self.booted_at = self.clock.monotonic()
            return True
        raise KeyError(name)

    async def app_takeover(self, title: str | None = None) -> str:
        """The WiiM app plays its own queue; MA's session looks paused forever (lq:Q21)."""
        old = self.transport.current
        pos = self.transport.truth_position()
        self.transport.take_over("app", emit_event=True, title=title or "WiiM app")
        if old is not None and old.owner == "ma" and self.knobs.get("released_source_looks_paused"):
            self.rec.knob("fired", knob="released_source_looks_paused", quirks=["lq:Q21"])
            self.masquerade = old
            self.masquerade_pos = pos
        self.avt_changed()
        return "app"


def _didl_duration(meta: str) -> float | None:
    if not meta or "duration=" not in meta:
        return None
    import re

    m = re.search(r'duration="([0-9:.]+)"', meta)
    return proto.parse_hms(m.group(1)) if m else None


def _esc(text: str) -> str:
    from html import escape

    return escape(text)
