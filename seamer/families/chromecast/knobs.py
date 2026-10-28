"""Knobs of the chromecast family. Importing this module registers them.

Quirk IDs: ``C1``..``C29`` from cast_airplay_spotify.md, ``C30``..``C47`` from
cast_airplay2.md, ``CC-xx`` from PLAN.md section 3.5 and ``X5`` from bug_crossmatch.md.
"""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "chromecast"

# -- HTTP fetch of contentId ----------------------------------------------------------------

knob(
    "content_length_cutoff_bytes",
    family=FAMILY,
    type="int",
    default=0,
    unit="bytes",
    quirks=("C9", "C33"),
    confidence="high",
    doc="When the response has a Content-Length, stop reading after this many bytes and "
    "treat the item as finished: the device then GETs the next queue item (MA's "
    "next.mp3) with no error. Real value 25165824 (MP3 320k stops at 626-630 s). 0 = off.",
)
knob(
    "no_content_length_stop_after_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C10", "C33"),
    confidence="medium",
    doc="When the response has no Content-Length (chunked or close-delimited), stop "
    "reading after this much audio and finish the item (Nest Mini: 300-600 s). 0 = off.",
)
knob(
    "idle_on_eof",
    family=FAMILY,
    type="enum",
    default="FINISHED",
    choices=("FINISHED", "stay_buffering", "ERROR"),
    quirks=("C1", "C4", "C28", "C42"),
    confidence="high",
    doc="What the receiver reports when the last queue item's stream runs out: IDLE/"
    "FINISHED, BUFFERING forever (LIVE streams, groups) or IDLE/ERROR.",
)
knob(
    "clip_tail_cut_ms",
    family=FAMILY,
    type="int",
    default=0,
    unit="ms",
    quirks=("C28", "C42"),
    confidence="low",
    doc="The last N ms of the final item are never played (group TTS tail cut, #6695).",
)
knob(
    "strict_content_type",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("C12", "C5"),
    confidence="high",
    doc="Vendor Cast stacks: when the served Content-Type differs from the LOAD/"
    "QUEUE_INSERT contentType, fail the item (LOAD_FAILED / ERROR, then IDLE/ERROR).",
)
knob(
    "public_dns_only",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("C13",),
    confidence="high",
    doc="The device resolves names through Google DNS only: a contentId host ending in "
    ".local/.lan/.home or without a dot fails to load (IP literals work).",
)
knob(
    "reject_http_media",
    family=FAMILY,
    type="enum",
    default="off",
    choices=("off", "silent", "load_failed"),
    quirks=("C36",),
    confidence="medium",
    doc="Third-party receivers (AirReceiver): an http:// contentId plays nothing. "
    "silent = BUFFERING forever with no error; load_failed = LOAD_FAILED + IDLE/ERROR.",
)
knob(
    "range_rerequest_after_bytes",
    family=FAMILY,
    type="int",
    default=0,
    unit="bytes",
    quirks=("X5", "C33"),
    confidence="medium",
    doc="Samsung soundbars: after this many bytes, re-request the stream with "
    "'Range: bytes=N-'. A 200 answer (server ignores Range) makes the device drop the "
    "item (IDLE/ERROR). Real value about 10-13 MB. 0 = off.",
)
knob(
    "kill_control_socket_after_flow_restart_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C34", "CC-07"),
    confidence="medium",
    doc="LG S95TR: this long after the second (and every later) LOAD of a session, "
    "reset every CASTV2 connection (Errno 104 at the sender). 0 = off.",
)

# -- media session / status -------------------------------------------------------------

knob(
    "spontaneous_error",
    family=FAMILY,
    type="dict",
    default={},
    quirks=("C30", "C46", "CC-04"),
    confidence="high",
    doc="Mid-flow decode error: ERROR detailedErrorCode=<code> sent <dup> times <gap_ms> "
    "apart, then IDLE/ERROR with extendedStatus LOADING + loadingItemId, then the next "
    "item is fetched. Params: mean_s (exponential interval; 0 = only via the "
    "spontaneous_error sim action), code (102), dup (2), gap_ms (30), "
    "then ('load_next' | 'idle').",
)
knob(
    "next_item_fetch_delay_ms",
    family=FAMILY,
    type="int",
    default=50,
    unit="ms",
    quirks=("C30", "C32"),
    confidence="high",
    doc="Delay between the IDLE/ERROR status and the GET of the next item (C30: <50 ms).",
)
knob(
    "app_error_rate",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("C31",),
    confidence="medium",
    doc="Answer a media request with unicast {type: ERROR, reason: APP_ERROR, requestId}; "
    "the state does not change. Param 'on': list of request types "
    "(default GET_STATUS, QUEUE_UPDATE, SEEK).",
)
knob(
    "status_sparse_media",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("C2",),
    confidence="high",
    doc="MEDIA_STATUS carries the 'media' object only when it changed (senders merge).",
)
knob(
    "current_time_jump",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("C3", "CC-09"),
    confidence="medium",
    doc="A status reports currentTime off by 'by_s' (default -5) from the truth.",
)
knob(
    "omit_idle_reason",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("C47", "C1"),
    confidence="medium",
    doc="IDLE statuses never carry idleReason (pychromecast then clears idle_reason).",
)
knob(
    "empty_status_after_stop",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("C47",),
    confidence="medium",
    doc="Answer a media STOP with status: [] (pychromecast ignores it, MA stays PLAYING).",
)
knob(
    "supported_media_commands",
    family=FAMILY,
    type="int",
    default=12303,
    quirks=("C6",),
    confidence="high",
    doc="supportedMediaCommands bitmask (PAUSE 1, SEEK 2, STREAM_VOLUME 4, STREAM_MUTE 8, "
    "QUEUE_NEXT 64, QUEUE_PREV 128, ...).",
)
knob(
    "idle_app_timeout_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C41",),
    confidence="medium",
    doc="The receiver quits an app that has sat IDLE this long (native: about 300 s). 0 = never.",
)
knob(
    "spontaneous_pause_mean_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C21",),
    confidence="medium",
    doc="The device pauses the session by itself (voice, touch, another sender) with "
    "requestId 0, on average every N s of playing. 0 = only via the device_pause action.",
)

