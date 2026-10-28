# MPD family (Music Player Daemon, moOde, Volumio, Mopidy-MPD)

Package `seamer.families.mpd`, default profile `mpd_0_23`. The MPD port is the
`mpd_port` knob (0 = a free port; real servers use 6600).

```
seamer run mpd/mpd_0_23
seamer run mpd/moode -k mixer=failing
seamer run mpd/mpd_0_24 -k drop_connection='{"p": 1.0, "which": "command"}'
```

Quirk IDs: `MPn` (MPD), `MPYn` (Mopidy-MPD), `MOn` (moOde) and `VOn` (Volumio) are the
headings in simresearch/more_families.md. `pympd#n` is a python-mpd2 issue.

## What the device serves

**The MPD text protocol over TCP.** It follows MPD's `client/ClientProcess.cxx` and
`command/AllCommands.cxx`:

- **Greeting.** `OK MPD <version>\n`. With `max_connections` reached, the connection is
  accepted and then closed with no greeting.
- **Line rules.**
  - A line that does not start with a lower-case letter closes the connection.
  - Quoting and backslash escapes follow MPD's tokenizer. Tokenizer errors
    (`Missing closing '"'`, `Space expected after closing '"'`,
    `Invalid unquoted character`) come back as `ACK [2@0] {} ...`.
- **Idle.**
  - `idle [subsystems...]` returns at once if a matching flag is already pending.
    Otherwise it waits for `changed: <subsystem>` lines and then `OK`.
  - A notify clears every pending flag, as `Client::IdleNotify` does.
  - An unknown name gives `ACK [2@0] {idle} Unrecognized idle event: x`.
  - `noidle` while idling answers `OK`. A stray `noidle` is ignored (MP2).
  - Any other command sent while idling closes the connection.
- **Command lists.**
  - `command_list_begin` / `command_list_ok_begin` ... `command_list_end` are
    supported, with `list_OK` after each command.
  - The first error stops the list with `ACK [code@index] {cmd} msg`.
- **ACKs.**
  - Format: `ACK [error@index] {command} message`.
  - Codes: 1 not_list, 2 arg, 3 password, 4 permission, 5 unknown, 50 no_exist,
    51 playlist_max, 52 system, 53 playlist_load, 54 update_already, 55 player_sync,
    56 exist.
  - The messages are MPD's own: `wrong number of arguments for "x"`,
    `too few/many arguments`, `Integer expected: x`, `Number too large: 101`,
    `Number is negative`, `Boolean (0/1) expected`, `Float expected`, `Bad song index`,
    `No such song`, `Not playing`, `Unsupported URI scheme`, `incorrect password`,
    `you don't have permission for "x"`, and `unknown command "x"` (with `{}`).
  - Permissions are checked before argument counts.
- **Commands.**
  - Connection: ping, close, password, commands, notcommands, tagtypes, urlhandlers,
    decoders, binarylimit.
  - Status: status, currentsong, stats, idle/noidle, clearerror.
  - Playback: play, playid, pause, stop, next, previous, seek, seekid, seekcur
    (absolute and +/-).
  - Volume: setvol, volume, getvol (0.23 and later).
  - Options: repeat, random, single, consume (with `oneshot` from 0.24), crossfade,
    replay_gain_mode.
  - Queue: add (with a position from 0.23.3), addid, clear, delete, deleteid,
    playlist, playlistinfo, playlistid, plchanges.
  - Outputs: outputs, enableoutput, disableoutput, toggleoutput.
  - Art: albumart, readpicture.
  - Commands are gated by `version`. Status and song fields follow
    `status_dialect`: in the MPD dialect, `partition` appears from 0.22 and `Added:`
    from 0.24.
- **Sink.**
  - `add` + `play` makes the sink GET the song's HTTP URL with MPD's `User-Agent`, so
    a Music Assistant flow stream is really fetched and decoded.
  - The duration is estimated from Content-Length and the codec bitrate.
  - Every seek, resume or next opens a new GET (MP7).
  - The `http_request` and `http_response` events record the headers.
- **`/_sim` HTTP API** on the device's base URL, plus these sim actions:
  - `external` `{"cmd": ...}` or `{"cmds": [...]}`: run commands as another client,
    such as a phone app. The raw reply text is returned and idle clients are woken.
  - `drop` `{"which": "command"|"idle"|"all"}`: close connections by role. The role
    is learned from use: python-mpd2's keepalive `idle database` marks a command
    connection, and a bare `idle` marks the idle one.
  - `renderer` `{"renderer": "airplay"|"upnp"|...|null}`: on moOde or Volumio, another
    renderer takes over the ALSA device.
  - `connections`: list the open connections.

## Profiles

