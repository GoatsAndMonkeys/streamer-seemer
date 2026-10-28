"""UPnP surface of a generic DLNA MediaRenderer: description, SCPDs, SOAP, faults, GENA.

Started as a copy of the LinkPlay family's UPnP module (generic parts only), then
extended: SCPD allowed values/ranges (async_upnp_client reads SeekMode and the Volume
range from them), presentationURL, GetCurrentConnectionInfo, optional OpenHome
services, and the DLNA GENA quirks (dsh:40-44).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid as uuidlib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
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

I, O = "in", "out"  # noqa: E741
Var = tuple[str, str]  # (dataType, sendEvents)
Args = list[tuple[str, str, str]]  # (name, direction, relatedStateVariable)

AVT_VARS: dict[str, Var] = {
    "TransportState": ("string", "no"),
    "TransportStatus": ("string", "no"),
    "TransportPlaySpeed": ("string", "no"),
    "CurrentPlayMode": ("string", "no"),
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
}
_IID = ("InstanceID", I, "A_ARG_TYPE_InstanceID")
AVT_ACTIONS: dict[str, Args] = {
    "SetAVTransportURI": [
        _IID,
        ("CurrentURI", I, "AVTransportURI"),
        ("CurrentURIMetaData", I, "AVTransportURIMetaData"),
    ],
    "SetNextAVTransportURI": [
        _IID,
        ("NextURI", I, "NextAVTransportURI"),
        ("NextURIMetaData", I, "NextAVTransportURIMetaData"),
    ],
    "Play": [_IID, ("Speed", I, "TransportPlaySpeed")],
    "Pause": [_IID],
    "Stop": [_IID],
    "Next": [_IID],
    "Previous": [_IID],
    "Seek": [
        _IID,
        ("Unit", I, "A_ARG_TYPE_SeekMode"),
        ("Target", I, "A_ARG_TYPE_SeekTarget"),
    ],
    "GetTransportInfo": [
        _IID,
        ("CurrentTransportState", O, "TransportState"),
        ("CurrentTransportStatus", O, "TransportStatus"),
        ("CurrentSpeed", O, "TransportPlaySpeed"),
    ],
    "GetPositionInfo": [
        _IID,
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
        _IID,
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
    "GetTransportSettings": [
        _IID,
        ("PlayMode", O, "CurrentPlayMode"),
        ("RecQualityMode", O, "TransportStatus"),
    ],
    "GetCurrentTransportActions": [_IID, ("Actions", O, "CurrentTransportActions")],
}
RC_VARS: dict[str, Var] = {
    "Volume": ("ui2", "no"),
    "Mute": ("boolean", "no"),
    "PresetNameList": ("string", "no"),
    "LastChange": ("string", "yes"),
    "A_ARG_TYPE_InstanceID": ("ui4", "no"),
    "A_ARG_TYPE_Channel": ("string", "no"),
    "A_ARG_TYPE_PresetName": ("string", "no"),
}
_CH = ("Channel", I, "A_ARG_TYPE_Channel")
RC_ACTIONS: dict[str, Args] = {
    "ListPresets": [_IID, ("CurrentPresetNameList", O, "PresetNameList")],
    "SelectPreset": [_IID, ("PresetName", I, "A_ARG_TYPE_PresetName")],
    "GetVolume": [_IID, _CH, ("CurrentVolume", O, "Volume")],
    "SetVolume": [_IID, _CH, ("DesiredVolume", I, "Volume")],
    "GetMute": [_IID, _CH, ("CurrentMute", O, "Mute")],
    "SetMute": [_IID, _CH, ("DesiredMute", I, "Mute")],
}
CM_VARS: dict[str, Var] = {
    "SourceProtocolInfo": ("string", "yes"),
    "SinkProtocolInfo": ("string", "yes"),
    "CurrentConnectionIDs": ("string", "yes"),
    "A_ARG_TYPE_ConnectionStatus": ("string", "no"),
    "A_ARG_TYPE_ConnectionManager": ("string", "no"),
    "A_ARG_TYPE_Direction": ("string", "no"),
    "A_ARG_TYPE_ProtocolInfo": ("string", "no"),
    "A_ARG_TYPE_ConnectionID": ("i4", "no"),
    "A_ARG_TYPE_AVTransportID": ("i4", "no"),
    "A_ARG_TYPE_RcsID": ("i4", "no"),
}
CM_ACTIONS: dict[str, Args] = {
    "GetProtocolInfo": [("Source", O, "SourceProtocolInfo"), ("Sink", O, "SinkProtocolInfo")],
    "GetCurrentConnectionIDs": [("ConnectionIDs", O, "CurrentConnectionIDs")],
    "GetCurrentConnectionInfo": [
        ("ConnectionID", I, "A_ARG_TYPE_ConnectionID"),
        ("RcsID", O, "A_ARG_TYPE_RcsID"),
        ("AVTransportID", O, "A_ARG_TYPE_AVTransportID"),
        ("ProtocolInfo", O, "A_ARG_TYPE_ProtocolInfo"),
        ("PeerConnectionManager", O, "A_ARG_TYPE_ConnectionManager"),
        ("PeerConnectionID", O, "A_ARG_TYPE_ConnectionID"),
        ("Direction", O, "A_ARG_TYPE_Direction"),
        ("Status", O, "A_ARG_TYPE_ConnectionStatus"),
    ],
}
TRANSPORT_STATES = (
    "STOPPED",
    "PLAYING",
    "PAUSED_PLAYBACK",
    "TRANSITIONING",
    "NO_MEDIA_PRESENT",
)
SINK_PROTOCOLS = (
    "http-get:*:audio/flac:*",
    "http-get:*:audio/x-flac:*",
    "http-get:*:audio/mpeg:*",
    "http-get:*:audio/wav:*",
    "http-get:*:audio/x-wav:*",
    "http-get:*:audio/L16;rate=44100;channels=2:DLNA.ORG_PN=LPCM",
    "http-get:*:audio/L16;rate=48000;channels=2:DLNA.ORG_PN=LPCM",
    "http-get:*:audio/mp4:*",
    "http-get:*:audio/aac:*",
)

# key -> (serviceType, serviceId, vars, actions); OpenHome keys are added by openhome.py
SERVICES: dict[str, tuple[str, str, dict[str, Var], dict[str, Args]]] = {
    "avt": (AVT, "urn:upnp-org:serviceId:AVTransport", AVT_VARS, AVT_ACTIONS),
    "rc": (RC, "urn:upnp-org:serviceId:RenderingControl", RC_VARS, RC_ACTIONS),
    "cm": (CM, "urn:upnp-org:serviceId:ConnectionManager", CM_VARS, CM_ACTIONS),
}
EVENT_NS = {
    "avt": "urn:schemas-upnp-org:metadata-1-0/AVT/",
    "rc": "urn:schemas-upnp-org:metadata-1-0/RCS/",
}


@dataclass
class VarSpec:
    """Per-device SCPD extras: allowedValueList / allowedValueRange by variable name."""

    allowed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    ranges: dict[str, tuple[int, int, int]] = field(default_factory=dict)


def scpd(variables: dict[str, Var], actions: dict[str, Args], spec: VarSpec | None = None) -> str:
    spec = spec or VarSpec()
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
    svars = []
    for n, (t, ev) in variables.items():
        extra = ""
        if n in spec.allowed:
            extra = (
                "<allowedValueList>"
                + "".join(f"<allowedValue>{escape(v)}</allowedValue>" for v in spec.allowed[n])
                + "</allowedValueList>"
            )
        elif n in spec.ranges:
            lo, hi, step = spec.ranges[n]
            extra = (
                f"<allowedValueRange><minimum>{lo}</minimum><maximum>{hi}</maximum>"
                f"<step>{step}</step></allowedValueRange>"
            )
        svars.append(
            f'<stateVariable sendEvents="{ev}"><name>{n}</name><dataType>{t}</dataType>'
            f"{extra}</stateVariable>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        f"<actionList>{acts}</actionList>"
        f"<serviceStateTable>{''.join(svars)}</serviceStateTable></scpd>"
    )


def description(
    identity: dict[str, str], udn: str, service_keys: list[str], presentation_url: str
) -> str:
    services = "".join(
        f"<service><serviceType>{SERVICES[k][0]}</serviceType>"
        f"<serviceId>{SERVICES[k][1]}</serviceId><SCPDURL>/upnp/{k}/scpd.xml</SCPDURL>"
        f"<controlURL>/upnp/{k}/control</controlURL>"
        f"<eventSubURL>/upnp/{k}/event</eventSubURL></service>"
        for k in service_keys
    )
    e = {k: escape(v) for k, v in identity.items()}
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0" '
        'xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
        "<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
        f"<friendlyName>{e.get('name', 'DLNA Renderer')}</friendlyName>"
        f"<manufacturer>{e.get('manufacturer', 'Generic')}</manufacturer>"
        f"<manufacturerURL>{e.get('manufacturer_url', 'http://example.com/')}"
        "</manufacturerURL>"
        f"<modelDescription>{e.get('model_description', 'Media Renderer')}"
        "</modelDescription>"
        f"<modelName>{e.get('model', 'Renderer')}</modelName>"
        f"<modelNumber>{e.get('model_number', e.get('model', ''))}</modelNumber>"
        f"<serialNumber>{e.get('serial', '')}</serialNumber>"
        "<dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>"
        f"<UDN>{escape(udn)}</UDN>"
        f"<presentationURL>{escape(presentation_url)}</presentationURL>"
        f"<serviceList>{services}</serviceList></device></root>"
    )


def soap_args(body: str) -> dict[str, str]:
    root = ET.fromstring(body)
    return {el.tag.split("}")[-1]: (el.text or "") for el in root.iter() if len(el) == 0}


ENVELOPE_OPEN = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
)
ENVELOPE_CLOSE = "</s:Body></s:Envelope>"
# dsh:34: one Latin-1 byte (0xE9) that is not valid UTF-8, inside a comment
NON_UTF8_COMMENT = b"<!--caf\xe9-->"


def soap_body(stype: str, action: str, out: dict[str, str]) -> str:
    inner = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in out.items())
    return (
        f'{ENVELOPE_OPEN}<u:{action}Response xmlns:u="{stype}">{inner}'
        f"</u:{action}Response>{ENVELOPE_CLOSE}"
    )


FAULTS = {
    401: "Invalid Action",
    402: "Invalid Args",
    701: "Transition not available",
    710: "Seek mode not supported",
    711: "Illegal seek target",
    714: "Illegal MIME-type",
    716: "Resource not found",
    718: "Invalid InstanceID",
}


class UpnpFault(Exception):
    def __init__(self, code: int, desc: str | None = None) -> None:
        super().__init__(f"{code} {desc or FAULTS.get(code, '')}")
        self.code = code
        self.desc = desc or FAULTS.get(code, "Error")


def fault_body(code: int, desc: str) -> str:
    return (
        f"{ENVELOPE_OPEN}<s:Fault>"
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode><errorDescription>{escape(desc)}</errorDescription>"
        f"</UPnPError></detail></s:Fault>{ENVELOPE_CLOSE}"
    )


def xml_response(body: str, *, status: int = 200, non_utf8: bool = False) -> web.Response:
    raw = body.encode("utf-8")
    if non_utf8:
        cut = raw.rfind(b"</s:Body>")
        raw = raw[:cut] + NON_UTF8_COMMENT + raw[cut:] if cut >= 0 else raw + NON_UTF8_COMMENT
    return web.Response(
        status=status, body=raw, headers={"Content-Type": 'text/xml; charset="utf-8"'}
    )


def last_change(ns: str, fields: list[tuple[str, dict[str, str]]], inst: str = "InstanceID") -> str:
    """``<Event>`` body: ``fields`` is [(tag, {attr: value})]; values are escaped."""
    parts = "".join(
        f"<{tag} " + " ".join(f'{a}="{escape(v, quote=True)}"' for a, v in attrs.items()) + "/>"
        for tag, attrs in fields
    )
    return f'<Event xmlns="{ns}"><{inst} val="0">{parts}</{inst}></Event>'


def propertyset(props: dict[str, str]) -> str:
    inner = "".join(f"<e:property><{k}>{escape(v)}</{k}></e:property>" for k, v in props.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">{inner}</e:propertyset>'
    )


@dataclass
class Subscription:
    sid: str
    service: str
    callback: str
    expires: float
    seq: int = 0


DEFAULT_TIMEOUT_S = 1800


def _requested_timeout(header: str | None) -> int | None:
    if not header:
        return None
    val = header.strip().lower()
    if val.startswith("second-"):
        val = val[7:]
    if val == "infinite":
        return None
    try:
        return int(val)
    except ValueError:
        return None


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

    def _new_sid(self) -> str:
        return f"uuid:{uuidlib.UUID(int=self.knobs.rng.stream('gena').getrandbits(128))}"

    def _granted(self, req: web.Request) -> tuple[int | None, str]:
        """(seconds or None for infinite, TIMEOUT header value)."""
        fixed = int(self.knobs.get("granted_timeout_s"))
        if fixed < 0:
            self.rec.knob("fired", knob="granted_timeout_s", quirks=["dsh:42"], value=fixed)
            return None, "Second-infinite"
        if fixed > 0:
            return fixed, f"Second-{fixed}"
        secs = _requested_timeout(req.headers.get("TIMEOUT")) or DEFAULT_TIMEOUT_S
        return secs, f"Second-{secs}"

    # -- HTTP handlers -------------------------------------------------------------

    async def subscribe(self, req: web.Request, service: str) -> web.Response:
        if self.knobs.fires("reject_subscribe", service=service):
            return web.Response(status=500, text="Internal Server Error")
        secs, header = self._granted(req)
        sid = req.headers.get("SID")
        now = self.clock.monotonic()
        expires = now + secs if secs is not None else float("inf")
        if sid is not None:  # renewal
            if req.headers.get("CALLBACK") or req.headers.get("NT"):
                return web.Response(status=400, text="Incompatible header fields")
            sub = self.subs.get(sid)
            if sub is None or sub.service != service or sub.expires < now:
                self.subs.pop(sid, None)
                return web.Response(status=412, text="Precondition Failed")
            sub.expires = expires
            if self.knobs.fires("new_sid_on_renew", service=service, old_sid=sid):
                self.subs.pop(sid)
                sub.sid = self._new_sid()
                self.subs[sub.sid] = sub
            self.rec.event("gena_renew", service=service, sid=sub.sid, old_sid=sid)
            headers = {"SID": sub.sid, "TIMEOUT": header}
            if self.knobs.fires("renew_response_without_sid", service=service):
                headers.pop("SID")
            return web.Response(headers=headers)
        cb = req.headers.get("CALLBACK", "").strip()
        if not cb.startswith("<") or req.headers.get("NT") != "upnp:event":
            return web.Response(status=412, text="Precondition Failed")
        callback = cb.split(">")[0].lstrip("<")
        sid = self._new_sid()
        sub = Subscription(sid, service, callback, expires)
        self.subs[sid] = sub
        self.rec.event(
            "gena_subscribe", service=service, sid=sid, callback=callback, timeout=header
        )
        if self.knobs.fires("notify_before_subscribe_response", service=service, sid=sid):
            # dsh:40: the initial NOTIFY reaches the control point before the SID does
            await self._send(sub, self.full_state(service), initial=True)
        else:
            self._spawn(self._initial(sid))
        return web.Response(headers={"SID": sid, "TIMEOUT": header})

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
            return
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
        if self.knobs.get("subscribe_ok_no_notify"):
            self.rec.event("gena_dropped", service=sub.service, sid=sub.sid, quirk="dsh:41")
            return
        delay = float(self.knobs.get("event_delay_s"))
        if delay > 0:
            await self.clock.sleep(delay)
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        seq = sub.seq
        sub.seq += 1
        body = propertyset(props)
        raw = body.encode("utf-8")
        if self.knobs.get("non_utf8_bodies"):
            cut = raw.rfind(b"</e:propertyset>")
            raw = raw[:cut] + NON_UTF8_COMMENT + raw[cut:]
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
                "NOTIFY", sub.callback, data=raw, headers=headers
            ) as resp:
                await resp.read()
                if resp.status >= 400:
                    self.rec.event(
                        "gena_notify_rejected", service=sub.service, seq=seq, status=resp.status
                    )
        except Exception as err:
            self.rec.event("gena_notify_failed", service=sub.service, error=repr(err))
