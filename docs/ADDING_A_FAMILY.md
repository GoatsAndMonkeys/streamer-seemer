# Adding a device family

This is the contract between `seamer.simkit` (shared core) and a family package such as
`linkplay` or `bluos`. Families are developed in parallel, so **a family touches only
its own directories**:

```
seamer/families/<name>/        your package (auto-discovered)
tests/families/<name>/         your tests
```

Do not edit `seamer/simkit/*`, `seamer/families/__init__.py`, `seamer/cli.py` or
`pyproject.toml` in a family change. If the core is missing something, write it up as a
separate core change. Small, generic additions (a new core knob, a kit helper) are
welcome there.

`seamer/families/example/` is the reference implementation. Copy it.

## 1. Package layout

```
seamer/families/<name>/
  __init__.py      FAMILY = Family(...)          (required)
  knobs.py         knob(...) declarations         (imported by device.py)
  device.py        class <Name>Device(SimDevice)
  lens.py          optional ReportLens subclass
  protocol/...     optional: XML/JSON builders, parsers, fixtures captured from hardware
  profiles/
    _base.toml     shared defaults ("_" files are not listed)
    <model>.toml   one per model/firmware you simulate
```

`__init__.py`:

```python
from pathlib import Path
from .. import Family
from .device import LinkplayDevice

FAMILY = Family(
    name="linkplay",                   # registry + CLI name; must equal device_cls.family
    device_cls=LinkplayDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="WiiM / LinkPlay (httpapi.asp + UPnP)",
    default_profile="wiim_pro",        # used when no --profile is given
    default_port=0,                    # suggested port for `seamer run` (0 = free port)
)
```

Discovery: `seamer.families.discover()` imports every subpackage that defines `FAMILY`.
Out-of-tree packages can register through the `seamer.families` entry-point group
(value: the module that defines `FAMILY`). An import error does not break other
families. It shows up in `seamer families` and in `seamer.families.errors()`.

## 2. Knobs

Declare every quirk as a knob in `knobs.py`, at import time:

```python
from ...simkit.knobs import knob
FAMILY = "linkplay"

knob("status_position_zero_on_stream", family=FAMILY, type="bool", default=False,
     quirks=("LP-07",), confidence="high",
     doc="getPlayerStatusEx reports curpos=0 for HTTP streams.")
knob("ignore_seek", family=FAMILY, type="prob", default=False, quirks=("LP-12",),
     confidence="low", doc="Seek is acknowledged but not applied.")
```

- **type**: `bool`, `int`, `float`, `str`, `enum` (needs `choices`), `prob`, `dict`.
  A `prob` knob accepts `false`, `true`, a probability `0.3`, or a table `{p=0.3, ...}`.
  Extra keys are parameters, read with `knobs.param(name, "window_s", default)`.
- **quirks**: the IDs from the plan / quirk catalogue. They are recorded whenever the knob fires.
- **confidence**: `high` | `medium` | `low`, meaning how sure we are that real hardware
  does this. Scenario results flag passes or failures that depended on low-confidence
  knobs.
- Names are per family. You **may not** reuse a core knob name. Run
  `seamer knobs example` to see the core set:
  - transport: `load_delay_s`, `transition_gap_s`, `auto_advance`, `set_uri_stops_playback`
  - sink: `buffer_capacity_s`, `prebuffer_s`, `read_mode`, `on_server_close`,
    `pause_closes_socket`, `byte_cap`, `time_cap_s`, `probe`, `require_range`,
    `reject_chunked`, `assumed_kbps`, `decoder`
  - HTTP faults (applied by the middleware): `latency_s`, `jitter_s`, `http_error`,
    `hang`, `offline`
  - report lens: `report_position_zero`, `report_duration_zero`,
    `report_position_refresh_s`, `report_stale_after_load`

  Use these where they fit rather than re-implementing them.

Using knobs in device code (`self.knobs` is a `KnobSet`):

| Call | Use for |
|---|---|
| `knobs.get(name)` / `knobs[name]` | plain values (no randomness, not recorded) |
| `knobs.fires(name, **ctx)` | "does the quirk happen this time?" Draws from the knob's own seeded stream for `prob` knobs. Records `knob/fired` with quirk IDs when it happens. **Use this at the decision point for every quirk**, so timelines show which quirks shaped a run. |
| `knobs.enabled(name)` | is the quirk on at all (no draw) |
| `knobs.param(name, key, default)` | parameters of `prob`/`dict` knobs |
| `knobs.set_live(name, value)` | change mid-run (also exposed as `POST /_sim/knobs`) |

## 3. Profiles

TOML, in the family's `profiles/` directory:

