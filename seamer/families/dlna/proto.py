"""DLNA wire-format helpers: UPnP state names, H:MM:SS times, DIDL-Lite parsing."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import escape

from ...simkit.transport import TState

UPNP_STATE = {
    TState.NO_MEDIA: "NO_MEDIA_PRESENT",
    TState.STOPPED: "STOPPED",
    TState.LOADING: "TRANSITIONING",
    TState.BUFFERING: "TRANSITIONING",
    TState.PLAYING: "PLAYING",
    TState.PAUSED: "PAUSED_PLAYBACK",
}
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

DIDL_NS = (
    'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
)


def hms(seconds: float | None) -> str:
    """UPnP H+:MM:SS, as most renderers write it (``0:03:07``)."""
    s = max(0, int(seconds or 0))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


_HMS = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d+))?$")


def parse_hms(text: str) -> float | None:
    m = _HMS.match(text.strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + (float(f"0.{frac}") if frac else 0.0)


def didl_fields(meta: str) -> dict[str, str | None]:
    """title/artist/album/duration/protocolInfo/res from DIDL-Lite (Nones if absent)."""
    found: dict[str, str | None] = {
        "title": None,
        "artist": None,
        "album": None,
        "duration": None,
        "protocolInfo": None,
        "res": None,
    }
    if not meta:
        return found
    try:
        root = ET.fromstring(meta)
    except ET.ParseError:
        return found
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("title", "artist", "album", "creator"):
            key = "artist" if tag == "creator" else tag
            if found[key] is None:
                found[key] = el.text
        elif tag == "res" and found["res"] is None:
            found["res"] = el.text
            found["duration"] = el.get("duration")
            found["protocolInfo"] = el.get("protocolInfo")
    return found


def didl_duration(meta: str) -> float | None:
    dur = didl_fields(meta)["duration"]
    return parse_hms(dur) if dur else None


def didl_mime(meta: str) -> str | None:
    """MIME type from res@protocolInfo (``http-get:*:audio/flac:*`` -> ``audio/flac``)."""
    info = didl_fields(meta)["protocolInfo"]
    if not info:
        return None
    parts = info.split(":")
    return parts[2] if len(parts) >= 3 else None


def didl(uri: str, title: str | None, duration_s: float | None, mime: str | None) -> str:
    """The DIDL-Lite a generic renderer echoes back for the current item."""
    s = max(0, int(duration_s or 0))
    dur = f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}.000"
    return (
        f'<DIDL-Lite {DIDL_NS}><item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title or '')}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:{escape(mime or "audio/*")}:*" duration="{dur}">'
        f"{escape(uri)}</res></item></DIDL-Lite>"
    )


def codec_name(content_type: str | None) -> str:
    """OpenHome Info.Details CodecName for a stream Content-Type."""
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "audio/flac": "FLAC",
        "audio/x-flac": "FLAC",
        "audio/wav": "WAV",
        "audio/x-wav": "WAV",
        "audio/wave": "WAV",
        "audio/mpeg": "MP3",
        "audio/mp3": "MP3",
        "audio/l16": "PCM",
        "audio/aac": "AAC",
        "audio/mp4": "ALAC",
    }.get(ct, ct.split("/")[-1].upper() if ct else "")
