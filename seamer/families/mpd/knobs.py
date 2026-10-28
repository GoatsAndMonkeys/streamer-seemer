"""Knobs of the MPD family. Importing this module registers them.

Quirk IDs: MP* (MPD), MO* (moOde), VO* (Volumio) from ``more_families.md``; MPY* are
Mopidy-MPD differences noted while building this family.
"""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "mpd"

# -- server / connection ----------------------------------------------------------

knob(
    "mpd_port",
    family=FAMILY,
    type="int",
    default=0,
    confidence="high",
    doc="TCP port of the MPD line protocol (real servers use 6600; 0 = a free port).",
)
knob(
    "version",
    family=FAMILY,
    type="str",
    default="0.23.5",
    quirks=("MP11",),
    confidence="medium",
    doc="Protocol version in the `OK MPD x.y.z` greeting; gates version-specific commands "
    "and fields (getvol >= 0.23, consume oneshot / Added: / lastloadedplaylist >= 0.24).",
)
knob(
    "status_dialect",
    family=FAMILY,
    type="enum",
    default="mpd",
    choices=("mpd", "mopidy"),
    quirks=("MP11", "MPY2"),
    confidence="medium",
    doc="Shape of `status`/`currentsong`: real MPD, or Mopidy-MPD (xfade always, no "
    "partition/mixrampdb/audio/duration; streams report `Time: 0`).",
)
knob(
    "max_connections",
    family=FAMILY,
    type="int",
    default=100,
    quirks=("MP3",),
    confidence="medium",
    doc="Concurrent client limit. One more connection is accepted and closed with no "
    "greeting (python-mpd2 then waits 5 s for the hello).",
)
knob(
    "greeting_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    quirks=("MP3",),
    confidence="high",
    unit="s",
    doc="Delay before the `OK MPD` greeting (> 5 s trips python-mpd2's hello timeout).",
)
knob(
    "password",
    family=FAMILY,
    type="str",
    default="",
    quirks=("MP4",),
    confidence="high",
    doc="Server password (`password` command). Empty = no password configured.",
)
knob(
    "default_permissions",
    family=FAMILY,
    type="str",
    default="",
    quirks=("MP4",),
    confidence="high",
    doc="Comma list of read,add,control,admin for an unauthenticated client. Empty = MPD's "
    "default: everything without a password, nothing once a password is set.",
)
knob(
    "connection_timeout_s",
    family=FAMILY,
    type="float",
    default=60.0,
    quirks=("MP12",),
    confidence="medium",
    unit="s",
    doc="MPD `connection_timeout`: a client that sends nothing for this long is dropped. "
    "Clients waiting in `idle` are exempt unless timeout_idle_too. 0 = never.",
)
knob(
    "timeout_idle_too",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("MP12",),
    confidence="low",
    doc="Apply connection_timeout_s to idling clients too (a NAT or proxy that drops "
    "quiet sockets).",
)
knob(
    "drop_connection",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("MP12",),
    confidence="medium",
    doc="On each line received, close the socket instead of answering. Param `which`: "
    "command (python-mpd2 keepalive `idle database`), idle (bare `idle`), or all.",
)
knob(
    "stray_noidle",
    family=FAMILY,
    type="enum",
    default="ignore",
    choices=("ignore", "ok", "ack"),
    quirks=("MP2",),
    confidence="high",
    doc="Reply to `noidle` from a client that is not idling. Real MPD ignores it; `ok`/`ack` "
    "are bogus replies that desync clients racing idle/noidle (pympd#173, #195).",
)
knob(
    "unsupported_commands",
    family=FAMILY,
    type="str",
    default="",
    quirks=("MP11",),
    confidence="medium",
    doc="Comma list of commands answered with `unknown command` (Mopidy: getvol, "
    "readpicture, albumart, binarylimit).",
)

# -- mixer --------------------------------------------------------------------------

