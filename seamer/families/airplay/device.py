"""The AirPlay receiver: what a HomePod, Sonos, WiiM, AirPort Express or shairport-sync does.

Music Assistant never talks AirPlay itself; it spawns ``cliairplay`` per stream. So the
simulator has two halves:

- :class:`AirplayDevice` (this module), listening on the receiver's AirPlay port. It
  serves ``GET /info`` (the binary plist MA probes for audio formats) and a JSON side
  channel ``POST /_wire/<op>`` that stands in for RTSP/RTP. The fake helper calls
  ``connect``, ``start``, ``flush``, ``pause``, ``play``, ``volume``, ``meta``,
  ``teardown`` and long-polls ``events``. Every answer and delay comes from the knobs.
- :mod:`.fake_cli`, the helper process MA spawns. It turns MA's stdin PCM and command
  FIFO into ``/_wire`` calls and prints the ``[STATUS]`` lines MA parses.

Audio: on START the device opens the helper's PCM URL in its sink at the anchor time,
so ``expect.audible_coverage`` checks exactly what MA wrote to the helper's stdin.

Volume feedback: the receiver reports level changes to the sender's DACP server
(``GET /ctrl-int/1/setproperty?dmcp.device-volume=<dB>`` with an ``Active-Remote``
header), which is how MA learns about the speaker's own buttons and app (MA#5018) and how
the Sonos volume staircase arises (MA#6664). Set the target with the ``set_dacp_target``
sim action (MA does not tell the receiver its DACP port except through mDNS).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import plistlib
import secrets
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from ...simkit.device import SimDevice
from ...simkit.kits.discovery import MdnsAdvertiser
from ...simkit.transport import ReportLens, TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)

MUTE_DB = -144.0


def volume_to_db(level: int) -> float:
    """0..100 to AirPlay dB (-30..0, 0 = mute at -144), as receivers report it."""
    if level <= 0:
        return MUTE_DB
    return round(-30.0 + 30.0 * level / 100.0, 6)


def db_to_volume(db: float) -> int:
    if db <= -30.0:
        return 0
    return max(0, min(100, round((db + 30.0) * 100.0 / 30.0)))


@dataclass
class Session:
    id: str
    protocol: str
    timing: str
    buffered: bool
    active_remote: str | None
    dacp_id: str | None
    clock_stalled: bool
    lock_ms: int
    connected_at: float
    pre_volume: int
    anchors_to_refuse: int = 0
    starts: int = 0
    meta: dict[str, str] = field(default_factory=dict)
    stream_url: str | None = None
    starved: bool = False
    events: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    timers: list[Any] = field(default_factory=list)

    def cancel(self) -> None:
        for t in self.timers:
            with contextlib.suppress(Exception):
                t.cancel()
        self.timers.clear()


class AirplayLens(ReportLens):
    """AirPlay has no position reports of its own; MA counts from the helper's lines."""


