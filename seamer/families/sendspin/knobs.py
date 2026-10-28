"""Knobs of the Sendspin family. Importing this module registers them.

Quirk IDs (SSnn) refer to ``docs/families/sendspin.md`` and the research dossier
(sendspin.md §2). Confidence follows the dossier's H/M/L tags.
"""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "sendspin"

# -- wire generation and identity ---------------------------------------------------

knob(
    "wire_gen",
    family=FAMILY,
    type="enum",
    default="G2",
    choices=("G0", "G2"),
    quirks=("SS18", "SS53"),
    confidence="high",
    doc="G2 = client/init + Noise KKpsk2, 13-byte audio header (ESPHome 2026.10.0b2+, "
    "aiosendspin 10). G0 = unencrypted text client/hello carrying client_id and version, "
    "9-byte audio header (ESPHome 2026.5 to 2026.10.0b1, shipped Voice PE 26.9.x).",
)
knob(
    "client_id_mode",
    family=FAMILY,
    type="enum",
    default="key",
    choices=("key", "mac"),
    quirks=("SS1", "SS3"),
    confidence="high",
    doc="key: client_id is the Noise public key (G2). mac: lowercase MAC without colons "
    "(G0 ESPHome). Switching a device from mac to key reproduces the 2026.10 ID change.",
)
knob(
    "mac_address",
    family=FAMILY,
    type="str",
    default="aa:bb:cc:00:00:01",
    quirks=("SS1", "SS2"),
    confidence="high",
    doc="device_info.mac_address (MA #6928 uses it to migrate MAC-ID players). "
    "Empty string omits it (python-cli before 8.0).",
)
knob(
    "suite",
    family=FAMILY,
    type="enum",
    default="chachapoly",
    choices=("chachapoly", "aesgcm"),
    confidence="high",
    doc="Noise cipher suite offered in client/init.",
)
knob(
    "unpaired_access",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("SS14",),
    confidence="high",
    doc="Advertise and accept Sentinel (unpaired, operator-approved) sessions.",
)

# -- hello and state ----------------------------------------------------------------

knob(
    "roles",
    family=FAMILY,
    type="str",
    default="player,controller,metadata",
    confidence="high",
    doc="Comma-separated roles (player, controller, metadata). ESP: player,controller.",
)
knob(
    "formats",
    family=FAMILY,
    type="str",
    default="flac:48000:16:2,pcm:48000:16:2",
    quirks=("SS20", "SS51"),
    confidence="high",
    doc="Supported formats in preference order, codec:rate:bits:channels, comma-separated.",
)
knob(
    "buffer_capacity",
    family=FAMILY,
    type="int",
    default=621_312,
    unit="bytes",
    quirks=("SS21", "SS35"),
    confidence="high",
    doc="Advertised byte budget. Each chunk is charged header + payload until it has played. "
    "ESP: 621,312 (2/3 of the 1,000,000-byte ring, capped). Legacy ESPHome: 800,000.",
)
knob(
    "required_lead_time_ms",
    family=FAMILY,
    type="float",
    default=150.0,
    unit="ms",
    quirks=("SS22", "SS33"),
    confidence="high",
    doc="client/state required_lead_time_ms (ESP 150, aiosendspin default 250).",
)
knob(
    "min_buffer_ms",
    family=FAMILY,
    type="float",
    default=500.0,
    unit="ms",
    quirks=("SS22",),
    confidence="high",
    doc="client/state min_buffer_ms (ESP 500, aiosendspin default 250).",
)
knob(
    "output_delay_ms",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="ms",
    confidence="high",
    doc="Initial output_delay_ms (static_delay_ms on G0).",
)
knob(
    "supported_commands",
    family=FAMILY,
    type="str",
    default="volume,mute",
    quirks=("SS57",),
    confidence="high",
    doc="Player commands declared in client/state (G2) or the hello (G0). "
    "Add set_output_delay for adjustable delay.",
)
knob(
    "initial_volume",
    family=FAMILY,
    type="int",
    default=50,
    quirks=("SS44",),
    confidence="medium",
    doc="Volume reported at connect (persisted across restarts).",
)
knob(
    "volume_unmutes",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("SS44",),
    confidence="medium",
    doc="A volume command also clears mute (ESPHome before 2026.9.0).",
)
knob(
    "state_echo",
    family=FAMILY,
    type="enum",
    default="immediate",
    choices=("immediate", "delayed", "missing"),
    quirks=("SS24",),
    confidence="high",
    doc="How client/state follows a server/command. delayed waits state_echo_delay_ms "
    "(ESP: one loop tick). missing never echoes (noncompliant).",
)
knob(
    "volume_steps",
    family=FAMILY,
    type="int",
    default=0,
    quirks=("SS24", "SS44"),
    confidence="medium",
    doc="Hardware volume with this many steps (0 = continuous): a commanded volume snaps "
    "to the nearest step and client/state reports the snapped value, as a DAC or "
    "external volume controller does.",
)
knob(
    "state_echo_delay_ms",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="ms",
    quirks=("SS24", "SS9"),
    confidence="medium",
    doc="Delay of the client/state echo when state_echo=delayed.",
)
knob(
    "available_mode",
    family=FAMILY,
    type="enum",
    default="until_converged",
    choices=("until_converged", "never"),
    quirks=("SS36",),
    confidence="high",
    doc="until_converged: available flips true once the time filter converges (G2 spec; "
    "G0 reports state 'synchronized' at once). never: the player never reports available.",
)
knob(
    "legacy_state_field",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("SS36", "SS49"),
    confidence="high",
    doc="G0 only: send top-level state 'synchronized'/'error' in client/state "
    "('error' after an underrun).",
)

