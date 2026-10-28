"""Knobs of the AirPlay family. Importing this module registers them.

Quirk IDs:

- ``ca2:Ann``: simresearch/cast_airplay2.md (A28-A52)
- ``cas:Pn``: simresearch/cast_airplay_spotify.md (P1-P27)
- ``Xn``: simresearch/bug_crossmatch.md (cross-project quirks)
- ``MA#n``: the support issue the quirk was reported in

Two groups: *receiver* knobs (what the speaker does, applied by :class:`AirplayDevice`)
and *helper* knobs prefixed ``cli_`` (how the fake cliairplay process behaves, ca2:A52).
The fake binary reads the helper knobs from the device at connect time, so one profile
drives both halves. Generic behaviours use the core knobs: ``offline``/``latency_s``/
``hang`` on the ``/_wire`` control calls, ``prebuffer_s``/``buffer_capacity_s`` for the
audio buffer.
"""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "airplay"

# -- route, clock, start ----------------------------------------------------------------

knob(
    "airplay2",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("cas:P1",),
    confidence="high",
    doc="Speaks AirPlay 2 (feature bit 38/48). False: RAOP only (AirPort Express, old AVRs).",
)
knob(
    "buffered",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("cas:P1", "ca2:A35"),
    confidence="medium",
    doc="AirPlay 2 buffered route (type 103) instead of realtime (type 96).",
)
knob(
    "ntp_streams",
    family=FAMILY,
    type="enum",
    default="accept",
    choices=("accept", "reject_400"),
    quirks=("ca2:A32",),
    confidence="high",
    doc="shairport-sync: an AP2 stream with NTP timing is refused (SETUP 400).",
)
knob(
    "ptp_respond",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("ca2:A31", "ca2:A32"),
    confidence="high",
    doc="Answers the sender's PTP. False: clock_ready goes 'stalled' and nothing is heard.",
)
knob(
    "first_probe_ms",
    family=FAMILY,
    type="int",
    default=60,
    quirks=("cas:P6", "ca2:A35"),
    confidence="high",
    unit="ms",
    doc="First PTP exchange after connect (Sonos ~1078 ms, HomePod OS 27 ~1000 ms).",
)
knob(
    "clock_lock_ms",
    family=FAMILY,
    type="dict",
    default={"min": 150, "max": 300},
    quirks=("cas:P6", "ca2:A29", "ca2:A35"),
    confidence="high",
    unit="ms",
    doc="PTP lock after connect, drawn per session from {min, max} (Sonos 1700-2300 ms).",
)
knob(
    "ptp_role",
    family=FAMILY,
    type="enum",
    default="slave",
    choices=("slave", "standalone_own_clock", "master_if_silent"),
    quirks=("ca2:A28", "ca2:A30"),
    confidence="medium",
    doc=(
        "standalone_own_clock: OS 27 HomePod keeps its own clock; silent unless the helper "
        "follows it (cli_ptp_follow). master_if_silent: self-promotes without Sync."
    ),
)
knob(
    "anchor_400",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("ca2:A34", "MA#6348", "MA#6407"),
    confidence="high",
    doc=(
        "SETRATEANCHORTIME answers 400 until the clock is seated. Params: count (consecutive "
        "400s per session, default 2), retry_ms (helper retry spacing, default 500)."
    ),
)
knob(
    "buffered_anchor_always_400",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("ca2:A34", "MA#6430"),
    confidence="medium",
    doc="A buffered-route anchor is always refused (OS 27 HomePod, type 103).",
)
knob(
    "min_audible_latency_ms",
    family=FAMILY,
    type="int",
    default=0,
    quirks=("ca2:A36", "MA#6419"),
    confidence="medium",
    unit="ms",
    doc="Audio anchored sooner than this after the START is dropped (ESP32/Yamaha 2500 ms).",
)
knob(
    "output_latency_ms",
    family=FAMILY,
    type="int",
    default=2000,
    quirks=("ca2:A36",),
    confidence="medium",
    unit="ms",
    doc="The latency lead the receiver asks for (reported on the [STATUS] latency line).",
)

# -- session, auth, errors --------------------------------------------------------------

