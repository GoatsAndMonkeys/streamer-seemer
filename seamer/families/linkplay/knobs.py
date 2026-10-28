"""Knobs of the LinkPlay / WiiM family. Importing this module registers them.

Quirk IDs:

- ``lq:Qn``: simresearch/linkplay.md (Q1-Q50)
- ``Lnn``: simresearch/linkplay2.md (L51-L88)
- ``LP-nn``: simresearch/quirks.md
- ``MA#n`` / ``HA#n``: the support issue the quirk was reported in

Generic behaviours use the core knobs instead of new ones: ``report_position_zero``
(LP-01), ``report_duration_zero`` (LP-02), ``transition_gap_s`` (LP-04),
``latency_s``/``hang``/``offline`` (lq:Q2, L55), ``probe`` (L72).
"""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "linkplay"

# -- transport, TLS, identity ---------------------------------------------------------

knob(
    "http_scheme",
    family=FAMILY,
    type="enum",
    default="https_only",
    choices=("https_only", "http_only", "both"),
    quirks=("lq:Q1", "L57"),
    confidence="high",
    doc="Which listeners serve /httpapi.asp. WiiM: HTTPS only; A31 modules: HTTP only.",
)
knob(
    "https_port",
    family=FAMILY,
    type="int",
    default=0,
    quirks=("lq:Q1", "L56"),
    confidence="high",
    doc="HTTPS API port (real: 443 WiiM, 4443 Audio Pro MkII, 8443 W-gen). 0 = free port.",
)
knob(
    "http_port",
    family=FAMILY,
    type="int",
    default=0,
    quirks=("lq:Q1",),
    confidence="high",
    doc="Plain HTTP API port (real: 80). 0 = free port.",
)
knob(
    "client_cert_required",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("lq:Q1", "L56"),
    confidence="high",
    doc="The HTTPS API demands a client certificate signed by the device CA (mutual TLS).",
)
knob(
    "wrong_scheme_reply",
    family=FAMILY,
    type="enum",
    default="404_text",
    choices=("404_text", "conn_reset"),
    quirks=("L57",),
    confidence="medium",
    doc="What /httpapi.asp on the UPnP port answers: a plain '404: Not Found' body, or a "
    "dropped connection.",
)
knob(
    "uuid_form",
    family=FAMILY,
    type="enum",
    default="compact24",
    choices=("compact24", "full32"),
    quirks=("lq:Q4", "L83"),
    confidence="high",
    doc="getStatusEx 'uuid': the 24-hex id, or the 32-hex form (id + its first 8 chars).",
)
knob(
    "wmrm_version",
    family=FAMILY,
    type="str",
    default="4.2",
    quirks=("lq:Q49",),
    confidence="high",
    doc="Multiroom protocol version in getStatusEx/getSlaveList (grouping needs equal major).",
)
knob(
    "netfield_style",
    family=FAMILY,
    type="enum",
    default="wiim",
    choices=("wiim", "a31"),
    quirks=("lq:Q6", "L85", "L61"),
    confidence="high",
    doc="getStatusEx network/security fields: WiiM (apcli0 = IP, eth0 0.0.0.0, security "
    "https/2.0) or A31 (apcli0 empty string, security_version 2.0, no security field).",
)

# -- HTTP payload encoding ----------------------------------------------------------------