class AirplayDevice(SimDevice):
    family = "airplay"
    lens_cls = AirplayLens
    lens: AirplayLens

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.volume = int(self.identity.get("volume", 30))  # level the speaker plays at
        self.volume_target = self.volume  # last level a sender asked for
        self.session: Session | None = None
        self.sessions_seen: list[str] = []  # Active-Remote ids, oldest first
        self.paired = False
        self.dacp_target: tuple[str, int] | None = None
        self.pcm_base = ""
        self.dacp_sent: list[dict[str, Any]] = []
        self._volume_task: asyncio.Task[None] | None = None
        self._vol_queue: list[int] = []
        self._bg: set[asyncio.Task[Any]] = set()
        self._adverts: list[MdnsAdvertiser] = []

    # -- app -------------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app(exempt_prefixes=("/_sim", "/_wire/events"))
        app.router.add_get("/info", self._info)
        app.router.add_post("/_wire/{op}", self._wire)
        return app

    def device_id(self) -> str:
        return str(self.identity.get("deviceid") or self.identity.get("mac", "02:00:00:00:00:01"))

    async def _info(self, _req: web.Request) -> web.Response:
        ident = self.identity
        body: dict[str, Any] = {
            "deviceID": self.device_id(),
            "model": ident.get("model", "AirPlay"),
            "name": ident.get("name", self.id),
            "sourceVersion": ident.get("srcvers", "366.0"),
            "features": int(str(ident.get("features", "0x445F8A00,0x1C340")).split(",")[0], 16),
            "statusFlags": 4,
        }
        if self.knobs.get("airplay2") and (bits := ident.get("audio_formats_extended")):
            body["supportedAudioFormatsExtended"] = {
                "audioStream": list(bits),
                "bufferStream": list(ident.get("buffer_formats_extended", bits)),
            }
        return web.Response(
            body=plistlib.dumps(body), content_type="application/x-apple-binary-plist"
        )

    # -- TXT records / mDNS (layer 2) ------------------------------------------------

    def txt_airplay(self) -> dict[str, str]:
        """``_airplay._tcp`` TXT record of this profile."""
        ident = self.identity
        txt = {
            "deviceid": self.device_id(),
            "features": str(ident.get("features", "0x445F8A00,0x1C340")),
            "flags": str(ident.get("flags", "0x4")),
            "model": str(ident.get("model", "AirPlay")),
            "manufacturer": str(ident.get("manufacturer", "")),
            "srcvers": str(ident.get("srcvers", "366.0")),
            "pi": str(ident.get("pi", "00000000-0000-4000-8000-000000000000")),
            "gid": str(ident.get("gid", "00000000-0000-4000-8000-000000000001")),
            "pk": str(ident.get("pk", "00" * 32)),
            "acl": "0",
            "protovers": "1.1",
        }
        if fw := ident.get("firmware"):
            txt["fv"] = str(fw)
        if self.knobs.get("auth") == "password":
            txt["pw"] = "true"
        return {k: v for k, v in txt.items() if v != ""}

    def txt_raop(self) -> dict[str, str]:
        """``_raop._tcp`` TXT record of this profile."""
        ident = self.identity
        txt = {
            "txtvers": "1",
            "ch": "2",
            "cn": "0,1,2,3",
            "et": str(ident.get("et", "0,3,5")),
            "md": "0,1,2",
            "sr": "44100",
            "ss": "16",
            "tp": "UDP",
            "vn": "65537",
            "vs": str(ident.get("srcvers", "366.0")),
            "am": str(ident.get("model", "AirPlay")),
            "ft": str(ident.get("features", "0x445F8A00,0x1C340")),
            "sf": str(ident.get("flags", "0x4")),
            "pw": "true" if self.knobs.get("auth") == "password" else "false",
        }
        if pk := ident.get("pk"):
            txt["pk"] = str(pk)
        return txt

    def raop_service_name(self) -> str:
        return f"{self.device_id().replace(':', '')}@{self.identity.get('name', self.id)}"

    async def advertise(self, addresses: list[str] | None = None) -> bool:
        """Publish ``_airplay._tcp`` (AP2 profiles) and ``_raop._tcp`` adverts."""
        if not MdnsAdvertiser.available():
            return False
        addrs = addresses or [self.host]
        name = str(self.identity.get("name", self.id))
        services = [("_raop._tcp.local.", self.raop_service_name(), self.txt_raop())]
        if self.knobs.get("airplay2"):
            services.insert(0, ("_airplay._tcp.local.", name, self.txt_airplay()))
        for stype, sname, props in services:
            adv = MdnsAdvertiser(stype, sname, self.port, addrs, props)
            await adv.start()
            self._adverts.append(adv)
            self.rec.event("mdns_advertised", service=stype, instance=sname)
        return True

    async def on_stop(self) -> None:
        for adv in self._adverts:
            with contextlib.suppress(Exception):
                await adv.stop()
        self._adverts.clear()
        if self.session is not None:
            self.session.cancel()
        for t in list(self._bg):
            t.cancel()

    # -- wire ------------------------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _later(self, seconds: float, coro_fn: Any, session: Session | None = None) -> Any:
        handle = self.clock.call_later(max(0.0, seconds), lambda: self._spawn(coro_fn()))
        if session is not None:
            session.timers.append(handle)
        return handle

    async def _wire(self, req: web.Request) -> web.Response:
        op = req.match_info["op"]
        try:
            raw = await req.read()  # cached: the command middleware already read it
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            return web.json_response({"ok": False, "detail": f"unknown op {op}"}, status=404)
        sess = self.session
        if op not in ("connect",) and (sess is None or body.get("session") != sess.id):
            if op == "events":
                await self.clock.sleep(min(float(body.get("timeout", 20)), 1.0))
                return web.json_response({"events": ["teardown"]})
            return web.json_response({"ok": False, "detail": "no such session"})
        return web.json_response(await handler(body))

    def _error(self, code: str, http: int, detail: str) -> dict[str, Any]:
        self.rec.event("connect_refused", code=code, http=http, detail=detail)
        return {"ok": False, "code": code, "http": http, "detail": detail}

    async def _op_connect(self, b: dict[str, Any]) -> dict[str, Any]:
        k = self.knobs
        if ms := k.get("connect_ms"):
            await self.clock.sleep(ms / 1000)
        if (first := k.get("rtsp_first_status")) != 200:
            return self._error("connect_failed", first, f"RTSP {first}")
        requested = str(b.get("protocol") or "auto")
        if requested in ("airplay2", "airplay2-compat") and not k.get("airplay2"):
            return self._error("connect_failed", 0, "receiver does not speak AirPlay 2")
        protocol = "airplay2" if requested != "raop" and k.get("airplay2") else "raop"
        if protocol == "raop" and k.get("raop_mode") == "auth_setup_404":
            return self._error("connect_failed", 404, "POST /auth-setup 404")
        auth = k.get("auth")
        if auth == "password":
            want = str(self.identity.get("password", "seamer"))
            if not b.get("password"):
                return self._error("auth_required", 401, "password required")
            if b.get("password") != want:
                return self._error("auth_failed", 401, "wrong password")
        elif auth == "pin" and not (b.get("has_auth") or self.paired):
            return self._error("auth_required", 470, "pairing required")
        timing = "ntp" if protocol == "raop" else str(b.get("timing") or "ptp")
        if protocol == "airplay2" and timing == "ntp" and k.get("ntp_streams") == "reject_400":
            return self._error("connect_failed", 400, "SETUP 400: NTP timing not supported")
        if self.session is not None:  # a new sender takes over
            await self._end_session("replaced")
        stalled = False
        if timing == "ptp":
            stalled = not k.get("ptp_respond") or (
                k.get("ptp_role") == "standalone_own_clock" and not k.get("cli_ptp_follow")
            )
        lo = int(k.param("clock_lock_ms", "min", 150))
        hi = int(k.param("clock_lock_ms", "max", lo))
        lock_ms = int(self.rng.uniform("clock_lock", lo, max(lo, hi)))
        sess = Session(
            id=secrets.token_hex(4),
            protocol=protocol,
            timing=timing,
            buffered=bool(k.get("buffered")) and protocol == "airplay2",
            active_remote=b.get("active_remote"),
            dacp_id=b.get("dacp_id"),
            clock_stalled=stalled,
            lock_ms=lock_ms,
            connected_at=self.clock.time(),
            pre_volume=self.volume,
        )
        if k.fires("anchor_400"):
            sess.anchors_to_refuse = int(k.param("anchor_400", "count", 2))
        sess.stream_url = None
        self.session = sess
        if sess.active_remote and sess.active_remote not in self.sessions_seen:
            self.sessions_seen.append(sess.active_remote)
        self.pcm_base = str(b.get("pcm_base", ""))
        if (reset := k.get("reset_control_after_s")) > 0:
            self._later(reset, lambda: self._control_reset(sess), sess)
        self.rec.event(
            "connected",
            session=sess.id,
            protocol=protocol,
            timing=timing,
            lock_ms=lock_ms,
            stalled=stalled,
            active_remote=sess.active_remote,
        )
        return {
            "ok": True,
            "session": sess.id,
            "protocol": protocol,
            "timing": timing,
            "buffered": sess.buffered,
            "first_probe_ms": k.get("first_probe_ms"),
            "lock_ms": lock_ms,
            "clock_stalled": stalled,
            "latency_ms": k.get("output_latency_ms"),
            "anchor_retry_ms": k.param("anchor_400", "retry_ms", 500),
            "pcm_base": self.pcm_base,
            "cli": {
                "exit_after_eof": k.get("cli_exit_after_eof"),
                "ignore_sigterm": k.get("cli_ignore_sigterm"),
                "die_after_ms": k.get("cli_die_after_ms"),
                "start_ack": k.get("cli_start_ack"),
            },
        }

    async def _op_start(self, b: dict[str, Any]) -> dict[str, Any]:
        sess = self.session
        assert sess is not None
        k = self.knobs
        if sess.buffered and k.get("buffered_anchor_always_400"):
            return {"ok": False, "http": 400, "detail": "SETRATEANCHORTIME 400 (buffered)"}
        if sess.anchors_to_refuse > 0:
            sess.anchors_to_refuse -= 1
            self.rec.event("anchor_refused", session=sess.id, left=sess.anchors_to_refuse)
            return {"ok": False, "http": 400, "detail": "SETRATEANCHORTIME 400"}
        at_ms = int(b.get("anchor_unix_ms") or self.clock.time() * 1000)
        url = str(b["stream_url"])
        sess.starts += 1
        sess.stream_url = url
        sess.starved = False
        lead_ms = at_ms - self.clock.time() * 1000
        if (min_lat := k.get("min_audible_latency_ms")) and lead_ms < min_lat:
            drop = int(min_lat - max(0.0, lead_ms))
            url += f"?skip_ms={drop}"
            self.rec.event("audio_dropped_short_latency", session=sess.id, dropped_ms=drop)
        self.rec.event("anchored", session=sess.id, at_unix_ms=at_ms, start=sess.starts)
        self._later(lead_ms / 1000, lambda: self._begin_audio(sess, url), sess)
        return {"ok": True, "at_unix_ms": at_ms}

    def _silent(self, sess: Session) -> str | None:
        if sess.clock_stalled:
            return "clock_stalled"
        if sess.protocol == "raop" and self.knobs.get("raop_mode") == "silent":
            return "raop_silent"
        if self.knobs.get("require_metadata_before_audio") and not sess.meta:
            return "waiting_for_metadata"
        return None

    async def _begin_audio(self, sess: Session, url: str) -> None:
        if self.session is not sess or sess.stream_url is None:
            return
        if why := self._silent(sess):
            self.rec.event("silent", session=sess.id, reason=why)
            return
        title = sess.meta.get("title")
        await self.transport.set_uri(url, title=title)
        await self.transport.play()
        starve = int(self.knobs.param("starve_after_ms", "ms", 0))
        first_only = bool(self.knobs.param("starve_after_ms", "first_play_only", False))
        if starve > 0 and (not first_only or sess.starts == 1):
            self._later(starve / 1000, lambda: self._starve(sess), sess)

    async def _starve(self, sess: Session) -> None:
        if self.session is not sess or self.transport.state not in (
            TState.PLAYING,
            TState.BUFFERING,
        ):
            return
        sess.starved = True
        self.rec.event("audio_starved", session=sess.id)
        await self.transport.stop()

    async def _control_reset(self, sess: Session) -> None:
        if self.session is not sess:
            return
        self.rec.event("control_reset", session=sess.id)
        await sess.events.put("control_reset")
        await self._end_session("control_reset", notify=False)

    async def _op_flush(self, _b: dict[str, Any]) -> dict[str, Any]:
        sess = self.session
        assert sess is not None
        sess.stream_url = None
        for t in sess.timers:
            t.cancel()
        sess.timers.clear()
        await self.transport.stop()
        ack = self.knobs.get("flush_ack") == "ok"
        self.rec.event("flushed", session=sess.id, ack=ack)
        return {"ok": True, "ack": ack}

    async def _op_pause(self, b: dict[str, Any]) -> dict[str, Any]:
        if self.knobs.get("pause_reject"):
            self.rec.event("pause_rejected")
            return {"ok": False}
        await self.transport.pause()
        return {"ok": True}

    async def _op_play(self, _b: dict[str, Any]) -> dict[str, Any]:
        sess = self.session
        assert sess is not None
        if sess.starved and sess.stream_url:
            sess.starved = False
            await self.transport.set_uri(sess.stream_url, title=sess.meta.get("title"))
        await self.transport.play()
        return {"ok": True, "delay_ms": 0}

    async def _op_meta(self, b: dict[str, Any]) -> dict[str, Any]:
        sess = self.session
        assert sess is not None
        first = not sess.meta
        sess.meta = {k: str(v) for k, v in b.items() if k != "session"}
        self.rec.event("metadata", **sess.meta)
        if first and self.knobs.get("require_metadata_before_audio") and sess.stream_url:
            self._spawn(self._begin_audio(sess, sess.stream_url))
        return {"ok": True}

    async def _op_events(self, b: dict[str, Any]) -> dict[str, Any]:
        sess = self.session
        assert sess is not None
        try:
            ev = await asyncio.wait_for(sess.events.get(), float(b.get("timeout", 20)))
        except TimeoutError:
            return {"events": []}
        return {"events": [ev]}

    async def _op_teardown(self, _b: dict[str, Any]) -> dict[str, Any]:
        await self._end_session("teardown")
        return {"ok": True}

    async def _end_session(self, reason: str, notify: bool = True) -> None:
        sess, self.session = self.session, None
        if sess is None:
            return
        sess.cancel()
        if notify:
            sess.events.put_nowait("teardown")
        await self.transport.stop()
        self.rec.event("session_ended", session=sess.id, reason=reason)
        delay = self.knobs.get("dacp_after_teardown_ms")
        if delay >= 0 and sess.active_remote:
            remote = sess.active_remote
            self._later(delay / 1000, lambda: self._send_dacp_volume(self.volume, remote))

    # -- volume --------------------------------------------------------------------------

    async def _op_volume(self, b: dict[str, Any]) -> dict[str, Any]:
        level = max(0, min(100, int(b.get("volume", self.volume))))
        self.volume_target = level
        self.rec.event("volume_requested", level=level, applied=self.volume)
        tick_hi = float(self.knobs.param("volume_apply_tick_s", "max", 0.0))
        if tick_hi <= 0:
            await self._apply_volume(level)
            return {"ok": True}
        self._vol_queue.append(level)
        if self._volume_task is None or self._volume_task.done():
            self._volume_task = asyncio.create_task(self._volume_ticker())
        return {"ok": True}

    async def _volume_ticker(self) -> None:
        """Sonos in an AirPlay session (ca2:A49, MA#6664).

        Requests queue up and are applied on a slow tick. With ``step`` the level moves at
        most that much per tick; otherwise every queued level is applied in order and
        echoed, the echoes 30 ms apart.
        """
        k = self.knobs
        lo = float(k.param("volume_apply_tick_s", "min", 0.0))
        hi = float(k.param("volume_apply_tick_s", "max", 0.0))
        step = int(k.param("volume_apply_tick_s", "step", 0))
        while self._vol_queue:
            await self.clock.sleep(self.rng.uniform("volume_tick", lo, max(lo, hi)))
            queued, self._vol_queue = self._vol_queue, []
            if step > 0:
                target = queued[-1]
                delta = max(-step, min(step, target - self.volume))
                levels = [self.volume + delta]
                if levels[0] != target:
                    self._vol_queue.insert(0, target)
            else:
                levels = [lvl for i, lvl in enumerate(queued) if i == 0 or lvl != queued[i - 1]]
            self.rec.event("volume_tick", applied=levels)
            await self._apply_volume(levels[-1], report=False)
            for i, lvl in enumerate(levels):
                self._echo(lvl, offset_s=0.03 * i)

    async def _apply_volume(self, level: int, *, report: bool = True) -> None:
        if level == self.volume:
            return
        old, self.volume = self.volume, level
        self.rec.state("volume", old=old, new=level)
        if report:
            self._echo(level)
        sess = self.session
        if sess is not None and self.knobs.get("volume_reverts_in_session"):
            revert_to = sess.pre_volume
            self._later(1.0, lambda: self._apply_volume(revert_to), sess)

    def _echo(self, level: int, offset_s: float = 0.0) -> None:
        k = self.knobs
        if not k.param("volume_echo", "enabled", True):
            return
        delay = float(k.param("volume_echo", "delay_ms", 250)) / 1000 + offset_s
        burst = int(k.param("volume_echo", "burst", 1))

        async def _send() -> None:
            for i in range(burst):
                if i:
                    await self.clock.sleep(0.03)
                await self._send_dacp_volume(level)

        self._later(delay, _send)

    def active_remote_for_report(self) -> str | None:
        mode = self.knobs.get("dacp_active_remote")
        seen = self.sessions_seen
        current = self.session.active_remote if self.session else None
        if mode == "first_seen" and seen:
            return seen[0]
        if mode == "previous":
            if current and current in seen and seen.index(current) > 0:
                return seen[seen.index(current) - 1]
            if current is None and len(seen) >= 2:
                return seen[-2]
        return current or (seen[-1] if seen else None)

    async def _send_dacp_volume(self, level: int, remote: str | None = None) -> None:
        remote = remote or self.active_remote_for_report()
        if self.session is None and not self.knobs.get("dacp_report_idle") and remote is None:
            return
        db = volume_to_db(level)
        await self.send_dacp(f"setproperty?dmcp.device-volume={db:.6f}", remote)

    async def send_dacp(self, command: str, remote: str | None = None) -> int | None:
        """Send one DACP request (``/ctrl-int/1/<command>``) to the sender. Returns status."""
        remote = remote or self.active_remote_for_report()
        entry: dict[str, Any] = {"command": command, "active_remote": remote}
        self.dacp_sent.append(entry)
        if self.dacp_target is None or remote is None:
            entry["status"] = None
            self.rec.event("dacp_skipped", **entry)
            return None
        host, port = self.dacp_target
        dacp_id = self.session.dacp_id if self.session else None
        req = (
            f"GET /ctrl-int/1/{command} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Active-Remote: {remote}\r\n"
            + (f"DACP-ID: {dacp_id}\r\n" if dacp_id else "")
            + "User-Agent: AirPlay/366.0\r\n\r\n"
        ).encode()
        status = None
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(req)  # one write: MA reads the request with a single read(1024)
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 3.0)
            status = int(line.split()[1]) if line else None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        except (OSError, TimeoutError, ValueError, IndexError) as exc:
            entry["error"] = repr(exc)
        entry["status"] = status
        self.rec.event("dacp_sent", **entry)
        return status

    # -- hooks ---------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        sess = self.session
        return {
            "volume": self.volume,
            "volume_target": self.volume_target,
            "session": None
            if sess is None
            else {
                "id": sess.id,
                "protocol": sess.protocol,
                "timing": sess.timing,
                "active_remote": sess.active_remote,
                "starts": sess.starts,
                "meta": sess.meta,
            },
            "dacp_target": list(self.dacp_target) if self.dacp_target else None,
            "dacp_sent": self.dacp_sent[-20:],
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "set_dacp_target":
            self.dacp_target = (str(payload.get("host", "127.0.0.1")), int(payload["port"]))
            return {"dacp_target": list(self.dacp_target)}
        if name == "press":  # the speaker's own buttons / app
            button = payload.get("button", "volume")
            if button == "volume":
                level = max(0, min(100, int(payload["level"])))
                self.volume_target = level
                await self._apply_volume(level, report=False)
                if self.session is not None or self.knobs.get("dacp_report_idle"):
                    await self._send_dacp_volume(level)
                return {"volume": self.volume}
            dacp = {"play": "play", "pause": "pause", "playpause": "playpause", "next": "nextitem"}
            if button in dacp:
                return {"status": await self.send_dacp(dacp[button])}
        if name == "pair":
            self.paired = True
            return {"paired": True}
        if name == "advertise":
            return {"advertised": await self.advertise(payload.get("addresses"))}
        raise KeyError(name)