knob(
    "auth",
    family=FAMILY,
    type="enum",
    default="none",
    choices=("none", "password", "pin"),
    quirks=("ca2:A41", "ca2:A44"),
    confidence="high",
    doc="Access control: password (RAOP pw / 401) or PIN pairing (470 until paired).",
)
knob(
    "raop_mode",
    family=FAMILY,
    type="enum",
    default="ok",
    choices=("ok", "auth_setup_404", "silent"),
    quirks=("ca2:A40", "MA#5851", "MA#6338"),
    confidence="high",
    doc="OS 27 broke RAOP to HomePod/Apple TV: /auth-setup 404, or accepts and stays silent.",
)
knob(
    "rtsp_first_status",
    family=FAMILY,
    type="int",
    default=200,
    quirks=("ca2:A41", "MA#6655", "MA#5831"),
    confidence="medium",
    doc="Status of the first RTSP request (403 JBL, 455 Apple TV 3). 200 = normal.",
)
knob(
    "connect_ms",
    family=FAMILY,
    type="int",
    default=120,
    quirks=("ca2:A35",),
    confidence="medium",
    unit="ms",
    doc="Handshake time from spawn to '[STATUS] connected' (pair-verify, SETUP, RECORD).",
)
knob(
    "starve_after_ms",
    family=FAMILY,
    type="dict",
    default={"ms": 0, "first_play_only": False},
    quirks=("ca2:A39", "cas:P5", "MA#5714"),
    confidence="medium",
    doc=(
        "WiiM Pro Plus: audio cuts out this long into a play (0 = never). first_play_only: "
        "only the first START of a session; pause/play recovers."
    ),
)
knob(
    "reset_control_after_s",
    family=FAMILY,
    type="float",
    default=0.0,
    quirks=("ca2:A32", "ca2:A45", "MA#6243", "MA#6743"),
    confidence="medium",
    unit="s",
    doc="The control channel is reset this long into a session (shairport 75-85 s, JBL 40 s).",
)
knob(
    "flush_ack",
    family=FAMILY,
    type="enum",
    default="ok",
    choices=("ok", "never"),
    quirks=("ca2:A45", "MA#6323"),
    confidence="medium",
    doc="never: FLUSH is not acknowledged (Arylic A31), so a warm seek waits out the timeout.",
)
knob(
    "pause_reject",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("ca2:A45",),
    confidence="low",
    doc="Pause is refused (Arylic A31): the speaker keeps playing.",
)
knob(
    "require_metadata_before_audio",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("ca2:A50",),
    confidence="medium",
    doc="Sonos holds back audio until track metadata arrived (MA pushes it before START).",
)

# -- volume and DACP --------------------------------------------------------------------

knob(
    "volume_apply_tick_s",
    family=FAMILY,
    type="dict",
    default={"min": 0.0, "max": 0.0, "step": 0},
    quirks=("ca2:A49", "X8", "MA#6664", "MA#6838"),
    confidence="medium",
    unit="s",
    doc=(
        "Sonos in an AirPlay session applies volume on a tick drawn from {min, max} s "
        "(45-65 s) and echoes every queued level, 30 ms apart. step: at most this many % "
        "per tick (0 = apply the queue). max 0 = immediate."
    ),
)
knob(
    "volume_echo",
    family=FAMILY,
    type="dict",
    default={"enabled": True, "delay_ms": 250, "burst": 1},
    quirks=("cas:P21", "ca2:A49"),
    confidence="high",
    doc=(
        "After the level changes the receiver reports it back over DACP (dmcp.device-volume) "
        "after delay_ms. burst: copies of each report, 30 ms apart."
    ),
)
knob(
    "dacp_active_remote",
    family=FAMILY,
    type="enum",
    default="session",
    choices=("session", "first_seen", "previous"),
    quirks=("X7", "MA#5018", "MA#5929"),
    confidence="low",
    doc=(
        "Active-Remote the receiver puts on DACP: the current session's, the first one it "
        "ever saw, or the previous session's (stale id; MA logs UNKNOWN PLAYER)."
    ),
)
knob(
    "dacp_after_teardown_ms",
    family=FAMILY,
    type="int",
    default=-1,
    quirks=("ca2:A49", "X7", "MA#5018"),
    confidence="medium",
    unit="ms",
    doc="Denon: sends a volume DACP this long after TEARDOWN with the old id (-1 = never).",
)
knob(
    "dacp_report_idle",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("X7", "MA#5018"),
    confidence="low",
    doc="Physical volume changes are reported over DACP even with no live session.",
)
knob(
    "volume_reverts_in_session",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("ca2:A49",),
    confidence="low",
    doc="Sonos Port: the level snaps back to the pre-session level while a session is live.",
)

# -- helper process (fake cliairplay) -----------------------------------------------------

knob(
    "cli_exit_after_eof",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("ca2:A52",),
    confidence="medium",
    doc="The helper exits right after '[STATUS] eof' instead of waiting for ACTION=STOP.",
)
knob(
    "cli_ignore_sigterm",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("ca2:A52",),
    confidence="medium",
    doc="The helper ignores SIGTERM and stdin EOF, so MA's stop() escalates to SIGKILL.",
)
knob(
    "cli_die_after_ms",
    family=FAMILY,
    type="int",
    default=0,
    quirks=("ca2:A52",),
    confidence="medium",
    unit="ms",
    doc="The helper exits with code 1 this long after connecting, with no stderr (0 = never).",
)
knob(
    "cli_start_ack",
    family=FAMILY,
    type="enum",
    default="normal",
    choices=("normal", "missing", "late"),
    quirks=("ca2:A52", "ca2:A36"),
    confidence="low",
    doc="'[STATUS] started' ack: normal, never printed, or printed 3 s late.",
)
knob(
    "cli_ptp_follow",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("ca2:A28",),
    confidence="high",
    doc="The helper follows a receiver that keeps its own clock (airplay-cli >= 0.5.4).",
)
