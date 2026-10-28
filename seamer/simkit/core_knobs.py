"""Knobs every device has: transport timing, the audio sink, the HTTP layer and the
generic report lens. Family knobs are declared in ``seamer/families/<name>/knobs.py``.
"""

from __future__ import annotations

from .knobs import CORE, knob

# -- transport ---------------------------------------------------------------
knob(
    "load_delay_s",
    family=CORE,
    type="float",
    default=0.0,
    unit="s",
    confidence="high",
    doc="Delay between a play command and the start of the stream request.",
)
knob(
    "transition_gap_s",
    family=CORE,
    type="float",
    default=0.0,
    unit="s",
    quirks=("LP-04",),
    doc="Gap (state LOADING) when the device moves from one track to the queued next one.",
)
knob(
    "auto_advance",
    family=CORE,
    type="bool",
    default=True,
    confidence="high",
    doc="When the current stream ends and a next URI is set, play it automatically.",
)
knob(
    "set_uri_stops_playback",
    family=CORE,
    type="bool",
    default=True,
    doc="A new current URI stops what is playing (AVTransport semantics).",
)

# -- audio sink --------------------------------------------------------------
knob(
    "buffer_capacity_s",
    family=CORE,
    type="float",
    default=8.0,
    unit="s",
    doc="How far ahead of playback the device reads the stream.",
)
knob(
    "prebuffer_s",
    family=CORE,
    type="float",
    default=0.5,
    unit="s",
    doc="Audio needed before first sound (and after an underrun).",
)
knob(
    "read_mode",
    family=CORE,
    type="enum",
    default="realtime",
    choices=("realtime", "burst"),
    doc="realtime: read only while the buffer has room (TCP backpressure reaches MA). "
    "burst: read as fast as the server sends.",
)
knob(
    "on_server_close",
    family=CORE,
    type="enum",
    default="drain",
    choices=("drain", "drop", "refetch"),
    quirks=("bss:B5", "bss:B6"),
    doc="When the server ends the body: play out the buffer, discard it, or re-request.",
)
knob(
    "pause_closes_socket",
    family=CORE,
    type="bool",
    default=False,
    doc="Pause closes the HTTP connection; resume re-requests the URL.",
)
knob(
    "byte_cap",
    family=CORE,
    type="int",
    default=0,
    unit="bytes",
    quirks=("cas:C9",),
    doc="Hang up after this many bytes (0 = unlimited).",
)
knob(
    "time_cap_s",
    family=CORE,
    type="float",
    default=0.0,
    unit="s",
    quirks=("cas:C10", "CC-07"),
    doc="Hang up after this many seconds of audio received (0 = unlimited).",
)
knob(
    "probe",
    family=CORE,
    type="enum",
    default="none",
    choices=("none", "head", "range0"),
    quirks=("DL-07", "DL-15"),
    doc="Probe request before the real GET.",
)
knob(
    "require_range",
    family=CORE,
    type="bool",
    default=False,
    quirks=("dsh:28",),
    doc="Send 'Range: bytes=0-' and fail if the server does not answer 206/accept ranges.",
)
knob(
    "reject_chunked",
    family=CORE,
    type="bool",
    default=False,
    quirks=("dsh:29",),
    doc="Fail on chunked transfer encoding.",
)
knob(
    "assumed_kbps",
    family=CORE,
    type="float",
    default=320.0,
    unit="kbit/s",
    doc="Bitrate used to turn bytes into seconds when no decoder can.",
)
knob(
    "decoder",
    family=CORE,
    type="enum",
    default="auto",
    choices=("auto", "pcm", "ffmpeg", "bitrate"),
    doc="Force a decoder; auto picks by content type and ffmpeg availability.",
)

# -- HTTP command layer ------------------------------------------------------
knob(
    "latency_s",
    family=CORE,
    type="float",
    default=0.0,
    unit="s",
    quirks=("lq:Q1",),
    doc="Added latency on every protocol HTTP request.",
)
knob(
    "jitter_s",
    family=CORE,
    type="float",
    default=0.0,
    unit="s",
    doc="Uniform random extra latency 0..jitter_s (seeded).",
)
knob(
    "http_error",
    family=CORE,
    type="prob",
    default=False,
    doc="Answer protocol requests with HTTP 500. Params: status (default 500).",
)
knob(
    "hang",
    family=CORE,
    type="prob",
    default=False,
    quirks=("lq:Q2",),
    doc="Never answer a protocol request (client times out). Params: hold_s (default 60).",
)
knob(
    "offline",
    family=CORE,
    type="bool",
    default=False,
    quirks=("BL-08",),
    doc="Refuse every protocol request with 503 as if unreachable.",
)

# -- generic report lens -----------------------------------------------------
knob(
    "report_position_zero",
    family=CORE,
    type="bool",
    default=False,
    quirks=("LP-01",),
    doc="Report position 0 while playing.",
)
knob(
    "report_duration_zero",
    family=CORE,
    type="bool",
    default=False,
    quirks=("LP-02",),
    doc="Report duration 0 / unknown.",
)
knob(
    "report_position_refresh_s",
    family=CORE,
    type="float",
    default=0.0,
    unit="s",
    doc="Reported position only updates every N seconds (0 = live).",
)
knob(
    "report_stale_after_load",
    family=CORE,
    type="prob",
    default=False,
    quirks=("lq:Q17",),
    doc="After a new URI loads, keep reporting the previous position for window_s "
    "(param, default 2.0).",
)
