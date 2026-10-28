"""Knobs of the generic DLNA/UPnP renderer family. Importing this module registers them.

Quirk IDs: ``dsh:N`` = simresearch/dlna_sonos_heos.md item N, ``DL-NN`` =
simresearch/quirks.md, ``X5`` = bug_crossmatch.md new quirk, ``ma:#N`` = Music
Assistant support issue N.
"""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "dlna"

# -- AVTransport: actions, Play, SetNext -------------------------------------------------

knob(
    "transport_actions",
    family=FAMILY,
    type="enum",
    default="accurate",
    choices=("accurate", "stale", "empty"),
    quirks=("dsh:1", "DL-06", "ma:#6425"),
    confidence="high",
    doc="CurrentTransportActions: accurate; stale = frozen at the value computed at the "
    "last SetAVTransportURI (so 'Pause' never appears while playing); empty = always ''.",
)
knob(
    "play_blocks_until_audio_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("dsh:3", "DL-05"),
    confidence="high",
    doc="Hold the SOAP Play response until the sink plays its first audio, at most this "
    "long (UDA allows 30 s). 0 = answer at once.",
)
knob(
    "play_response_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("dsh:3", "DL-05"),
    confidence="high",
    doc="Fixed extra delay before every Play response (after the play has started).",
)
knob(
    "autoplay_on_set_uri",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:2", "dsh:8"),
    confidence="high",
    doc="SetAVTransportURI starts playback by itself (no Play needed).",
)
knob(
    "previous_media_lingers_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("dsh:2",),
    confidence="high",
    doc="After SetAVTransportURI while playing, GetMediaInfo/GetTransportInfo keep "
    "reporting the previous URI and state for this long.",
)
knob(
    "set_next",
    family=FAMILY,
    type="enum",
    default="ok",
    choices=("ok", "absent", "ignored", "playing_only", "fault_701"),
    quirks=("dsh:10", "dsh:11", "dsh:12", "ma:#5852"),
    confidence="high",
    doc="SetNextAVTransportURI: ok; absent (not in the SCPD, 401); ignored (200 and "
    "reported in GetMediaInfo, but the device stops at EOF); playing_only (701 unless "
    "PLAYING, Kodi); fault_701 (always 701, low confidence Sonos S1 #5852).",
)
knob(
    "stop_when_idle_701",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:4",),
    confidence="high",
    doc="Stop on a STOPPED/NO_MEDIA_PRESENT device returns UPnP fault 701.",
)
knob(
    "skip_settle_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("dsh:5",),
    confidence="high",
    doc="Next and Seek are acknowledged at once but only take effect after this delay.",
)
knob(
    "track_gap_state",
    family=FAMILY,
    type="enum",
    default="transitioning",
    choices=("transitioning", "stopped"),
    quirks=("dsh:7",),
    confidence="high",
    doc="What TransportState says while advancing to the next item (transition_gap_s).",
)
knob(
    "pause_needs_content_length",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:6",),
    confidence="high",
    doc="Pause returns 701 when the stream had no Content-Length (chunked/endless).",
)

# -- Seek ---------------------------------------------------------------------------------

knob(
    "seek_units",
    family=FAMILY,
    type="enum",
    default="all",
    choices=("all", "rel_time_only", "rel_time_silent"),
    quirks=("dsh:13",),
    confidence="high",
    doc="all = REL_TIME and ABS_TIME; rel_time_only = other units get 710; "
    "rel_time_silent = other units return 200 and do nothing (gmrender, Kodi).",
)
knob(
    "seek_when_paused",
    family=FAMILY,
    type="enum",
    default="ok",
    choices=("ok", "error", "resume_silently"),
    quirks=("dsh:14",),
    confidence="high",
    doc="Seek while PAUSED: ok (stays paused at the target); error (701); "
    "resume_silently (starts playing, gmrender #83).",
)
knob(
    "seek_emits_event",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("dsh:14",),
    confidence="high",
    doc="False: no AVTransport LastChange for 2 s after a Seek (gmrender #252).",
)

# -- Position and metadata ----------------------------------------------------------------

knob(
    "reltime",
    family=FAMILY,
    type="enum",
    default="live",
    choices=("live", "stuck", "not_implemented"),
    quirks=("DL-01", "dsh:16", "dsh:17", "ma:#6874"),
    confidence="high",
    doc="GetPositionInfo RelTime: live; stuck at 0:00:00; or the string NOT_IMPLEMENTED.",
)
knob(
    "abs_time_not_implemented",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:16", "dsh:17"),
    confidence="high",
    doc="AbsTime (and AbsCount) report NOT_IMPLEMENTED.",
)
knob(
    "didl_malformed",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:22",),
    confidence="high",
    doc="TrackMetaData / CurrentURIMetaData come back as DIDL-Lite that does not parse "
    "(Bose SoundTouch).",
)
knob(
    "lastchange_malformed",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("DL-03",),
    confidence="high",
    doc="An AVTransport LastChange event is cut short (invalid XML).",
)
knob(
    "pause_emits_empty_uri_event",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:9",),
    confidence="high",
    doc="After PAUSED_PLAYBACK a second LastChange says CurrentTrackURI='' and "
    "CurrentTransportActions='Play,Stop' (Sony BDP-S590).",
)
knob(
    "duplicate_lastchange_and_vars",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:9", "DL-02"),
    confidence="high",
    doc="NOTIFY carries TransportState etc. as separate properties next to LastChange.",
)