| Profile | Version | Notes |
|---|---|---|
| `mpd_0_23` | 0.23.5 | Stock Debian 12 MPD; software mixer. |
| `mpd_0_24` | 0.24.0 | `consume`/`single oneshot`, `Added:` tag. |
| `moode` | 0.23.5 | No mixer (`volume: -1`, setvol fails with ACK 52); `ownqueue_clears_mpd`; 20 connections. |
| `volumio` | 0.21.25 | Starts in consume mode; software mixer. |
| `mopidy` | 0.19.0 | Mopidy status dialect (xfade, no partition/audio/duration); `volume: -1`; getvol/readpicture/albumart/binarylimit unknown; add fetches the URL; GStreamer UA. |

All of them share `_base.toml`, which sets the identity, an initial volume of 50 and a
20 s read-ahead buffer (MP7).

## Knobs

There are 26 family knobs, plus the core ones (latency_s, jitter_s, hang, offline and
others).

| Knob | Type | Default | Quirks | Conf. | What it does |
|---|---|---|---|---|---|
| `mpd_port` | int | 0 | - | high | TCP port of the MPD line protocol (real servers use 6600; 0 = a free port). |
| `version` | str | `0.23.5` | MP11 | medium | Protocol version in the `OK MPD x.y.z` greeting; gates version-specific commands and fields (getvol >= 0.23, consume oneshot / Added: / lastloadedplaylist >= 0.24). |
| `status_dialect` | enum | 'mpd' | MP11, MPY2 | medium | Shape of `status`/`currentsong`: real MPD, or Mopidy-MPD (xfade always, no partition/mixrampdb/audio/duration; streams report `Time: 0`). |
| `max_connections` | int | 100 | MP3 | medium | Concurrent client limit. One more connection is accepted and closed with no greeting (python-mpd2 then waits 5 s for the hello). |
| `greeting_delay_s` | float | 0.0 | MP3 | high | Delay before the `OK MPD` greeting (> 5 s trips python-mpd2's hello timeout). |
| `password` | str | "" | MP4 | high | Server password (`password` command). Empty = no password configured. |
| `default_permissions` | str | "" | MP4 | high | Comma list of read,add,control,admin for an unauthenticated client. Empty = MPD's default: everything without a password, nothing once a password is set. |
| `connection_timeout_s` | float | 60.0 | MP12 | medium | MPD `connection_timeout`: a client that sends nothing for this long is dropped. Clients waiting in `idle` are exempt unless timeout_idle_too. 0 = never. |
| `timeout_idle_too` | bool | False | MP12 | low | Apply connection_timeout_s to idling clients too (a NAT or proxy that drops quiet sockets). |
| `drop_connection` | prob | {'p': 0.0} | MP12 | medium | On each line received, close the socket instead of answering. Param `which`: command (python-mpd2 keepalive `idle database`), idle (bare `idle`), or all. |
| `stray_noidle` | enum | 'ignore' | MP2 | high | Reply to `noidle` from a client that is not idling. Real MPD ignores it; `ok`/`ack` are bogus replies that desync clients racing idle/noidle (pympd#173, #195). |
| `unsupported_commands` | str | "" | MP11 | medium | Comma list of commands answered with `unknown command` (Mopidy: getvol, readpicture, albumart, binarylimit). |
| `mixer` | enum | 'software' | MP5, MO2, VO8 | high | Output mixer. none: no volume in status and setvol ACKs; failing: volume is reported but setvol ACKs (a hardware mixer that went away). |
| `volume_unavailable` | enum | 'omit' | MP5, MO2 | high | How status shows a missing mixer: no volume line (MPD >= 0.21) or `volume: -1` (older MPD, Mopidy, moOde fixed/CamillaDSP volume). |
| `setvol_error` | str | `problems setting volume` | MP5 | low | Message of the `ACK [52@0] {setvol}` reply when there is no working mixer. |
| `pause_in_stop_noop` | bool | True | MP8 | medium | `pause 0`/`pause 1` while stopped answers OK and does nothing (real MPD). False makes `pause 0` start the current song (not real MPD; to show a client fix). |
| `not_seekable_without_length` | bool | True | MP10 | high | seek/seekcur on an HTTP stream sent without Content-Length fails with `ACK [5@0] {} Not seekable` (pympd#166). |
| `report_duration` | enum | 'content_length' | MP6 | high | Duration of an HTTP stream: estimated from Content-Length (MA's forced length profile gives a bogus 12 h) or never reported (endless stream). |
| `codec_probe_fail` | str | `flac` | MP6 | high | Comma list of codecs MPD cannot probe on an endless HTTP stream (no Content-Length): the song fails with `error: Failed to decode`. |
| `icy_metadata_header` | bool | False | MP6 | medium | Send `Icy-MetaData: 1` like MPD's curl input. Off by default: the sim does not strip ICY blocks, so only enable it while MA's ICY setting is disabled (its default). |
| `art_probe_get` | bool | True | MP9 | high | readpicture/albumart on an http song make the server GET the stream URL again (readpicture) or `<dir>/cover.png` (albumart). |
| `add_probe_get` | bool | False | MPY1 | medium | `add` of an http URI fetches it once to scan it (Mopidy-Stream's lookup) before playback opens it again. |
| `initial_consume` | bool | False | VO6 | low | Start with consume mode on (Volumio's statemachine runs MPD in consume mode): a finished song leaves the queue. |
| `output_busy` | bool | False | MO1, VO7 | medium | Another renderer holds the audio device: play answers OK, then the song fails with an `error:` line and the player stays stopped. Set by the `renderer` action. |
| `output_error_text` | str | ALSA busy text | MO1 | low | The `error:` text while output_busy. |
| `ownqueue_clears_mpd` | bool | False | MO5 | medium | A UPnP renderer takeover (`renderer` action, renderer=upnp) also clears MPD's queue (upmpdcli ownqueue=1). |

## Music Assistant e2e

`tests/e2e/mpd_ma_smoke.py` is skipped unless `MA_REPO` is set. It runs MA's own server
in a subprocess, using MA's Python 3.14 venv with python-mpd2 3.1.1:

- The server uses a throwaway data dir and free 127.0.0.1 ports.
- Sendspin is moved off 8927 and mDNS/discovery are stubbed out.
- The mpd provider is configured with `manual_discovery_ip_addresses = ["127.0.0.1:<port>"]`.

Against MA dev (2026-10-28), the smoke scenario passes:

- MA registers `mpd_127.0.0.1_<port>` (model `MPD 0.23.5`) and opens two connections,
  which the sim sees as roles `command` and `idle`.
- **Play.** MA sends `clear`, `add <flow URL>`, `play`. The sink fetched and decoded
  MA's mp3 flow stream, with UA `Music Player Daemon 0.23.5`.
- **Pause and resume** work.
- **Seek.** MA restarts the flow at 30 s, which opens a second stream; MA then reports
  33 s elapsed.
- **Volume.** setvol 33 works.
- **State read-back.** An external `setvol 55` while playing reaches MA through the
  poll. A stop is read back as idle.

```
MA_REPO=/path/to/music-assistant-server .venv/bin/pytest -q tests/e2e/mpd_ma_smoke.py
PYTHONPATH=$MA_REPO:. $MA_REPO/.venv/bin/python tests/e2e/mpd_ma_smoke.py mp1   # one scenario, JSON out
```

## Bug repros

Each of these tests asserts the correct behaviour, so it fails on MA dev today.

| Test | Quirk | Result on MA dev |
|---|---|---|
| `test_mp1_idle_event_refreshes_ma_state` | MP1 (confirmed in MA code) | The sim pushes `changed: player` to MA's idle socket at once. Because python-mpd2 yields `['player']` and MA tests `subsystem in ("player", ...)`, the push is ignored. An external pause is seen only after 4.8 s, through the 5 s poll. An external resume or `setvol 21` while MA thinks it is paused is never seen, because the poll only runs while PLAYING. |
| `test_mp12_command_socket_drop_recovers` | MP12 (pympd#217, #220) | Close only MA's command socket. MA logs `Failed to fetch MPD status` on every poll. `volume_set` raises `PlayerCommandFailed: Can not send command to disconnected client`. MA never reconnects (only an idle-loop failure triggers that), and the player stays "available". |
| `test_mp8_play_after_external_stop_plays` | MP8 | Pause, then another client sends `stop`. MA's play then sends `pause 0`, which is a no-op on a stopped MPD (the `pause_in_stop_noop` knob fires). MA reports `playing`, but no stream is opened and the device stays stopped. |

Of these, only MP12's research cites issues (pympd#217, #220). MP1 and MP8 are confirmed
by reading `providers/mpd/player.py` and python-mpd2's `asyncio.py`, and MP1 is named
in the task as confirmed in code. The issue-cited protocol quirks MP2 (pympd#173, #195)
and MP10 (pympd#166) are covered by unit tests instead of MA e2e: MA dev never sends a
stray `noidle` and never uses `seekcur`.

## Fidelity notes

- Mopidy's unknown-command ACK may name the command (`{getvol}`). The sim uses `{}`, as
  MPD does.
- A pause makes MPD emit one `player` idle event. The sim can emit two, one from the
  transport listener and one explicit. Clients only see a single wake-up per `idle`
  call, so this rarely shows.
- `elapsed` restarts at 0 for each new GET, as it does for real MPD on a new URL. MA's
  flow mode makes the whole queue one long stream.