knob(
    "metadata_encoding",
    family=FAMILY,
    type="enum",
    default="hex",
    choices=("hex", "plain", "mixed"),
    quirks=("lq:Q12", "L52"),
    confidence="high",
    doc="Title/Artist/Album in getPlayerStatus(Ex): hex UTF-8, plain text, or mixed "
    "(Title hex, Artist/Album plain 'unknown').",
)
knob(
    "idle_unknown_sentinel",
    family=FAMILY,
    type="str",
    default="Unknown",
    quirks=("L52", "lq:Q13", "L80"),
    confidence="high",
    doc="Text reported for Title/Artist/Album when nothing is known (hex-encoded if hex).",
)
knob(
    "artwork_sentinel",
    family=FAMILY,
    type="str",
    default="unknow",
    quirks=("lq:Q13", "L80"),
    confidence="high",
    doc="albumArtURI reported when there is no artwork.",
)
knob(
    "self_hosted_art",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("L58", "L61"),
    confidence="medium",
    doc="While AirPlay plays, albumArtURI points at the device's own self-signed HTTPS "
    "listener (/data/AirplayArtWorkData.jpeg), served with no Content-Type. Seen via "
    "getMetaInfo (HA#187893); the DIDL carrying it too is inferred from HA core#179523.",
)
knob(
    "duplicate_vendor_key",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("L51",),
    confidence="medium",
    doc="getPlayerStatusEx carries both 'vendor' and 'Vendor':'unknown'.",
)
knob(
    "boot_status_none_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("L53",),
    confidence="high",
    doc="For this long after start, getPlayerStatusEx reports status 'none' and mode '-1'.",
)
knob(
    "json_corruption",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("L54",),
    confidence="medium",
    doc="getPlayerStatus(Ex) JSON has a doubled comma and a trailing '#'.",
)
knob(
    "pos_units",
    family=FAMILY,
    type="enum",
    default="ms",
    choices=("ms", "us"),
    quirks=("lq:Q14",),
    confidence="high",
    doc="Units of HTTP curpos/totlen/offset_pts.",
)
knob(
    "metainfo",
    family=FAMILY,
    type="enum",
    default="full",
    choices=("full", "trailing_space", "unsupported"),
    quirks=("lq:Q11", "L59"),
    confidence="high",
    doc="getMetaInfo: normal keys, PDF-style keys with trailing spaces, or not available. "
    "Always 'Failed' while idle.",
)
knob(
    "failure_word",
    family=FAMILY,
    type="enum",
    default="Failed",
    choices=("Failed", "Fail"),
    quirks=("lq:Q7", "L60"),
    confidence="high",
    doc="Body of a failed command (Arylic vendor calls say 'Fail').",
)
knob(
    "reboot_reply",
    family=FAMILY,
    type="enum",
    default="json_ok",
    choices=("json_ok", "empty", "unknown"),
    quirks=("L60", "lq:Q50"),
    confidence="high",
    doc="Reply to 'reboot' (StartRebootTime:1 always answers OK).",
)
knob(
    "eq_reply_json",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("L60",),
    confidence="high",
    doc='EQOn/EQOff/EQLoad answer {"status":"OK"} (true) or plain OK (false).',
)
knob(
    "unsupported_commands",
    family=FAMILY,
    type="str",
    default="getStatus,getAudioOutputStatus",
    quirks=("lq:Q8", "lq:Q10", "L87", "L56"),
    confidence="high",
    doc="Comma-separated commands this unit answers 'unknown command' (HTTP 200).",
)
knob(
    "slavelist_shape",
    family=FAMILY,
    type="enum",
    default="modern",
    choices=("modern", "legacy_caps_strings"),
    quirks=("L84", "lq:Q44"),
    confidence="high",
    doc="multiroom:getSlaveList shape: ints, no slave_list when standalone; or the 2014 "
    "capitalised string form.",
)

# -- position and state misreports -----------------------------------------------------------

knob(
    "play_start_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("lq:Q17", "MA#6867"),
    confidence="medium",
    doc="After Play on a new URI the device stays STOPPED this long before it starts loading "
    "(TRANSITIONING). Reporter captures: about 0.8 s.",
)
knob(
    "stale_prev_position",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("lq:Q17", "MA#6867"),
    confidence="medium",
    doc="While a new URI loads (TRANSITIONING), RelTime/curpos still show where the previous "
    "stream was. Params: window_s (max duration, default 1.0).",
)
knob(
    "end_position_after_load_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("lq:Q16", "MA#6839"),
    confidence="high",
    doc="For this long after a new stream starts, position reads equal to duration.",
)
knob(
    "false_playing_without_uri_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("lq:Q18",),
    confidence="high",
    doc="After a transport command with no URI loaded (network mode), report PLAYING for "
    "this long.",
)
knob(
    "curpos_negative_on_restart",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("LP-03",),
    confidence="medium",
    doc="While an advanced/restarted stream loads, HTTP curpos is negative.",
)
knob(
    "released_source_looks_paused",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("lq:Q21",),
    confidence="high",
    doc="When the WiiM app takes the device (sim action app_takeover), MA's session is "
    "reported as plain 'pause' with the old metadata, indefinitely.",
)
knob(
    "status_stop_while_connect_playing",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("lq:Q20", "L69"),
    confidence="high",
    doc="Qobuz Connect and Google Cast sessions report status 'stop' while playing "
    "(Cast also mode '5', vendor 'CAST').",
)

# -- sources ------------------------------------------------------------------------------

knob(
    "input_switch_without_upnp_event",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("lq:Q24", "MA#6897"),
    confidence="medium",
    doc="A physical input switch (CEC turning the TV on) changes HTTP 'mode' but sends no "
    "PlaybackStorageMedium in the AVTransport events; only TransportState moves.",
)
knob(
    "input_switch_stop_s",
    family=FAMILY,
    type="float",
    default=1.4,
    unit="s",
    quirks=("lq:Q24", "MA#6897"),
    confidence="medium",
    doc="During an input switch the transport reports STOPPED for this long, then PLAYING.",
)
knob(
    "stale_metadata_after_switch",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("lq:Q23",),
    confidence="medium",
    doc="After switching to a physical input, Title and totlen of the last stream remain.",
)
knob(
    "switchmode_empty_body_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("L67",),
    confidence="medium",
    doc="For this long after switchmode, getPlayerStatusEx answers an empty 200 body.",
)
knob(
    "switchmode_silent_noop",
    family=FAMILY,
    type="str",
    default="",
    quirks=("lq:Q30", "L67"),
    confidence="medium",
    doc="Comma-separated switchmode targets that answer OK but change nothing.",
)

