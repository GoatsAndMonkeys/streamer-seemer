"""Optional OpenHome services (knob ``openhome``): Product, Info, Time and Volume.

LUMIN, Linn and many streamer DACs expose these next to UPnP AV. The useful part for
MA bugs is ``Info.Details``: a renderer that "plays" without decoding anything shows
``CodecName=""`` and ``SampleRate=0`` (the #6775 LUMIN log, dsh:28), which is a check
a control point could make but MA's dlna provider does not.

Evented state variables are sent as plain properties (OpenHome does not use LastChange).
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

from ...simkit.transport import TState
from . import proto, upnp

if TYPE_CHECKING:
    from .device import DlnaDevice

I, O = upnp.I, upnp.O  # noqa: E741  (In/Out argument markers, as in upnp.py)

PRODUCT = "urn:av-openhome-org:service:Product:1"
INFO = "urn:av-openhome-org:service:Info:1"
TIME = "urn:av-openhome-org:service:Time:1"
VOLUME = "urn:av-openhome-org:service:Volume:1"

PRODUCT_ACTIONS: dict[str, upnp.Args] = {
    "Manufacturer": [
        ("Name", O, "ManufacturerName"),
        ("Info", O, "ManufacturerInfo"),
        ("Url", O, "ManufacturerUrl"),
        ("ImageUri", O, "ManufacturerImageUri"),
    ],
    "Model": [
        ("Name", O, "ModelName"),
        ("Info", O, "ModelInfo"),
        ("Url", O, "ModelUrl"),
        ("ImageUri", O, "ModelImageUri"),
    ],
    "Product": [
        ("Room", O, "ProductRoom"),
        ("Name", O, "ProductName"),
        ("Info", O, "ProductInfo"),
        ("Url", O, "ProductUrl"),
        ("ImageUri", O, "ProductImageUri"),
    ],
    "Standby": [("Value", O, "Standby")],
    "SetStandby": [("Value", I, "Standby")],
    "SourceCount": [("Value", O, "SourceCount")],
    "SourceXml": [("Value", O, "SourceXml")],
    "SourceIndex": [("Value", O, "SourceIndex")],
    "SetSourceIndex": [("Value", I, "SourceIndex")],
    "Source": [
        ("Index", I, "SourceIndex"),
        ("SystemName", O, "SourceSystemName"),
        ("Type", O, "SourceType"),
        ("Name", O, "SourceName"),
        ("Visible", O, "SourceVisible"),
    ],
}
INFO_ACTIONS: dict[str, upnp.Args] = {
    "Counters": [
        ("TrackCount", O, "TrackCount"),
        ("DetailsCount", O, "DetailsCount"),
        ("MetatextCount", O, "MetatextCount"),
    ],
    "Track": [("Uri", O, "Uri"), ("Metadata", O, "Metadata")],
    "Details": [
        ("Duration", O, "Duration"),
        ("BitRate", O, "BitRate"),
        ("BitDepth", O, "BitDepth"),
        ("SampleRate", O, "SampleRate"),
        ("Lossless", O, "Lossless"),
        ("CodecName", O, "CodecName"),
    ],
    "Metatext": [("Value", O, "Metatext")],
}
TIME_ACTIONS: dict[str, upnp.Args] = {
    "Time": [
        ("TrackCount", O, "TrackCount"),
        ("Duration", O, "Duration"),
        ("Seconds", O, "Seconds"),
    ],
}
VOLUME_ACTIONS: dict[str, upnp.Args] = {
    "Volume": [("Value", O, "Volume")],
    "SetVolume": [("Value", I, "Volume")],
    "VolumeInc": [],
    "VolumeDec": [],
    "Mute": [("Value", O, "Mute")],
    "SetMute": [("Value", I, "Mute")],
    "VolumeLimit": [("Value", O, "VolumeLimit")],
    "Characteristics": [
        ("VolumeMax", O, "VolumeMax"),
        ("VolumeUnity", O, "VolumeUnity"),
        ("VolumeSteps", O, "VolumeSteps"),
        ("VolumeMilliDbPerStep", O, "VolumeMilliDbPerStep"),
        ("BalanceMax", O, "BalanceMax"),
        ("FadeMax", O, "FadeMax"),
    ],
}
_UI4 = {
    "SourceCount",
    "SourceIndex",
    "TrackCount",
    "DetailsCount",
    "MetatextCount",
    "Duration",
    "BitRate",
    "BitDepth",
    "SampleRate",
    "Seconds",
    "Volume",
    "VolumeLimit",
    "VolumeMax",
    "VolumeUnity",
    "VolumeSteps",
    "VolumeMilliDbPerStep",
    "BalanceMax",
    "FadeMax",
}
_BOOL = {"Standby", "Lossless", "Mute", "SourceVisible"}
_EVENTED = {
    "ohproduct": {"ManufacturerName", "ModelName", "ProductName", "Standby", "SourceIndex"},
    "ohinfo": {
        "TrackCount",
        "DetailsCount",
        "MetatextCount",
        "Uri",
        "Metadata",
        "Duration",
        "BitRate",
        "BitDepth",
        "SampleRate",
        "Lossless",
        "CodecName",
        "Metatext",
    },
    "ohtime": {"TrackCount", "Duration", "Seconds"},
    "ohvolume": {"Volume", "Mute", "VolumeLimit", "VolumeMax"},
}


def _vars(key: str, actions: dict[str, upnp.Args]) -> dict[str, upnp.Var]:
    out: dict[str, upnp.Var] = {}
    for args in actions.values():
        for _name, _d, var in args:
            dtype = "ui4" if var in _UI4 else "boolean" if var in _BOOL else "string"
            out[var] = (dtype, "yes" if var in _EVENTED[key] else "no")
    return out


OH_SERVICES: dict[str, tuple[str, str, dict[str, upnp.Var], dict[str, upnp.Args]]] = {
    "ohproduct": (
        PRODUCT,
        "urn:av-openhome-org:serviceId:Product",
        _vars("ohproduct", PRODUCT_ACTIONS),
        PRODUCT_ACTIONS,
    ),
    "ohinfo": (
        INFO,
        "urn:av-openhome-org:serviceId:Info",
        _vars("ohinfo", INFO_ACTIONS),
        INFO_ACTIONS,
    ),
    "ohtime": (
        TIME,
        "urn:av-openhome-org:serviceId:Time",
        _vars("ohtime", TIME_ACTIONS),
        TIME_ACTIONS,
    ),
    "ohvolume": (
        VOLUME,
        "urn:av-openhome-org:serviceId:Volume",
        _vars("ohvolume", VOLUME_ACTIONS),
        VOLUME_ACTIONS,
    ),
}
upnp.SERVICES.update(OH_SERVICES)

SOURCES = (  # (SystemName, Type, Name, Visible)
    ("Playlist", "Playlist", "Playlist", True),
    ("Radio", "Radio", "Radio", True),
    ("UPnP AV", "UpnpAv", "UPnP AV", False),
    ("Songcast", "Receiver", "Songcast", True),
)
UPNP_AV_INDEX = 2


def source_xml() -> str:
    items = "".join(
        f"<Source><Name>{escape(n)}</Name><Type>{t}</Type>"
        f"<Visible>{'true' if v else 'false'}</Visible></Source>"
        for _s, t, n, v in SOURCES
    )
    return f"<SourceList>{items}</SourceList>"


def _b(v: bool) -> str:
    return "true" if v else "false"


class OpenHome:
    """Action handlers and evented state for the four OpenHome services."""

    def __init__(self, dev: DlnaDevice) -> None:
        self.dev = dev
        self.standby = False
        self.source_index = UPNP_AV_INDEX
        self.track_count = 0
        self.details_count = 0

    # -- state -----------------------------------------------------------------------

    def details(self) -> dict[str, str]:
        """What the device says it is decoding (CodecName/SampleRate are the tell)."""
        d = self.dev
        dur = d.lens.duration()
        sink = d.sink
        decoding = (
            d.fake_play is None
            and d.transport.state in (TState.PLAYING, TState.PAUSED)
            and sink is not None
            and sink.decoder_name is not None
        )
        if not decoding:
            return {
                "Duration": str(int(dur or 0)) if d.fake_play is not None else "0",
                "BitRate": "0",
                "BitDepth": "0",
                "SampleRate": "0",
                "Lossless": "false",
                "CodecName": "",
            }
        assert sink is not None
        codec = proto.codec_name(d.stream_ctype)
        rate = int(getattr(sink, "_pcm_rate", 44100))
        chans = int(getattr(sink, "_pcm_channels", 2))
        lossless = codec in ("FLAC", "WAV", "PCM", "ALAC")
        return {
            "Duration": str(int(dur or 0)),
            "BitRate": str(rate * chans * 16 if lossless else 320000),
            "BitDepth": "16" if lossless else "0",
            "SampleRate": str(rate),
            "Lossless": _b(lossless),
            "CodecName": codec,
        }

    def full_state(self, key: str) -> dict[str, str]:
        d = self.dev
        if key == "ohproduct":
            ident = d.identity
            return {
                "ManufacturerName": ident.get("manufacturer", ""),
                "ModelName": ident.get("model", ""),
                "ProductName": ident.get("name", ""),
                "Standby": _b(self.standby),
                "SourceIndex": str(self.source_index),
            }
        if key == "ohinfo":
            uri, meta = d.reported_uri()
            return {
                "TrackCount": str(self.track_count),
                "DetailsCount": str(self.details_count),
                "MetatextCount": "0",
                "Uri": uri,
                "Metadata": meta,
                **self.details(),
                "Metatext": "",
            }
        if key == "ohtime":
            pos = d.lens.position() or 0.0
            return {
                "TrackCount": str(self.track_count),
                "Duration": str(int(d.lens.duration() or 0)),
                "Seconds": str(int(pos)),
            }
        return {
            "Volume": str(d.volume),
            "Mute": _b(d.muted),
            "VolumeLimit": str(d.volume_max),
            "VolumeMax": str(d.volume_max),
        }

    def changed(self, *keys: str) -> None:
        for key in keys or ("ohinfo", "ohtime"):
            self.dev.gena.notify(key, self.full_state(key))

    def on_track(self) -> None:
        self.track_count += 1
        self.details_count += 1
        self.changed("ohinfo", "ohtime")

    # -- dispatch ----------------------------------------------------------------------

    async def handle(self, key: str, action: str, a: dict[str, str]) -> dict[str, str]:
        d = self.dev
        st = self.full_state(key)
        if key == "ohproduct":
            ident = d.identity
            if action == "Manufacturer":
                return {
                    "Name": ident.get("manufacturer", ""),
                    "Info": "",
                    "Url": ident.get("manufacturer_url", ""),
                    "ImageUri": "",
                }
            if action == "Model":
                return {"Name": ident.get("model", ""), "Info": "", "Url": "", "ImageUri": ""}
            if action == "Product":
                return {
                    "Room": ident.get("room", "Main Room"),
                    "Name": ident.get("name", ""),
                    "Info": "",
                    "Url": "",
                    "ImageUri": "",
                }
            if action == "Standby":
                return {"Value": st["Standby"]}
            if action == "SetStandby":
                self.standby = a.get("Value", "").lower() in ("1", "true")
                self.changed("ohproduct")
                return {}
            if action == "SourceCount":
                return {"Value": str(len(SOURCES))}
            if action == "SourceXml":
                return {"Value": source_xml()}
            if action == "SourceIndex":
                return {"Value": str(self.source_index)}
            if action == "SetSourceIndex":
                idx = _int(a.get("Value"))
                if idx is None or not 0 <= idx < len(SOURCES):
                    raise upnp.UpnpFault(800, "Source index out of range")
                self.source_index = idx
                self.changed("ohproduct")
                return {}
            idx = _int(a.get("Index"))
            if idx is None or not 0 <= idx < len(SOURCES):
                raise upnp.UpnpFault(800, "Source index out of range")
            sysname, typ, name, vis = SOURCES[idx]
            return {"SystemName": sysname, "Type": typ, "Name": name, "Visible": _b(vis)}
        if key == "ohinfo":
            if action == "Counters":
                return {k: st[k] for k in ("TrackCount", "DetailsCount", "MetatextCount")}
            if action == "Track":
                return {"Uri": st["Uri"], "Metadata": st["Metadata"]}
            if action == "Details":
                return self.details()
            return {"Value": ""}
        if key == "ohtime":
            return st
        if action == "Volume":
            return {"Value": str(d.volume)}
        if action in ("SetVolume", "VolumeInc", "VolumeDec"):
            level = (
                _int(a.get("Value"))
                if action == "SetVolume"
                else d.volume + (1 if action == "VolumeInc" else -1)
            )
            if level is None or not 0 <= level <= d.volume_max:
                raise upnp.UpnpFault(402, "Invalid Args")
            d.set_volume(level, via="openhome")
            return {}
        if action == "Mute":
            return {"Value": _b(d.muted)}
        if action == "SetMute":
            d.set_mute(a.get("Value", "").lower() in ("1", "true"), via="openhome")
            return {}
        if action == "VolumeLimit":
            return {"Value": str(d.volume_max)}
        return {
            "VolumeMax": str(d.volume_max),
            "VolumeUnity": str(d.volume_max),
            "VolumeSteps": str(d.volume_max),
            "VolumeMilliDbPerStep": "1024",
            "BalanceMax": "0",
            "FadeMax": "0",
        }


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
