# DLNA / UPnP renderer family

Package `seamer.families.dlna`, default profile `generic_cheap`. A generic UPnP AV
MediaRenderer as Music Assistant's `dlna` provider (async_upnp_client `DmrDevice`) sees it.

```
seamer run dlna/gmrender_resurrect
seamer run dlna/lumin_teac -k range_required=error
```

Quirk IDs: `dsh:N` = simresearch/dlna_sonos_heos.md item N, `DL-NN` = simresearch/quirks.md,
`X5` = bug_crossmatch.md new quirk, `ma:#N` = Music Assistant support issue.

## What the device serves

| Listener | Serves |
|---|---|
| HTTP (the device port) | `/description.xml`, `/upnp/<svc>/scpd.xml`, `/upnp/<svc>/control` (SOAP), `SUBSCRIBE`/`UNSUBSCRIBE /upnp/<svc>/event` (GENA), `/_sim` |
| SSDP (UDP, loopback) | M-SEARCH replies for `MediaRenderer:1` and each service type; `ssdp_port` option, default ephemeral |

The UDN is a stable uuid5 of the device id and serial. Addresses in profiles are RFC 5737
(`192.0.2.x`) and MACs are locally administered fakes.

**Services**

- **AVTransport:1:** SetAVTransportURI, SetNextAVTransportURI, Play, Pause, Stop, Next,
  Previous, Seek (REL_TIME, ABS_TIME, TRACK_NR), GetTransportInfo, GetPositionInfo,
  GetMediaInfo, GetTransportSettings, GetCurrentTransportActions.
- **RenderingControl:1:** GetVolume, SetVolume, GetMute, SetMute, ListPresets and
  SelectPreset. Only the Master channel exists.
- **ConnectionManager:1:** GetProtocolInfo, GetCurrentConnectionIDs and
  GetCurrentConnectionInfo.
- **OpenHome** (only with `openhome`): Product, Info, Time and Volume.
  - Product lists its sources: Playlist, Radio, UPnP AV and Songcast.
  - Info.Details reports the real codec and sample rate. While the device fakes playing it
    reports `CodecName=""` and `SampleRate=0`.

**SOAP**

- Bodies are exact UDA 1.0 envelopes.
- Faults are `s:Client` / `UPnPError` with the standard codes: 401, 402, 701, 710, 711, 714,
  716, 718 and 706.
- Times are `H:MM:SS`, with no zero-padded hour.
- RelCount and AbsCount are `2147483647`.

**GENA**

- Subscribing returns a SID and a TIMEOUT (default `Second-1800`, or whatever was requested).
- The initial NOTIFY is sent with SEQ 0. Each LastChange is an `<Event>` in the AVT or RCS
  namespace with `<InstanceID val="0">`.
- A renewal with CALLBACK or NT returns 400, and an unknown SID returns 412.

**Audio**

- The device really fetches the stream with `DlnaSink`, a simkit `HttpSink` that can also
  re-request the stream with Range mid-stream.
- What the sink plays is the truth. Knobs only change what the device *reports* (through
  `DlnaLens`) or how it answers.

**Sim actions**

- `local_volume {level}`: someone turns the volume on the device itself.
- `drop_subscriptions`: the device forgets every subscription, as after a reboot.

## Profiles

| Profile | Models | Knobs set |
|---|---|---|
| `gmrender_resurrect` | gmrender-resurrect on a Raspberry Pi | `seek_units=rel_time_silent`, `seek_when_paused=resume_silently`, `seek_emits_event=false`, `abs_time_not_implemented` |
| `lumin_teac` | TEAC NR-7CD / LUMIN-style strict-range streamer | `range_required=silent`, core `require_range`, `openhome` |
| `eversolo` | Eversolo DMP-A6 | `play_blocks_until_audio_s=10`, `openhome` |
| `samsung_tv` | Samsung Q80 TV DMR | `head_check_on_set_uri`, `protocolinfo_filter`, `new_sid_on_renew`, `range_rerequest_after_bytes=262144`, `set_next=playing_only` |
| `generic_cheap` | no-name amp or speaker firmware | `transport_actions=stale`, `set_next=ignored`, `didl_malformed`, `no_rc_events`, `abs_time_not_implemented` |

## Knobs

There are 40 family knobs; the core transport and sink knobs (`load_delay_s`,
`prebuffer_s`, `require_range`, and so on) apply as well. Every knob has a test in
`tests/families/dlna/test_dlna_knobs.py`, and a meta-test enforces that.