# -- receiver / app ---------------------------------------------------------------------------

knob(
    "launch",
    family=FAMILY,
    type="enum",
    default="normal",
    choices=("normal", "broadcast_only", "never"),
    quirks=("C15", "C16", "CC-13"),
    confidence="high",
    doc="LAUNCH reply: RECEIVER_STATUS with the requestId (normal), only a requestId-0 "
    "broadcast (owntone workaround; MA times out after 30 s), or nothing at all.",
)
knob(
    "launch_error",
    family=FAMILY,
    type="dict",
    default={},
    quirks=("C35", "C23"),
    confidence="high",
    doc="{appId: reason}: answer LAUNCH of that app with LAUNCH_ERROR <reason> "
    "(NOT_FOUND, CANCELLED, ...). 'silent' = LAUNCH_ERROR with no reason.",
)
knob(
    "launch_fail_once",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("C35",),
    confidence="medium",
    doc="The first LAUNCH after start fails with a reason-less LAUNCH_ERROR (MA logs "
    "'no reason given'); the retry succeeds.",
)
knob(
    "quit_ack_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C17",),
    confidence="high",
    doc="Delay before a receiver STOP (quit app) is answered; the old appId is reported "
    "until then.",
)
knob(
    "foreign_takeover",
    family=FAMILY,
    type="dict",
    default={},
    quirks=("C40", "C20", "CC-06"),
    confidence="high",
    doc="Another sender takes over through an app: off (+0), app <app> idle (+2 s), "
    "BUFFERING (+7 s), PLAYING the foreign stream (+10 s). Params: at_s (after the first "
    "LOAD; 0 = only via the foreign_takeover action), app (CC1AD845), "
    "content_id, title.",
)

# -- volume -------------------------------------------------------------------------------------

knob(
    "control_type",
    family=FAMILY,
    type="enum",
    default="attenuation",
    choices=("attenuation", "fixed", "master"),
    quirks=("C7",),
    confidence="high",
    doc="RECEIVER_STATUS volume.controlType; 'fixed' ignores SET_VOLUME levels.",
)
knob(
    "step_interval",
    family=FAMILY,
    type="float",
    default=0.05,
    quirks=("C7",),
    confidence="low",
    doc="RECEIVER_STATUS volume.stepInterval.",
)
knob(
    "ignore_idle_volume",
    family=FAMILY,
    type="enum",
    default="off",
    choices=("off", "not_applied", "ignored"),
    quirks=("C8", "C43"),
    confidence="medium",
    doc="SET_VOLUME while no app runs: not_applied = acked and reported, but the old level "
    "comes back when an app launches (C8); ignored = acked, level unchanged (C43).",
)

# -- connection / discovery ----------------------------------------------------------------

knob(
    "heartbeat_stop_after_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C18",),
    confidence="high",
    doc="Stop answering PING this long after the device started (pychromecast declares "
    "the link lost after 10 s + 10 s and reconnects every 5 s). 0 = off.",
)
knob(
    "device_ping_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C18",),
    confidence="low",
    doc="The device PINGs each sender this often (the sender answers PONG). 0 = off.",
)
knob(
    "eureka_info",
    family=FAMILY,
    type="enum",
    default="ok",
    choices=("ok", "404", "empty", "tls_fail", "refused"),
    quirks=("C36", "C37"),
    confidence="high",
    doc="/setup/eureka_info: normal JSON, HTTP 404, {}, a TLS handshake failure on the "
    "HTTPS port, or no 8008/8443 listener at all.",
)
knob(
    "pair_schema",
    family=FAMILY,
    type="enum",
    default="legacy",
    choices=("legacy", "new"),
    quirks=("C38", "C27"),
    confidence="high",
    doc="eureka_info multizone schema of a stereo pair: legacy {cast_port, "
    "elected_leader} or new {leader} without cast_port.",
)
knob(
    "leader_moves_after_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("C25",),
    confidence="high",
    doc="Group endpoint: the elected leader changes after this long. Connections to the "
    "old leader are reset, the port stays, and the endpoint reappears on the new host "
    "after 'leader_move_gap_s'. 0 = only via the leader_move action.",
)
knob(
    "leader_move_gap_s",
    family=FAMILY,
    type="float",
    default=2.0,
    unit="s",
    quirks=("C25",),
    confidence="low",
    doc="How long the group endpoint refuses connections while the leader moves.",
)
knob(
    "output_delay_ms",
    family=FAMILY,
    type="int",
    default=330,
    unit="ms",
    quirks=("C14",),
    confidence="medium",
    doc="Static output latency of the model (Home Mini 330, Nest Mini 427, CCA 335, "
    "Hub 188, LG SPx 370). Reported in /_sim/state.",
)
