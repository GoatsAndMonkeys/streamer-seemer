"""Knobs of the BluOS (Bluesound / NAD) family. Importing this module registers them.

Quirk ID prefixes (see ``docs/families/bluos.md``):

- ``bss:Bn``: ``simresearch/bluos_slim_snap.md`` (B1-B26)
- ``bl2:Bn``: ``simresearch/bluos2.md`` (B27-B51 and the R1-R7 protocol reference)
- ``BL-nn``: the quirk catalogue (``quirks.md``)
- ``ma#n`` / ``ha#n``: the Music Assistant / Home Assistant issue the quirk explains

Behaviour knobs are checked with ``knobs.fires()`` where the quirk takes effect, so the
timeline shows when one shaped a run. Pure wire-shape knobs (framing, element sets that
are fixed per firmware) are read with ``get()``; they are logged with the device's
non-default knobs at start.
"""

from __future__ import annotations

from typing import Any

from ...simkit.knobs import Confidence, KnobType, knob

FAMILY = "bluos"


def _k(
    name: str,
    type: KnobType,
    default: Any,
    quirks: tuple[str, ...],
    confidence: Confidence,
    doc: str,
    choices: tuple[str, ...] = (),
    unit: str = "",
) -> None:
    knob(
        name,
        family=FAMILY,
        type=type,
        default=default,
        quirks=quirks,
        confidence=confidence,
        doc=doc,
        choices=choices,
        unit=unit,
    )


# -- framing and firmware ---------------------------------------------------------
_k(
    "xml_prolog",
    "bool",
    True,
    ("bl2:B27",),
    "high",
    'Every response starts with <?xml version="1.0" encoding="UTF-8"?> and a newline.',
)
_k(
    "empty_tag_style",
    "enum",
    "pair",
    ("bl2:B27",),
    "high",
    "Empty elements as <a></a> (real players) or <a/> (the API doc).",
    choices=("pair", "selfclose"),
)
_k(
    "fw_profile",
    "enum",
    "4.16",
    ("bl2:B28", "bl2:B51"),
    "high",
    "Firmware generation: schemaVersion, SyncStatus version/class attributes and the "
    "attributes on <slave>. bluos3 is low confidence (no public capture).",
    choices=("bluos3", "4.16", "4.18"),
)
_k(
    "device_class",
    "enum",
    "streamer",
    ("bl2:B28",),
    "high",
    "SyncStatus class and its children (zoneOptions, pairWithSub, bluetoothOutput, "
    "audioPresetUrl) and the input list.",
    choices=("streamer", "streamer-amplifier", "speaker", "soundbar"),
)
_k(
    "drop_model_attrs",
    "bool",
    False,
    ("bl2:B47", "ha#127500"),
    "medium",
    "Older firmware: SyncStatus has no modelName, class or version. pyblu needs modelName.",
)
_k(
    "error_http_200",
    "bool",
    True,
    ("bl2:B31",),
    "high",
    "Refused commands answer HTTP 200 with an <error> body (off: HTTP 400, same body).",
)

# -- /Status and long-poll ----------------------------------------------------------
_k(
    "longpoll",
    "bool",
    True,
    ("bl2:B38", "BL-01"),
    "high",
    "?etag=&timeout= holds until the etag changes or the timeout, then answers 200 with "
    "the current (possibly unchanged) body. Off: answer at once.",
)
_k(
    "etag_ignores_secs",
    "bool",
    True,
    ("bss:B1",),
    "high",
    "The /Status etag is a hash of everything except <secs>, so progress never wakes a long-poll.",
)
_k(
    "longpoll_min_gap_s",
    "float",
    1.0,
    ("bss:B2", "BL-02"),
    "high",
    "Long-polls for the same resource closer than this are recorded as "
    "longpoll_rate_violation (0 disables).",
    unit="s",
)
_k(
    "connecting_on_buffer_refill",
    "bool",
    True,
    ("bss:B4",),
    "high",
    "Report 'connecting' while the buffer refills after an underrun.",
)
_k(
    "connecting_at_tail",
    "prob",
    True,
    ("bss:B4", "ma#5644"),
    "high",
    "After the server closes the stream, report 'connecting' while the buffered tail "
    "plays out (only matters when on_server_close=drain). Decided once per stream.",
)
_k(
    "connect_delay_s",
    "dict",
    {"min": 0.3, "max": 1.0},
    ("bl2:B44", "ma#3488"),
    "medium",
    "After /Play?url the player shows 'connecting' this long (uniform min..max) before it "
    "requests the stream.",
)
_k(
    "loop_on_http_profile",
    "prob",
    True,
    ("bss:B6", "ma#4318"),
    "high",
    "A stream sent chunked or without Content-Length restarts from 0 when it ends "
    "(param max_loops, default 3). MA forces a Content-Length profile to avoid it.",
)
_k(
    "pause_drops_quality",
    "bool",
    True,
    ("bss:B7",),
    "high",
    "Paused /Status loses <quality> but keeps the metadata.",
)
_k(
    "stop_keeps_metadata",
    "bool",
    True,
    ("bss:B8", "ha#129728"),
    "high",
    "Stopped /Status keeps the last item's titles and image with secs=0.",
)
_k(
    "fresh_stop_bare",
    "bool",
    True,
    ("bl2:B37",),
    "high",
    "A player that has not played since boot reports no metadata and no service.",
)
_k(
    "metadata_style",
    "enum",
    "titles",
    ("bss:B9", "bss:B23"),
    "high",
    "URL streams carry only title1/title2 (titles) or also name/artist/album (named).",
    choices=("titles", "named"),
)
_k(
    "quality_mode",
    "enum",
    "kbps",
    ("bl2:B35",),
    "high",
    "<quality> for lossy streams: kbps (real Apple Music capture) or bps (the doc). "
    "Lossless is always cd/hd.",
    choices=("kbps", "bps"),
)
_k(
    "extra_status_fields",
    "bool",
    True,
    ("bl2:B39",),
    "high",
    "Undocumented /Status elements: canMovePlayback=true (the word), serviceName, serviceType.",
)
_k(
    "inject_state",
    "prob",
    False,
    ("bl2:B43",),
    "medium",
    "Report <state> as param 'value' (default empty). MA's PLAYBACK_STATE_MAP lookup "
    "raises KeyError on anything outside the five known states.",
)
_k(
    "indexing",
    "bool",
    False,
    ("bss:B24",),
    "high",
    "<indexing>1</indexing>: a library rescan is running.",
)
_k(
    "status_fail",
    "prob",
    False,
    ("bl2:B47", "ma#4398"),
    "medium",
    "A /Status request fails (param status, default 503). MA marks the player "
    "unavailable after one failed poll.",
)

