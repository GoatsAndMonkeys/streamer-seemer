# AirPlay family

Package `seamer.families.airplay`, default profile `sonos_era100`.

```
seamer run airplay/sonos_era100
seamer run airplay/shairport_sync -k ntp_streams=accept
```

Quirk IDs: `ca2:Ann` = simresearch/cast_airplay2.md, `cas:Pn` =
simresearch/cast_airplay_spotify.md, `Xn` = simresearch/bug_crossmatch.md, `MA#n` = Music
Assistant issue.

## Two layers

Music Assistant never speaks RTSP/RTP itself: for every stream it spawns the
`cliairplay` helper, pipes PCM into its stdin, sends commands down a FIFO and parses the
`[STATUS]` lines it prints. The family simulates both ends:

1. **Fake helper** (`seamer/families/airplay/fake_cli.py`, standard library only). It has
   the same arguments, stdin/FIFO/stdout/stderr contract and status timing as the real
   binary (`--check`, `--ptp-daemon`, `--protocol --dacp --activeremote --cmdpipe
   --samplerate --bitdepth ... <address>`). Instead of RTSP it calls the receiver's
   `/_wire/*` JSON side channel, and it serves the PCM it got on stdin as `audio/L16`, so
   the device's sink pulls and tone-checks exactly what MA sent. `fake_cli.install(dir)`
   writes an executable wrapper and returns its path. Knobs named `cli_*` are read from
   the device at connect time, so one profile sets both sides.
2. **Receiver** (`AirplayDevice`): the speaker. It owns the knobs, the transport state,
   volume, the sink, the mDNS adverts and the DACP client that reports back to MA.

## What the device serves

| Surface | Serves |
|---|---|
| HTTP `GET /info` | binary plist: deviceID, model, features, name, sourceVersion, `supportedAudioFormatsExtended` only with `airplay2` |
| HTTP `POST /_wire/{connect,start,play,pause,flush,volume,meta,events,teardown}` | the helper's side channel; all ops except `connect` need the current session id (a stale id gets `no such session`, and `events` gets `teardown`) |
| mDNS (opt-in, `advertise` action) | `_airplay._tcp` (AirPlay 2 profiles) and `_raop._tcp` `<MAC>@<name>` with the profile's TXT records |
| DACP client | `GET /ctrl-int/1/setproperty?dmcp.device-volume=<dB>` and play/pause/nextitem, with `Active-Remote` and `DACP-ID` headers, to the target set by `set_dacp_target` |