# -- SOAP / HTTP encoding -----------------------------------------------------------------

knob(
    "non_utf8_bodies",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:34",),
    confidence="high",
    doc="SOAP responses and NOTIFY bodies contain a Latin-1 byte that is invalid UTF-8.",
)
knob(
    "content_length_off_by_one",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:35", "ma:#6567"),
    confidence="high",
    doc="GetMediaInfo declares a Content-Length one byte larger than its body "
    "(Busch-Jaeger BJE_Radio_iNet).",
)
knob(
    "soap_fault_http200",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:36",),
    confidence="high",
    doc="SOAP faults are sent with HTTP 200 instead of 500.",
)
knob(
    "relative_presentation_url",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:37", "ma:#6481"),
    confidence="high",
    doc="<presentationURL>/</presentationURL> instead of an absolute URL.",
)

# -- RenderingControl ---------------------------------------------------------------------

knob(
    "volume_max",
    family=FAMILY,
    type="int",
    default=100,
    quirks=("dsh:20",),
    confidence="high",
    doc="Volume allowedValueRange maximum in the RenderingControl SCPD.",
)
knob(
    "volume_before_play_ignored",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:21", "ma:#6762"),
    confidence="low",
    doc="SetVolume while not playing is acknowledged (and read back), but Play restores "
    "the level the amp had before (Onkyo).",
)
knob(
    "no_rc_events",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("ma:dlna#3474",),
    confidence="medium",
    doc="RenderingControl sends only the initial NOTIFY; volume/mute must be polled.",
)
knob(
    "rc_lastchange_namespace",
    family=FAMILY,
    type="enum",
    default="rcs",
    choices=("rcs", "avt"),
    quirks=("dsh:45",),
    confidence="high",
    doc="Namespace of the RenderingControl LastChange (avt = old gmrender bug #42).",
)

# -- Media negotiation and the HTTP fetch -------------------------------------------------

knob(
    "protocolinfo_filter",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:25", "DL-08", "DL-09"),
    confidence="medium",
    doc="SetAVTransportURI returns 714 when the DIDL res@protocolInfo MIME type is not "
    "in the sink protocol list.",
)
knob(
    "protocolinfo_empty_entries",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:26",),
    confidence="high",
    doc="GetProtocolInfo Sink contains empty entries (',,' and a trailing comma).",
)
knob(
    "head_check_on_set_uri",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("DL-07",),
    confidence="high",
    doc="SetAVTransportURI sends HEAD for the URL and returns 716 if that fails (Samsung).",
)
knob(
    "range_required",
    family=FAMILY,
    type="enum",
    default="off",
    choices=("off", "silent", "error"),
    quirks=("dsh:28", "ma:#6775"),
    confidence="high",
    doc="On Play the device probes the URL with Range bytes=0-, 12- and 16384- (the "
    "LUMIN log in #6775). Anything but 206: silent = report PLAYING with an advancing "
    "RelTime and OpenHome CodecName='' but play nothing; error = fault 716.",
)
knob(
    "range_rerequest_after_bytes",
    family=FAMILY,
    type="int",
    default=0,
    unit="bytes",
    quirks=("X5", "ma:#6775"),
    confidence="low",
    doc="After this many bytes the sink drops the connection and asks for the rest with "
    "Range: bytes=N-; a non-206 answer ends playback (Samsung, seen over Cast). 0 = off.",
)

# -- GENA ---------------------------------------------------------------------------------

knob(
    "notify_before_subscribe_response",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:40",),
    confidence="high",
    doc="The initial NOTIFY (SEQ 0) is delivered before the SUBSCRIBE 200 carrying the SID.",
)
knob(
    "granted_timeout_s",
    family=FAMILY,
    type="int",
    default=0,
    unit="s",
    quirks=("dsh:42",),
    confidence="high",
    doc="TIMEOUT granted: 0 = what was asked (1800 if none), -1 = Second-infinite, "
    ">0 = always this.",
)
knob(
    "new_sid_on_renew",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:43", "DL-04"),
    confidence="high",
    doc="A renewal answers with a new SID (the old one stops getting events).",
)
knob(
    "renew_response_without_sid",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("DL-04",),
    confidence="high",
    doc="The renewal response has no SID header.",
)
knob(
    "reject_subscribe",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:41",),
    confidence="high",
    doc="SUBSCRIBE answers 500; clients must poll.",
)
knob(
    "subscribe_ok_no_notify",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:41",),
    confidence="high",
    doc="SUBSCRIBE succeeds but no NOTIFY is ever delivered.",
)
knob(
    "event_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("dsh:44",),
    confidence="low",
    doc="Every NOTIFY is delivered this late (gmrender #251 saw 10 s).",
)

# -- OpenHome -----------------------------------------------------------------------------

knob(
    "openhome",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("dsh:50", "dsh:28"),
    confidence="high",
    doc="Also expose OpenHome Product, Info, Time and Volume (lite). Info.Details is the "
    "'really decoding' signal: CodecName='' and SampleRate=0 while faking playback.",
)