```toml
description = "WiiM Pro, firmware 4.8.x"
extends = ["_base"]                 # merged in order, this file wins

[identity]                          # free-form; exposed as device.identity
name = "WiiM Pro"
model = "WiiM_Pro_with_gc4a"
firmware = "4.8.618402"
uuid = "uuid:00000000-0000-0000-0000-000000000001"   # never real serials/MACs/IPs

[surfaces]
list = ["httpapi", "upnp"]          # protocol surfaces this model exposes (family-defined)

[knobs]
status_position_zero_on_stream = true
buffer_capacity_s = 12
ignore_seek = { p = 0.2 }
```

- Unknown knob names in a device profile are errors (typos fail fast). In a fault bundle
  they are ignored, so one bundle can serve every family.
- Specs stack: `wiim_pro+faults/slow_commands+faults/events_dead`. Per-device overrides
  (`scene.device("linkplay/wiim_pro", latency_s=0.5)`, `--knob`) apply last.
- Fault bundles are shared and live in `seamer/simkit/profiles/faults/`. A family-only
  fault can live in your `profiles/` as e.g. `fault_<x>.toml` and be stacked by name.

## 4. The device class

```python
from aiohttp import web
from ...simkit.device import SimDevice
from ...simkit.transport import ReportLens, TState
from . import knobs as _knobs  # noqa: F401  (registers knobs)

class LinkplayLens(ReportLens):
    def status_ex(self) -> dict: ...         # truth -> protocol vocabulary, via self.position(),
                                             # self.duration(), self.state(), self.metadata()

class LinkplayDevice(SimDevice):
    family = "linkplay"
    lens_cls = LinkplayLens
    use_sink = True                          # False if the device never fetches audio

    def build_app(self) -> web.Application:  # REQUIRED: the main protocol port
        app = self.make_app()                # includes recording + HTTP fault middleware
        app.router.add_get("/httpapi.asp", self._httpapi)
        return app

    async def on_start(self) -> None:        # optional: extra ports, SSDP, mDNS
        self.upnp = await self.serve(self._upnp_app(), port=0)

    async def on_stop(self) -> None: ...     # optional

    def status(self) -> dict: ...            # optional: extra data for GET /_sim/state

    async def sim_action(self, name, payload):  # optional: POST /_sim/action/<name>
        if name == "press_preset":
            ...
            return "ok"
        raise KeyError(name)                 # -> 404
```

What the base class gives you (all set up in `SimDevice.__init__`, before `build_app`):

| Attribute | What |
|---|---|
| `self.id`, `self.profile`, `self.identity` | device id, loaded `Profile`, identity table (mutable copy) |
| `self.clock` | `Clock`. **Always** use `self.clock.sleep/monotonic/call_later`, never `asyncio.sleep` or `time.*` directly, so that virtual-time tests work |
| `self.rec` | `Emitter` bound to `dev:<id>`. Use `self.rec.child("upnp")` for sub-sources |
| `self.rng` | `SeededRng`. Use `self.rng.stream("name")` for non-knob randomness |
| `self.knobs` | `KnobSet` (core + family knobs) |
| `self.transport` | `TransportMachine`, the ground truth. Protocol handlers call it |
| `self.sink` | `HttpSink` or `None`. Owned by the transport; you rarely touch it |
| `self.lens` | instance of `lens_cls`. **Report through it, never from the transport directly** |
| `self.base_url`, `self.host`, `self.port` | of the main server, after `start()` |
| `self.make_app()`, `self.serve(app, port, ssl_context=...)` | protocol apps, extra listeners (stopped automatically) |

The `/_sim/*` routes are added to the main app for you. Don't add your own routes under `/_sim`.

### Truth vs report

`TransportMachine` is what actually happens:

- state `TState`: `NO_MEDIA`, `STOPPED`, `LOADING`, `BUFFERING`, `PLAYING`, `PAUSED`
- `current` / `next_slot` (`MediaSlot`: uri, meta, duration_s, title, extra, owner), and `owner`
  (`"ma"` or e.g. `"hdmi"` after `take_over`)
- `truth_position()`

Commands:

```python
await tm.set_uri(uri, meta="", duration_s=None, title=..., **extra)
await tm.set_next(uri_or_None, ...)
await tm.play()
await tm.pause()
await tm.stop()
await tm.seek(s)
await tm.next()                       # -> bool
tm.take_over(owner, emit_event=..., title=...)
```

With a sink, playback really fetches `uri`. LOADING/BUFFERING/PLAYING follow the sink,
and the end of a track comes from the stream ending and the buffer draining. Without a
sink (or with a foreign owner) the end of a track is timed by `duration_s`.
`auto_advance` / `transition_gap_s` handle gapless next.

Subscribe with `tm.add_listener(cb(name, data))`. The events are:

- `state` (data: old, new)
- `track_started`
- `track_ended`
- `advanced`
- `error`