# -- queue, advance, transport commands ----------------------------------------------------

knob(
    "set_next_supported",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("LP-06",),
    confidence="medium",
    doc="SetNextAVTransportURI works (false: UPnP fault 401 Invalid Action).",
)
knob(
    "http_add_is_noop",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("lq:Q34",),
    confidence="high",
    doc="setPlayerCmd:add:<url> answers OK but queues nothing (plicount stays put).",
)
knob(
    "advance_event_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("lq:Q35", "MA#6557"),
    confidence="medium",
    doc="AVTransport events about a natural track advance are held back this long.",
)
knob(
    "drop_advance_event",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("lq:Q35", "MA#6557", "MA#5900"),
    confidence="medium",
    doc="A natural advance sends no AVTransport event at all (until the next command); "
    "polls still show the new track.",
)
knob(
    "resume_701",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("LP-08", "lq:Q39"),
    confidence="medium",
    doc="Play while PAUSED_PLAYBACK fails with UPnP fault 701 (A31 over DLNA).",
)
knob(
    "pause_in_stop_fault",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("L65",),
    confidence="high",
    doc="Pause while stopped answers UPnP fault 701 'Transition not available'.",
)
knob(
    "playqueue",
    family=FAMILY,
    type="enum",
    default="present",
    choices=("present", "hidden", "absent"),
    quirks=("LP-09", "LP-11", "L82"),
    confidence="medium",
    doc="The vendor PlayQueue service: listed in description.xml, working but unlisted, or "
    "absent (404).",
)
knob(
    "playqueue_loopmode_tag",
    family=FAMILY,
    type="enum",
    default="LoopMode",
    choices=("LoopMode", "LoopMpde"),
    quirks=("L81",),
    confidence="high",
    doc="Tag name of the loop mode in PlayQueue LastChange (some firmware misspells it).",
)
knob(
    "getinfoex_supported",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("L79",),
    confidence="high",
    doc="The vendor AVTransport action GetInfoEx exists.",
)

# -- events --------------------------------------------------------------------------------

knob(
    "no_state_events",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("LP-07", "lq:Q37"),
    confidence="medium",
    doc="AVTransport sends only the initial NOTIFY; state changes are never evented.",
)
knob(
    "echo_http_volume_events",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("L64", "HA#152720"),
    confidence="medium",
    doc="Volume/mute set over HTTP (setPlayerCmd:vol/mute) is evented on RenderingControl.",
)
knob(
    "notify_delivery",
    family=FAMILY,
    type="enum",
    default="ok",
    choices=("ok", "drop_silently"),
    quirks=("L66", "HA#177517"),
    confidence="high",
    doc="drop_silently: SUBSCRIBE answers 200 with a SID but no NOTIFY is ever sent.",
)
knob(
    "gena_timeout_s",
    family=FAMILY,
    type="int",
    default=1800,
    unit="s",
    quirks=("L66", "lq:Q37"),
    confidence="high",
    doc="Subscription lifetime granted; unrenewed subscriptions stop receiving events.",
)

# -- volume ---------------------------------------------------------------------------------

knob(
    "vol_collision_window_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("L62", "HA#145166"),
    confidence="medium",
    doc="A volume command arriving within this window of the previous one is held until "
    "the window passes and then dropped (only the first sticks).",
)

# -- multiroom ------------------------------------------------------------------------------

knob(
    "leader_restart_on_first_join",
    family=FAMILY,
    type="bool",
    default=True,
    quirks=("lq:Q46", "L71"),
    confidence="high",
    doc="When the first follower joins, the leader goes TRANSITIONING and re-fetches its "
    "URI from the start (live streams too).",
)
knob(
    "role_propagation_delay_s",
    family=FAMILY,
    type="float",
    default=0.0,
    unit="s",
    quirks=("lq:Q43",),
    confidence="high",
    doc="A join/leave answers OK at once; getStatusEx/getSlaveList change after this delay.",
)
knob(
    "follower_status",
    family=FAMILY,
    type="enum",
    default="stay_idle",
    choices=("stay_idle", "mirror_leader"),
    quirks=("L70",),
    confidence="medium",
    doc="A follower's own getPlayerStatusEx: type '1' and idle, or a copy of the leader.",
)