Sim actions: `set_dacp_target {host, port}`, `press {button: volume|play|pause|playpause|next, level}`
(the speaker's own buttons or app), `pair`, `advertise {addresses}`.

Identity uses `192.0.2.x` addresses and locally administered `02:...` MACs only.

## Driving it from Music Assistant

MA has no override for the helper path, so the e2e test (`tests/e2e/airplay_ma_smoke.py`)
monkeypatches it. The recipe, all done in MA's own interpreter before `mass.start()`:

- Set `get_cli_binary` to an async function that returns the `fake_cli.install()` path, in
  every module that imports it by name: `providers.airplay.helpers`, `.stream`,
  `.provider` and `.pairing`.
- Set `providers.airplay.provider.get_primary_ip_address_from_zeroconf` to return
  `127.0.0.1`, because MA filters out loopback addresses.
- Keep MA off the LAN: no-op `DiscoveryController._register_mass_service`,
  `_setup_mdns_browser`, `run_provider_discovery` and `_schedule_periodic_upnp_discovery`,
  and `AirPlayProvider._register_dacp_service` (then set `prov._dacp_info = None`).
- The builtin Sendspin provider always binds 8927, which clashes with a running MA. Patch
  `providers.sendspin.provider.SENDSPIN_SERVER_PORT` to a free port, no-op
  `SendspinServer._start_mdns_advertising`/`_start_mdns_discovery`, and make
  `DiscoveryController._create_aiozc` return
  `AsyncZeroconf(interfaces=["127.0.0.1"], ip_version=IPVersion.V4Only)`.
- After `mass.start()`, replace `mass.discovery.async_find_mdns_service` so it answers
  with the device's `_raop` record. Point the device at MA's DACP socket
  (`prov._dacp_server`), then call `prov.on_mdns_service_state_change(name, Added, info)`
  with the device's `_airplay._tcp` `AsyncServiceInfo`.
- The AirPlay player is `ap<mac>`. About 15 s later MA wraps it in a Universal Player
  (`player.protocol_parent_id`, `up...`), which owns the queue: use that id for
  `player_queues.*`.

Use a throwaway data dir and free high ports (never 8095). Run scenarios one at a time,
because each MA binds the same default DACP port.

```
MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/airplay_ma_smoke.py
PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/airplay_ma_smoke.py smoke   # prints RESULT {json}
```

## Profiles

| Profile | What it models |
|---|---|
| `sonos_era100` | Sonos Era 100 (AirPlay 2): slow PTP lock, anchor 400s, volume applied and echoed on a slow tick |
| `homepod_mini_os27` | HomePod mini, OS 27: its own PTP clock, RAOP broken (auth-setup 404), buffered anchors refused |
| `wiim_pro_plus` | WiiM Pro Plus as an AirPlay 2 receiver: early drop-out in the first play, echoes volume over DACP |
| `airport_express` | AirPort Express, 1st gen: RAOP only, NTP timing, 2 s latency, no `_airplay._tcp` |
| `shairport_sync` | shairport-sync 4.x (AirPlay 2 build): refuses AP2 streams with NTP timing, resets control about 80 s in |

## Knobs (31)

| Knob | Default | Confidence | Quirks | What it does |
|---|---|---|---|---|
| `airplay2` | True | high | cas:P1 | Speaks AirPlay 2 (feature bit 38/48). False: RAOP only (AirPort Express, old AVRs). |
| `buffered` | False | medium | cas:P1, ca2:A35 | AirPlay 2 buffered route (type 103) instead of realtime (type 96). |
| `ntp_streams` | accept | high | ca2:A32 | shairport-sync: an AP2 stream with NTP timing is refused (SETUP 400). |
| `ptp_respond` | True | high | ca2:A31, ca2:A32 | Answers the sender's PTP. False: clock_ready goes 'stalled' and nothing is heard. |
| `first_probe_ms` | 60 | high | cas:P6, ca2:A35 | First PTP exchange after connect (Sonos ~1078 ms, HomePod OS 27 ~1000 ms). |
| `clock_lock_ms` | {min=150, max=300} | high | cas:P6, ca2:A29, ca2:A35 | PTP lock after connect, drawn per session from {min, max} (Sonos 1700-2300 ms). |
| `ptp_role` | slave | medium | ca2:A28, ca2:A30 | standalone_own_clock: OS 27 HomePod keeps its own clock; silent unless the helper follows it (cli_ptp_follow). master_if_silent: self-promotes without Sync. |
| `anchor_400` | {p=0.0} | high | ca2:A34, MA#6348, MA#6407 | SETRATEANCHORTIME answers 400 until the clock is seated. Params: count (consecutive 400s per session, default 2), retry_ms (helper retry spacing, default 500). |
| `buffered_anchor_always_400` | False | medium | ca2:A34, MA#6430 | A buffered-route anchor is always refused (OS 27 HomePod, type 103). |
| `min_audible_latency_ms` | 0 | medium | ca2:A36, MA#6419 | Audio anchored sooner than this after the START is dropped (ESP32/Yamaha 2500 ms). |
| `output_latency_ms` | 2000 | medium | ca2:A36 | The latency lead the receiver asks for (reported on the [STATUS] latency line). |
| `auth` | none | high | ca2:A41, ca2:A44 | Access control: password (RAOP pw / 401) or PIN pairing (470 until paired). |
| `raop_mode` | ok | high | ca2:A40, MA#5851, MA#6338 | OS 27 broke RAOP to HomePod/Apple TV: /auth-setup 404, or accepts and stays silent. |
| `rtsp_first_status` | 200 | medium | ca2:A41, MA#6655, MA#5831 | Status of the first RTSP request (403 JBL, 455 Apple TV 3). 200 = normal. |
| `connect_ms` | 120 | medium | ca2:A35 | Handshake time from spawn to '[STATUS] connected' (pair-verify, SETUP, RECORD). |
| `starve_after_ms` | {ms=0, first_play_only=False} | medium | ca2:A39, cas:P5, MA#5714 | WiiM Pro Plus: audio cuts out this long into a play (0 = never). first_play_only: only the first START of a session; pause/play recovers. |
| `reset_control_after_s` | 0.0 | medium | ca2:A32, ca2:A45, MA#6243, MA#6743 | The control channel is reset this long into a session (shairport 75-85 s, JBL 40 s). |
| `flush_ack` | ok | medium | ca2:A45, MA#6323 | never: FLUSH is not acknowledged (Arylic A31), so a warm seek waits out the timeout. |
| `pause_reject` | False | low | ca2:A45 | Pause is refused (Arylic A31): the speaker keeps playing. |
| `require_metadata_before_audio` | False | medium | ca2:A50 | Sonos holds back audio until track metadata arrived (MA pushes it before START). |
| `volume_apply_tick_s` | {min=0.0, max=0.0, step=0} | medium | ca2:A49, X8, MA#6664, MA#6838 | Sonos in an AirPlay session applies volume on a tick drawn from {min, max} s (45-65 s) and echoes every queued level, 30 ms apart. step: at most this many % per tick (0 = apply the queue). max 0 = immediate. |
| `volume_echo` | {enabled=True, delay_ms=250, burst=1} | high | cas:P21, ca2:A49 | After the level changes the receiver reports it back over DACP (dmcp.device-volume) after delay_ms. burst: copies of each report, 30 ms apart. |
| `dacp_active_remote` | session | low | X7, MA#5018, MA#5929 | Active-Remote the receiver puts on DACP: the current session's, the first one it ever saw, or the previous session's (stale id; MA logs UNKNOWN PLAYER). |
| `dacp_after_teardown_ms` | -1 | medium | ca2:A49, X7, MA#5018 | Denon: sends a volume DACP this long after TEARDOWN with the old id (-1 = never). |
| `dacp_report_idle` | False | low | X7, MA#5018 | Physical volume changes are reported over DACP even with no live session. |
| `volume_reverts_in_session` | False | low | ca2:A49 | Sonos Port: the level snaps back to the pre-session level while a session is live. |
| `cli_exit_after_eof` | False | medium | ca2:A52 | The helper exits right after '[STATUS] eof' instead of waiting for ACTION=STOP. |
| `cli_ignore_sigterm` | False | medium | ca2:A52 | The helper ignores SIGTERM and stdin EOF, so MA's stop() escalates to SIGKILL. |
| `cli_die_after_ms` | 0 | medium | ca2:A52 | The helper exits with code 1 this long after connecting, with no stderr (0 = never). |
| `cli_start_ack` | normal | low | ca2:A52, ca2:A36 | '[STATUS] started' ack: normal, never printed, or printed 3 s late. |
| `cli_ptp_follow` | True | high | ca2:A28 | The helper follows a receiver that keeps its own clock (airplay-cli >= 0.5.4). |

## Bug repros against MA dev

Run against an MA checkout whose `providers/airplay`, `controllers/player_queues` and
`models/protocol_backed_player.py` are identical to `dev` (October 2026).

| Test | Result on dev | What happens |
|---|---|---|
| `test_ma_drives_airplay_receiver` | passes | Discovery, play (the sink hears MA's PCM), pause, resume, seek (about 31 s for a 30 s target), volume 33 and stop, read back on both MA and the device |
| `test_bug_queue_play_resumes_paused_airplay` | **fails** | `player_queues.play` on a paused queue owned by a Universal Player never reaches the AirPlay player. `_handle_play` calls `ProtocolBackedPlayer.play()`, which only forwards to a Chromecast/DLNA player with an external source and does nothing while an output protocol is active. MA waits out the 5 s start timeout and the pause watcher later stops playback. Pause works because it goes through `players._handle_cmd_pause`. Newly found; the play line came in with #6779 |
| `test_bug_6664_sonos_volume_echo_keeps_user_level` | **fails** (MA#6664) | Two quick changes (40 then 50). The Sonos applies them on its tick and echoes both after MA's 2 s grace. MA treats each echo as a user change and sends it again, so the pair ping-pongs: [40, 50] six times in 20 s (10 extra sends) |
| `test_bug_5018_external_volume_is_reflected` | passes | Can't be reproduced on dev. Volume changed on the speaker is reflected in a session (55/55), after TEARDOWN with the old session id (55/55) and between sessions (25/25). The Active-Remote id that stays stable per MAC appears to have fixed it |