# -- clocks and the fake DAC --------------------------------------------------------

knob(
    "clock_offset_ms",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="ms",
    confidence="high",
    doc="Offset of the device clock from the host monotonic clock.",
)
knob(
    "clock_skew_ppm",
    family=FAMILY,
    type="float",
    default=20.0,
    unit="ppm",
    quirks=("SS32",),
    confidence="medium",
    doc="Device crystal error: the clock handed to aiosendspin runs fast by this much.",
)
knob(
    "clock_jitter_us",
    family=FAMILY,
    type="int",
    default=50,
    unit="us",
    quirks=("SS10",),
    confidence="medium",
    doc="Uniform read jitter on every clock read (monotonic is preserved).",
)
knob(
    "dac_skew_ppm",
    family=FAMILY,
    type="float",
    default=30.0,
    unit="ppm",
    confidence="medium",
    doc="Rate error of the fake DAC against the device clock; the corrector fights it.",
)
knob(
    "corrector",
    family=FAMILY,
    type="enum",
    default="cpp",
    choices=("none", "spec", "cpp", "python_cli"),
    quirks=("SS19", "SS23"),
    confidence="medium",
    doc="Sync corrector bounding DAC drift: cpp/spec = 1-frame steps with a 100 us dead "
    "band, python_cli = 2 ms dead band, none = drift accumulates.",
)
knob(
    "output_latency_error_ms",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="ms",
    quirks=("SS32",),
    confidence="medium",
    doc="Host output-latency misestimate added to the sync error (sendspin-cpp-cli).",
)

# -- buffer and decode behaviour ----------------------------------------------------

