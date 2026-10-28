# BluOS family (Bluesound / NAD)

Package `seamer.families.bluos`, default profile `node_n130`, default port 11000.

```
seamer run bluos/node_n130
seamer run bluos/nad_c700 -k inject_state='{"p": 1.0, "value": "unknown"}'
```

Quirk IDs: `bss:Bn` = simresearch/bluos_slim_snap.md (B1-B26), `bl2:Bn` =
simresearch/bluos2.md (B27-B51, R1-R7), `BL-nn` = simresearch/quirks.md, `ma#n` / `ha#n` =
the Music Assistant / Home Assistant issue the quirk explains.

## What the device serves

One HTTP listener (the device port; 11000 on hardware). Every reply is `text/xml;
charset=utf-8` with an `<?xml version="1.0" encoding="UTF-8"?>` prolog and empty elements
written as `<a></a>` (both knobs).

| Path | Notes |
|---|---|
| `/Status` | `?etag=&timeout=` long-poll; etag = md5 of the document without `<secs>`. A follower answers with its leader's document. |
| `/SyncStatus` | `?etag=&timeout=` long-poll on `syncStat`; attributes per firmware/class; `<master>`/`<slave>` for groups |
| `/Volume` | `level`, `abs_db`, `db` (relative), `mute`, `tell_slaves`; long-polls too; dB from a per-model linear map |
| `/Play` | plain resume, `seek`, `id` (queue), `url` (HTTP stream, preset URL or input URL), `inputIndex`, `inputTypeIndex` |
| `/Pause` `/Stop` `/Skip` `/Back` | `/Pause?toggle=1`; Skip wraps; Back within 4 s goes to the previous track |
| `/Shuffle` `/Repeat` `/Sleep` | playlist / sleep documents |
| `/AddSlave` `/RemoveSlave` | `slave`+`port` or batched `slaves`+`ports`; handover when the leader leaves a 3+ group |
| `/Presets` `/Preset` | `id`, `+1`/`-1`; a preset carries its volume |
| `/RadioBrowse?service=Capture` | the input list (per device class) |
| `/Browse` `/Services` `/Playlist` `/Clear` `/Save` `/Delete` `/Move` `/Add` | queue and browse basics |
| `/reboot` (GET/POST) | HTML reply, then connection refused for `reboot_downtime_s` |

Refused commands answer HTTP 200 with `<error>...</error>` (`no slave available as new
master`, `Cannot move input source`, `Preset not found`, `empty`, `Invalid input`). Legacy
browse keys get 400 with an empty body; unknown paths 404.