Use them to drive the protocol's eventing (UPnP NOTIFY, long-poll bumps). Gate them with
your knobs, for example "events dead".

The lens converts truth into what a protocol surface reports, and records a `report`
event whenever the two differ. Put protocol-specific lies in your lens subclass (or the
handler) as `if self.knobs.fires("..."):` branches. Several surfaces (httpapi vs UPnP)
can each get their own lens: `LinkplayLens(self.transport, self.knobs, self.rng,
surface="upnp")`.

### Kits

- `kits.http.LongPoll(clock, name, rec, min_interval_s=0)` implements etag/version
  long-polling:
  - `etag`, `bump()`, and `await wait(etag, timeout, client=...) -> bool`.
  - Clients polling faster than `min_interval_s` are recorded as
    `longpoll_rate_violation`. Suited to BluOS `?etag=&timeout=`.
- `kits.http.text/xml/json_response`, plus `HttpServer` if you need one outside the device.
- `kits.discovery`:
  - `SsdpResponder([SsdpTarget(location, usn, [st...])])` answers M-SEARCH by unicast.
    It binds to loopback by default; pass a port to share.
  - `ssdp_response/ssdp_notify/parse_ssdp`.
  - `MdnsAdvertiser` (zeroconf, optional extra) for `_musc._tcp` and similar.
- `media.MediaServer`/`MediaFactory` supply test streams. Track `n` is a sine at
  `300+100n` Hz, so the sink identifies what was audible.

## 5. Recording conventions

The command middleware already records every request as `cmd` and every reply as `resp`
(the source is `dev:<id>`). Inside the family:

| Emitter call | When |
|---|---|
| `rec.event(name, **data)` | outbound protocol events: UPnP NOTIFY sent, long-poll woken, SSDP answered |
| `rec.state(...)` | the transport does this itself; families rarely need it |
| `rec.report(...)` | the lens does this itself |
| `rec.log(name, **data)` | diagnostics |
| `rec.cmd(name, **data)` | non-HTTP inbound commands (e.g. a TCP or UPnP SOAP action you parse yourself) |

Data must be JSON-serialisable. Never record real IPs, tokens or home-network names.
Use `127.0.0.1` and invented identities.

## 6. Tests

Put them in `tests/families/<name>/`. The pytest plugin provides:

- `scene`: an async `Scene` with real clock, recorder, media and servers, torn down for you.
  Use `await scene.device("linkplay/wiim_pro", **knobs)`, `await scene.media_server()`,
  `scene.media.track(n, seconds)`, `scene.media_url(path, route="stream", rate=1)`,
  `await scene.wait_for(pred, timeout, what)`, `scene.expect.*`, and `with scene.step("x"):`.
- `make_device`: shorthand for `scene.device`.
- `virtual_clock`, `recorder`, `media`, `sim_seed`.

`Expect` methods:

- `eventually(**match)`
- `never(**match)`
- `count(n, **match)`
- `order(m1, m2, ...)`
- `max_jump(series, max_s)`
- `audible_coverage(track, at_least_s)`
- `played_close_to(...)`

Match keys:

- `kind=`, `name=` and `src=` (a trailing `*` matches a prefix)
- `data__key=` for fields in the event data
- callables as predicates

On failure the error message includes the timeline context.

Minimum expected coverage per family:

1. Each profile loads (`FAMILY.load_profile(p)` for `p in FAMILY.profiles()`).
2. The device starts on port 0 and answers its main status command in the right format.
   Golden fixtures captured from real hardware are ideal (scrub identifiers first).
3. Play a `scene.media_url(...)` through the protocol, reach PLAYING, then STOPPED at
   EOF, with `audible_coverage` passing.
4. One test per quirk knob, showing that it changes the protocol output when on.

Use virtual time (a `VirtualClock` passed as `Scene(clock=...)`, or as `clock=` to
`build_device`) for long protocol timers such as long-poll timeouts and UPnP
subscription expiry. Use the real clock for anything that moves bytes through the sink.

## 7. Checklist

- [ ] `seamer families` lists the family with no import error.
- [ ] `seamer knobs <name> --profile <each>` works.
- [ ] `seamer run <name> --port 0` serves the protocol and `/_sim/state`.
- [ ] Every quirk is a knob with quirk IDs and an honest confidence level, and is checked via `fires()`.
- [ ] No timing via `asyncio.sleep`/`time.time` (use `self.clock`).
- [ ] No real network details in profiles, fixtures or tests.
- [ ] `ruff check . && ruff format --check . && pytest -q` pass.

For which quirks to model per family, see the knob tables for LinkPlay and BluOS in the
design plan (PLAN.md). Each row there becomes one `knob(...)` with its quirk ID.