knob(
    "drop_late_chunks",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("SS11",),
    confidence="high",
    doc="Drop chunks whose play time is already past (ESP drops them undecoded).",
)
knob(
    "late_chunk_threshold_ms",
    family=FAMILY,
    type="float",
    default=5.0,
    unit="ms",
    quirks=("SS45",),
    confidence="high",
    doc="How late a chunk may be before it is dropped (cpp/py 5, go 50, jvm 1000).",
)
knob(
    "ring_overrun_policy",
    family=FAMILY,
    type="enum",
    default="drop_warn",
    choices=("drop_warn", "close_connection"),
    quirks=("SS21",),
    confidence="high",
    doc="What happens when a chunk does not fit in buffer_capacity.",
)
knob(
    "opus_decode_rt_factor",
    family=FAMILY,
    type="float",
    default=0.05,
    quirks=("SS5", "SS31"),
    confidence="medium",
    doc="Opus decode time as a fraction of audio duration. Above 1.0 the decoder falls "
    "behind and chunks miss their play time (plain ESP32 without PSRAM).",
)
knob(
    "decode_rt_factor",
    family=FAMILY,
    type="float",
    default=0.02,
    quirks=("SS5", "SS47"),
    confidence="low",
    doc="Decode time fraction for FLAC/PCM (Pi Zero 2 W python-cli is slower).",
)
knob(
    "start_gate_ms",
    family=FAMILY,
    type="float",
    default=25.0,
    unit="ms",
    quirks=("SS23",),
    confidence="medium",
    doc="Audio that must be buffered before output starts (ESP 25 ms priming, python-cli 200 ms).",
)

# -- connections, goodbyes and faults -----------------------------------------------

knob(
    "goodbye_reason",
    family=FAMILY,
    type="enum",
    default="restart",
    choices=(
        "restart",
        "shutdown",
        "another_server",
        "user_request",
        "unauthorized",
        "pairing_required",
        "none",
    ),
    quirks=("SS26", "SS27"),
    confidence="high",
    doc="Reason sent in client/goodbye by the restart/goodbye actions; none = drop "
    "abruptly. python-cli Ctrl-C sends shutdown (#6888).",
)
knob(
    "mdns_on_goodbye",
    family=FAMILY,
    type="enum",
    default="withdraw",
    choices=("withdraw", "keep"),
    quirks=("SS26", "SS13"),
    confidence="medium",
    doc="During a restart: withdraw the mDNS record while down and re-add it (ESPHome), "
    "or keep it registered so only an identical re-announcement is seen.",
)
knob(
    "restart_down_s",
    family=FAMILY,
    type="float",
    default=2.0,
    unit="s",
    quirks=("SS56",),
    confidence="medium",
    doc="How long the device is off the network during a restart.",
)
knob(
    "advertise_mdns",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("SS41",),
    confidence="high",
    doc="Register _sendspin._tcp.local. (TXT path=/sendspin, name). Off by default so "
    "tests stay off the LAN; needs the zeroconf extra.",
)
knob(
    "reject_origin_header",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("SS25",),
    confidence="high",
    doc="Refuse a WebSocket upgrade that carries Origin (browser CSRF guard).",
)
knob(
    "fault_mode",
    family=FAMILY,
    type="enum",
    default="none",
    choices=("none", "accept_and_hang", "blackhole", "never_consume"),
    quirks=("SS28", "SS29", "SS34"),
    confidence="high",
    doc="accept_and_hang: upgrade the WebSocket, then never send a hello. blackhole: after "
    "blackhole_after_s stop reading and answering but keep the socket open. "
    "never_consume: the DAC never plays, so the buffer fills and overruns.",
)
knob(
    "blackhole_after_s",
    family=FAMILY,
    type="float",
    default=5.0,
    unit="s",
    quirks=("SS29",),
    confidence="medium",
    doc="Seconds after connect at which fault_mode=blackhole takes effect.",
)
knob(
    "liveness_timeout_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("SS55",),
    confidence="medium",
    doc="Drop the connection with goodbye 'restart' after this much server silence "
    "(ESP 60 s). 0 disables.",
)
knob(
    "rx_window_stall_ms",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="ms",
    quirks=("SS30", "SS6"),
    confidence="medium",
    doc="Wi-Fi starvation: stop reading the socket (zero TCP window) for this long, once, "
    "rx_window_stall_at_s after the first connection opens. 0 disables.",
)
knob(
    "rx_window_stall_at_s",
    family=FAMILY,
    type="float",
    default=20.0,
    unit="s",
    quirks=("SS30",),
    confidence="medium",
    doc="When the rx_window_stall_ms stall starts, in seconds after the first connection.",
)