knob(
    "mixer",
    family=FAMILY,
    type="enum",
    default="software",
    choices=("software", "hardware", "none", "failing"),
    quirks=("MP5", "MO2", "VO8"),
    confidence="high",
    doc="Output mixer. none: no volume in status and setvol ACKs; failing: volume is "
    "reported but setvol ACKs (a hardware mixer that went away).",
)
knob(
    "volume_unavailable",
    family=FAMILY,
    type="enum",
    default="omit",
    choices=("omit", "minus_one"),
    quirks=("MP5", "MO2"),
    confidence="high",
    doc="How status shows a missing mixer: no volume line (MPD >= 0.21) or `volume: -1` "
    "(older MPD, Mopidy, moOde fixed/CamillaDSP volume).",
)
knob(
    "setvol_error",
    family=FAMILY,
    type="str",
    default="problems setting volume",
    quirks=("MP5",),
    confidence="low",
    doc="Message of the `ACK [52@0] {setvol}` reply when there is no working mixer.",
)

# -- playback / stream ------------------------------------------------------------------

knob(
    "pause_in_stop_noop",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("MP8",),
    confidence="medium",
    doc="`pause 0`/`pause 1` while stopped answers OK and does nothing (real MPD). False "
    "makes `pause 0` start the current song (not real MPD; to show a client fix).",
)
knob(
    "not_seekable_without_length",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("MP10",),
    confidence="high",
    doc="seek/seekcur on an HTTP stream sent without Content-Length fails with "
    "`ACK [5@0] {} Not seekable` (pympd#166).",
)
knob(
    "report_duration",
    family=FAMILY,
    type="enum",
    default="content_length",
    choices=("content_length", "none"),
    quirks=("MP6",),
    confidence="high",
    doc="Duration of an HTTP stream: estimated from Content-Length (MA's forced length "
    "profile gives a bogus 12 h) or never reported (endless stream).",
)
knob(
    "codec_probe_fail",
    family=FAMILY,
    type="str",
    default="flac",
    quirks=("MP6",),
    confidence="high",
    doc="Comma list of codecs MPD cannot probe on an endless HTTP stream (no Content-Length): "
    "the song fails with `error: Failed to decode`.",
)
knob(
    "icy_metadata_header",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("MP6",),
    confidence="medium",
    doc="Send `Icy-MetaData: 1` like MPD's curl input. Off by default: the sim does not strip "
    "ICY blocks, so only enable it while MA's ICY setting is disabled (its default).",
)
knob(
    "art_probe_get",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("MP9",),
    confidence="high",
    doc="readpicture/albumart on an http song make the server GET the stream URL again "
    "(readpicture) or `<dir>/cover.png` (albumart).",
)
knob(
    "add_probe_get",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("MPY1",),
    confidence="medium",
    doc="`add` of an http URI fetches it once to scan it (Mopidy-Stream's lookup) before "
    "playback opens it again.",
)
knob(
    "initial_consume",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("VO6",),
    confidence="low",
    doc="Start with consume mode on (Volumio's statemachine runs MPD in consume mode): "
    "a finished song leaves the queue.",
)

# -- moOde / Volumio renderers ------------------------------------------------------------

knob(
    "output_busy",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("MO1", "VO7"),
    confidence="medium",
    doc="Another renderer holds the audio device: play answers OK, then the song fails "
    "with an `error:` line and the player stays stopped. Set by the `renderer` action.",
)
knob(
    "output_error_text",
    family=FAMILY,
    type="str",
    default='Failed to open "ALSA Default" (alsa); Failed to open ALSA device "_audioout": '
    "Device or resource busy",
    quirks=("MO1",),
    confidence="low",
    doc="The `error:` text while output_busy.",
)
knob(
    "ownqueue_clears_mpd",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("MO5",),
    confidence="medium",
    doc="A UPnP renderer takeover (`renderer` action, renderer=upnp) also clears MPD's queue "
    "(upmpdcli ownqueue=1).",
)
