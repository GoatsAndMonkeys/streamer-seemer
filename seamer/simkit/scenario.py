"""Scenario DSL and runner.

A scenario is an ``async def fn(s: Scene, **params)`` registered with
:func:`scenario`. The :class:`Scene` gives it a clock, a recorder (one timeline per
run), test media and a media server, device factories, waits and assertions::

    @scenario("drain_plays_out", families=["example"], quirks=["bss:B5"])
    async def drain_plays_out(s: Scene, profile: str = "example/basic") -> None:
        srv = await s.media_server()
        track = s.media.track(1, seconds=2)
        dev = await s.device(profile)
        ...
        s.expect.audible_coverage(track=1, at_least_s=1.8)

Run with :func:`run_scenario` (returns a :class:`ScenarioResult`) or from pytest via
the ``scene`` fixture. Driving Music Assistant itself (``Scene.ma``) is plugged in
by an MA harness; this module does not import MA.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clock import Clock, RealClock
from .device import SimDevice
from .expect import Expect, ExpectationError, wait_for
from .kits.http import HttpServer
from .media import MediaFactory, MediaServer
from .recorder import Recorder

ScenarioFn = Callable[..., Awaitable[None]]


class Scene:
    """Per-run context handed to scenarios."""

    def __init__(
        self,
        out_dir: str | Path,
        *,
        seed: int = 0,
        clock: Clock | None = None,
        echo: bool = False,
        name: str = "scene",
        media_dir: str | Path | None = None,
    ) -> None:
        self.name = name
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.clock: Clock = clock or RealClock()
        self.timeline_path = self.out_dir / "timeline.jsonl"
        self.rec = Recorder(self.timeline_path, self.clock, echo=echo)
        self.log = self.rec.bind("scene")
        self.media = MediaFactory(Path(media_dir) if media_dir else self.out_dir / "media")
        self.expect = Expect(self.rec)
        self.devices: list[SimDevice] = []
        self.ma: Any = None  # set by an MA harness
        self._servers: list[HttpServer] = []
        self._media_server: tuple[MediaServer, HttpServer] | None = None

    # -- devices & media -----------------------------------------------------------

    async def device(
        self,
        profile: str,
        *,
        device_id: str | None = None,
        port: int = 0,
        start: bool = True,
        **knobs: Any,
    ) -> SimDevice:
        """Create and start a device. ``profile`` is ``"<family>/<spec>"`` or ``"<family>"``.

        Keyword arguments are knob overrides for this device only.
        """
        from ..families import build_device

        family, _, spec = profile.partition("/")
        dev = build_device(
            family,
            spec or None,
            recorder=self.rec,
            clock=self.clock,
            seed=self.seed,
            device_id=device_id or f"{family}{len(self.devices) + 1}",
            port=port,
            overrides=knobs or None,
        )
        self.devices.append(dev)
        if start:
            await dev.start()
        return dev

    async def media_server(self) -> MediaServer:
        """Start (once) a server for ``self.media``; returns it with ``base_url`` set."""
        if self._media_server is None:
            ms = MediaServer(self.media.root)
            srv = HttpServer(ms.app())
            await srv.start()
            ms.base_url = srv.base_url  # type: ignore[attr-defined]
            self._servers.append(srv)
            self._media_server = (ms, srv)
            self.log.log("media_server", url=srv.base_url)
        return self._media_server[0]

    def media_url(self, path: Path, route: str = "media", **query: Any) -> str:
        assert self._media_server is not None, "call await scene.media_server() first"
        base = self._media_server[1].base_url
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        return f"{base}/{route}/{path.name}" + (f"?{qs}" if qs else "")

    # -- flow ------------------------------------------------------------------------

    def now(self) -> float:
        return self.rec.now()

    async def wait_for(
        self, pred: Callable[[], bool], timeout: float, what: str = "condition", poll: float = 0.02
    ) -> None:
        try:
            await wait_for(pred, timeout, self.clock, poll, what)
        except ExpectationError as err:
            raise ExpectationError(
                f"{err}\n--- timeline ---\n{self.rec.timeline().context(None)}"
            ) from None

    async def at(self, t: float, action: Awaitable[Any]) -> Any:
        """Await ``action`` once scene time reaches ``t`` seconds."""
        delay = t - self.now()
        if delay > 0:
            await self.clock.sleep(delay)
        return await action

    @contextmanager
    def step(self, name: str, **data: Any) -> Iterator[None]:
        self.rec.emit("scene", "step", f"begin:{name}", **data)
        try:
            yield
        finally:
            self.rec.emit("scene", "step", f"end:{name}")

    async def close(self) -> None:
        for dev in reversed(self.devices):
            try:
                await dev.stop()
            except Exception as err:
                self.log.log("device_stop_error", device=dev.id, error=repr(err))
        for srv in reversed(self._servers):
            await srv.stop()
        self.rec.close()

    async def __aenter__(self) -> Scene:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# registry and runner


@dataclass
class ScenarioSpec:
    name: str
    fn: ScenarioFn
    families: tuple[str, ...] = ()
    catches: str = ""
    issues: tuple[str, ...] = ()
    quirks: tuple[str, ...] = ()
    seed: int = 0
    params: dict[str, Any] = field(default_factory=dict)


SCENARIOS: dict[str, ScenarioSpec] = {}


def scenario(
    name: str,
    *,
    families: Sequence[str] = (),
    catches: str = "",
    issues: Sequence[str] = (),
    quirks: Sequence[str] = (),
    seed: int = 0,
    **params: Any,
) -> Callable[[ScenarioFn], ScenarioFn]:
    """Register a scenario. ``params`` are default keyword arguments for the function."""

    def deco(fn: ScenarioFn) -> ScenarioFn:
        SCENARIOS[name] = ScenarioSpec(
            name, fn, tuple(families), catches, tuple(issues), tuple(quirks), seed, dict(params)
        )
        fn.scenario = SCENARIOS[name]  # type: ignore[attr-defined]
        return fn

    return deco


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    seed: int
    duration_s: float
    timeline: Path
    error: str | None = None
    knobs_fired: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        low = [k for k in self.knobs_fired if k.get("confidence") == "low"]
        extra = f" (low-confidence knobs fired: {len(low)})" if low else ""
        return f"{verdict} {self.name} seed={self.seed} {self.duration_s:.1f}s{extra}"


async def run_scenario(
    spec: ScenarioSpec | ScenarioFn | str,
    out_dir: str | Path,
    *,
    seed: int | None = None,
    clock: Clock | None = None,
    echo: bool = False,
    **params: Any,
) -> ScenarioResult:
    """Run one scenario in a fresh Scene; never raises for scenario failures."""
    if isinstance(spec, str):
        spec = SCENARIOS[spec]
    elif not isinstance(spec, ScenarioSpec):
        spec = getattr(spec, "scenario", None) or ScenarioSpec(spec.__name__, spec)
    seed = spec.seed if seed is None else seed
    scene = Scene(out_dir, seed=seed, clock=clock, echo=echo, name=spec.name)
    scene.rec.emit(
        "scene", "step", "scenario", scenario=spec.name, seed=seed, params={**spec.params, **params}
    )
    t0 = time.monotonic()
    error = None
    try:
        await spec.fn(scene, **{**spec.params, **params})
    except Exception:
        error = traceback.format_exc()
    finally:
        fired = [
            dict(e.data, src=e.src, t=e.t)
            for e in scene.rec.timeline().where(kind="knob", name="fired")
        ]
        scene.rec.emit("scene", "step", "result", passed=error is None)
        await scene.close()
    return ScenarioResult(
        spec.name, error is None, seed, time.monotonic() - t0, scene.timeline_path, error, fired
    )
