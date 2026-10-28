"""LinkPlay wire-format helpers: hex text, UPnP times, UUID forms, DIDL, mode tables."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import escape

from ...simkit.transport import TState

# UPnP TransportState and HTTP getPlayerStatusEx "status" for each truth state
UPNP_STATE = {
    TState.NO_MEDIA: "NO_MEDIA_PRESENT",
    TState.STOPPED: "STOPPED",
    TState.LOADING: "TRANSITIONING",
    TState.BUFFERING: "TRANSITIONING",
    TState.PLAYING: "PLAYING",
    TState.PAUSED: "PAUSED_PLAYBACK",
}
HTTP_STATUS = {
    TState.NO_MEDIA: "none",
    TState.STOPPED: "stop",
    TState.LOADING: "load",
    TState.BUFFERING: "load",
    TState.PLAYING: "play",
    TState.PAUSED: "pause",
}

# source name -> (getPlayerStatusEx "mode", PlaybackStorageMedium, vendor)
# modes from wiim.consts.PlayingMode; media from linkplay2.md protocol reference
SOURCES: dict[str, tuple[str, str, str]] = {
    "ma": ("10", "NETWORK", ""),  # DLNA push (SetAVTransportURI)
    "app": ("10", "SONGLIST-NETWORK", ""),  # the WiiM app's own queue
    "airplay": ("1", "AIRPLAY", ""),
    "cast": ("5", "NETWORK", "CAST"),  # L69: Google Cast reports mode 5
    "spotify": ("31", "NETWORK", "Spotify"),
    "qobuz": ("10", "NETWORK", "Qobuz Connect"),
    "tidal": ("32", "TIDAL_CONNECT", "Tidal Connect"),
    "bluetooth": ("41", "BLUETOOTH", ""),
    "line-in": ("40", "LINE-IN", ""),
    "optical": ("43", "OPTICAL", ""),
    "hdmi": ("49", "HDMI", ""),
    "phono": ("54", "PHONO", ""),
    "aux": ("60", "AUXIN", ""),
}
PHYSICAL_INPUTS = {"bluetooth", "line-in", "optical", "hdmi", "phono", "aux"}
# switchmode:<name> argument -> source key
SWITCHMODE = {
    "wifi": "ma",
    "bluetooth": "bluetooth",
    "line-in": "line-in",
    "optical": "optical",
    "hdmi": "hdmi",
    "phono": "phono",
    "auxin": "aux",
    "aux": "aux",
}

DIDL_NS = (
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns:song="www.linkplay.com/song/" '
    'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
)


def hexenc(text: str) -> str:
    """LinkPlay's metadata encoding: upper-case hex of the UTF-8 bytes."""
    return text.encode("utf-8").hex().upper()


def hexdec(text: str) -> str:
    return bytes.fromhex(text).decode("utf-8", "replace")


def hms(seconds: float | None) -> str:
    """UPnP H+:MM:SS (LinkPlay always zero-pads hours to two digits)."""
    s = max(0, int(seconds or 0))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


_HMS = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d+))?$")


def parse_hms(text: str) -> float | None:
    m = _HMS.match(text.strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + (float(f"0.{frac}") if frac else 0.0)


def upnp_uuid(compact: str) -> str:
    """24-hex device id -> UDN-style UUID: the id plus its first 8 chars, dashed (L83)."""
    full = (compact + compact[:8]).upper()
    return f"{full[:8]}-{full[8:12]}-{full[12:16]}-{full[16:20]}-{full[20:32]}"


def full32(compact: str) -> str:
    return (compact + compact[:8]).upper()


def didl_title(meta: str) -> tuple[str | None, str | None, str | None]:
    """(title, artist, album) from a DIDL-Lite string, or Nones."""
    if not meta:
        return None, None, None
    try:
        root = ET.fromstring(meta)
    except ET.ParseError:
        return None, None, None
    found: dict[str, str | None] = {"title": None, "artist": None, "album": None}
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in found and found[tag] is None:
            found[tag] = el.text
    return found["title"], found["artist"], found["album"]


def didl(
    uri: str,
    title: str | None,
    artist: str | None = None,
    album: str | None = None,
    duration_s: float | None = None,
    art: str = "",
) -> str:
    """The DIDL a LinkPlay device reports back for an item (with its song: namespace)."""
    s = max(0, int(duration_s or 0))
    dur = f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}.000"
    return (
        f"<DIDL-Lite {DIDL_NS}><item><song:id></song:id><song:coding_f></song:coding_f>"
        f"<dc:title>{escape(title or '')}</dc:title>"
        f"<upnp:artist>{escape(artist or '')}</upnp:artist>"
        f"<upnp:album>{escape(album or '')}</upnp:album>"
        f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>"
        f'<res protocolInfo="http-get:*:audio/*:*" duration="{dur}">{escape(uri)}</res>'
        "</item></DIDL-Lite>"
    )
