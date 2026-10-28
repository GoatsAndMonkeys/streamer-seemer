# Chromecast family (`chromecast`, alias `cast`)

A simulated Google Cast receiver: the CASTV2 control protocol on TLS, the eureka_info
HTTP endpoint and `_googlecast._tcp` mDNS. The media it is given is really fetched over
HTTP into the shared sink, so a sender's stream server sees a real client. Wire shapes,
error bodies and timings come from cast_airplay_spotify.md (C1-C29), cast_airplay2.md
(C30-C47), PLAN.md section 3.5 (CC-xx) and bug_crossmatch.md (X5).

## Surfaces

| Surface | Where | What |
|---|---|---|
| CASTV2 | TLS on `cast_port` (8009; any free port otherwise) | 4-byte length + `CastMessage` protobuf |
| `tp.connection` | | CONNECT / CLOSE of virtual connections |
| `tp.heartbeat` | | PING / PONG (`heartbeat_stop_after_s`, `device_ping_s`) |
| `receiver` | `receiver-0` | GET_STATUS, LAUNCH (CC1AD845 Default Media Receiver, C35B0678 MA receiver, others), STOP, SET_VOLUME, GET_APP_AVAILABILITY; RECEIVER_STATUS, LAUNCH_ERROR |
| `media` | the app's `transportId` | LOAD, QUEUE_LOAD, PLAY, PAUSE, STOP, SEEK, QUEUE_INSERT/UPDATE/NEXT/PREV/REMOVE, GET_STATUS, SET_VOLUME; MEDIA_STATUS with `idleReason` (FINISHED, CANCELLED, INTERRUPTED, ERROR), ERROR / LOAD_FAILED / INVALID_REQUEST |
| `multizone` | `receiver-0` | GET_STATUS (MULTIZONE_STATUS with members), GET_CASTING_GROUPS |
| MA custom namespace | | accepted (MA's receiver app) |
| HTTP | main port (8008 on a real device) | `/setup/eureka_info` (`params=`, `options=detail`), `/setup/icon.png` |
| HTTPS | `eureka_https_port` (8443) | the same routes |
| mDNS | `_googlecast._tcp` | TXT id/cd/md/fn/ca/st/rs/... as pychromecast parses them |

Device options for `build_device(..., **options)`: `cast_port`, `eureka_https_port`
(`None` disables HTTPS), `mdns`.

Sim actions (`POST /_sim/action/<name>`): `spontaneous_error`, `foreign_takeover`,
`device_pause`, `press_next`, `leader_move`, `quit_app`.

## Knobs (34)

| Knob | Type | Default | Quirks | Confidence |
|---|---|---|---|---|
| `content_length_cutoff_bytes` | int | `0` | C9, C33 | high |
| `no_content_length_stop_after_s` | float | `0.0` | C10, C33 | medium |
| `idle_on_eof` | enum | `'FINISHED'` | C1, C4, C28, C42 | high |
| `clip_tail_cut_ms` | int | `0` | C28, C42 | low |
| `strict_content_type` | bool | `False` | C12, C5 | high |
| `public_dns_only` | bool | `False` | C13 | high |
| `reject_http_media` | enum | `'off'` | C36 | medium |
| `range_rerequest_after_bytes` | int | `0` | X5, C33 | medium |
| `kill_control_socket_after_flow_restart_s` | float | `0.0` | C34, CC-07 | medium |
| `spontaneous_error` | dict | `{}` | C30, C46, CC-04 | high |
| `next_item_fetch_delay_ms` | int | `50` | C30, C32 | high |
| `app_error_rate` | prob | `{'p': 0.0}` | C31 | medium |
| `status_sparse_media` | bool | `False` | C2 | high |
| `current_time_jump` | prob | `{'p': 0.0}` | C3, CC-09 | medium |
| `omit_idle_reason` | bool | `False` | C47, C1 | medium |
| `empty_status_after_stop` | bool | `False` | C47 | medium |
| `supported_media_commands` | int | `12303` | C6 | high |
| `idle_app_timeout_s` | float | `0.0` | C41 | medium |
| `spontaneous_pause_mean_s` | float | `0.0` | C21 | medium |
| `launch` | enum | `'normal'` | C15, C16, CC-13 | high |
| `launch_error` | dict | `{}` | C35, C23 | high |
| `launch_fail_once` | bool | `False` | C35 | medium |
| `quit_ack_delay_s` | float | `0.0` | C17 | high |
| `foreign_takeover` | dict | `{}` | C40, C20, CC-06 | high |
| `control_type` | enum | `'attenuation'` | C7 | high |
| `step_interval` | float | `0.05` | C7 | low |
| `ignore_idle_volume` | enum | `'off'` | C8, C43 | medium |
| `heartbeat_stop_after_s` | float | `0.0` | C18 | high |
| `device_ping_s` | float | `0.0` | C18 | low |
| `eureka_info` | enum | `'ok'` | C36, C37 | high |
| `pair_schema` | enum | `'legacy'` | C38, C27 | high |
| `leader_moves_after_s` | float | `0.0` | C25 | high |
| `leader_move_gap_s` | float | `2.0` | C25 | low |
| `output_delay_ms` | int | `330` | C14 | medium |

Each knob's doc string in `seamer/families/chromecast/knobs.py` gives the real values
seen in the research. Every time a knob changes behaviour the device records a
`knob fired` event with its quirk IDs and confidence.

## Profiles

| Profile | Device | Notable knobs |
|---|---|---|
| `nest_mini` | Google Nest Mini (2nd gen) | no-Content-Length stop after ~7 min (C10/C33), app quits after 5 min idle (C41) |
| `google_home_speaker` | Google Home / Nest Audio class | 24 MiB Content-Length cutoff (C9/C33), rare decode errors (C30), no MA receiver app (C39) |
| `chromecast_google_tv` | Chromecast with Google TV | video device, Backdrop idle app, master volume over CEC |
| `cast_group` | Cast speaker group (members: Kitchen speaker, Living room speaker) | BUFFERING forever at end of flow (C1/C42), 1.5 s announcement tail cut (C28/#6695), leader moves (C25) |
| `lg_s95tr` | Third-party Cast stack (LG soundbar style) | strict Content-Type (C12), Range re-request (X5), control socket reset after a flow restart (C34), no MA receiver (C35) |

All identities use 192.0.2.x addresses and locally administered MACs (02:00:5e:...).

## Music Assistant end-to-end (`tests/e2e/chromecast_ma_smoke.py`)

Skipped unless `MA_REPO` points at an MA checkout with its own `.venv`. Each scenario
runs MA in a subprocess on loopback, with a throwaway data dir and free high ports (never
8095/8097/8927).

Workarounds needed to run MA against the sim without touching the LAN:

- **Discovery**: the sim is not advertised over mDNS. A `pychromecast.models.CastInfo`
  for it is put into the provider's `browser.devices` and the provider's own callback
  `ChromecastProvider._on_chromecast_discovered(uuid, "")` is called, so MA runs the same
  eureka_info probes and pychromecast connect as for a real device.
- **LAN silence**: MA's discovery controller (mDNS browser, service registration,
  provider discovery), `ChromecastProvider.discover_players` (it starts a LAN
  `CastBrowser`) and aiosendspin's mDNS advertising/discovery are patched to no-ops.
- **Fixed eureka ports**: pychromecast's `dial.FORMAT_BASE_URL_HTTP/HTTPS` are
  hard-wired to 8008/8443; they are pointed at the sim's HTTP/HTTPS ports.
- **Port 8009**: `dial.get_cast_type` calls any device whose cast port is not 8009 a
  group, and MA then takes the group volume path. Single speakers bind 127.0.0.1:8009
  when free; otherwise (and for the group's member speakers) the CastInfo carries the
  cast type and manufacturer so no guess is made.
- **Sendspin**: MA's builtin Sendspin provider binds 8927; its port is patched to a free one.

Scenarios and results against MA dev (2026-10-28):

| Scenario | Result |
|---|---|
| `smoke` (nest_mini) | pass: MA registers a ChromecastPlayer after the eureka probes, launches C35B0678, LOADs its flow stream (LIVE, audio/flac) and the sink receives it; pause, seek (30 s), volume 0.33/33 and stop (IDLE/CANCELLED) read back on both sides |
| `bug6658` (C30 error 80 s before the end of the last track) | no replay of earlier tracks, but MA's queue index jumps from 2 back to 0 with no new LOAD and MA goes idle: the remaining ~80 s are lost (xfail, #6658/#6895 partial) |
| `bug6658_idle` (error after the flow finished, device stays IDLE/ERROR) | pass: no replay, index stays on the last item |
| `x5` (Range re-request after 1.5 MB) | repro: MA answers `Range: bytes=N-` with 200 from the start, the device drops the item (IDLE/ERROR), MA idles (xfail, #6775/#6106) |
| `bug6695` (4 s announcement on a Cast group, 1.5 s tail cut) | repro: the group plays the announcement through its own stream and only ~2.4 s are heard; MA does not pad the end (xfail). Without simulated member speakers MA silently plays nothing on the group (`_members_announce_in_step` is true for an empty member list) |

Run one scenario directly (prints `RESULT {json}`; `SEAMER_KEEP=1` keeps MA's log):

    PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/chromecast_ma_smoke.py smoke