State words: `stop`, `connecting`, `stream`, `play`, `pause` (`play` only for the local
queue). `/Play?url=` shows `connecting` for `connect_delay_s`, then fetches the URL with
the core HTTP sink (120 s buffer, realtime reads, `on_server_close = "drop"`: when the
server closes, the buffered tail is thrown away, which is the #6872 mechanism). A whole
file that arrives before the 1 s prebuffer fills is therefore dropped too; MA always sends
a realtime-paced stream, so this edge does not affect the MA runs.

Discovery: with the device option `mdns=True` the player announces `_musc._tcp` (zones
past the first: `_musp._tcp`) with TXT `mac`, `model`, `_objectType`, `version`
(`mdns_txt()`); `mdns_omit_mac` drops the `mac` key MA uses as the player id.

Grouping: `/AddSlave` on the leader attaches followers (they stop and mirror the leader's
`/Status`); transport commands sent to a follower run on the leader; `/Play?url` or
`/Preset` on a follower takes it out of its group. Devices find each other through a
module registry keyed by `(advertise_ip, port)`.

Sim actions: `select_input {id}`, `load_queue {tracks, play}`, `set_presets {presets}`,
`network_blip`, `reboot`, `set_name {name}`.

## Profiles

| Profile | Model | Firmware | Class | MAC | Notes | Confidence |
|---|---|---|---|---|---|---|
| `node_n130` | N130 NODE | 4.16.22 | streamer | 00:00:5E:00:53:01 | default | high |
| `powernode` | N330 POWERNODE | 4.16.6 | streamer-amplifier | :02 | Kitchen | high |
| `pulse_flex_2i` | P125 PULSE FLEX 2i | 4.16.22 | speaker | :03 | dB -49.5..0.5 | medium |
| `nad_c700` | NAD C700 (C 700) | 4.18.18 | streamer-amplifier | :04 | fw 4.18, dB -70.1..-20.1, 2-4 s connect | medium |
| `bluos3` | N125 NODE 2 | 3.20.45 | streamer | :05 | schema 32, no version/class, `quality` in bps, group names joined | low (no public capture) |

All profiles use the documentation MAC range 00:00:5E:00:53:xx.

## Knobs

Bool and probability knobs are checked with `knobs.fires()` where the quirk takes effect;
pure wire-shape knobs (framing, per-firmware element sets) and enums are read with `get()`.
`inputs_missing_attr` records an `inputs_missing_attr` event when it applies.

| Knob | Type | Default | Conf. | Quirks | What it does |
|---|---|---|---|---|---|
| `xml_prolog` | bool | `True` | high | bl2:B27 | Every response starts with <?xml version="1.0" encoding="UTF-8"?> and a newline. |
| `empty_tag_style` | enum | `pair` | high | bl2:B27 | Empty elements as <a></a> (real players) or <a/> (the API doc). |
| `fw_profile` | enum | `4.16` | high | bl2:B28, bl2:B51 | Firmware generation: schemaVersion, SyncStatus version/class attributes and the attributes on <slave>. bluos3 is low confidence (no public capture). |
| `device_class` | enum | `streamer` | high | bl2:B28 | SyncStatus class and its children (zoneOptions, pairWithSub, bluetoothOutput, audioPresetUrl) and the input list. |
| `drop_model_attrs` | bool | `False` | medium | bl2:B47, ha#127500 | Older firmware: SyncStatus has no modelName, class or version. pyblu needs modelName. |
| `error_http_200` | bool | `True` | high | bl2:B31 | Refused commands answer HTTP 200 with an <error> body (off: HTTP 400, same body). |
| `longpoll` | bool | `True` | high | bl2:B38, BL-01 | ?etag=&timeout= holds until the etag changes or the timeout, then answers 200 with the current (possibly unchanged) body. Off: answer at once. |
| `etag_ignores_secs` | bool | `True` | high | bss:B1 | The /Status etag is a hash of everything except <secs>, so progress never wakes a long-poll. |
| `longpoll_min_gap_s` | float | `1.0` | high | bss:B2, BL-02 | Long-polls for the same resource closer than this are recorded as longpoll_rate_violation (0 disables). |
| `connecting_on_buffer_refill` | bool | `True` | high | bss:B4 | Report 'connecting' while the buffer refills after an underrun. |
| `connecting_at_tail` | prob | `p=1.0` | high | bss:B4, ma#5644 | After the server closes the stream, report 'connecting' while the buffered tail plays out (only matters when on_server_close=drain). Decided once per stream. |
| `connect_delay_s` | dict | `min=0.3, max=1.0` | medium | bl2:B44, ma#3488 | After /Play?url the player shows 'connecting' this long (uniform min..max) before it requests the stream. |
| `loop_on_http_profile` | prob | `p=1.0` | high | bss:B6, ma#4318 | A stream sent chunked or without Content-Length restarts from 0 when it ends (param max_loops, default 3). MA forces a Content-Length profile to avoid it. |
| `pause_drops_quality` | bool | `True` | high | bss:B7 | Paused /Status loses <quality> but keeps the metadata. |
| `stop_keeps_metadata` | bool | `True` | high | bss:B8, ha#129728 | Stopped /Status keeps the last item's titles and image with secs=0. |
| `fresh_stop_bare` | bool | `True` | high | bl2:B37 | A player that has not played since boot reports no metadata and no service. |
| `metadata_style` | enum | `titles` | high | bss:B9, bss:B23 | URL streams carry only title1/title2 (titles) or also name/artist/album (named). |
| `quality_mode` | enum | `kbps` | high | bl2:B35 | <quality> for lossy streams: kbps (real Apple Music capture) or bps (the doc). Lossless is always cd/hd. |
| `extra_status_fields` | bool | `True` | high | bl2:B39 | Undocumented /Status elements: canMovePlayback=true (the word), serviceName, serviceType. |
| `inject_state` | prob | `p=0.0` | medium | bl2:B43 | Report <state> as param 'value' (default empty). MA's PLAYBACK_STATE_MAP lookup raises KeyError on anything outside the five known states. |
| `indexing` | bool | `False` | high | bss:B24 | <indexing>1</indexing>: a library rescan is running. |
| `status_fail` | prob | `p=0.0` | medium | bl2:B47, ma#4398 | A /Status request fails (param status, default 503). MA marks the player unavailable after one failed poll. |
| `volume_db_range` | dict | `lo=-80.0, hi=0.0` | low | bl2:B40 | Linear map of level 0..100 to dB. Fitted per model from captures (C700 -70.1..-20.1, P125 -49.5..0.5); the default is a guess. |
| `volume_limits_db` | dict | `{}` | medium | bl2:B40, bl2:B50, BL-06 | {min, max} dB: requested levels are clamped and the clamped level is returned. |
| `fixed_volume` | bool | `False` | high | bss:B12, BL-04 | Fixed line output: volume reads -1 and volume commands are ignored. |
| `volume_step_stuck` | bool | `False` | low | bss:B13, ha#129956 | Levels round through 2 dB steps, so level=N+1 can come back as N. |
| `tell_slaves_default` | bool | `False` | high | bl2:B40, BL-05, ha#161607 | A leader's volume change reaches its followers without an explicit tell_slaves=1. |
| `volume_source_attr` | bool | `True` | high | bl2:B40 | /Volume carries the undocumented source="" attribute. |
| `mute_param_inverted` | bool | `False` | medium | bl2:B40 | Treat mute=0 as mute, as the v1.7 parameter table says (real players use mute=1). |
| `follower_status_mirror` | bool | `True` | high | bl2:B29, bss:B17 | A follower's /Status is the leader's document, etag, volume and db included. |
| `follower_proxies_commands` | bool | `True` | high | bss:B17 | Transport commands sent to a follower are run by its leader. |
| `group_name_style` | enum | `plus_n` | medium | bl2:B30 | Leader group attribute: 'Name + N' (N = followers) or 'Name+Other'. |
| `addslave_on_follower` | enum | `empty` | high | bl2:B32 | AddSlave sent to a follower: empty <addSlave></addSlave> (real) or proxy to leader. |
| `addslave_nest_leaders` | bool | `True` | high | bl2:B32 | Adding a player that leads its own group nests that group (3 levels); the nested players stop. Off: the nested group is dissolved first. |
| `addslave_reorder` | bool | `False` | medium | bl2:B32 | The batched AddSlave response lists the slaves in reverse order. |
| `addslave_multi_broken` | bool | `False` | low | bss:B18, BL-07, ha#177228 | AddSlave?slaves= with a single entry answers success but groups nothing. |
| `staged_sync_reads` | int | `1` | high | bl2:B32 | After AddSlave the leader's /SyncStatus lists the new follower this many reads late. |
| `syncstat_step` | dict | `min=2, max=7` | high | bl2:B32 | Each SyncStatus change advances syncStat/etag by a random step in min..max. |
| `leader_leave_on_input` | enum | `refuse` | high | bl2:B33, bl2:B31 | A leader removing itself while it plays a local input: refuse with <error>Cannot move input source</error>, or allow. |
| `handover_pick` | enum | `last` | medium | bl2:B33 | Which follower leads after the leader leaves a group of 3+. |
| `regroup_on_primary_remove` | bool | `True` | high | bss:B18, ha#102256 | Removing the leader of a 3+ group regroups the rest under a new leader (off: everyone becomes standalone). |
| `wedged_pause_state` | prob | `p=0.0` | low | bss:B15, BL-10, ha#188764 | After an external source takes over a paused player, /Status keeps saying 'pause' while audio plays; /Pause has no effect; /Pause?toggle=1 unwedges it. |
| `inputs_missing_attr` | enum | `none` | high | bss:B21, ha#150991 | Leave one attribute off the /RadioBrowse?service=Capture items. pyblu 2.x reads item.attrib['image'] directly, so 'image' breaks MA's poll. |
| `input_secs_origin` | float | `3600.0` | high | bl2:B36 | On a local input, secs counts from when the input started, not per track. |
| `settings_group_mismatch` | bool | `False` | high | bl2:B36 | settingsGroupId names a different input than inputId (P430: input4 vs input2). |
| `service_without_input_id` | bool | `True` | high | bss:B22, ma#6889 | LocalMusic queue playback reports <service> but no <inputId>. |
| `preset_url_ignores_volume` | bool | `True` | high | bss:B25, ha#140922 | /Preset?id applies the preset volume; /Play?url=<preset url> does not. |
| `playlist_len_style` | enum | `attrs` | medium | bl2:B42 | /Playlist?length=1 answers with attributes (real) or child elements (the doc). |
| `pause_toggle_param` | enum | `honor` | high | bl2:B49, bss:B14 | /Pause?toggle=1 toggles (honor) or always pauses (ignore). Plain /Pause never toggles. |
| `legacy_browse_400` | bool | `True` | medium | bl2:B34 | Hand-built legacy browse keys (/Albums?, /Artists?, /Songs?) get HTTP 400 and an empty body. |
| `mdns_omit_mac` | bool | `False` | high | bl2:B45 | The _musc._tcp TXT record has no 'mac' key; MA ignores such a device. |
| `mdns_object_type` | str | `player` | low | bl2:B45 | TXT _objectType value (real value not captured; empty omits the key). |
| `ipv6_only_announce` | bool | `False` | high | bss:B20, ma#6863 | After a network blip (sim action network_blip) mDNS re-announces only an IPv6 ULA address. |
| `zones` | int | `1` | high | bss:B19, BL-03 | Players in one chassis (CI580: 4). Zone i is its own player on port +10*i with the same MAC, announced as _musp._tcp. |
| `chassis_reboot_all` | bool | `True` | high | BL-11 | /reboot on any zone takes the whole chassis down. |
| `reboot_downtime_s` | float | `75.0` | medium | bl2:R6 | Seconds the player is unreachable after /reboot. |
| `serialized_connection` | bool | `False` | high | BL-12 | Requests are served one at a time: a held long-poll blocks every other request. |

## Music Assistant e2e

`tests/e2e/bluos_ma_smoke.py` (skipped unless `MA_REPO` points at an MA checkout with its
`.venv`). The pytest side runs each scenario in MA's Python as a subprocess. Each scenario
starts a fresh MA server (throwaway data dir, free high ports; never 8095), enables the
built-in `bluesound` provider, and feeds it the sim's mDNS service info directly (so no
multicast is needed). `SEAMER_KEEP=1` keeps the work dir.

