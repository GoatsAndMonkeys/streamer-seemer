# Sendspin family (`sendspin`)

Simulated Sendspin speakers: ESPHome speakers (ESP32-S3 with PSRAM, plain ESP32),
Home Assistant Voice PE, and the Linux python and C++ clients. Music Assistant's
`sendspin` provider is the server, and it **dials the device**.

Quirk IDs (SS1 to SS58) come from the research dossier `simresearch/sendspin.md`.
Confidence is high, medium or low, following the dossier's H/M/L tags.

## Endpoint

| Surface | Path | Port | Notes |
|---|---|---|---|
| `sendspin_ws` | `GET /sendspin` (WebSocket) | 8928 (`default_port`) | The device listens and the server connects (SS41). It is found by mDNS `_sendspin._tcp` or by MA's "manual discovery IPs" entry. A browser `Origin` header gets a 403. |

Each device speaks one wire generation, set by the `wire_gen` knob:

- **G2** (`g2.py`): this is the real aiosendspin 10.2.1 `SendspinClient`.
  - It does `client/init`, then the Noise KKpsk2 handshake with the server as initiator.
  - It handles admission when several servers connect, and it does time sync.
  - Audio uses a 13-byte header.
  - This is what ESPHome 2026.10.0b2+ and the Linux clients speak.
- **G0** (`legacy.py`): this is `WirePuppet`, written from scratch.
  - It sends an unencrypted text `client/hello` with a MAC `client_id` and `version: 1`.
  - It uses `client/time`/`server/time`, a top-level `state: "synchronized"` and a 9-byte audio header.
  - This is what ESPHome 2026.5 to 2026.10.0b1 and the shipped Voice PE 26.9.x speak (SS18, SS53).

Both endpoints feed the same `FakeSink`. It never decodes; it only accounts for bytes:

- the byte budget, ring overrun and underruns
- decode cost per codec (Opus stutter on a plain ESP32), DAC drift, and late-chunk drops

Both run on the device's own `SkewedClock` (crystal ppm, offset, read jitter).

Transport truth works the same way on both:

- `stream/start` means playing.
- `stream/end` means paused or stopped, as told by `group/update`.
- `stream/clear` means seek or flush.

**Opus note.** aiosendspin's client only decodes PCM and FLAC, and it refuses Opus at
hello and at `stream/start`. Since the sink never decodes, `g2.py` patches
`DECODABLE_CODECS` at runtime in `aiosendspin.client.client` and `.connection` to add
Opus. The patch only applies inside this process.

## Sim actions

Send them as `POST /_sim/action/<name>`, or call the same-named device methods in tests:

- `goodbye` `{reason?}`: sends `client/goodbye` and closes the connection.
  - The reason is one of `restart`, `shutdown`, `another_server` or `user_request`.
  - With `none`, it drops the connection without a goodbye.
- `restart` `{reason?, down_s?}`: sends a goodbye and goes off the network for `down_s`.
  - mDNS is withdrawn too, unless `mdns_on_goodbye=keep`.
  - The device then listens again on the same port with the same identity.
  - It runs in the background and returns at once.
- `drop`: closes the TCP connection without a goodbye.
- `set_volume` `{volume}` and `set_mute` `{muted}`: a local change such as a hardware
  button, reported to the server.

## Profiles

| Profile | Wire | What it models |
|---|---|---|
| `sendspin/esphome_s3_psram` | G2 | ESPHome 2026.10.0b2+ on an S3 with PSRAM. FLAC, PCM and Opus. 621,312-byte budget. No metadata role. |
| `sendspin/esphome_legacy` | G0 | ESPHome 2026.5 to 2026.10.0b1. MAC as client_id (`aabbcc000002`). |
| `sendspin/voice_pe_26_9` | G0 | Voice PE with shipped firmware 26.9.x. FLAC and PCM only (`aabbcc000003`). |
| `sendspin/esp32_plain` | G2 | Plain ESP32 without PSRAM. Small budget. Opus decode falls behind (stutter) and late chunks are dropped. |
| `sendspin/python_cli_pi_zero2w` | G2 | python-cli daemon on a Pi Zero 2 W. Slow decode, 2 ms dead-band corrector. Ctrl-C sends `shutdown`. |
| `sendspin/cpp_cli` | G2 | sendspin-cpp-cli on a Linux desktop. Big buffer, cpp corrector, wrong guess of the host's output latency. |

The MACs are fake (`aa:bb:cc:00:00:0X`), and any example addresses use 192.0.2.x.

The `esphome` profile alias from the brief was left out. It lives outside this
family's paths.

## Knobs

The family has 38 knobs.

