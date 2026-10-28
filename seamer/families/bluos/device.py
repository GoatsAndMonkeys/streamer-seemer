"""BluOS player (Bluesound, NAD): the HTTP XML API on port 11000.

Endpoints (all GET unless noted; replies are ``text/xml`` with the BluOS framing):

- ``/Status[?etag=&timeout=]``, ``/SyncStatus[?etag=&timeout=]``,
  ``/Volume[?level=|abs_db=|db=|mute=&tell_slaves=][&etag=&timeout=]``: state documents.
  With ``etag`` they long-poll until the etag changes or ``timeout`` (default 100 s).
- ``/Play[?url=|seek=|id=|inputIndex=|inputTypeIndex=]``, ``/Pause[?toggle=1]``,
  ``/Stop``, ``/Skip``, ``/Back``, ``/Shuffle?state=``, ``/Repeat?state=``, ``/Sleep``.
- ``/AddSlave?slave=&port=`` (or ``slaves=&ports=``), ``/RemoveSlave?slave=&port=``.
- ``/Presets``, ``/Preset?id=N|+1|-1``, ``/RadioBrowse?service=Capture``, ``/Browse``,
  ``/Services``, ``/Playlist``, ``/Clear``, ``/Save?name=``, ``/Delete?id=``,
  ``/Move``, ``/Add``.
- ``/reboot`` (GET or POST): HTML "Rebooting", then the player is gone for
  ``reboot_downtime_s``.

Grouping is shared between devices in one process through :data:`REGISTRY`, keyed by
the ``(ip, port)`` a player advertises (``advertise_ip`` option, default the bind host).

Sim actions (``POST /_sim/action/<name>``): ``select_input`` {id}, ``load_queue``
{tracks: [{title, artist, album, secs}], play}, ``set_presets`` {presets},
``network_blip``, ``reboot``, ``set_name`` {name}. The built-in ``take_over`` {owner}
models an external source (Spotify, AirPlay) taking the player.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, unquote

from aiohttp import web

from ...simkit.device import DeviceContext, SimDevice
from ...simkit.kits.discovery import MdnsAdvertiser
from ...simkit.kits.http import LongPoll, xml
from ...simkit.transport import ReportLens, TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)
from .protocol.xml import Raw, Xml, md5_hex

# (ip, port) -> player, for grouping across simulated players in one process
REGISTRY: dict[tuple[str, int], BluosDevice] = {}

SCHEMA_VERSION = {"bluos3": "32", "4.16": "34", "4.18": "36"}
ULA_ADDRESS = "fd00::5e:5301"  # what B20's re-announce carries (a made-up ULA)
LEGACY_BROWSE = ("/Albums", "/Artists", "/Songs", "/Genres", "/Composers")
SLEEP_STEPS = ["15", "30", "45", "60", "90", ""]

INPUTS: dict[str, list[dict[str, str]]] = {
    "streamer": [
        {
            "id": "input2",
            "text": "Optical",
            "inputType": "spdif",
            "typeIndex": "spdif-1",
            "URL": "Capture:hw:imxspdif,0/1/25/2?id=input2",
            "image": "/images/capture/ic_opticalinput.png",
        },
        {
            "id": "input1",
            "text": "Bluetooth",
            "inputType": "bluetooth",
            "typeIndex": "bluetooth-1",
            "URL": "Capture:bluez:bluetooth",
            "image": "/images/capture/ic_bluetooth.png",
        },
    ],
    "streamer-amplifier": [
        {
            "id": "input2",
            "text": "Optical",
            "inputType": "spdif",
            "typeIndex": "spdif-1",
            "URL": "Capture:hw:imxspdif,0/1/25/2?id=input2",
            "image": "/images/capture/ic_opticalinput.png",
        },
        {
            "id": "input3",
            "text": "Coaxial",
            "inputType": "coax",
            "typeIndex": "coax-1",
            "URL": "Capture:hw:imxspdif,0/1/25/2?id=input3",
            "image": "/images/capture/ic_coaxialinput.png",
        },
        {
            "id": "input1",
            "text": "Analog Input",
            "inputType": "analog",
            "typeIndex": "analog-1",
            "URL": "Capture:plughw:0,0/2/25/2?id=input1",
            "image": "/images/capture/ic_analoginput.png",
        },
        {
            "id": "input5",
            "text": "Bluetooth",
            "inputType": "bluetooth",
            "typeIndex": "bluetooth-1",
            "URL": "Capture:bluez:bluetooth",
            "image": "/images/capture/ic_bluetooth.png",
        },
    ],
    "speaker": [
        {
            "id": "input1",
            "text": "Analog Input",
            "inputType": "analog",
            "typeIndex": "analog-1",
            "URL": "Capture:plughw:0,0/2/25/2?id=input1",
            "image": "/images/capture/ic_analoginput.png",
        },
        {
            "id": "input2",
            "text": "Bluetooth",
            "inputType": "bluetooth",
            "typeIndex": "bluetooth-1",
            "URL": "Capture:bluez:bluetooth",
            "image": "/images/capture/ic_bluetooth.png",
        },
    ],
    "soundbar": [
        {
            "id": "input4",
            "text": "HDMI ARC",
            "inputType": "earc",
            "typeIndex": "earc-1",
            "URL": "Capture:hdmi-input?id=input4",
            "image": "/images/capture/ic_hdmiinput.png",
        },
        {
            "id": "input2",
            "text": "Optical",
            "inputType": "spdif",
            "typeIndex": "spdif-1",
            "URL": "Capture:hw:imxspdif,0/1/25/2?id=input2",
            "image": "/images/capture/ic_opticalinput.png",
        },
        {
            "id": "input3",
            "text": "Bluetooth",
            "inputType": "bluetooth",
            "typeIndex": "bluetooth-1",
            "URL": "Capture:bluez:bluetooth",
            "image": "/images/capture/ic_bluetooth.png",
        },
    ],
}

DEFAULT_PRESETS = [
    {
        "id": 1,
        "name": "Classical Radio",
        "url": "TuneIn:s1001",
        "image": "/images/presets/tunein.png",
        "volume": None,
    },
    {
        "id": 2,
        "name": "Morning Jazz",
        "url": "TuneIn:s1002",
        "image": "/images/presets/tunein.png",
        "volume": 20,
    },
]


def fmt_db(value: float) -> str:
    s = f"{value:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return "0" if s == "-0" else s


class BluosLens(ReportLens):
    """Truth -> BluOS words: stop, connecting, stream, play, pause (bss:B3, bl2:B43)."""

    dev: BluosDevice

    def truth_word(self) -> str:
        st = self.tm.state
        if self.dev.connecting:
            return "connecting"
        if st in (TState.NO_MEDIA, TState.STOPPED):
            return "stop"
        if st in (TState.LOADING, TState.BUFFERING):
            return "connecting"
        if st == TState.PAUSED:
            return "pause"
        return "play" if self.dev.source == "queue" else "stream"

    def state_word(self, rendering: bool = False) -> str:
        dev, k, st = self.dev, self.knobs, self.tm.state
        truth = self.truth_word()
        word = truth
        if (
            st == TState.BUFFERING
            and dev.audio_started
            and not k.enabled("connecting_on_buffer_refill")
        ):
            word = "stream"
        if st == TState.PLAYING and dev.tail:
            word = "connecting"
        if st == TState.PLAYING and dev.wedged:
            word = "pause"
        if rendering and k.fires("inject_state", truth=truth):
            word = str(k.param("inject_state", "value", ""))
        return self._differs("state", truth, word)


class BluosDevice(SimDevice):
    family = "bluos"
    lens_cls = BluosLens
    lens: BluosLens

    def __init__(self, ctx: DeviceContext) -> None:
        super().__init__(ctx)
        self.lens.dev = self
        ident = self.identity
        k = self.knobs
        self.xml = Xml(prolog=k.get("xml_prolog"), selfclose=k.get("empty_tag_style") != "pair")
        self.advertise_ip = str(ctx.options.get("advertise_ip", ctx.host))
        self.name = ident.get("name", self.id)
        self.volume = int(ident.get("volume", "30"))
        self.muted = False
        self.leader: BluosDevice | None = None
        self.followers: list[BluosDevice] = []
        self.sync_stat = 1 + self.rng.stream("syncstat").randrange(40)
        self.notify = LongPoll(self.clock, "bluos")  # internal change notifier
        self.source = "none"  # none | url | input | queue | preset | external
        self.item: dict[str, Any] = {}  # metadata of what plays (kept after stop: bss:B8)
        self.has_played = False
        self.connecting = False  # /Play?url accepted, stream not requested yet (B44)
        self.audio_started = False
        self.tail = False  # server closed; buffered tail plays out (B4)
        self.wedged = False  # B15
        self.pid = 1
        self.repeat = 0
        self.shuffle = 0
        self.sleep = ""
        self.queue: list[dict[str, Any]] = []
        self.queue_index = 0
        self.queue_name = ""
        self.presets: list[dict[str, Any]] = [dict(p) for p in DEFAULT_PRESETS]
        self.preset_index = 0
        self.inputs = [dict(i) for i in INPUTS[k.get("device_class")]]
        self.chassis: list[BluosDevice] = [self]
        self.zone_children: list[BluosDevice] = []
        self.blipped = False
        self._hidden: tuple[BluosDevice, int] | None = None  # staged sync reads (B32)
        self._stream: dict[str, Any] = {}  # what the sink saw when it opened the URL
        self._loops = 0
        self._internal_takeover = False
        self._connect_task: asyncio.Task[None] | None = None
        self._bg: set[asyncio.Future[Any]] = set()
        self._answered: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()
        self._mdns: list[MdnsAdvertiser] = []
        self._down = False
        self.transport.add_listener(self._on_transport)
        if self.sink is not None:
            self.sink.add_listener(self._on_sink)

    # -- identity -----------------------------------------------------------------

    @property
    def addr(self) -> tuple[str, int]:
        return (self.advertise_ip, self.port)

    @property
    def mac(self) -> str:
        return self.identity.get("mac", "00:00:5E:00:53:00")

    @property
    def fw(self) -> str:
        return self.knobs.get("fw_profile")

    def mdns_txt(self) -> dict[str, str]:
        """TXT record of the ``_musc._tcp`` announcement (bl2:B45)."""
        txt: dict[str, str] = {}
        if not self.knobs.fires("mdns_omit_mac"):
            txt["mac"] = self.mac
        txt["model"] = self.identity.get("model", "")
        obj = self.knobs.get("mdns_object_type")
        if obj:
            txt["_objectType"] = obj
        txt["version"] = self.identity.get("firmware", "")
        return txt

    def mdns_service_type(self) -> str:
        return "_musp._tcp.local." if self.ctx.options.get("zone_of") else "_musc._tcp.local."

    def mdns_addresses(self) -> list[str]:
        if self.blipped and self.knobs.fires("ipv6_only_announce"):
            return [ULA_ADDRESS]
        return [self.advertise_ip]

    # -- change tracking ------------------------------------------------------------

    def changed(self) -> None:
        self.notify.bump()
        if self.leader is not None:
            self.root().notify.bump()

    def bump_sync(self) -> None:
        lo = int(self.knobs.param("syncstat_step", "min", 1))
        hi = max(lo, int(self.knobs.param("syncstat_step", "max", lo)))
        self.sync_stat += self.rng.stream("syncstat").randint(lo, hi)
        self.changed()

    def root(self) -> BluosDevice:
        dev = self
        while dev.leader is not None:
            dev = dev.leader
        return dev

    def _spawn(self, aw: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(aw)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _on_transport(self, name: str, data: dict[str, Any]) -> None:
        tm = self.transport
        if name == "state":
            old, new = data["old"], data["new"]
            if new in (TState.STOPPED, TState.NO_MEDIA):
                self.tail = False
                self.wedged = False
            if (
                old == TState.PAUSED
                and new == TState.PLAYING
                and tm.owner not in ("ma", "queue", "input", "preset", "handover")
                and not self._internal_takeover
                and self.knobs.fires("wedged_pause_state", owner=tm.owner)
            ):
                self.wedged = True
            if (
                not self._internal_takeover
                and tm.owner not in ("ma", "queue", "input", "preset", "handover")
                and self.source != "external"
            ):
                self.source = "external"
                self.item = {
                    "service": tm.owner.capitalize(),
                    "title1": (tm.current.title if tm.current else None) or tm.owner,
                }
                self.has_played = True
        elif name == "track_ended":
            self._maybe_loop(str(data.get("reason")))
        self.changed()

    def _on_sink(self, name: str, data: dict[str, Any]) -> None:
        if name == "opened":
            self._stream = dict(data)
            self.audio_started = False
            self.tail = False
        elif name == "first_audio":
            self.audio_started = True
        elif name == "eof":
            self._stream["ended_by_server"] = True
            if self.knobs.get("on_server_close") == "drain" and self.knobs.fires(
                "connecting_at_tail"
            ):
                self.tail = True
        elif name in ("ended", "closed"):
            self.tail = False
        self.changed()

    def _maybe_loop(self, reason: str) -> None:
        """bss:B6: without a Content-Length profile the player re-requests the URL."""
        info = self._stream
        if self.source != "url" or reason not in ("drained", "dropped", "eof"):
            return
        if not (info.get("chunked") or info.get("content_length") is None):
            return
        max_loops = int(self.knobs.param("loop_on_http_profile", "max_loops", 3))
        if self._loops >= max_loops or not self.knobs.fires(
            "loop_on_http_profile", loop=self._loops + 1
        ):
            return
        self._loops += 1
        url = self.item.get("streamUrl")
        self.rec.event("stream_loop", url=url, loop=self._loops)

        async def _restart() -> None:
            await self.transport.set_uri(url)
            await self.transport.play()

        self._spawn(_restart())

    # -- volume -----------------------------------------------------------------------

    def _db_range(self) -> tuple[float, float]:
        lo = float(self.knobs.param("volume_db_range", "lo", -80.0))
        hi = float(self.knobs.param("volume_db_range", "hi", 0.0))
        return lo, hi

    def db_of(self, level: int) -> float:
        lo, hi = self._db_range()
        return round(lo + (hi - lo) * level / 100.0, 1)

    def level_of(self, db: float) -> int:
        lo, hi = self._db_range()
        return max(0, min(100, round((db - lo) / (hi - lo) * 100.0)))

    def set_level(self, level: int) -> int:
        level = max(0, min(100, level))
        db = self.db_of(level)
        limits = self.knobs.get("volume_limits_db")
        if limits:
            lo = float(limits.get("min", -200.0))
            hi = float(limits.get("max", 200.0))
            clamped = max(lo, min(hi, db))
            if clamped != db:
                self.rec.event("volume_clamped", requested=level, db=clamped)
                level = self.level_of(clamped)
                db = clamped
        if level != self.volume and self.knobs.fires("volume_step_stuck", requested=level):
            level = self.level_of(math.floor(db / 2.0) * 2.0)
        if level != self.volume:
            self.volume = level
            self.bump_sync()
        return level

    def volume_fields(self) -> dict[str, Any]:
        if self.knobs.get("fixed_volume"):
            return {"volume": -1, "db": "0", "mute": 0}
        if self.muted:
            return {
                "volume": 0,
                "db": "-100",
                "mute": 1,
                "muteVolume": self.volume,
                "muteDb": fmt_db(self.db_of(self.volume)),
            }
        return {"volume": self.volume, "db": fmt_db(self.db_of(self.volume)), "mute": 0}

    def volume_etag(self) -> str:
        return md5_hex(f"vol:{self.volume_fields()}")

    def volume_doc(self) -> str:
        f = self.volume_fields()
        attrs: list[tuple[str, Any]] = [("db", f["db"])]
        if self.muted:
            attrs += [("mute", 1), ("muteDb", f["muteDb"]), ("muteVolume", f["muteVolume"])]
            attrs += [("offsetDb", 0)]
        else:
            attrs += [("offsetDb", 0), ("mute", 0)]
        attrs.append(("etag", self.volume_etag()))
        if self.knobs.get("volume_source_attr"):
            attrs.append(("source", ""))
        return self.xml.el("volume", f["volume"], attrs)

    # -- /Status ------------------------------------------------------------------------

    def status_fields(self, rendering: bool = False) -> dict[str, Any]:
        k = self.knobs
        word = self.lens.state_word(rendering=rendering)
        f: dict[str, Any] = {
            "canSeek": 0,
            "cursor": 0,
            "indexing": 1 if k.get("indexing") else 0,
            "mid": 0,
            "mode": 1,
            "pid": self.pid,
            "prid": 0,
            "repeat": self.repeat,
            "shuffle": self.shuffle,
            "sid": 5,
            "sleep": self.sleep,
            "song": self.queue_index if self.source == "queue" else 0,
            "state": word,
            "syncStat": self.sync_stat,
        }
        f.update(self.volume_fields())
        if self.followers:
            f["groupName"] = self.group_name()
            vols = [d.volume for d in (self, *self.followers)]
            f["groupVolume"] = round(sum(vols) / len(vols))
        if k.get("extra_status_fields"):
            f["canMovePlayback"] = "true"
        stopped = self.lens.truth_word() == "stop"
        bare = not self.has_played and k.get("fresh_stop_bare")
        if stopped and not k.get("stop_keeps_metadata"):
            bare = True
        if not bare and self.item:
            f.update(self.item)
            if self.source == "url":
                f["repeat"] = 2
        if "quality" in f and word == "pause" and k.get("pause_drops_quality"):
            del f["quality"]
        if stopped:
            f["secs"] = 0
        else:
            pos = self.lens.position() or 0.0
            if self.source == "input":
                pos += float(k.get("input_secs_origin"))
            f["secs"] = int(pos)
        return f

    def status_render(self, rendering: bool = False) -> tuple[str, str]:
        fields = self.status_fields(rendering=rendering)
        inner = self.xml.fields(fields)
        if self.knobs.get("etag_ignores_secs"):
            basis = self.xml.fields({k: v for k, v in fields.items() if k != "secs"})
        else:
            basis = inner
        if rendering:  # the etag never depends on an injected state word
            plain = self.status_fields()
            plain_basis = {k: v for k, v in plain.items() if k != "secs"}
            basis = self.xml.fields(plain_basis if self.knobs.get("etag_ignores_secs") else plain)
        etag = md5_hex(basis)
        return etag, self.xml.el("status", attrs={"etag": etag}, inner=inner)

    def status_etag(self) -> str:
        return self.status_render()[0]

    def status_owner(self) -> BluosDevice:
        """bl2:B29: a follower answers /Status with its leader's document."""
        if self.leader is not None and self.knobs.get("follower_status_mirror"):
            return self.root()
        return self

    # -- /SyncStatus ------------------------------------------------------------------

    def group_name(self) -> str:
        if self.knobs.get("group_name_style") == "names":
            return "+".join(d.name for d in (self, *self.followers))
        return f"{self.name} + {len(self.followers)}"

    def sync_doc(self, consume: bool = False) -> str:
        ident, k, fw = self.identity, self.knobs, self.fw
        drop = k.get("drop_model_attrs")
        vol = self.volume_fields()
        attrs: list[tuple[str, Any]] = [("etag", self.sync_stat), ("syncStat", self.sync_stat)]
        if fw != "bluos3" and not drop:
            attrs.append(("version", ident.get("firmware", "")))
        attrs += [
            ("id", f"{self.advertise_ip}:{self.port}"),
            ("db", vol["muteDb"] if self.muted else vol["db"]),
            ("volume", vol["muteVolume"] if self.muted else vol["volume"]),
            ("name", self.name),
            ("model", ident.get("model", "")),
        ]
        if not drop:
            attrs.append(("modelName", ident.get("modelName", ident.get("model", ""))))
        if fw != "bluos3" and not drop:
            attrs.append(("class", k.get("device_class")))
        attrs += [
            ("icon", ident.get("icon", "")),
            ("brand", ident.get("brand", "Bluesound")),
            ("schemaVersion", SCHEMA_VERSION[fw]),
            ("initialized", "true"),
        ]
        if self.muted:
            attrs += [("mute", 1), ("muteDb", vol["muteDb"]), ("muteVolume", vol["muteVolume"])]
        if self.followers:
            attrs.append(("group", self.group_name()))
        attrs.append(("mac", self.mac))
        cls = k.get("device_class")
        x = self.xml
        parts: list[str] = []
        if cls in ("speaker", "soundbar"):
            opts = ("left", "right", "side_left", "side_right") if cls == "speaker" else ("front",)
            parts.append(
                x.el(
                    "zoneOptions",
                    inner="".join(x.el("option", o, {"zoneMaster": "true"}) for o in opts),
                )
            )
        if self.leader is not None:
            parts.append(x.el("master", self.leader.advertise_ip, {"port": self.leader.port}))
        hidden = self._hidden
        for f in self.followers:
            if hidden is not None and hidden[0] is f and hidden[1] > 0:
                continue
            s_attrs: list[tuple[str, Any]] = [("id", f.advertise_ip), ("port", f.port)]
            if fw != "bluos3":
                s_attrs += [
                    ("name", f.name),
                    ("model", f.identity.get("model", "")),
                    ("icon", f.identity.get("icon", "")),
                ]
            parts.append(x.el("slave", attrs=s_attrs))
        if consume and hidden is not None:
            left = hidden[1] - 1
            self._hidden = (hidden[0], left) if left > 0 else None
        if cls in ("speaker", "soundbar", "streamer"):
            parts.append(x.el("pairWithSub"))
        if cls in ("streamer-amplifier", "soundbar", "streamer"):
            parts.append(x.el("bluetoothOutput"))
        if cls == "soundbar":
            parts.append(x.el("audioPresetUrl", attrs={"url": "/audioPreset"}))
        return x.el("SyncStatus", attrs=attrs, inner="".join(parts))

    # -- HTTP plumbing ------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app()
        routes: dict[str, Callable[[web.Request], Awaitable[web.StreamResponse]]] = {
            "/Status": self._h_status,
            "/SyncStatus": self._h_sync_status,
            "/Volume": self._h_volume,
            "/Play": self._h_play,
            "/Pause": self._h_pause,
            "/Stop": self._h_stop,
            "/Skip": self._h_skip,
            "/Back": self._h_back,
            "/Shuffle": self._h_shuffle,
            "/Repeat": self._h_repeat,
            "/Sleep": self._h_sleep,
            "/AddSlave": self._h_add_slave,
            "/RemoveSlave": self._h_remove_slave,
            "/Presets": self._h_presets,
            "/Preset": self._h_preset,
            "/RadioBrowse": self._h_radio_browse,
            "/Browse": self._h_browse,
            "/Services": self._h_services,
            "/Playlist": self._h_playlist,
            "/Clear": self._h_clear,
            "/Save": self._h_save,
            "/Delete": self._h_delete,
            "/Move": self._h_move,
            "/Add": self._h_add,
        }
        for path, handler in routes.items():
            app.router.add_get(path, self._wrap(handler))
        app.router.add_route("*", "/reboot", self._wrap(self._h_reboot))
        return app

    def _wrap(
        self, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def _run(req: web.Request) -> web.StreamResponse:
            if self.knobs.enabled("serialized_connection"):
                if self._lock.locked():
                    self.rec.event("serialized_wait", path=req.path)
                async with self._lock:
                    return await handler(req)
            return await handler(req)

        return _run

    def _reply(self, body: str, status: int = 200) -> web.Response:
        return xml(self.xml.doc(body), status=status)

    def _refuse(self, message: str) -> web.Response:
        """bl2:B31: a semantic refusal is HTTP 200 with an <error> root."""
        self.rec.event("refused", message=message)
        status = 200 if self.knobs.get("error_http_200") else 400
        return self._reply(self.xml.el("error", message), status)

    def _state_reply(self, word: str) -> web.Response:
        return self._reply(self.xml.el("state", word))

    async def _hold(
        self, req: web.Request, resource: str, etag_fn: Callable[[], str], notifier: LongPoll
    ) -> None:
        """bl2:B38 long-poll: hold until the etag changes or the timeout, then answer."""
        q = req.query
        if "etag" not in q:
            return
        client = f"{req.remote}"
        now = self.clock.monotonic()
        last = self._answered.get((client, resource))
        gap = float(self.knobs.get("longpoll_min_gap_s"))
        if gap > 0 and last is not None and now - last < gap:
            self.rec.event(
                "longpoll_rate_violation",
                resource=resource,
                client=client,
                interval_s=round(now - last, 3),
            )
        if self.knobs.get("longpoll"):
            try:
                timeout = float(q.get("timeout", "100"))
            except ValueError:
                timeout = 100.0
            deadline = now + timeout
            while etag_fn() == q["etag"]:
                remaining = deadline - self.clock.monotonic()
                if remaining <= 0:
                    break
                await notifier.wait(notifier.etag, min(1.0, remaining))
        self._answered[(client, resource)] = self.clock.monotonic()

    # -- state documents ----------------------------------------------------------------

    async def _h_status(self, req: web.Request) -> web.StreamResponse:
        if self.knobs.fires("status_fail", path=req.path):
            if self.knobs.param("status_fail", "mode", "http") == "timeout":
                await self.clock.sleep(float(self.knobs.param("status_fail", "hold_s", 10.0)))
            return web.Response(status=int(self.knobs.param("status_fail", "status", 503)))
        owner = self.status_owner()
        await self._hold(req, "status", owner.status_etag, owner.notify)
        return self._reply(owner.status_render(rendering=True)[1])

    async def _h_sync_status(self, req: web.Request) -> web.StreamResponse:
        await self._hold(req, "syncstatus", lambda: str(self.sync_stat), self.notify)
        return self._reply(self.sync_doc(consume=True))

    async def _h_volume(self, req: web.Request) -> web.StreamResponse:
        q = req.query
        k = self.knobs
        writes = any(p in q for p in ("level", "abs_db", "db", "mute"))
        if writes and k.get("fixed_volume") and k.fires("fixed_volume", query=dict(q)):
            pass  # fixed output: commands are accepted and ignored (BL-04)
        elif writes:
            if "mute" in q:
                mute = q["mute"] == "1"
                if k.fires("mute_param_inverted", mute=q["mute"]):
                    mute = not mute
                if mute != self.muted:
                    self.muted = mute
                    self.bump_sync()
            before = self.volume
            try:
                if "level" in q:
                    self.set_level(int(float(q["level"])))
                elif "abs_db" in q:
                    self.set_level(self.level_of(float(q["abs_db"])))
                elif "db" in q:
                    self.set_level(self.level_of(self.db_of(self.volume) + float(q["db"])))
            except ValueError:
                return self._refuse("Invalid volume")
            if "mute" not in q and ("level" in q or "abs_db" in q or "db" in q) and self.muted:
                self.muted = False  # a level change unmutes
                self.bump_sync()
            tell = q.get("tell_slaves")
            if (
                self.followers
                and self.volume != before
                and (tell == "1" or (tell is None and k.fires("tell_slaves_default")))
            ):
                for f in self.followers:
                    f.set_level(self.volume)
        await self._hold(req, "volume", self.volume_etag, self.notify)
        return self._reply(self.volume_doc())

    # -- transport ------------------------------------------------------------------------

    def _proxy_target(self) -> BluosDevice:
        """bss:B17: a follower hands transport commands to its leader."""
        if self.leader is not None and self.knobs.fires("follower_proxies_commands"):
            return self.root()
        return self

    async def _cancel_connect(self) -> None:
        task, self._connect_task = self._connect_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.connecting = False

    async def stop_all(self) -> None:
        await self._cancel_connect()
        await self.transport.stop()
        self.wedged = False
        self.tail = False
        self.changed()

    async def play_url(self, url: str, title: str | None = None, preset: bool = False) -> None:
        """Start an HTTP stream: 'connecting' for connect_delay_s, then fetch it."""
        await self._cancel_connect()
        self.source = "url"
        self.has_played = True
        self.wedged = False
        self.tail = False
        self._loops = 0
        self._stream = {}
        self.pid += 1
        self.item = self._url_item(url, title)
        await self.transport.set_uri(url, title=title)
        lo = float(self.knobs.param("connect_delay_s", "min", 0.0))
        hi = max(lo, float(self.knobs.param("connect_delay_s", "max", lo)))
        delay = self.rng.uniform("connect_delay", lo, hi)
        self.connecting = delay > 0
        self.changed()
        self._connect_task = asyncio.create_task(self._connect_then_play(delay))

    async def _connect_then_play(self, delay: float) -> None:
        try:
            if delay > 0:
                await self.clock.sleep(delay)
        finally:
            self.connecting = False
        await self.transport.play()
        self.changed()

    def _url_item(self, url: str, title: str | None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "canSeek": 0,
            "image": "/images/streamingNP.png",
            "streamUrl": url,
            "title1": title or url,
        }
        fmt, quality = self._format_for(url)
        item["streamFormat"] = fmt
        item["quality"] = quality
        if self.knobs.get("metadata_style") == "named":
            item["name"] = title or url
        return item

    def _format_for(self, url: str) -> tuple[str, Any]:
        low = url.lower().split("?")[0]
        if low.endswith((".mp3", ".aac", ".m4a", ".ogg", ".opus")):
            codec = "AAC" if low.endswith((".aac", ".m4a")) else "MP3"
            kbps = 320 if codec == "MP3" else 256
            q = kbps * 1000 if self.knobs.get("quality_mode") == "bps" else kbps
            return f"{codec} {kbps} kb/s", q
        if low.endswith(".wav"):
            return "WAV 44.1kHz/16bit", "cd"
        return "FLAC 44.1kHz/16bit", "cd"

    async def _external(self, owner: str, item: dict[str, Any], title: str | None) -> None:
        """Our own clock-mode sources: input, queue track, non-HTTP preset."""
        await self._cancel_connect()
        self._internal_takeover = True
        try:
            self.transport.take_over(owner, title=title)
        finally:
            self._internal_takeover = False
        self.source = owner
        self.item = item
        self.has_played = True
        self.wedged = False
        self.tail = False
        self.changed()

    async def play_input(self, inp: dict[str, str]) -> None:
        settings = (
            "capture-input2"
            if self.knobs.fires("settings_group_mismatch")
            else (f"capture-{inp['id']}")
        )
        image = "/images/capture/ic_streamingNP.png"
        item = {
            "actions": Raw(self.xml.el("action", attrs={"name": "back", "state": "0"})),
            "currentImage": image,
            "image": image,
            "inputId": inp["id"],
            "inputTypeIndex": inp["typeIndex"],
            "service": "Capture",
            "serviceType": "AudioInputs",
            "settingsGroupId": settings,
            "stationImage": image,
            "streamUrl": inp["URL"],
            "title1": inp["text"],
            "twoline_title1": inp["text"],
        }
        await self._external("input", item, inp["text"])

    def _find_input(self, q: Any) -> dict[str, str] | None:
        if "inputIndex" in q:
            idx = q["inputIndex"]
            return next((i for i in self.inputs if i["id"] == f"input{idx}"), None)
        if "inputTypeIndex" in q:
            return next((i for i in self.inputs if i["typeIndex"] == q["inputTypeIndex"]), None)
        url = q.get("url", "")
        return next((i for i in self.inputs if i["URL"] == url or i["id"] == url), None)

    async def play_queue_track(self, index: int) -> None:
        t = self.queue[index]
        self.queue_index = index
        songid = f"LocalMusic:{self.pid}-{index}"
        item: dict[str, Any] = {
            "album": t.get("album", ""),
            "artist": t.get("artist", ""),
            "canSeek": 1,
            "fn": f"/var/mnt/library/{t.get('title', index)}.flac",
            "image": f"/Artwork?service=LocalMusic&songid={quote(songid, safe='')}",
            "name": t.get("title", ""),
            "quality": "cd",
            "service": "LocalMusic",
            "title1": t.get("title", ""),
            "title2": t.get("artist", ""),
            "title3": t.get("album", ""),
            "totlen": int(t.get("secs", 0)),
            "twoline_title1": t.get("title", ""),
            "twoline_title2": f"{t.get('artist', '')} • {t.get('album', '')}",
        }
        if self.knobs.get("extra_status_fields"):
            item["serviceName"] = "Library"
        if not self.knobs.fires("service_without_input_id"):
            item["inputId"] = "LocalMusic"
        await self._external("queue", item, t.get("title"))
        if self.transport.current is not None:
            self.transport.current.duration_s = float(t.get("secs", 0)) or None

    async def play_preset(self, preset: dict[str, Any], apply_volume: bool) -> None:
        if apply_volume and preset.get("volume") is not None:
            self.set_level(int(preset["volume"]))
        url = str(preset["url"])
        if url.startswith("http"):
            await self.play_url(url, title=preset.get("name"))
            return
        service = url.split(":", 1)[0]
        item = {
            "canSeek": 0,
            "image": preset.get("image", ""),
            "is_preset": "true",
            "preset_name": preset.get("name", ""),
            "service": service,
            "stationImage": preset.get("image", ""),
            "streamUrl": url,
            "title1": preset.get("name", ""),
            "title2": service,
        }
        await self._external("preset", item, preset.get("name"))

    async def _h_play(self, req: web.Request) -> web.StreamResponse:
        q = req.query
        if "url" in q or "inputIndex" in q or "inputTypeIndex" in q:
            if self.leader is not None:  # a follower that is told to play leaves its group
                await self.leader.detach(self)
            inp = self._find_input(q)
            if inp is not None:
                await self.play_input(inp)
                return self._state_reply("stream")
            if "url" not in q:
                return self._refuse("Invalid input")
            url = q["url"]
            preset = next((p for p in self.presets if p["url"] == url), None)
            if preset is not None:
                apply = not self.knobs.fires("preset_url_ignores_volume", preset=preset["id"])
                await self.play_preset(preset, apply_volume=apply)
                return self._state_reply("stream")
            await self.play_url(url, title=q.get("title"))
            return self._state_reply("stream")
        target = self._proxy_target()
        return await target._play_plain(q)

    async def _play_plain(self, q: Any) -> web.Response:
        tm = self.transport
        if "id" in q and self.queue:
            idx = int(q["id"]) % len(self.queue)
            await self.play_queue_track(idx)
            if "seek" in q:
                await tm.seek(float(q["seek"]))
            return self._state_reply("play")
        if "seek" in q:
            if self.source in ("queue",):
                await tm.seek(float(q["seek"]))
            else:
                self.rec.event("seek_ignored", source=self.source, seek=q["seek"])
            return self._state_reply(self.lens.state_word())
        if tm.state == TState.PAUSED:
            self._internal_takeover = True
            try:
                await tm.play()
            finally:
                self._internal_takeover = False
            self.changed()
        elif tm.state in (TState.STOPPED, TState.NO_MEDIA) and self.queue:
            await self.play_queue_track(self.queue_index)
        return self._state_reply(self.lens.state_word())

    async def _h_pause(self, req: web.Request) -> web.StreamResponse:
        target = self._proxy_target()
        return await target._pause(req.query)

    async def _pause(self, q: Any) -> web.Response:
        tm = self.transport
        toggle = q.get("toggle") == "1" and self.knobs.get("pause_toggle_param") == "honor"
        if self.wedged:
            if toggle:  # B15: toggle gets it unstuck
                self.wedged = False
                self.changed()
                return self._state_reply("stream")
            self.rec.event("pause_ignored", reason="wedged")
            return self._state_reply("pause")
        if toggle and tm.state == TState.PAUSED:
            await tm.play()
            self.changed()
            return self._state_reply(self.lens.state_word())
        await self._cancel_connect()
        await tm.pause()
        self.changed()
        return self._state_reply("pause" if tm.state == TState.PAUSED else self.lens.state_word())

    async def _h_stop(self, _req: web.Request) -> web.StreamResponse:
        target = self._proxy_target()
        await target.stop_all()
        return self._state_reply("stop")

    async def _h_skip(self, _req: web.Request) -> web.StreamResponse:
        target = self._proxy_target()
        if target.source == "queue" and target.queue:
            # bss:B14: Skip on the last track wraps to the first, whatever repeat says
            await target.play_queue_track((target.queue_index + 1) % len(target.queue))
            return self._reply(self.xml.el("id", target.queue_index))
        if target.source == "preset":
            await target._step_preset(+1)
        return self._reply(self.xml.el("id", 0))

    async def _h_back(self, _req: web.Request) -> web.StreamResponse:
        target = self._proxy_target()
        if target.source == "queue" and target.queue:
            pos = target.transport.truth_position()
            idx = target.queue_index
            if pos < 4.0:  # bss:B14: within 4 s, previous track; else restart
                idx = (idx - 1) % len(target.queue)
            await target.play_queue_track(idx)
            return self._reply(self.xml.el("id", idx))
        return self._reply(self.xml.el("id", 0))

    def _playlist_attrs(self, **extra: Any) -> list[tuple[str, Any]]:
        attrs: list[tuple[str, Any]] = [
            ("name", self.queue_name),
            ("modified", 0),
            ("length", len(self.queue)),
        ]
        attrs += list(extra.items())
        attrs.append(("id", self.pid))
        return attrs

    async def _h_shuffle(self, req: web.Request) -> web.StreamResponse:
        target = self._proxy_target()
        if "state" in req.query:
            target.shuffle = 1 if req.query["state"] == "1" else 0
            target.changed()
        return self._reply(
            self.xml.el("playlist", attrs=target._playlist_attrs(shuffle=target.shuffle))
        )

    async def _h_repeat(self, req: web.Request) -> web.StreamResponse:
        target = self._proxy_target()
        if "state" in req.query:
            target.repeat = max(0, min(2, int(req.query["state"])))
            target.changed()
        attrs = [("length", len(target.queue)), ("id", target.pid), ("repeat", target.repeat)]
        return self._reply(self.xml.el("playlist", attrs=attrs))

    async def _h_sleep(self, _req: web.Request) -> web.StreamResponse:
        idx = SLEEP_STEPS.index(self.sleep) if self.sleep in SLEEP_STEPS else -1
        self.sleep = SLEEP_STEPS[(idx + 1) % len(SLEEP_STEPS)]
        self.changed()
        return self._reply(self.xml.el("sleep", self.sleep))

    # -- grouping -----------------------------------------------------------------------

    @staticmethod
    def _targets(q: Any) -> tuple[list[tuple[str, int]], bool]:
        if "slaves" in q:
            ips = [s for s in q["slaves"].split(",") if s]
            ports = [p for p in q.get("ports", "").split(",") if p]
            out = [(ip, int(ports[i]) if i < len(ports) else 11000) for i, ip in enumerate(ips)]
            return out, True
        if "slave" in q:
            return [(q["slave"], int(q.get("port", "11000")))], False
        return [], False

    async def attach(self, follower: BluosDevice) -> None:
        await follower.stop_all()
        follower.leader = self
        if follower not in self.followers:
            self.followers.append(follower)
        follower.bump_sync()
        self.bump_sync()
        self.rec.event("group_join", leader=self.id, follower=follower.id)

    async def detach(self, follower: BluosDevice) -> None:
        if follower in self.followers:
            self.followers.remove(follower)
        follower.leader = None
        if self._hidden is not None and self._hidden[0] is follower:
            self._hidden = None
        await follower.stop_all()  # a player that leaves a group stops (bl2:B33)
        follower.bump_sync()
        self.bump_sync()
        self.rec.event("group_leave", leader=self.id, follower=follower.id)

    async def _h_add_slave(self, req: web.Request) -> web.StreamResponse:
        q = req.query
        if self.leader is not None:
            if self.knobs.get("addslave_on_follower") == "proxy":
                return await self.root()._h_add_slave(req)
            self.rec.event("addslave_on_follower_ignored", leader=self.leader.id)
            return self._reply(self.xml.el("addSlave"))
        targets, multi = self._targets(q)
        if multi and len(targets) == 1 and self.knobs.fires("addslave_multi_broken"):
            ip, port = targets[0]
            slave = self.xml.el("slave", attrs={"id": ip, "port": port})
            return self._reply(self.xml.el("addSlave", inner=slave))
        added: list[BluosDevice] = []
        for ip, port in targets:
            t = REGISTRY.get((ip, port))
            if t is None or t is self:
                continue
            if t.leader is not None and t.leader is not self:
                await t.leader.detach(t)  # stolen from its old group
            if t.followers and not self.knobs.fires("addslave_nest_leaders", nested=t.id):
                for f in list(t.followers):
                    await t.detach(f)
            for f in t.followers:  # nested group: those players stop too
                await f.stop_all()
            await self.attach(t)
            added.append(t)
        if added and self.knobs.get("staged_sync_reads") > 0:
            self._hidden = (added[-1], int(self.knobs.get("staged_sync_reads")))
        listed = list(targets)
        if multi and len(listed) > 1 and self.knobs.fires("addslave_reorder"):
            listed.reverse()
        known = {d.addr for d in added}
        inner = "".join(
            self.xml.el("slave", attrs={"id": ip, "port": port})
            for ip, port in listed
            if (ip, port) in known
        )
        return self._reply(self.xml.el("addSlave", inner=inner))

    async def _h_remove_slave(self, req: web.Request) -> web.StreamResponse:
        targets, _ = self._targets(req.query)
        for ip, port in targets:
            if (ip, port) == self.addr:
                err = await self._leave_own_group()
                if err:
                    return self._refuse(err)
                continue
            t = REGISTRY.get((ip, port))
            if t is not None and t in self.followers:
                await self.detach(t)
        return self._reply(self.sync_doc())

    async def _leave_own_group(self) -> str | None:
        if not self.followers:
            return "no slave available as new master"
        if self.source == "input" and self.knobs.get("leader_leave_on_input") == "refuse":
            return "Cannot move input source"
        followers = list(self.followers)
        if len(followers) == 1:
            await self.detach(followers[0])
            return None
        if not self.knobs.fires("regroup_on_primary_remove", size=len(followers) + 1):
            for f in followers:
                await self.detach(f)
            return None
        pick = self.knobs.get("handover_pick")
        new = followers[-1] if pick == "last" else followers[0]
        playing = self.transport.state in (TState.PLAYING, TState.BUFFERING, TState.LOADING)
        item, source = dict(self.item), self.source
        for f in followers:
            self.followers.remove(f)
            f.leader = None
        self._hidden = None
        for f in followers:
            if f is not new:
                f.leader = new
                new.followers.append(f)
                f.bump_sync()
        if playing:  # playback moves to the new leader
            await new._external("handover", item, item.get("title1"))
            new.source = source if source != "url" else "handover"
        await self.stop_all()
        self.rec.event(
            "group_handover", old=self.id, new=new.id, followers=[f.id for f in new.followers]
        )
        new.bump_sync()
        self.bump_sync()
        return None

    # -- presets, sources, queue ----------------------------------------------------------

    async def _h_presets(self, _req: web.Request) -> web.StreamResponse:
        x = self.xml
        rows = []
        for p in self.presets:
            attrs: list[tuple[str, Any]] = [("id", p["id"]), ("name", p["name"]), ("url", p["url"])]
            if p.get("image"):
                attrs.append(("image", p["image"]))
            if p.get("volume") is not None:
                attrs.append(("volume", p["volume"]))
            rows.append("\n  " + x.el("preset", attrs=attrs))
        inner = "".join(rows) + ("\n" if rows else "")
        return self._reply(x.el("presets", attrs={"prid": 0}, inner=inner))

    async def _step_preset(self, step: int) -> None:
        if not self.presets:
            return
        self.preset_index = (self.preset_index + step) % len(self.presets)
        await self.play_preset(self.presets[self.preset_index], apply_volume=True)

    async def _h_preset(self, req: web.Request) -> web.StreamResponse:
        pid = req.query.get("id", "")
        if self.leader is not None:
            await self.leader.detach(self)
        if pid in ("+1", "-1", " 1"):
            await self._step_preset(-1 if pid == "-1" else +1)
            return self._state_reply("stream")
        preset = next((p for p in self.presets if str(p["id"]) == pid), None)
        if preset is None:
            return self._refuse("Preset not found")
        self.preset_index = self.presets.index(preset)
        await self.play_preset(preset, apply_volume=True)
        return self._state_reply("stream")

    async def _h_radio_browse(self, req: web.Request) -> web.StreamResponse:
        service = req.query.get("service", "")
        if service != "Capture":
            return self._reply(self.xml.el("radiotime", attrs={"service": service}))
        missing = self.knobs.get("inputs_missing_attr")
        if missing != "none":
            self.rec.event("inputs_missing_attr", attr=missing, knob="inputs_missing_attr")
        items = []
        for i in self.inputs:
            attrs: list[tuple[str, Any]] = [
                ("playerName", self.name),
                ("text", i["text"]),
                ("inputType", i["inputType"]),
                ("id", i["id"]),
                ("URL", quote(i["URL"], safe="")),
                ("image", i["image"]),
                ("type", "audio"),
            ]
            if missing != "none":  # bss:B21: an item without text= / image=
                attrs = [a for a in attrs if a[0] != missing]
            items.append(self.xml.el("item", attrs=attrs))
        return self._reply(
            self.xml.el("radiotime", attrs={"service": "Capture"}, inner="".join(items))
        )

    async def _h_browse(self, req: web.Request) -> web.StreamResponse:
        key = unquote(req.query.get("key", ""))
        if key.startswith(LEGACY_BROWSE) and self.knobs.fires("legacy_browse_400", key=key):
            return web.Response(status=400, body=b"")
        service = key.split(":", 1)[0] if ":" in key else ""
        attrs = {"sid": 1, "type": "menu", "service": service or None}
        return self._reply(self.xml.el("browse", attrs=attrs))

    async def _h_services(self, _req: web.Request) -> web.StreamResponse:
        svc = self.xml.el("service", attrs={"id": "LocalMusic", "name": "Library"})
        return self._reply(self.xml.el("services", inner=svc))

    async def _h_playlist(self, req: web.Request) -> web.StreamResponse:
        q = req.query
        x = self.xml
        if "start" in q or "end" in q:
            start = int(q.get("start", "0"))
            end = int(q.get("end", str(len(self.queue) - 1)))
            songs = "".join(
                x.el(
                    "song",
                    attrs={"id": i, "service": "LocalMusic", "songid": f"LocalMusic:{i}"},
                    inner=x.el("title", t.get("title", ""))
                    + x.el("art", t.get("artist", ""))
                    + x.el("alb", t.get("album", "")),
                )
                for i, t in enumerate(self.queue)
                if start <= i <= end
            )
            return self._reply(x.el("playlist", attrs=self._playlist_attrs(), inner=songs))
        if self.knobs.get("playlist_len_style") == "children":
            inner = "".join(x.el(k, v) for k, v in self._playlist_attrs())
            return self._reply(x.el("playlist", inner=inner))
        return self._reply(x.el("playlist", attrs=self._playlist_attrs()))

    async def _h_clear(self, _req: web.Request) -> web.StreamResponse:
        self.queue = []
        self.queue_index = 0
        self.pid += 1
        if self.source == "queue":
            await self.stop_all()
        self.changed()
        attrs = [("modified", 0), ("length", 0), ("id", self.pid)]
        return self._reply(self.xml.el("playlist", attrs=attrs))

    async def _h_save(self, req: web.Request) -> web.StreamResponse:
        if not self.queue:
            return self._refuse("empty")
        self.queue_name = req.query.get("name", "")
        return self._reply(self.xml.el("saved", inner=self.xml.el("entries", len(self.queue))))

    async def _h_delete(self, req: web.Request) -> web.StreamResponse:
        idx = int(req.query.get("id", "-1"))
        if 0 <= idx < len(self.queue):
            del self.queue[idx]
            self.changed()
        return self._reply(self.xml.el("deleted", idx))

    async def _h_move(self, req: web.Request) -> web.StreamResponse:
        old, new = int(req.query.get("old", "0")), int(req.query.get("new", "0"))
        if 0 <= old < len(self.queue):
            self.queue.insert(max(0, min(new, len(self.queue) - 1)), self.queue.pop(old))
            self.changed()
        return self._reply(self.xml.el("moved", "moved"))

    async def _h_add(self, req: web.Request) -> web.StreamResponse:
        q = req.query
        self.queue.append(
            {
                "title": q.get("title", q.get("file", "Track")),
                "artist": "",
                "album": "",
                "secs": 180,
            }
        )
        self.changed()
        attrs = [("id", len(self.queue) - 1), ("count", 1), ("length", len(self.queue))]
        return self._reply(self.xml.el("addsong", attrs=attrs))

    # -- reboot ---------------------------------------------------------------------------

    async def _h_reboot(self, _req: web.Request) -> web.StreamResponse:
        devs = (
            self.chassis
            if self.knobs.fires("chassis_reboot_all", zones=len(self.chassis))
            else [self]
        )
        for d in devs:
            d._spawn(d.reboot())
        return web.Response(
            text="<html><body><h1>Rebooting</h1></body></html>", content_type="text/html"
        )

    async def reboot(self) -> None:
        """Gone (connection refused) for reboot_downtime_s, then back with a fresh state."""
        if self._down or self.server is None:
            return
        self._down = True
        self.rec.event("reboot", downtime_s=self.knobs.get("reboot_downtime_s"))
        await self.clock.sleep(0.05)  # let the reply go out
        await self.stop_all()
        if self.leader is not None:
            await self.leader.detach(self)
        for f in list(self.followers):
            await self.detach(f)
        await self.server.stop()
        await self.clock.sleep(float(self.knobs.get("reboot_downtime_s")))
        self.has_played = False
        self.item = {}
        self.source = "none"
        self.muted = False
        app = self.build_app()
        app.add_routes(self._sim_routes())
        self.server.app = app
        await self.server.start()
        self._down = False
        self.bump_sync()
        self.rec.event("rebooted")

    # -- lifecycle ------------------------------------------------------------------------

    async def on_start(self) -> None:
        REGISTRY[self.addr] = self
        n = int(self.knobs.get("zones"))
        if n > 1 and not self.ctx.options.get("zone_of"):
            for i in range(1, n):
                ctx = DeviceContext(
                    device_id=f"{self.id}-z{i + 1}",
                    profile=self.profile.with_overrides({"zones": 1}, "zone"),
                    recorder=self.recorder,
                    clock=self.clock,
                    seed=self.ctx.seed,
                    host=self.ctx.host,
                    port=self.port + 10 * i,
                    options={**self.ctx.options, "zone_of": self.id},
                )
                child = BluosDevice(ctx)
                child.name = f"{self.name} {i + 1}"
                child.chassis = self.chassis
                self.chassis.append(child)
                await child.start()
                self.zone_children.append(child)
        if self.ctx.options.get("mdns") and MdnsAdvertiser.available():
            adv = MdnsAdvertiser(
                self.mdns_service_type(),
                self.name,
                self.port,
                self.mdns_addresses(),
                self.mdns_txt(),
            )
            await adv.start()
            self._mdns.append(adv)

    async def on_stop(self) -> None:
        for child in self.zone_children:
            await child.stop()
        self.zone_children.clear()
        for adv in self._mdns:
            with contextlib.suppress(Exception):
                await adv.stop()
        self._mdns.clear()
        await self._cancel_connect()
        for t in list(self._bg):
            t.cancel()
        if self.leader is not None:
            self.leader.followers = [f for f in self.leader.followers if f is not self]
        for f in self.followers:
            f.leader = None
        if self.server is not None and REGISTRY.get(self.addr) is self:
            del REGISTRY[self.addr]

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "volume": self.volume,
            "muted": self.muted,
            "sync_stat": self.sync_stat,
            "source": self.source,
            "state_word": self.lens.state_word(),
            "leader": self.leader.id if self.leader else None,
            "followers": [f.id for f in self.followers],
            "mdns_txt": self.mdns_txt(),
            "zones": [d.id for d in self.chassis],
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "select_input":
            inp = next((i for i in self.inputs if i["id"] == payload.get("id")), None)
            if inp is None:
                raise KeyError(name)
            await self.play_input(inp)
            return inp["id"]
        if name == "load_queue":
            self.queue = [dict(t) for t in payload.get("tracks", [])]
            self.queue_index = 0
            self.pid += 1
            if payload.get("play", True) and self.queue:
                await self.play_queue_track(0)
            self.changed()
            return len(self.queue)
        if name == "set_presets":
            self.presets = [dict(p) for p in payload.get("presets", [])]
            return len(self.presets)
        if name == "network_blip":
            self.blipped = True
            self.rec.event("network_blip", addresses=self.mdns_addresses())
            for adv in self._mdns:
                adv.addresses = self.mdns_addresses()
            return self.mdns_addresses()
        if name == "reboot":
            self._spawn(self.reboot())
            return True
        if name == "set_name":
            self.name = str(payload.get("name", self.name))
            self.bump_sync()
            return self.name
        raise KeyError(name)