# -- volume ---------------------------------------------------------------------------
_k(
    "volume_db_range",
    "dict",
    {"lo": -80.0, "hi": 0.0},
    ("bl2:B40",),
    "low",
    "Linear map of level 0..100 to dB. Fitted per model from captures (C700 -70.1..-20.1, "
    "P125 -49.5..0.5); the default is a guess.",
)
_k(
    "volume_limits_db",
    "dict",
    {},
    ("bl2:B40", "bl2:B50", "BL-06"),
    "medium",
    "{min, max} dB: requested levels are clamped and the clamped level is returned.",
)
_k(
    "fixed_volume",
    "bool",
    False,
    ("bss:B12", "BL-04"),
    "high",
    "Fixed line output: volume reads -1 and volume commands are ignored.",
)
_k(
    "volume_step_stuck",
    "bool",
    False,
    ("bss:B13", "ha#129956"),
    "low",
    "Levels round through 2 dB steps, so level=N+1 can come back as N.",
)
_k(
    "tell_slaves_default",
    "bool",
    False,
    ("bl2:B40", "BL-05", "ha#161607"),
    "high",
    "A leader's volume change reaches its followers without an explicit tell_slaves=1.",
)
_k(
    "volume_source_attr",
    "bool",
    True,
    ("bl2:B40",),
    "high",
    '/Volume carries the undocumented source="" attribute.',
)
_k(
    "mute_param_inverted",
    "bool",
    False,
    ("bl2:B40",),
    "medium",
    "Treat mute=0 as mute, as the v1.7 parameter table says (real players use mute=1).",
)

