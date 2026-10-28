# Streamer Seamer

It *seems* like a streamer, but it's a simulation.

Streamer Seamer simulates network audio players (WiiM/LinkPlay, BluOS, ...) well enough
to test [Music Assistant](https://music-assistant.io) and similar controllers against
them. That includes the parts that usually break: buffering, stalls, stale position
reports, events that never arrive.

- A simulated device answers its family's real protocol, so the controller's provider
  talks to it as it would to hardware.
- It really fetches the stream URL at real-time rate through a buffer model, so
  "what would have been heard" is measured, not guessed.
- Every quirk is a typed **knob** with a quirk ID and a confidence level. Profiles (TOML)
  set the knobs. Random quirks are seeded, so a failing run replays.
- Every command, reply, state change, report and sink event goes into one JSONL
  **timeline** per run. Assertions run against it.

## Layout

```
seamer/
  simkit/          shared core (no family-specific code)
    clock.py       RealClock / VirtualClock (+ wait_with_timeout, patch_time)
    rng.py         SeededRng: one stream per (seed, device, knob)
    recorder.py    Recorder / Emitter / Timeline (JSONL)
    expect.py      Expect assertions over a timeline, wait_for
    knobs.py       knob registry, Profile loading (extends, faults, overrides), KnobSet
    core_knobs.py  knobs every device has (transport, sink, http faults, report lens)
    media.py       tone test tracks + MediaServer (paced /stream, Range-capable /media)
    sink/          HttpSink (real HTTP pull), BufferModel, decoders (PCM/WAV, ffmpeg, bitrate)
    transport.py   TransportMachine (truth) + ReportLens (what a surface reports)
    kits/http.py   HttpServer, command middleware (record + faults), LongPoll (etag)
    kits/discovery.py  minimal SSDP responder, optional mDNS (zeroconf)
    device.py      SimDevice base class + /_sim control API
    scenario.py    Scene, @scenario, run_scenario
    pytest_plugin.py   fixtures (auto-loaded)
    profiles/faults/   fault bundles that stack onto any profile
  families/        one package per device family (auto-discovered)
    example/       tiny reference family; copy it to start a new one
  cli.py           `seamer` command
docs/ADDING_A_FAMILY.md   the interface family packages implement
```

## Quickstart

```sh
uv venv
uv pip install -e '.[dev]'          # add ,mdns for zeroconf advertising
.venv/bin/pytest -q
```

ffmpeg is optional. It is used for non-PCM streams and FLAC/MP3 test media, and is found
via `$FFMPEG` or `PATH`.

## Running a simulated device

```sh
seamer families                                  # what's installed
seamer profiles example                          # profiles + fault bundles
seamer knobs example --profile quirky -v         # effective knob values (* = non-default)
seamer run example --profile basic --port 0      # 0 = any free port
seamer run example --profile quirky+faults/slow_commands \
      --knob latency_s=0.2 --knob 'ignore_pause={p=0.3}' --seed 7 --timeline run.jsonl
seamer media ./media --tracks 3 --seconds 10     # tone tracks (track n = 300+100n Hz)
seamer timeline run.jsonl --kind cmd             # filter a recorded timeline
```

Profile specs are `name[+fault+...]`. Faults come from `seamer/simkit/profiles/faults/`
(`faults/events_dead`, `faults/slow_commands`, `faults/flaky_network`,
`faults/strict_http`, `faults/drop_on_close`). Extra profile directories can be added
with `SEAMER_PROFILE_PATH`. `--knob` values are parsed as TOML.

### Control API (`/_sim`)

Every device also serves a test-only API on its main port:

| Request | Effect |
|---|---|
| `GET /_sim/state` | identity, profile, transport truth (incl. sink snapshot), family status |
| `GET /_sim/knobs` | all knobs with value, default, quirk IDs, confidence, doc |
| `POST /_sim/knobs` `{"name": value}` | change knobs live (400 on bad values) |
| `GET /_sim/events?since=N[&all=1]` | timeline events from sequence N |
| `POST /_sim/action/take_over` `{"owner": "hdmi", "emit_event": false}` | another source takes over the device |
| `POST /_sim/action/<name>` | family-specific actions (404 if unknown) |

## Testing with it

The pytest plugin loads automatically. It adds `--sim-seed`, `--sim-keep DIR` and
`--sim-echo`, and these fixtures: `scene`, `make_device`, `media`, `virtual_clock`,
`recorder`, `sim_seed`.

```python
async def test_plays_out(scene):
    await scene.media_server()
    track = scene.media.track(1, seconds=2)
    dev = await scene.device("example/basic", on_server_close="drain")
    await dev.transport.set_uri(scene.media_url(track.path), duration_s=2)
    await dev.transport.play()
    await scene.wait_for(lambda: dev.transport.state.name == "STOPPED", 10, "ended")
    scene.expect.audible_coverage(track=1, at_least_s=1.9)
```

Named scenarios use `@scenario(...)` and `run_scenario(name, out_dir, seed=...)`. The
result has pass/fail, the timeline path and the list of knobs that fired. Results that
relied on low-confidence knobs are flagged.

## Adding a device family

See [docs/ADDING_A_FAMILY.md](docs/ADDING_A_FAMILY.md). In short: add a package
`seamer/families/<name>/` that defines `FAMILY = Family(...)`, a `SimDevice` subclass,
its knobs and TOML profiles. Discovery is automatic, so no shared file needs editing.

## License

Apache-2.0
