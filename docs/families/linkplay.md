# LinkPlay / WiiM family

Package `seamer.families.linkplay`, alias `wiim`, default profile `wiim_pro`.

```
seamer run linkplay/wiim_pro            # or wiim/wiim_pro
seamer run linkplay/arylic_a31 -k no_state_events=false
```

Quirk IDs: `lq:Qn` = simresearch/linkplay.md, `Lnn` = simresearch/linkplay2.md,
`LP-nn` = simresearch/quirks.md, `MA#n` = Music Assistant issue.

## What the device serves

One device = up to four listeners on 127.0.0.1 (TCP ports default to 0 = free):

| Listener | Serves |
|---|---|
| HTTPS (`https_port`) | `/httpapi.asp`; self-signed device cert, optional mutual TLS (`client_cert_required`) |
| HTTP (`http_port`) | `/httpapi.asp` only when `http_scheme` is `http_only`/`both` |
| UPnP HTTP (the device port) | `/description.xml`, `/upnp/<svc>/scpd.xml`, `/upnp/<svc>/control` (SOAP), `SUBSCRIBE`/`UNSUBSCRIBE /upnp/<svc>/event` (GENA) |
| SSDP (UDP) | M-SEARCH replies for `MediaRenderer:1` and each service type, `LOCATION` = description.xml |

On the UPnP port `/httpapi.asp` misbehaves as `wrong_scheme_reply` says (a WiiM has no
HTTP API there); with `http_only` there is no HTTPS listener at all.

**httpapi.asp commands**: `getStatusEx`/`getStatus`, `getPlayerStatusEx`/`getPlayerStatus`,
`getMetaInfo`, `setPlayerCmd:` (`play:<url>`, `add`, `pause`, `onepause`, `resume`, `stop`,
`next`, `prev`, `seek`, `vol`, `mute`, `loopmode`, `switchmode`), `multiroom:`
(`getSlaveList`, `Ungroup`, `LeaveGroup`, `SlaveKickout`, `JoinGroup`, `SlaveVolume`,
`SlaveMute`), `ConnectMasterAp:JoinGroupMaster`, `MCUKeyShortClick`, `timeSync`,
`StartRebootTime`, `reboot`, `EQOn/EQOff/EQLoad/EQGetStat/EQGetList`, `getShutdown`,
`wlanGetConnectState`, `getAudioInputCapbility`, `getSoundCardModeSupportList`,
`getActiveSoundCardOutputMode`, `setSoundCardOutputMode`, `getNewAudioOutputHardwareMode`,
`setAudioOutputHardwareMode`. Anything else (or listed in `unsupported_commands`) gets
`unknown command` with HTTP 200, as on hardware. JSON is served as `text/html`.

**UPnP services**:

- AVTransport: `SetAVTransportURI`, `SetNextAVTransportURI`, `Play`, `Pause`, `Stop`, `Next`,
  `Previous`, `Seek`, `GetTransportInfo`, `GetPositionInfo`, `GetMediaInfo`,
  `GetCurrentTransportActions`, `GetInfoEx` (WiiM extension)
- RenderingControl: `GetVolume`, `SetVolume`, `GetMute`, `SetMute`
- ConnectionManager: `GetProtocolInfo`, `GetCurrentConnectionIDs`
- PlayQueue (`urn:wiimu-com`): `BrowseQueue`, `PlayQueueWithIndex`, `SetQueueLoopMode`,
  `GetQueueLoopMode`, `GetKeyMapping` (hidden or absent per `playqueue`)

GENA sends `LastChange` NOTIFYs for AVTransport, RenderingControl and PlayQueue, with
SEQ numbering, 412 on an unknown SID and renew via `SID:`.

Sim actions (`seamer` control API / `dev.sim_action`): `switch_source` (physical input:
`hdmi`, `optical`, `line-in`, `bluetooth`), `app_takeover` (Spotify/AirPlay/Chromecast take
the renderer) and `reboot`.

## Profiles