| Knob | Type | Default | Quirks | Confidence | What it does |
|---|---|---|---|---|---|
| `transport_actions` | enum | 'accurate' | dsh:1, DL-06, ma:#6425 | high | CurrentTransportActions: accurate; stale = frozen at the value computed at the last SetAVTransportURI (so 'Pause' never appears while playing); empty = always ''. |
| `play_blocks_until_audio_s` | float | 0.0 | dsh:3, DL-05 | high | Hold the SOAP Play response until the sink plays its first audio, at most this long (UDA allows 30 s). 0 = answer at once. |
| `play_response_delay_s` | float | 0.0 | dsh:3, DL-05 | high | Fixed extra delay before every Play response (after the play has started). |
| `autoplay_on_set_uri` | bool | False | dsh:2, dsh:8 | high | SetAVTransportURI starts playback by itself (no Play needed). |
| `previous_media_lingers_s` | float | 0.0 | dsh:2 | high | After SetAVTransportURI while playing, GetMediaInfo/GetTransportInfo keep reporting the previous URI and state for this long. |
| `set_next` | enum | 'ok' | dsh:10, dsh:11, dsh:12, ma:#5852 | high | SetNextAVTransportURI: ok; absent (not in the SCPD, 401); ignored (200 and reported in GetMediaInfo, but the device stops at EOF); playing_only (701 unless PLAYING, Kodi); fault_701 (always 701, low confidence Sonos S1 #5852). |
| `stop_when_idle_701` | bool | False | dsh:4 | high | Stop on a STOPPED/NO_MEDIA_PRESENT device returns UPnP fault 701. |
| `skip_settle_delay_s` | float | 0.0 | dsh:5 | high | Next and Seek are acknowledged at once but only take effect after this delay. |
| `track_gap_state` | enum | 'transitioning' | dsh:7 | high | What TransportState says while advancing to the next item (transition_gap_s). |
| `pause_needs_content_length` | bool | False | dsh:6 | high | Pause returns 701 when the stream had no Content-Length (chunked/endless). |
| `seek_units` | enum | 'all' | dsh:13 | high | all = REL_TIME and ABS_TIME; rel_time_only = other units get 710; rel_time_silent = other units return 200 and do nothing (gmrender, Kodi). |
| `seek_when_paused` | enum | 'ok' | dsh:14 | high | Seek while PAUSED: ok (stays paused at the target); error (701); resume_silently (starts playing, gmrender #83). |
| `seek_emits_event` | bool | True | dsh:14 | high | False: no AVTransport LastChange for 2 s after a Seek (gmrender #252). |
| `reltime` | enum | 'live' | DL-01, dsh:16, dsh:17, ma:#6874 | high | GetPositionInfo RelTime: live; stuck at 0:00:00; or the string NOT_IMPLEMENTED. |
| `abs_time_not_implemented` | bool | False | dsh:16, dsh:17 | high | AbsTime (and AbsCount) report NOT_IMPLEMENTED. |
| `didl_malformed` | bool | False | dsh:22 | high | TrackMetaData / CurrentURIMetaData come back as DIDL-Lite that does not parse (Bose SoundTouch). |
| `lastchange_malformed` | prob | {'p': 0.0} | DL-03 | high | An AVTransport LastChange event is cut short (invalid XML). |
| `pause_emits_empty_uri_event` | bool | False | dsh:9 | high | After PAUSED_PLAYBACK a second LastChange says CurrentTrackURI='' and CurrentTransportActions='Play,Stop' (Sony BDP-S590). |
| `duplicate_lastchange_and_vars` | bool | False | dsh:9, DL-02 | high | NOTIFY carries TransportState etc. as separate properties next to LastChange. |
| `non_utf8_bodies` | bool | False | dsh:34 | high | SOAP responses and NOTIFY bodies contain a Latin-1 byte that is invalid UTF-8. |
| `content_length_off_by_one` | bool | False | dsh:35, ma:#6567 | high | GetMediaInfo declares a Content-Length one byte larger than its body (Busch-Jaeger BJE_Radio_iNet). |
| `soap_fault_http200` | bool | False | dsh:36 | high | SOAP faults are sent with HTTP 200 instead of 500. |
| `relative_presentation_url` | bool | False | dsh:37, ma:#6481 | high | <presentationURL>/</presentationURL> instead of an absolute URL. |
| `volume_max` | int | 100 | dsh:20 | high | Volume allowedValueRange maximum in the RenderingControl SCPD. |
| `volume_before_play_ignored` | bool | False | dsh:21, ma:#6762 | low | SetVolume while not playing is acknowledged (and read back), but Play restores the level the amp had before (Onkyo). |
| `no_rc_events` | bool | False | ma:dlna#3474 | medium | RenderingControl sends only the initial NOTIFY; volume/mute must be polled. |
| `rc_lastchange_namespace` | enum | 'rcs' | dsh:45 | high | Namespace of the RenderingControl LastChange (avt = old gmrender bug #42). |
| `protocolinfo_filter` | bool | False | dsh:25, DL-08, DL-09 | medium | SetAVTransportURI returns 714 when the DIDL res@protocolInfo MIME type is not in the sink protocol list. |
| `protocolinfo_empty_entries` | bool | False | dsh:26 | high | GetProtocolInfo Sink contains empty entries (',,' and a trailing comma). |
| `head_check_on_set_uri` | bool | False | DL-07 | high | SetAVTransportURI sends HEAD for the URL and returns 716 if that fails (Samsung). |
| `range_required` | enum | 'off' | dsh:28, ma:#6775 | high | On Play the device probes the URL with Range bytes=0-, 12- and 16384- (the LUMIN log in #6775). Anything but 206: silent = report PLAYING with an advancing RelTime and OpenHome CodecName='' but play nothing; error = fault 716. |
| `range_rerequest_after_bytes` | int | 0 | X5, ma:#6775 | low | After this many bytes the sink drops the connection and asks for the rest with Range: bytes=N-; a non-206 answer ends playback (Samsung, seen over Cast). 0 = off. |
| `notify_before_subscribe_response` | bool | False | dsh:40 | high | The initial NOTIFY (SEQ 0) is delivered before the SUBSCRIBE 200 carrying the SID. |
| `granted_timeout_s` | int | 0 | dsh:42 | high | TIMEOUT granted: 0 = what was asked (1800 if none), -1 = Second-infinite, >0 = always this. |
| `new_sid_on_renew` | bool | False | dsh:43, DL-04 | high | A renewal answers with a new SID (the old one stops getting events). |
| `renew_response_without_sid` | bool | False | DL-04 | high | The renewal response has no SID header. |
| `reject_subscribe` | bool | False | dsh:41 | high | SUBSCRIBE answers 500; clients must poll. |
| `subscribe_ok_no_notify` | bool | False | dsh:41 | high | SUBSCRIBE succeeds but no NOTIFY is ever delivered. |
| `event_delay_s` | float | 0.0 | dsh:44 | low | Every NOTIFY is delivered this late (gmrender #251 saw 10 s). |
| `openhome` | bool | False | dsh:50, dsh:28 | high | Also expose OpenHome Product, Info, Time and Volume (lite). Info.Details is the 'really decoding' signal: CodecName='' and SampleRate=0 while faking playback. |

## Music Assistant e2e

`tests/e2e/dlna_ma_smoke.py` is skipped unless `MA_REPO` is set. Each scenario runs MA
in a subprocess with MA's venv, on loopback only, on free ports above 20000 (never 8095),
with a throwaway data dir, and with mDNS and SSDP search off.

**How the device reaches MA**

- The sim is handed to `DLNAPlayerProvider._device_discovered(udn, description_url)`,
  exactly as an SSDP answer would be.
- MA's GENA callback is `/notify` on its stream server.
- On dev, MA registers the renderer as a *protocol* player (`DLNAPlayer`). About 15 s later
  it wraps it in a `UniversalPlayer`, and the queue lives on that wrapper.

**`smoke`** (eversolo) passes on dev (cdff463ec):

- MA subscribes to avt, rc and cm.
- `play_media` runs Stop, then SetAVTransportURI, then Play. The sink receives MA's flow
  stream, `audio/flac` chunked, and decodes it.
- Pause and resume both work.
- `seek(30)` works: MA restarts the flow stream at 30 s (a new SetAVTransportURI, not a
  Seek), and MA's elapsed time is 30.3.
- Setting the volume to 33 reads back as 33 on both sides.
- Stop works.

## Bug repros (MA dev cdff463ec)

| Issue | Scenario | Result on dev |
|---|---|---|
| [#6775](https://github.com/music-assistant/support/issues/6775) TEAC/LUMIN plays nothing | `bug6775` (lumin_teac) | **Reproduced; the test fails.** The renderer probes with `Range: bytes=0-`. MA's stream server answers `200` with `Accept-Ranges: none`, so the renderer goes into fake play. MA and the device both report PLAYING, and elapsed time counts to 6.3 s, but the sink got 0 bytes. A fix needs MA to answer Range requests with 206 (or a per-player "no range" workaround). |
| [#6762](https://github.com/music-assistant/support/issues/6762) volume set before play is lost | `bug6762` (eversolo + `volume_before_play_ignored`, low confidence) | **Reproduced; the test fails.** Volume 45 is set while the player is stopped. When playback starts, the amp restores 20, and MA shows 20 and never re-applies 45. Open PR 6944 targets this. |
| [#6800](https://github.com/music-assistant/support/issues/6800) Eversolo reconnects every ~130 s | none | **Not reproducible through DLNA.** The reconnects are on Eversolo's Squeeze Connect (slimproto) client (bug_crossmatch §3.9, quirk X1). The suspected cause is a missing LMS-style heartbeat from aioslimproto. MA's `dlna` provider is not involved, so it belongs in the squeezelite family. |

To run the repros:

```
MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/dlna_ma_smoke.py
PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/dlna_ma_smoke.py bug6775
```
