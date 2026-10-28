# Squeezelite / slimproto family

Package `seamer.families.squeezelite`, alias `slimproto`, default profile
`squeezelite_linux`.

```
seamer run squeezelite/squeezelite_linux -k server=127.0.0.1:34830
seamer run slimproto/squeezelite_esp32 -k stmd_early_s=3
```

Quirk IDs: `Sn` = the squeezelite rows of the research quirk table, `SQ-nn` =
simresearch/quirks.md, `X1`/`Y11` = cross-family rows; `MA #n` = music-assistant/support
issue.

## What the device does

The device is a slimproto **client**, written from scratch (no aioslimproto). It does not
listen on any TCP port; it dials the server:

| Endpoint | Direction | Notes |
|---|---|---|
| slimproto control (TCP) | device -> server `host:port` | `server` knob or the `connect` sim action. Real servers use 3483; tests use a free port |
| UDP discovery | device -> server UDP port | `discover` sim action: TLV request (`e`, IPAD/NAME/JSON/VERS/UUID) or legacy 18-byte `d`; connects to the first reply |
| audio (HTTP GET) | device -> stream URL | built from `strm s` (server ip/port + request line, 0 ip = the control peer), read by the core `HttpSink` |

Client frames: `HELO` (36 bytes + capability string, reconnect flag on reconnect),
`STAT` (STMc/STMs/STMd/STMu/STMl/STMp/STMr/STMf/STMt/STMo/STMn), `RESP`, `DSCO`,
`SETD` (name), `BYE!`. Server frames handled: `strm` (`s t q f p u a`), `cont`, `codc`,
`aude`, `audg`, `setd`, `vers`, `serv`; others are recorded and ignored.

Behaviour as squeezelite 2.0:

- `strm s` opens the stream; the body is held until `cont` (unless
  `codc_after_body_noise`); autostart 0/2 waits for `strm u` after STMl.
- STMs on first audio, DSCO + STMd when the stream is fully read, STMu when output drains.
- A `strm s` while a stream plays is queued gapless: the current HTTP read is capped where
  it is and the next stream starts when the current one ends.
- `strm p` with an interval = silent pause for that many ms; `strm u` with a jiffies
  timestamp starts at that time.
- `strm t` is echoed with `server_timestamp`; STMt also every `stmt_interval_s` while
  playing. Elapsed counts audible time (pauses excluded), from 0 per stream.
