"""Knobs of the squeezelite (slimproto) family. Importing this module registers them.

Quirk ids refer to the squeezelite/slimproto rows of the research quirk table
(``S*``, ``SQ-*``, ``X1``, ``Y11``) and the MA support issues they cite.
"""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "squeezelite"

# -- connection ---------------------------------------------------------------------
knob(
    "server",
    family=FAMILY,
    type="str",
    default="",
    doc="host:port of the slimproto server to connect to at start ('' = wait for the "
    "connect/discover sim action).",
)
knob(
    "reconnect_delay_s",
    family=FAMILY,
    type="float",
    default=1.0,
    unit="s",
    doc="Wait before reconnecting after the server closes the control connection.",
)
knob(
    "helo_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("Y11",),
    confidence="low",
    doc="Delay between TCP connect and HELO (some firmware takes 6-8 s; slimserver#1652).",
)
knob(
    "server_silence_timeout_s",
    family=FAMILY,
    type="float",
    default=35.0,
    unit="s",
    quirks=("X1",),
    doc="Drop and reconnect when nothing arrives from the server for this long "
    "(squeezelite: 35 s; Eversolo-style firmware ~120 s, MA #6800). 0 = never.",
)
knob(
    "batch_frames_per_recv",
    family=FAMILY,
    type="int",
    default=1,
    quirks=("S14",),
    doc="Coalesce this many client frames into one TCP write (servers must split them).",
)
knob(
    "send_bye_keep_socket",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("S13",),
    confidence="low",
    doc="The 'bye' action sends BYE! but leaves the socket open (MA #6581).",
)

# -- identity / capabilities --------------------------------------------------------
knob(
    "hello_caps",
    family=FAMILY,
    type="str",
    default="",
    quirks=("SQ-01", "S11"),
    doc="Replace the whole HELO capability string ('' = built from the identity).",
)
knob(
    "can_https",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("S11",),
    doc="Advertise CanHTTPS=1 and accept strm flag 0x20.",
)
knob(
    "max_pcm_bits",
    family=FAMILY,
    type="int",
    default=32,
    quirks=("SQ-01",),
    doc="Highest PCM sample size the output accepts; a deeper pcm strm/codc gets STMn.",
)
knob(
    "setd_name_nul",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("S12",),
    doc="SETD name reply is NUL-terminated (SqueezePlay firmware sends it bare).",
)

# -- stream / decoder ---------------------------------------------------------------
knob(
    "stream_buffer_kb",
    family=FAMILY,
    type="int",
    default=2048,
    unit="KB",
    quirks=("S4",),
    doc="Stream buffer size reported in STAT.",
)
knob(
    "stmd_early_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("S4", "SQ-06"),
    doc="Send STMd (decoder ready) this many seconds after STMs instead of at stream "
    "EOF (MA #6359/#6666: a tiny buffer drains the stream at once). 0 = at EOF.",
)
knob(
    "stale_stmt_after_flush",
    family=FAMILY,
    type="int",
    default=0,
    quirks=("S5",),
    doc="After strm q, send this many STMt still carrying the flushed stream's elapsed.",
)
knob(
    "stale_resp_after_flush",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("S5",),
    doc="After a flush and a new strm s, send the old stream's RESP once more after STMc.",
)
knob(
    "codc_after_body_noise",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("S7", "SQ-01"),
    doc="Read the body without waiting for cont; a codc arriving after audio started "
    "re-opens the decoder mid-stream: buffered audio is lost and a burst of noise is "
    "recorded (MA #6398/#4163).",
)
knob(
    "flac_sync_silent",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("S19",),
    confidence="low",
    doc="A FLAC stream never syncs: the body is read but nothing is played and no STMs "
    "is sent (MA #5506, multi-room flac).",
)
knob(
    "stmn_decode_error",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("S21",),
    doc="Decoder error at at_s seconds (param, default 2.0): STMn, the rest of the stream "
    "is dropped, buffered audio plays out, then STMu.",
)
knob(
    "stmo_underrun_at",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("S21",),
    doc="Output underrun at at_s (default 3.0) lasting stall_s (default 2.0): STMo, "
    "silence, then playback resumes on its own (no STAT, like squeezelite).",
)
knob(
    "dsco_reason",
    family=FAMILY,
    type="int",
    default=0,
    quirks=("S13",),
    doc="Reason byte of the DSCO sent when the data stream closes (0 = ok, 4 = timeout).",
)

# -- output / transport -------------------------------------------------------------
knob(
    "initial_gain_zero",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("S9",),
    doc="Output starts at gain 0 and stays silent until the first audg (MA #6561).",
)
knob(
    "hardware_volume",
    family=FAMILY,
    type="bool",
    default=False,
    doc="Volume goes to the ALSA mixer (-V): digital gain stays at unity.",
)
knob(
    "mute_aude_stops_output",
    family=FAMILY,
    type="bool",
    default=True,
    doc="aude with enable_spdif=0 (MA's mute) turns the output off; aude 1 leaves it "
    "stopped until strm u (real squeezelite).",
)
knob(
    "early_start_before_unpause_at",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("S16",),
    doc="Without autostart, start playing as soon as the buffer is ready instead of "
    "waiting for strm u (WiiM slimproto).",
)
knob(
    "heartbeat_while_paused_advances",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("S16",),
    doc="STMt elapsed keeps counting while paused (WiiM slimproto).",
)
knob(
    "pause_resume_position_jump",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("S17",),
    confidence="low",
    doc="After a resume, reported elapsed is ahead by jump_s (default 2.0).",
)
knob(
    "seek_elapsed_from_zero",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("S18",),
    doc="Elapsed restarts at 0 for each new stream (squeezelite). False: elapsed carries "
    "on from the stream before a flush, so a seek reports old+new time (MA #3704).",
)
knob(
    "elapsed_ms_bits",
    family=FAMILY,
    type="int",
    default=32,
    quirks=("S2",),
    confidence="low",
    doc="elapsed_ms is truncated to this many bits before sending.",
)
knob(
    "stmt_interval_s",
    family=FAMILY,
    type="float",
    default=1.0,
    unit="s",
    doc="Unsolicited STMt period while playing (0 = only answer strm t).",
)
knob(
    "jiffies_offset_ms",
    family=FAMILY,
    type="int",
    default=0,
    unit="ms",
    quirks=("S15",),
    doc="Offset of the player's jiffies clock.",
)
knob(
    "drift_ppm",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="ppm",
    quirks=("S15",),
    doc="Rate error of the jiffies clock.",
)