# -- grouping ---------------------------------------------------------------------------
_k(
    "follower_status_mirror",
    "bool",
    True,
    ("bl2:B29", "bss:B17"),
    "high",
    "A follower's /Status is the leader's document, etag, volume and db included.",
)
_k(
    "follower_proxies_commands",
    "bool",
    True,
    ("bss:B17",),
    "high",
    "Transport commands sent to a follower are run by its leader.",
)
_k(
    "group_name_style",
    "enum",
    "plus_n",
    ("bl2:B30",),
    "medium",
    "Leader group attribute: 'Name + N' (N = followers) or 'Name+Other'.",
    choices=("plus_n", "names"),
)
_k(
    "addslave_on_follower",
    "enum",
    "empty",
    ("bl2:B32",),
    "high",
    "AddSlave sent to a follower: empty <addSlave></addSlave> (real) or proxy to leader.",
    choices=("empty", "proxy"),
)
_k(
    "addslave_nest_leaders",
    "bool",
    True,
    ("bl2:B32",),
    "high",
    "Adding a player that leads its own group nests that group (3 levels); the nested "
    "players stop. Off: the nested group is dissolved first.",
)
_k(
    "addslave_reorder",
    "bool",
    False,
    ("bl2:B32",),
    "medium",
    "The batched AddSlave response lists the slaves in reverse order.",
)
_k(
    "addslave_multi_broken",
    "bool",
    False,
    ("bss:B18", "BL-07", "ha#177228"),
    "low",
    "AddSlave?slaves= with a single entry answers success but groups nothing.",
)
_k(
    "staged_sync_reads",
    "int",
    1,
    ("bl2:B32",),
    "high",
    "After AddSlave the leader's /SyncStatus lists the new follower this many reads late.",
)
_k(
    "syncstat_step",
    "dict",
    {"min": 2, "max": 7},
    ("bl2:B32",),
    "high",
    "Each SyncStatus change advances syncStat/etag by a random step in min..max.",
)
_k(
    "leader_leave_on_input",
    "enum",
    "refuse",
    ("bl2:B33", "bl2:B31"),
    "high",
    "A leader removing itself while it plays a local input: refuse with "
    "<error>Cannot move input source</error>, or allow.",
    choices=("refuse", "allow"),
)
_k(
    "handover_pick",
    "enum",
    "last",
    ("bl2:B33",),
    "medium",
    "Which follower leads after the leader leaves a group of 3+.",
    choices=("last", "first"),
)
_k(
    "regroup_on_primary_remove",
    "bool",
    True,
    ("bss:B18", "ha#102256"),
    "high",
    "Removing the leader of a 3+ group regroups the rest under a new leader (off: "
    "everyone becomes standalone).",
)
_k(
    "wedged_pause_state",
    "prob",
    False,
    ("bss:B15", "BL-10", "ha#188764"),
    "low",
    "After an external source takes over a paused player, /Status keeps saying 'pause' "
    "while audio plays; /Pause has no effect; /Pause?toggle=1 unwedges it.",
)

# -- sources, queue, presets ---------------------------------------------------------
_k(
    "inputs_missing_attr",
    "enum",
    "none",
    ("bss:B21", "ha#150991"),
    "high",
    "Leave one attribute off the /RadioBrowse?service=Capture items. pyblu 2.x reads "
    "item.attrib['image'] directly, so 'image' breaks MA's poll.",
    choices=("none", "text", "image"),
)
_k(
    "input_secs_origin",
    "float",
    3600.0,
    ("bl2:B36",),
    "high",
    "On a local input, secs counts from when the input started, not per track.",
    unit="s",
)
_k(
    "settings_group_mismatch",
    "bool",
    False,
    ("bl2:B36",),
    "high",
    "settingsGroupId names a different input than inputId (P430: input4 vs input2).",
)
_k(
    "service_without_input_id",
    "bool",
    True,
    ("bss:B22", "ma#6889"),
    "high",
    "LocalMusic queue playback reports <service> but no <inputId>.",
)
_k(
    "preset_url_ignores_volume",
    "bool",
    True,
    ("bss:B25", "ha#140922"),
    "high",
    "/Preset?id applies the preset volume; /Play?url=<preset url> does not.",
)
_k(
    "playlist_len_style",
    "enum",
    "attrs",
    ("bl2:B42",),
    "medium",
    "/Playlist?length=1 answers with attributes (real) or child elements (the doc).",
    choices=("attrs", "children"),
)
_k(
    "pause_toggle_param",
    "enum",
    "honor",
    ("bl2:B49", "bss:B14"),
    "high",
    "/Pause?toggle=1 toggles (honor) or always pauses (ignore). Plain /Pause never toggles.",
    choices=("honor", "ignore"),
)
_k(
    "legacy_browse_400",
    "bool",
    True,
    ("bl2:B34",),
    "medium",
    "Hand-built legacy browse keys (/Albums?, /Artists?, /Songs?) get HTTP 400 and an empty body.",
)

# -- discovery, chassis, connections ------------------------------------------------
_k(
    "mdns_omit_mac",
    "bool",
    False,
    ("bl2:B45",),
    "high",
    "The _musc._tcp TXT record has no 'mac' key; MA ignores such a device.",
)
_k(
    "mdns_object_type",
    "str",
    "player",
    ("bl2:B45",),
    "low",
    "TXT _objectType value (real value not captured; empty omits the key).",
)
_k(
    "ipv6_only_announce",
    "bool",
    False,
    ("bss:B20", "ma#6863"),
    "high",
    "After a network blip (sim action network_blip) mDNS re-announces only an IPv6 ULA address.",
)
_k(
    "zones",
    "int",
    1,
    ("bss:B19", "BL-03"),
    "high",
    "Players in one chassis (CI580: 4). Zone i is its own player on port +10*i with the "
    "same MAC, announced as _musp._tcp.",
)
_k(
    "chassis_reboot_all",
    "bool",
    True,
    ("BL-11",),
    "high",
    "/reboot on any zone takes the whole chassis down.",
)
_k(
    "reboot_downtime_s",
    "float",
    75.0,
    ("bl2:R6",),
    "medium",
    "Seconds the player is unreachable after /reboot.",
    unit="s",
)
_k(
    "serialized_connection",
    "bool",
    False,
    ("BL-12",),
    "high",
    "Requests are served one at a time: a held long-poll blocks every other request.",
)