- `smoke`: MA discovers the player, plays (the sink receives and plays audio), pauses,
  resumes, seeks (flow mode: a new `/Play?url`), sets volume and reads it back, follows a
  device-side volume change, stops.
- `bug6872` (`BLUOS_E2E_LONG=1`, about 25 min): a 5 x 240 s album in flow mode; asserts
  that less than 1.5 s of the album end is lost (about 0.9 s is a fixed sim residual:
  ffmpeg keeps a little decoded audio until EOF, even single-threaded; see the simkit
  decoder commit).
- `bug_state`: `inject_state` returns an unknown state word; asserts MA keeps tracking the
  player (volume change seen).
- `bug4398`: one 503 and one hanging `/Status`; asserts MA neither logs the 503 as an error
  nor marks the player unavailable.
- `bug_inputs_image`: `inputs_missing_attr = "image"`; asserts the player still registers.

## Bug repros

Run against MA dev (S/ma-dev) and, for #6872, the fix branch.

| Bug | Knobs | dev | fix |
|---|---|---|---|
| ma#6872 album end cut off | profile defaults (`on_server_close = "drop"`) | fails: 7.0 s of track 5 lost (player was 13.6 s ahead; MA closed the stream after its fixed 8 s lead-out) | passes: 0.9 s lost (the sim residual); stream held until the player caught up |
| B43 unknown state KeyError | `inject_state` p=1, value "" | fails: 15 "Error while requesting latest state" warnings, MA volume stuck at 30 (device 55) | not run (branch targets #6872 only) |
| ma#4398 single failed poll | `status_fail` (503, then a hang) | fails: 503 logged as an error, player unavailable about 1 s | not run |
| inputs without image | `inputs_missing_attr = "image"` | fails: `PlayerUnexpectedResponseError: 'image'`, never registers | not run |

## Bug hunt (`tests/e2e/hunt/bluos_hunt.py`)

These 13 scenarios use the same harness as the smoke test, with up to three players per
scenario. Sendspin is switched off, so the hunt never binds port 8927. Each test asserts
the correct behaviour, so a failure on MA dev means the bug reproduces. Results against
MA dev `cdff463ec`, each run 3 times (all 3/3 identical):

| Scenario | Bug | dev |
|---|---|---|
| `inputs_text` | bss:B21, ha#151728 | fails: both inputs show as "Input: None" |
| `follower_mute` | bl2:B29 | fails: the follower's mute in MA is the leader's, in both directions |
| `input_volume` | volume guard | fails: on Optical, a device volume change (40 -> 55) never reaches MA |
| `nad_connect` | bl2:B44, ma#3488 | fails: the queue goes IDLE for about 0.5 s while the C700 is still connecting (no skip) |
| `drop_model` | ha#127500 | fails: pyblu raises on the missing `modelName`, so the player never registers |
| `wedged_pause` | bss:B15 (low) | fails: MA's pause does not silence a player whose status is stuck on 'pause' |
| `leader_ungroup`, `input_leader_ungroup`, `group_volume`, `preset`, `reboot`, `long_pause`, `bluos3` | B18/B33, B31, ha#161607, B25, R6, ma#6756, B51 | pass |