| Profile | Scheme | Notable |
|---|---|---|
| `wiim_pro` | HTTPS only | A98, fw 4.8; PlayQueue, gapless, full GENA. Registers as `WiimPlayer` in MA |
| `wiim_mini` | HTTPS only | R328, fw 4.6; `getPlayerStatusEx` answers empty for 1 s after `switchmode` (L67) |
| `wiim_amp` | HTTPS only | HDMI ARC input; HTTP volume not evented (L64); input switch without UPnP event (MA#6897) |
| `audiopro_mkii` | HTTPS + mTLS | client cert required; no `getPlayerStatusEx`, no `getMetaInfo`, PlayQueue hidden |
| `arylic_a31` | HTTP only | old LinkPlay: position reports 0, no SetNext, no state events, `Fail` replies, legacy slave list, 701 on resume half the time, 3.5 s transition gap |

All identities use 192.0.2.x addresses and locally administered MACs.

## Knobs

Generic behaviours use core knobs (`report_position_zero`, `report_duration_zero`,
`transition_gap_s`, `latency_s`, `hang`, `offline`, `probe`). Family knobs:

| Knob | Type | Default | Quirks | Conf. | What it does |
|---|---|---|---|---|---|
| `http_scheme` | enum | `'https_only'` | lq:Q1, L57 | high | Which listeners serve /httpapi.asp. WiiM: HTTPS only; A31 modules: HTTP only. |
| `https_port` | int | `0` | lq:Q1, L56 | high | HTTPS API port (real: 443 WiiM, 4443 Audio Pro MkII, 8443 W-gen). 0 = free port. |
| `http_port` | int | `0` | lq:Q1 | high | Plain HTTP API port (real: 80). 0 = free port. |
| `client_cert_required` | bool | `False` | lq:Q1, L56 | high | The HTTPS API demands a client certificate signed by the device CA (mutual TLS). |
| `wrong_scheme_reply` | enum | `'404_text'` | L57 | medium | What /httpapi.asp on the UPnP port answers: a plain '404: Not Found' body, or a dropped connection. |
| `uuid_form` | enum | `'compact24'` | lq:Q4, L83 | high | getStatusEx 'uuid': the 24-hex id, or the 32-hex form (id + its first 8 chars). |
| `wmrm_version` | str | `'4.2'` | lq:Q49 | high | Multiroom protocol version in getStatusEx/getSlaveList (grouping needs equal major). |
| `netfield_style` | enum | `'wiim'` | lq:Q6, L85, L61 | high | getStatusEx network/security fields: WiiM (apcli0 = IP, eth0 0.0.0.0, security https/2.0) or A31 (apcli0 empty string, security_version 2.0, no security field). |
| `metadata_encoding` | enum | `'hex'` | lq:Q12, L52 | high | Title/Artist/Album in getPlayerStatus(Ex): hex UTF-8, plain text, or mixed (Title hex, Artist/Album plain 'unknown'). |
| `idle_unknown_sentinel` | str | `'Unknown'` | L52, lq:Q13, L80 | high | Text reported for Title/Artist/Album when nothing is known (hex-encoded if hex). |
| `artwork_sentinel` | str | `'unknow'` | lq:Q13, L80 | high | albumArtURI reported when there is no artwork. |
| `duplicate_vendor_key` | bool | `False` | L51 | medium | getPlayerStatusEx carries both 'vendor' and 'Vendor':'unknown'. |
| `boot_status_none_s` | float | `0.0` | L53 | high | For this long after start, getPlayerStatusEx reports status 'none' and mode '-1'. |
| `json_corruption` | prob | `{'p': 0.0}` | L54 | medium | getPlayerStatus(Ex) JSON has a doubled comma and a trailing '#'. |
| `pos_units` | enum | `'ms'` | lq:Q14 | high | Units of HTTP curpos/totlen/offset_pts. |
| `metainfo` | enum | `'full'` | lq:Q11, L59 | high | getMetaInfo: normal keys, PDF-style keys with trailing spaces, or not available. Always 'Failed' while idle. |
| `failure_word` | enum | `'Failed'` | lq:Q7, L60 | high | Body of a failed command (Arylic vendor calls say 'Fail'). |
| `reboot_reply` | enum | `'json_ok'` | L60, lq:Q50 | high | Reply to 'reboot' (StartRebootTime:1 always answers OK). |
| `eq_reply_json` | bool | `True` | L60 | high | EQOn/EQOff/EQLoad answer {"status":"OK"} (true) or plain OK (false). |
| `unsupported_commands` | str | `'getStatus,getAudioOutputStatus'` | lq:Q8, lq:Q10, L87, L56 | high | Comma-separated commands this unit answers 'unknown command' (HTTP 200). |
| `slavelist_shape` | enum | `'modern'` | L84, lq:Q44 | high | multiroom:getSlaveList shape: ints, no slave_list when standalone; or the 2014 capitalised string form. |
| `play_start_delay_s` | float | `0.0` | lq:Q17, MA#6867 | medium | After Play on a new URI the device stays STOPPED this long before it starts loading (TRANSITIONING). Reporter captures: about 0.8 s. |
| `stale_prev_position` | prob | `{'p': 0.0}` | lq:Q17, MA#6867 | medium | While a new URI loads (TRANSITIONING), RelTime/curpos still show where the previous stream was. Params: window_s (max duration, default 1.0). |
| `end_position_after_load_s` | float | `0.0` | lq:Q16, MA#6839 | high | For this long after a new stream starts, position reads equal to duration. |
| `false_playing_without_uri_s` | float | `0.0` | lq:Q18 | high | After a transport command with no URI loaded (network mode), report PLAYING for this long. |
| `curpos_negative_on_restart` | prob | `{'p': 0.0}` | LP-03 | medium | While an advanced/restarted stream loads, HTTP curpos is negative. |
| `released_source_looks_paused` | bool | `True` | lq:Q21 | high | When the WiiM app takes the device (sim action app_takeover), MA's session is reported as plain 'pause' with the old metadata, indefinitely. |
| `status_stop_while_connect_playing` | bool | `True` | lq:Q20, L69 | high | Qobuz Connect and Google Cast sessions report status 'stop' while playing (Cast also mode '5', vendor 'CAST'). |
| `input_switch_without_upnp_event` | bool | `False` | lq:Q24, MA#6897 | medium | A physical input switch (CEC turning the TV on) changes HTTP 'mode' but sends no PlaybackStorageMedium in the AVTransport events; only TransportState moves. |
| `input_switch_stop_s` | float | `1.4` | lq:Q24, MA#6897 | medium | During an input switch the transport reports STOPPED for this long, then PLAYING. |
| `stale_metadata_after_switch` | bool | `False` | lq:Q23 | medium | After switching to a physical input, Title and totlen of the last stream remain. |
| `switchmode_empty_body_s` | float | `0.0` | L67 | medium | For this long after switchmode, getPlayerStatusEx answers an empty 200 body. |
| `switchmode_silent_noop` | str | `''` | lq:Q30, L67 | medium | Comma-separated switchmode targets that answer OK but change nothing. |
| `set_next_supported` | bool | `True` | LP-06 | medium | SetNextAVTransportURI works (false: UPnP fault 401 Invalid Action). |
| `http_add_is_noop` | bool | `True` | lq:Q34 | high | setPlayerCmd:add:<url> answers OK but queues nothing (plicount stays put). |
| `advance_event_delay_s` | float | `0.0` | lq:Q35, MA#6557 | medium | AVTransport events about a natural track advance are held back this long. |
| `drop_advance_event` | prob | `{'p': 0.0}` | lq:Q35, MA#6557, MA#5900 | medium | A natural advance sends no AVTransport event at all (until the next command); polls still show the new track. |
| `resume_701` | prob | `{'p': 0.0}` | LP-08, lq:Q39 | medium | Play while PAUSED_PLAYBACK fails with UPnP fault 701 (A31 over DLNA). |
| `pause_in_stop_fault` | bool | `True` | L65 | high | Pause while stopped answers UPnP fault 701 'Transition not available'. |
| `playqueue` | enum | `'present'` | LP-09, LP-11, L82 | medium | The vendor PlayQueue service: listed in description.xml, working but unlisted, or absent (404). |
| `playqueue_loopmode_tag` | enum | `'LoopMode'` | L81 | high | Tag name of the loop mode in PlayQueue LastChange (some firmware misspells it). |
| `getinfoex_supported` | bool | `True` | L79 | high | The vendor AVTransport action GetInfoEx exists. |
| `no_state_events` | bool | `False` | LP-07, lq:Q37 | medium | AVTransport sends only the initial NOTIFY; state changes are never evented. |
| `echo_http_volume_events` | bool | `True` | L64, HA#152720 | medium | Volume/mute set over HTTP (setPlayerCmd:vol/mute) is evented on RenderingControl. |
| `notify_delivery` | enum | `'ok'` | L66, HA#177517 | high | drop_silently: SUBSCRIBE answers 200 with a SID but no NOTIFY is ever sent. |
| `gena_timeout_s` | int | `1800` | L66, lq:Q37 | high | Subscription lifetime granted; unrenewed subscriptions stop receiving events. |
| `vol_collision_window_s` | float | `0.0` | L62, HA#145166 | medium | A volume command arriving within this window of the previous one is held until the window passes and then dropped (only the first sticks). |
| `leader_restart_on_first_join` | bool | `True` | lq:Q46, L71 | high | When the first follower joins, the leader goes TRANSITIONING and re-fetches its URI from the start (live streams too). |
| `role_propagation_delay_s` | float | `0.0` | lq:Q43 | high | A join/leave answers OK at once; getStatusEx/getSlaveList change after this delay. |
| `follower_status` | enum | `'stay_idle'` | L70 | medium | A follower's own getPlayerStatusEx: type '1' and idle, or a copy of the leader. |

Every knob has a `test_<knob>` in `tests/families/linkplay/test_linkplay_knobs.py`; a meta
test fails if one is missing.

## Music Assistant e2e

`tests/e2e/linkplay_ma_smoke.py` runs a real MA server (from `MA_REPO`, its own venv)
against the sim. It is skipped unless `MA_REPO` is set. MA's discovery is pointed at the
sim's description URL and the WiiM SDK's HTTP endpoint at the sim's HTTPS port; MA uses free
high ports and a throwaway data dir (`SEAMER_KEEP=1` keeps it).

```
MA_REPO=~/src/music-assistant .venv/bin/pytest -q tests/e2e/linkplay_ma_smoke.py
PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/linkplay_ma_smoke.py smoke|bug6867|bug6897
```

Result on MA `dev` (2026-10-28):

- **smoke: passes.** MA registers `wiim_pro` as `WiimPlayer`, plays a 300 s FLAC (device
  and MA both `playing`, ~400 kB reached the sink), pauses, seeks to 30 s (MA elapsed
  30.0), sets volume 33 (read back as 33 on both sides) and stops. httpapi commands used:
  `getStatusEx`, `getPlayerStatusEx`, `getAudioInputCapbility`,
  `getSoundCardModeSupportList`, `multiroom`, `setPlayerCmd`.
  Registration takes ~18 s because pywiim's capability probe also tries ports the sim
  does not serve and retries before giving up.

## Bug repros

| Bug | Knobs | Result on dev |
|---|---|---|
| MA#6867 elapsed time jumps ahead after a seek | `wiim_pro` + `stale_prev_position={p=1, window_s=0.6}`, `play_start_delay_s=0.8` | **Reproduces (test fails).** Three seeks (to 120/200/90 s) put MA's elapsed 7.2/15.2/9.2 s ahead: during TRANSITIONING the device still reports the old track position, and `_sync_position` accepts it because the URI already matches |
| MA#6897 TV/HDMI input does not release the MA queue | `wiim_amp` + `input_switch_without_upnp_event=true` | **Reproduces (test fails).** Device is in mode 49 (HDMI) with owner `hdmi`; 20 s later MA still shows `active_source` = its queue, `playing`, old title |
| L64 HTTP volume change not evented | `wiim_amp` (`echo_http_volume_events=false`) | Not reproducible against MA: MA caches the level it set, so the missing event is invisible |
| lq:Q46 join needs a restart of the leader's stream | `role_propagation_delay_s` | Worked around on dev (`JOIN_RESTART_TIMEOUT`); not a failing test |

The two bug tests (`test_bug_6867_no_jump_ahead_after_seek`,
`test_bug_6897_tv_input_releases_the_queue`) assert the correct behaviour, so they fail on
current dev and should pass once fixed.

Fix branches (2026-10-28, 3 runs each, all deterministic): `fix-wiim-seek-position` passes
#6867 (0.0 s ahead, MA settles on the device's position within 0.1 s); dev fails 3/3.
`fix-wiim-external-input` passes #6897 (`active_source` becomes `hdmi`); dev fails 3/3.
That branch still shows the last MA track title under the HDMI source: the WiiM SDK only
replaces its track info from a UPnP event or populated polled metadata, and the switch
sends neither (`input_switch_without_upnp_event`).