- `audg` sets volume (MA's 0-100 is recovered from the old-gain table) and digital gain;
  `aude` 0 turns output off; `aude` 1 leaves a stopped output stopped (real squeezelite, so
  MA's mute acts as pause and unmute does not resume).

Sim actions: `connect {host, port}`, `disconnect`, `bye`, `discover`.

## Configuring Music Assistant against it

Give MA's squeezelite provider a free `port` and set `cli_telnet_port` and `cli_json_port`
to 0 so no CLI listeners start; `discovery` can stay off because the sim dials the port
directly. aioslimproto binds its UDP discovery on the control port, not 3483. The player
id is the profile MAC. See `tests/e2e/squeezelite_ma_smoke.py`.

## Profiles

All MACs are fake (`02:00:00:5c:00:0n`).

| Profile | Notable |
|---|---|
| `squeezelite_linux` | squeezelite 2.0 on Linux, 2 MB buffer, 32-bit PCM, https |
| `picoreplayer_hwvol` | piCorePlayer with `-V` hardware volume: gain stays at unity |
| `squeezelite_esp32` | device id squeezeesp32, 256 KB buffer, 16-bit max, no https, 4 s sink buffer |
| `squeezebox_radio` | SqueezePlay `baby`, bare SETD name, no https, 0.5 s HELO delay |
| `wiim_slimproto` | WiiM slimproto: elapsed advances while paused, starts before `strm u` |

## Knobs

| Knob | Type | Default | Quirks | Meaning |
|---|---|---|---|---|
| `server` | str | `''` |  | host:port of the slimproto server to connect to at start ('' = wait for the connect/discover sim action). |
| `reconnect_delay_s` | float | `1.0` |  | Wait before reconnecting after the server closes the control connection. |
| `helo_delay_s` | float | `0.0` | Y11 | Delay between TCP connect and HELO (some firmware takes 6-8 s; slimserver#1652). |
| `server_silence_timeout_s` | float | `35.0` | X1 | Drop and reconnect when nothing arrives from the server for this long (squeezelite: 35 s; Eversolo-style firmware ~120 s, MA #6800). 0 = never. |
| `batch_frames_per_recv` | int | `1` | S14 | Coalesce this many client frames into one TCP write (servers must split them). |
| `send_bye_keep_socket` | bool | `False` | S13 | The 'bye' action sends BYE! but leaves the socket open (MA #6581). |
| `hello_caps` | str | `''` | SQ-01, S11 | Replace the whole HELO capability string ('' = built from the identity). |
| `can_https` | bool | `True` | S11 | Advertise CanHTTPS=1 and accept strm flag 0x20. |
| `max_pcm_bits` | int | `32` | SQ-01 | Highest PCM sample size the output accepts; a deeper pcm strm/codc gets STMn. |
| `setd_name_nul` | bool | `True` | S12 | SETD name reply is NUL-terminated (SqueezePlay firmware sends it bare). |
| `stream_buffer_kb` | int | `2048` | S4 | Stream buffer size reported in STAT. |
| `stmd_early_s` | float | `0.0` | S4, SQ-06 | Send STMd (decoder ready) this many seconds after STMs instead of at stream EOF (MA #6359/#6666: a tiny buffer drains the stream at once). 0 = at EOF. |
| `stale_stmt_after_flush` | int | `0` | S5 | After strm q, send this many STMt still carrying the flushed stream's elapsed. |
| `stale_resp_after_flush` | bool | `False` | S5 | After a flush and a new strm s, send the old stream's RESP once more after STMc. |
| `codc_after_body_noise` | bool | `False` | S7, SQ-01 | Read the body without waiting for cont; a codc arriving after audio started re-opens the decoder mid-stream: buffered audio is lost and a burst of noise is recorded (MA #6398/#4163). |
| `flac_sync_silent` | prob | `{'p': 0.0}` | S19 | A FLAC stream never syncs: the body is read but nothing is played and no STMs is sent (MA #5506, multi-room flac). |
| `stmn_decode_error` | prob | `{'p': 0.0}` | S21 | Decoder error at at_s seconds (param, default 2.0): STMn, the rest of the stream is dropped, buffered audio plays out, then STMu. |
| `stmo_underrun_at` | prob | `{'p': 0.0}` | S21 | Output underrun at at_s (default 3.0) lasting stall_s (default 2.0): STMo, silence, then playback resumes on its own (no STAT, like squeezelite). |
| `dsco_reason` | int | `0` | S13 | Reason byte of the DSCO sent when the data stream closes (0 = ok, 4 = timeout). |
| `initial_gain_zero` | bool | `False` | S9 | Output starts at gain 0 and stays silent until the first audg (MA #6561). |
| `hardware_volume` | bool | `False` |  | Volume goes to the ALSA mixer (-V): digital gain stays at unity. |
| `mute_aude_stops_output` | bool | `True` |  | aude with enable_spdif=0 (MA's mute) turns the output off; aude 1 leaves it stopped until strm u (real squeezelite). |
| `early_start_before_unpause_at` | bool | `False` | S16 | Without autostart, start playing as soon as the buffer is ready instead of waiting for strm u (WiiM slimproto). |
| `heartbeat_while_paused_advances` | bool | `False` | S16 | STMt elapsed keeps counting while paused (WiiM slimproto). |
| `pause_resume_position_jump` | prob | `{'p': 0.0}` | S17 | After a resume, reported elapsed is ahead by jump_s (default 2.0). |
| `seek_elapsed_from_zero` | bool | `True` | S18 | Elapsed restarts at 0 for each new stream (squeezelite). False: elapsed carries on from the stream before a flush, so a seek reports old+new time (MA #3704). |
| `elapsed_ms_bits` | int | `32` | S2 | elapsed_ms is truncated to this many bits before sending. |
| `stmt_interval_s` | float | `1.0` |  | Unsolicited STMt period while playing (0 = only answer strm t). |
| `jiffies_offset_ms` | int | `0` | S15 | Offset of the player's jiffies clock. |
| `drift_ppm` | float | `0.0` | S15 | Rate error of the jiffies clock. |

## Approximations

- Decoding is the core sink's: the stream is not really decoded; codec checks are on the
  `strm`/`codc` format and PCM size only.
- The next gapless stream is opened when the current one ends, not while it plays out
  (squeezelite opens it as soon as the stream buffer has room).
- Output buffer, `STMl` threshold and STMt `output_buffer_*` fields are approximate.
- No sync groups (`strm` `a` skip-ahead is recorded only), IR, display (`grfe`) or
  visualiser frames.
- `serv` (switch server) disconnects and reconnects to the given IP.

## MA end-to-end results (ma-dev)

`tests/e2e/squeezelite_ma_smoke.py`, skipped unless `MA_REPO` is set.

| Scenario | Profile / knobs | Result on dev (aioslimproto 3.3.0) |
|---|---|---|
| `smoke` | `squeezelite_linux` | passes: MA registers the player (`SqueezelitePlayer`), the sink receives MA's FLAC stream, pause/resume, seek to 30 s (MA and device agree within 0.2 s), volume 33 read back on both sides, stop |
| `bug6359` | `squeezelite_esp32`, `stmd_early_s=3` | passes (fixed): MA sends the next track on the early STMd, the player queues it gapless, track 1 plays in full |
| `bug6160` | `squeezelite_linux` | passes (fixed): after 30 s paused MA stops the queue by design (#6614) and resumes from the paused position; it advances at track end |
| `bug3704` | `seek_elapsed_from_zero=False` | **fails**: after a seek to 100 s MA shows ~110 s; it adds the elapsed the firmware carries over from the flushed stream |
| `shutdown_hang` | `squeezelite_linux` | **fails** (new, no support issue): once a player has disconnected, `SlimServer.stop()` never returns, so MA's stop / provider reload hangs. aioslimproto's reader sets `_connected = False` and then calls `disconnect()`, which returns early, so the server-side writer is never closed and `Server.wait_closed()` waits forever |

Two more knobs map to issues that cannot fail against dev: `initial_gain_zero` (#6561)
because aioslimproto sends `audg` right after HELO, and `server_silence_timeout_s` (#6800)
because it sends `strm t` every 5 s. `codc_after_body_noise` (#6398) needs `codc` to
arrive after the first audio, which a loopback MA never does.

Also seen: MA's mute is `aude 0`, which real squeezelite treats as output off; unmute does
not resume while MA keeps showing PLAYING (`mute_aude_stops_output`, no issue found).
