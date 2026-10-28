"""UPnP surface of a LinkPlay renderer: description, SCPDs, SOAP, faults and GENA.

Services (linkplay2.md protocol reference): AVTransport (with the vendor ``GetInfoEx``
action), RenderingControl, ConnectionManager and the vendor PlayQueue
(``urn:schemas-wiimu-com:service:PlayQueue:1``).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid as uuidlib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from typing import Any

import aiohttp
from aiohttp import web

from ...simkit.clock import Clock
from ...simkit.knobs import KnobSet
from ...simkit.recorder import Emitter

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
PQ = "urn:schemas-wiimu-com:service:PlayQueue:1"

I, O = "in", "out"  # noqa: E741
Var = tuple[str, str]  # (dataType, sendEvents)

AVT_VARS: dict[str, Var] = {
    "TransportState": ("string", "no"),
    "TransportStatus": ("string", "no"),
    "TransportPlaySpeed": ("string", "no"),
    "CurrentTrack": ("ui4", "no"),
    "CurrentTrackDuration": ("string", "no"),
    "CurrentTrackMetaData": ("string", "no"),
    "CurrentTrackURI": ("string", "no"),
    "AVTransportURI": ("string", "no"),
    "AVTransportURIMetaData": ("string", "no"),
    "NextAVTransportURI": ("string", "no"),
    "NextAVTransportURIMetaData": ("string", "no"),
    "NumberOfTracks": ("ui4", "no"),
    "CurrentMediaDuration": ("string", "no"),
    "PlaybackStorageMedium": ("string", "no"),
    "RecordStorageMedium": ("string", "no"),
    "RecordMediumWriteStatus": ("string", "no"),
    "RelativeTimePosition": ("string", "no"),
    "AbsoluteTimePosition": ("string", "no"),
    "RelativeCounterPosition": ("i4", "no"),
    "AbsoluteCounterPosition": ("i4", "no"),
    "CurrentTransportActions": ("string", "no"),
    "LastChange": ("string", "yes"),
    "A_ARG_TYPE_InstanceID": ("ui4", "no"),
    "A_ARG_TYPE_SeekMode": ("string", "no"),
    "A_ARG_TYPE_SeekTarget": ("string", "no"),
    "TrackSource": ("string", "no"),
}
AVT_ACTIONS: dict[str, list[tuple[str, str, str]]] = {
    "SetAVTransportURI": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("CurrentURI", I, "AVTransportURI"),
        ("CurrentURIMetaData", I, "AVTransportURIMetaData"),
    ],
    "SetNextAVTransportURI": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("NextURI", I, "NextAVTransportURI"),
        ("NextURIMetaData", I, "NextAVTransportURIMetaData"),
    ],
    "Play": [("InstanceID", I, "A_ARG_TYPE_InstanceID"), ("Speed", I, "TransportPlaySpeed")],
    "Pause": [("InstanceID", I, "A_ARG_TYPE_InstanceID")],
    "Stop": [("InstanceID", I, "A_ARG_TYPE_InstanceID")],
    "Next": [("InstanceID", I, "A_ARG_TYPE_InstanceID")],
    "Previous": [("InstanceID", I, "A_ARG_TYPE_InstanceID")],
    "Seek": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("Unit", I, "A_ARG_TYPE_SeekMode"),
        ("Target", I, "A_ARG_TYPE_SeekTarget"),
    ],
    "GetTransportInfo": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("CurrentTransportState", O, "TransportState"),
        ("CurrentTransportStatus", O, "TransportStatus"),
        ("CurrentSpeed", O, "TransportPlaySpeed"),
    ],
    "GetPositionInfo": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("Track", O, "CurrentTrack"),
        ("TrackDuration", O, "CurrentTrackDuration"),
        ("TrackMetaData", O, "CurrentTrackMetaData"),
        ("TrackURI", O, "CurrentTrackURI"),
        ("RelTime", O, "RelativeTimePosition"),
        ("AbsTime", O, "AbsoluteTimePosition"),
        ("RelCount", O, "RelativeCounterPosition"),
        ("AbsCount", O, "AbsoluteCounterPosition"),
    ],
    "GetMediaInfo": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("NrTracks", O, "NumberOfTracks"),
        ("MediaDuration", O, "CurrentMediaDuration"),
        ("CurrentURI", O, "AVTransportURI"),
        ("CurrentURIMetaData", O, "AVTransportURIMetaData"),
        ("NextURI", O, "NextAVTransportURI"),
        ("NextURIMetaData", O, "NextAVTransportURIMetaData"),
        ("PlayMedium", O, "PlaybackStorageMedium"),
        ("RecordMedium", O, "RecordStorageMedium"),
        ("WriteStatus", O, "RecordMediumWriteStatus"),
    ],
    "GetCurrentTransportActions": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("Actions", O, "CurrentTransportActions"),
    ],
    "GetInfoEx": [  # vendor (L79)
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("CurrentTransportState", O, "TransportState"),
        ("Track", O, "CurrentTrack"),
        ("TrackDuration", O, "CurrentTrackDuration"),
        ("TrackMetaData", O, "CurrentTrackMetaData"),
        ("TrackURI", O, "CurrentTrackURI"),
        ("RelTime", O, "RelativeTimePosition"),
        ("PlayMedium", O, "PlaybackStorageMedium"),
        ("TrackSource", O, "TrackSource"),
    ],
}
RC_VARS: dict[str, Var] = {
    "Volume": ("ui2", "no"),
    "Mute": ("boolean", "no"),
    "LastChange": ("string", "yes"),
    "A_ARG_TYPE_InstanceID": ("ui4", "no"),
    "A_ARG_TYPE_Channel": ("string", "no"),
}
RC_ACTIONS: dict[str, list[tuple[str, str, str]]] = {
    "GetVolume": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("Channel", I, "A_ARG_TYPE_Channel"),
        ("CurrentVolume", O, "Volume"),
    ],
    "SetVolume": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("Channel", I, "A_ARG_TYPE_Channel"),
        ("DesiredVolume", I, "Volume"),
    ],
    "GetMute": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("Channel", I, "A_ARG_TYPE_Channel"),
        ("CurrentMute", O, "Mute"),
    ],
    "SetMute": [
        ("InstanceID", I, "A_ARG_TYPE_InstanceID"),
        ("Channel", I, "A_ARG_TYPE_Channel"),
        ("DesiredMute", I, "Mute"),
    ],
}
CM_VARS: dict[str, Var] = {
    "SourceProtocolInfo": ("string", "yes"),
    "SinkProtocolInfo": ("string", "yes"),
    "CurrentConnectionIDs": ("string", "yes"),
}
CM_ACTIONS: dict[str, list[tuple[str, str, str]]] = {
    "GetProtocolInfo": [("Source", O, "SourceProtocolInfo"), ("Sink", O, "SinkProtocolInfo")],
    "GetCurrentConnectionIDs": [("ConnectionIDs", O, "CurrentConnectionIDs")],
}
PQ_VARS: dict[str, Var] = {
    "LastChange": ("string", "yes"),
    "A_ARG_TYPE_QueueName": ("string", "no"),
    "A_ARG_TYPE_QueueContext": ("string", "no"),
    "A_ARG_TYPE_Index": ("ui4", "no"),
    "A_ARG_TYPE_LoopMode": ("ui4", "no"),
}
PQ_ACTIONS: dict[str, list[tuple[str, str, str]]] = {
    "BrowseQueue": [
        ("QueueName", I, "A_ARG_TYPE_QueueName"),
        ("QueueContext", O, "A_ARG_TYPE_QueueContext"),
    ],
    "PlayQueueWithIndex": [
        ("QueueName", I, "A_ARG_TYPE_QueueName"),
        ("Index", I, "A_ARG_TYPE_Index"),
    ],
    "SetQueueLoopMode": [("LoopMode", I, "A_ARG_TYPE_LoopMode")],
    "GetQueueLoopMode": [("LoopMode", O, "A_ARG_TYPE_LoopMode")],
    "GetKeyMapping": [("QueueContext", O, "A_ARG_TYPE_QueueContext")],
}
SINK_PROTOCOLS = (
    "http-get:*:audio/flac:*,http-get:*:audio/mpeg:*,http-get:*:audio/wav:*,"
    "http-get:*:audio/x-wav:*,http-get:*:audio/L16:*,http-get:*:audio/mp4:*,"
    "http-get:*:audio/aac:*,http-get:*:audio/ogg:*"
)

# key -> (serviceType, serviceId, vars, actions)
SERVICES: dict[str, tuple[str, str, dict[str, Var], dict[str, Any]]] = {
    "avt": (AVT, "urn:upnp-org:serviceId:AVTransport", AVT_VARS, AVT_ACTIONS),
    "rc": (RC, "urn:upnp-org:serviceId:RenderingControl", RC_VARS, RC_ACTIONS),
    "cm": (CM, "urn:upnp-org:serviceId:ConnectionManager", CM_VARS, CM_ACTIONS),
    "pq": (PQ, "urn:wiimu-com:serviceId:PlayQueue", PQ_VARS, PQ_ACTIONS),
}
EVENT_NS = {
    "avt": "urn:schemas-upnp-org:metadata-1-0/AVT/",
    "rc": "urn:schemas-upnp-org:metadata-1-0/RCS/",
    "pq": "urn:schemas-wiimu-com:metadata-1-0/PlayQueue/",
}


def scpd(variables: dict[str, Var], actions: dict[str, list[tuple[str, str, str]]]) -> str:
    acts = "".join(
        f"<action><name>{name}</name><argumentList>"
        + "".join(
            f"<argument><name>{a}</name><direction>{d}</direction>"
            f"<relatedStateVariable>{v}</relatedStateVariable></argument>"
            for a, d, v in args
        )
        + "</argumentList></action>"
        for name, args in actions.items()
    )
    svars = "".join(
        f'<stateVariable sendEvents="{ev}"><name>{n}</name><dataType>{t}</dataType></stateVariable>'
        for n, (t, ev) in variables.items()
    )
    return (
        '<?xml version="1.0"?>\n<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        f"<actionList>{acts}</actionList><serviceStateTable>{svars}</serviceStateTable></scpd>"
    )


def description(identity: dict[str, str], udn: str, service_keys: list[str]) -> str:
    services = "".join(
        f"<service><serviceType>{SERVICES[k][0]}</serviceType>"
        f"<serviceId>{SERVICES[k][1]}</serviceId><SCPDURL>/upnp/{k}/scpd.xml</SCPDURL>"
        f"<controlURL>/upnp/{k}/control</controlURL>"
        f"<eventSubURL>/upnp/{k}/event</eventSubURL></service>"
        for k in service_keys
    )
    e = {k: escape(v) for k, v in identity.items()}
    return (
        '<?xml version="1.0"?>\n<root xmlns="urn:schemas-upnp-org:device-1-0" '
        'xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
        "<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
        f"<friendlyName>{e.get('name', 'WiiM')}</friendlyName>"
        f"<manufacturer>{e.get('manufacturer', 'Linkplay')}</manufacturer>"
        f"<manufacturerURL>{e.get('manufacturer_url', 'http://www.linkplay.com')}"
        "</manufacturerURL>"
        f"<modelDescription>{e.get('model_description', 'Linkplay Wireless Speaker')}"
        "</modelDescription>"
        f"<modelName>{e.get('model', 'WiiM')}</modelName>"
        f"<modelNumber>{e.get('model_number', e.get('model', ''))}</modelNumber>"
        f"<serialNumber>{e.get('serial', '')}</serialNumber>"
        "<dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>"
        f"<UDN>{escape(udn)}</UDN><serviceList>{services}</serviceList></device></root>"
    )


def soap_args(body: str) -> dict[str, str]:
    root = ET.fromstring(body)
    return {el.tag.split("}")[-1]: (el.text or "") for el in root.iter() if len(el) == 0}


def soap_response(stype: str, action: str, out: dict[str, str]) -> web.Response:
    inner = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in out.items())
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action}Response xmlns:u="{stype}">{inner}</u:{action}Response>'
        "</s:Body></s:Envelope>"
    )
    return web.Response(text=body, content_type="text/xml", charset="utf-8")


FAULTS = {
    401: "Invalid Action",
    402: "Invalid Args",
    701: "Transition not available",
    710: "Seek mode not supported",
    711: "Illegal seek target",
    714: "Illegal MIME-type",
    716: "Resource not found",
}


class UpnpFault(Exception):
    def __init__(self, code: int, desc: str | None = None) -> None:
        super().__init__(f"{code} {desc or FAULTS.get(code, '')}")
        self.code = code
        self.desc = desc or FAULTS.get(code, "Error")


def soap_fault(code: int, desc: str) -> web.Response:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode><errorDescription>{escape(desc)}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    )
    return web.Response(status=500, text=body, content_type="text/xml", charset="utf-8")


def last_change(
    key: str, fields: list[tuple[str, dict[str, str]]], inst: str = "InstanceID"
) -> str:
    """``<Event>`` body: ``fields`` is [(tag, {attr: value})], ``val`` attr escaped."""
    parts = "".join(
        f"<{tag} " + " ".join(f'{a}="{escape(v, quote=True)}"' for a, v in attrs.items()) + "/>"
        for tag, attrs in fields
    )
    return f'<Event xmlns="{EVENT_NS[key]}"><{inst} val="0">{parts}</{inst}></Event>'


def propertyset(props: dict[str, str]) -> str:
    inner = "".join(f"<e:property><{k}>{escape(v)}</{k}></e:property>" for k, v in props.items())
    return (
        '<?xml version="1.0"?>\n'
        f'<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">{inner}</e:propertyset>'
    )


@dataclass
class Subscription:
    sid: str
    service: str
    callback: str
    expires: float
    seq: int = 0


class Gena:
    """GENA subscriptions and NOTIFY delivery for all services of one device.

    ``full_state(service)`` returns the propertyset props of the initial NOTIFY.
    """

    def __init__(
        self,
        clock: Clock,
        rec: Emitter,
        knobs: KnobSet,
        full_state: Callable[[str], dict[str, str]],
    ) -> None:
        self.clock = clock
        self.rec = rec
        self.knobs = knobs
        self.full_state = full_state
        self.subs: dict[str, Subscription] = {}
        self._session: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_sent: dict[str, str] = {}

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- HTTP handlers -------------------------------------------------------------

    async def subscribe(self, req: web.Request, service: str) -> web.Response:
        timeout = int(self.knobs.get("gena_timeout_s"))
        sid = req.headers.get("SID")
        now = self.clock.monotonic()
        if sid is not None:  # renewal
            sub = self.subs.get(sid)
            if sub is None or sub.service != service or sub.expires < now:
                self.subs.pop(sid, None)
                return web.Response(status=412, text="Precondition Failed")
            sub.expires = now + timeout
            self.rec.event("gena_renew", service=service, sid=sid)
            return web.Response(headers={"SID": sid, "TIMEOUT": f"Second-{timeout}"})
        cb = req.headers.get("CALLBACK", "").strip()
        if not cb.startswith("<") or req.headers.get("NT") != "upnp:event":
            return web.Response(status=412, text="Precondition Failed")
        callback = cb.split(">")[0].lstrip("<")
        sid = f"uuid:{uuidlib.UUID(int=self.knobs.rng.stream('gena').getrandbits(128))}"
        self.subs[sid] = Subscription(sid, service, callback, now + timeout)
        self.rec.event("gena_subscribe", service=service, sid=sid, callback=callback)
        self._spawn(self._initial(sid))
        return web.Response(headers={"SID": sid, "TIMEOUT": f"Second-{timeout}"})

    async def unsubscribe(self, req: web.Request, service: str) -> web.Response:
        sid = req.headers.get("SID", "")
        if self.subs.pop(sid, None) is None:
            return web.Response(status=412, text="Precondition Failed")
        self.rec.event("gena_unsubscribe", service=service, sid=sid)
        return web.Response()

    # -- delivery --------------------------------------------------------------------

    async def _initial(self, sid: str) -> None:
        await self.clock.sleep(0.05)
        sub = self.subs.get(sid)
        if sub is not None:
            await self._send(sub, self.full_state(sub.service), initial=True)

    def notify(self, service: str, props: dict[str, str], *, dedupe: bool = True) -> None:
        """Queue a NOTIFY with ``props`` to every live subscriber of ``service``."""
        body = propertyset(props)
        if dedupe and self._last_sent.get(service) == body:
            return  # identical bodies are not re-sent (lq:Q37)
        self._last_sent[service] = body
        now = self.clock.monotonic()
        for sub in list(self.subs.values()):
            if sub.service != service:
                continue
            if sub.expires < now:
                self.rec.event("gena_expired", service=service, sid=sub.sid)
                self.subs.pop(sub.sid, None)
                continue
            self._spawn(self._send(sub, props))

    async def _send(self, sub: Subscription, props: dict[str, str], initial: bool = False) -> None:
        if self.knobs.get("notify_delivery") == "drop_silently":
            self.rec.event("gena_dropped", service=sub.service, sid=sub.sid, quirk="L66")
            return
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        seq = sub.seq
        sub.seq += 1
        body = propertyset(props)
        self.rec.event(
            "gena_notify",
            service=sub.service,
            sid=sub.sid,
            seq=seq,
            initial=initial,
            props=sorted(props),
            body=body[:2048],
        )
        headers = {
            "HOST": sub.callback.split("/")[2],
            "CONTENT-TYPE": 'text/xml; charset="utf-8"',
            "NT": "upnp:event",
            "NTS": "upnp:propchange",
            "SID": sub.sid,
            "SEQ": str(seq),
        }
        try:
            async with self._session.request(
                "NOTIFY", sub.callback, data=body.encode(), headers=headers
            ) as resp:
                await resp.read()
        except Exception as err:
            self.rec.event("gena_notify_failed", service=sub.service, error=repr(err))