| Knob | Default | Quirks | Confidence | What it does |
|---|---|---|---|---|
| `wire_gen` | `'G2'` | SS18, SS53 | high | G2 = client/init + Noise KKpsk2, 13-byte audio header (ESPHome 2026.10.0b2+, aiosendspin 10). G0 = unencrypted text client/hello carrying client_id and version, 9-byte audio header (ESPHome 2026.5 to 2026.10.0b1, shipped Voice PE 26.9.x). |
| `client_id_mode` | `'key'` | SS1, SS3 | high | key: client_id is the Noise public key (G2). mac: lowercase MAC without colons (G0 ESPHome). Switching a device from mac to key reproduces the 2026.10 ID change. |
| `mac_address` | `'aa:bb:cc:00:00:01'` | SS1, SS2 | high | device_info.mac_address (MA #6928 uses it to migrate MAC-ID players). Empty string omits it (python-cli before 8.0). |
| `suite` | `'chachapoly'` | - | high | Noise cipher suite offered in client/init. |
| `unpaired_access` | `True` | SS14 | high | Advertise and accept Sentinel (unpaired, operator-approved) sessions. |
| `roles` | `'player,controller,metadata'` | - | high | Comma-separated roles (player, controller, metadata). ESP: player,controller. |
| `formats` | `'flac:48000:16:2,pcm:48000:16:2'` | SS20, SS51 | high | Supported formats in preference order, codec:rate:bits:channels, comma-separated. |
| `buffer_capacity` | `621312` | SS21, SS35 | high | Advertised byte budget. Each chunk is charged header + payload until it has played. ESP: 621,312 (2/3 of the 1,000,000-byte ring, capped). Legacy ESPHome: 800,000. |
| `required_lead_time_ms` | `150.0` | SS22, SS33 | high | client/state required_lead_time_ms (ESP 150, aiosendspin default 250). |
| `min_buffer_ms` | `500.0` | SS22 | high | client/state min_buffer_ms (ESP 500, aiosendspin default 250). |
| `output_delay_ms` | `0.0` | - | high | Initial output_delay_ms (static_delay_ms on G0). |
| `supported_commands` | `'volume,mute'` | SS57 | high | Player commands declared in client/state (G2) or the hello (G0). Add set_output_delay for adjustable delay. |
| `initial_volume` | `50` | SS44 | medium | Volume reported at connect (persisted across restarts). |
| `volume_unmutes` | `False` | SS44 | medium | A volume command also clears mute (ESPHome before 2026.9.0). |
| `volume_steps` | `0` | SS24, SS44 | medium | Hardware volume resolution: the device snaps a commanded volume to the nearest of this many steps and echoes the snapped value. 0 = exact. |
| `state_echo` | `'immediate'` | SS24 | high | How client/state follows a server/command. delayed waits state_echo_delay_ms (ESP: one loop tick). missing never echoes (noncompliant). |
| `state_echo_delay_ms` | `0.0` | SS24, SS9 | medium | Delay of the client/state echo when state_echo=delayed. |
| `available_mode` | `'until_converged'` | SS36 | high | until_converged: available flips true once the time filter converges (G2 spec; G0 reports state 'synchronized' at once). never: the player never reports available. |
| `legacy_state_field` | `True` | SS36, SS49 | high | G0 only: send top-level state 'synchronized'/'error' in client/state ('error' after an underrun). |
| `clock_offset_ms` | `0.0` | - | high | Offset of the device clock from the host monotonic clock. |
| `clock_skew_ppm` | `20.0` | SS32 | medium | Device crystal error: the clock handed to aiosendspin runs fast by this much. |
| `clock_jitter_us` | `50` | SS10 | medium | Uniform read jitter on every clock read (monotonic is preserved). |
| `dac_skew_ppm` | `30.0` | - | medium | Rate error of the fake DAC against the device clock; the corrector fights it. |
| `corrector` | `'cpp'` | SS19, SS23 | medium | Sync corrector bounding DAC drift: cpp/spec = 1-frame steps with a 100 us dead band, python_cli = 2 ms dead band, none = drift accumulates. |
| `output_latency_error_ms` | `0.0` | SS32 | medium | Host output-latency misestimate added to the sync error (sendspin-cpp-cli). |
| `drop_late_chunks` | `True` | SS11 | high | Drop chunks whose play time is already past (ESP drops them undecoded). |
| `late_chunk_threshold_ms` | `5.0` | SS45 | high | How late a chunk may be before it is dropped (cpp/py 5, go 50, jvm 1000). |
| `ring_overrun_policy` | `'drop_warn'` | SS21 | high | What happens when a chunk does not fit in buffer_capacity. |
| `opus_decode_rt_factor` | `0.05` | SS5, SS31 | medium | Opus decode time as a fraction of audio duration. Above 1.0 the decoder falls behind and chunks miss their play time (plain ESP32 without PSRAM). |
| `decode_rt_factor` | `0.02` | SS5, SS47 | low | Decode time fraction for FLAC/PCM (Pi Zero 2 W python-cli is slower). |
| `start_gate_ms` | `25.0` | SS23 | medium | Audio that must be buffered before output starts (ESP 25 ms priming, python-cli 200 ms). |
| `goodbye_reason` | `'restart'` | SS26, SS27 | high | Reason sent in client/goodbye by the restart/goodbye actions; none = drop abruptly. python-cli Ctrl-C sends shutdown (#6888). |
| `mdns_on_goodbye` | `'withdraw'` | SS26, SS13 | medium | During a restart: withdraw the mDNS record while down and re-add it (ESPHome), or keep it registered so only an identical re-announcement is seen. |
| `restart_down_s` | `2.0` | SS56 | medium | How long the device is off the network during a restart. |
| `advertise_mdns` | `False` | SS41 | high | Register _sendspin._tcp.local. (TXT path=/sendspin, name). Off by default so tests stay off the LAN; needs the zeroconf extra. |
| `reject_origin_header` | `True` | SS25 | high | Refuse a WebSocket upgrade that carries Origin (browser CSRF guard). |
| `fault_mode` | `'none'` | SS28, SS29, SS34 | high | accept_and_hang: upgrade the WebSocket, then never send a hello. blackhole: after blackhole_after_s stop reading and answering but keep the socket open. never_consume: the DAC never plays, so the buffer fills and overruns. |
| `blackhole_after_s` | `5.0` | SS29 | medium | Seconds after connect at which fault_mode=blackhole takes effect. |
| `liveness_timeout_s` | `0.0` | SS55 | medium | Drop the connection with goodbye 'restart' after this much server silence (ESP 60 s). 0 disables. |
| `rx_window_stall_ms` | `0.0` | SS30, SS6 | medium | Wi-Fi starvation: stop reading the socket for this long (once, at rx_window_stall_at_s after connect) while the link stays up, so the server's writes back up. 0 disables. |
| `rx_window_stall_at_s` | `20.0` | SS30 | medium | Seconds after connect at which the rx_window_stall_ms stall starts. |

## Tests

- `tests/families/sendspin/`: 21 in-process tests against aiosendspin's own
  `SendspinServer` (no MA). They cover:
  - the G2 and G0 handshakes and header sizes
  - Noise
  - volume echo modes and the goodbye reasons
  - restart and re-dial, the Origin 403, and accept-and-hang
  - ring overrun, both stall and close
  - Opus stutter and local volume
- `tests/e2e/sendspin_ma_smoke.py`: a real MA server in a subprocess, run with MA's own
  interpreter.
  - It runs on loopback with free high ports (never 8095) and a throwaway data dir.
  - MA's mDNS is switched off, and the device is entered as a manual discovery URL.
  - It is skipped unless `MA_REPO` is set:

```
MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q -s tests/e2e/sendspin_ma_smoke.py
PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/sendspin_ma_smoke.py smoke   # prints RESULT {json}
```

To keep the work dir and `server.log`, set `SEAMER_KEEP=1`. To run a scenario with
another profile, set `SEAMER_PROFILE=sendspin/<name>`.

### Notes on MA's behaviour (dev, cdff463ec)

- MA dials the device and auto-trusts a G2 device that allows unpaired access.
- A device with a MAC is registered as a *protocol* player. About 15 s later MA adds the
  visible universal player (`up…`, linked through `protocol_parent_id`), and the queue
  lives on that player.
- Sendspin has no native pause in MA. Pause stops the stream, so the device ends up
  stopped and MA reads idle. Resume and seek each start a new stream.

### Results on MA dev (cdff463ec)

| Scenario | Result |
|---|---|
| `smoke` (S3 G2) | **pass**. MA discovers and accepts the device, then play, pause, resume, seek to 30 s and volume 33 all work. MA's volume and the device's local volume 61 read back on both sides. |
| `bug6888` (python-cli, goodbye `shutdown`) | **fails, which reproduces #6888.** The `restart` control is re-dialed. After `shutdown`, MA never dials again; aiosendspin logs `Not reconnecting to … (goodbye reason: shutdown)`. |
| `bug6888_user_request` | **fails**, by the same mechanism: only `restart` keeps the reconnect loop alive. |
| `bug6887` (goodbye `another_server`) | **passes on dev**, for S3 G2, esphome_legacy G0 and voice_pe G0 alike. The player stays listed and available, and play reclaims the device (a new session, and chunks play). Dev's held-elsewhere handling and aiosendspin's retention of `ANOTHER_SERVER` clients seem to fix the 2.10.8 symptom. The test stays as a regression guard. |
