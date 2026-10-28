"""SimDevice: the base class every simulated device family extends.

A device = identity + KnobSet + TransportMachine (truth) + HttpSink (audio) +
ReportLens (what it says) + one or more HTTP servers (protocol surfaces) + the
``/_sim`` control API. Families implement :meth:`SimDevice.build_app` and, optionally,
the other hooks. See ``docs/ADDING_A_FAMILY.md``.
"""

from __future__ import annotations

import ssl
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from aiohttp import web

from .clock import Clock, RealClock
from .kits.http import HttpServer, command_middleware
from .knobs import KnobSet, Profile
from .recorder import Recorder
from .rng import SeededRng
from .sink.http_sink import HttpSink
from .transport import ReportLens, TransportMachine


@dataclass
class DeviceContext:
    """Everything a device needs from the outside world."""

    device_id: str
    profile: Profile
    recorder: Recorder
    clock: Clock = field(default_factory=RealClock)
    seed: int = 0
    host: str = "127.0.0.1"
    port: int = 0  # 0 = pick a free port
    ssl_context: ssl.SSLContext | None = None
    options: dict[str, Any] = field(default_factory=dict)  # family-specific extras


class SimDevice:
    """Base class for simulated devices.

    Class attributes a family sets:

    - ``family``: registry name (``"linkplay"``)
    - ``lens_cls``: its :class:`ReportLens` subclass
    - ``use_sink``: False for devices that never fetch audio themselves
    """

    family: ClassVar[str] = ""
    lens_cls: ClassVar[type[ReportLens]] = ReportLens
    use_sink: ClassVar[bool] = True

    def __init__(self, ctx: DeviceContext) -> None:
        self.ctx = ctx
        self.id = ctx.device_id
        self.profile = ctx.profile
        self.identity = dict(ctx.profile.identity)
        self.clock = ctx.clock
        self.recorder = ctx.recorder
        self.rec = ctx.recorder.bind(f"dev:{self.id}")
        self.rng = SeededRng(ctx.seed, self.id)
        self.knobs = KnobSet.from_profile(ctx.profile, self.rng, self.rec)
        self.sink: HttpSink | None = (
            HttpSink(self.clock, self.rec.child("sink"), self.knobs) if self.use_sink else None
        )
        self.transport = TransportMachine(self.clock, self.rec, self.knobs, self.sink)
        self.lens = self.lens_cls(self.transport, self.knobs, self.rng)
        self.server: HttpServer | None = None
        self.extra_servers: list[HttpServer] = []

    # -- hooks for families --------------------------------------------------------

    def build_app(self) -> web.Application:
        """Return the main protocol app. Start from :meth:`make_app`."""
        raise NotImplementedError

    async def on_start(self) -> None:
        """Called after the main server is listening (start extra servers here)."""

    async def on_stop(self) -> None:
        """Called before servers shut down."""

    def status(self) -> dict[str, Any]:
        """Extra family state for ``GET /_sim/state``."""
        return {}

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        """Family-specific test actions for ``POST /_sim/action/<name>``.

        Raise ``KeyError`` for unknown actions. Built-in: ``take_over``.
        """
        raise KeyError(name)

    # -- helpers -------------------------------------------------------------------

    def make_app(
        self,
        *,
        record: bool = True,
        exempt_prefixes: tuple[str, ...] = ("/_sim", "/media", "/stream"),
    ) -> web.Application:
        """A new app with the command middleware (recording + latency/fault knobs)."""
        middlewares = []
        if record:
            middlewares.append(
                command_middleware(
                    self.rec, self.knobs, self.clock, exempt_prefixes=exempt_prefixes
                )
            )
        return web.Application(middlewares=middlewares)

    async def serve(
        self,
        app: web.Application,
        port: int = 0,
        *,
        ssl_context: ssl.SSLContext | None = None,
        host: str | None = None,
    ) -> HttpServer:
        """Start an extra listener (e.g. a second protocol port); stopped with the device."""
        srv = HttpServer(app, host or self.ctx.host, port, ssl_context)
        await srv.start()
        self.extra_servers.append(srv)
        self.rec.log("listening", url=srv.base_url, extra=True)
        return srv

    @property
    def base_url(self) -> str:
        assert self.server is not None, "device not started"
        return self.server.base_url

    @property
    def host(self) -> str:
        return self.ctx.host

    @property
    def port(self) -> int:
        assert self.server is not None, "device not started"
        return self.server.port

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        app = self.build_app()
        app.add_routes(self._sim_routes())
        self.server = HttpServer(app, self.ctx.host, self.ctx.port, self.ctx.ssl_context)
        await self.server.start()
        self.rec.log(
            "started",
            family=self.family,
            profile=self.profile.name,
            url=self.server.base_url,
            seed=self.ctx.seed,
            knobs=self.knobs.non_default(),
            identity=self.identity,
        )
        await self.on_start()

    async def stop(self) -> None:
        await self.on_stop()
        await self.transport.close()
        for srv in reversed(self.extra_servers):
            await srv.stop()
        self.extra_servers.clear()
        if self.server is not None:
            await self.server.stop()
        self.rec.log("stopped")

    async def __aenter__(self) -> SimDevice:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def state(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "profile": self.profile.name,
            "identity": self.identity,
            "transport": self.transport.snapshot(),
            "family_state": self.status(),
        }

    # -- /_sim control API ---------------------------------------------------------

    def _sim_routes(self) -> list[web.RouteDef]:
        return [
            web.get("/_sim/state", self._sim_state),
            web.get("/_sim/knobs", self._sim_knobs),
            web.post("/_sim/knobs", self._sim_set_knobs),
            web.get("/_sim/events", self._sim_events),
            web.post("/_sim/action/{name}", self._sim_action),
        ]

    async def _sim_state(self, _req: web.Request) -> web.Response:
        return web.json_response(self.state(), dumps=_dumps)

    async def _sim_knobs(self, _req: web.Request) -> web.Response:
        return web.json_response(self.knobs.describe(), dumps=_dumps)

    async def _sim_set_knobs(self, req: web.Request) -> web.Response:
        body = await req.json()
        try:
            changed = {k: self.knobs.set_live(k, v) for k, v in body.items()}
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        return web.json_response(changed, dumps=_dumps)

    async def _sim_events(self, req: web.Request) -> web.Response:
        since = int(req.query.get("since", "0"))
        events = [
            e
            for e in self.recorder.events[since:]
            if req.query.get("all") or e.src.startswith(f"dev:{self.id}")
        ]
        return web.json_response([asdict(e) for e in events], dumps=_dumps)

    async def _sim_action(self, req: web.Request) -> web.Response:
        name = req.match_info["name"]
        payload = await req.json() if req.can_read_body else {}
        self.rec.cmd(f"sim_action:{name}", **payload)
        try:
            if name == "take_over":
                self.transport.take_over(
                    payload.get("owner", "external"),
                    emit_event=bool(payload.get("emit_event", True)),
                    title=payload.get("title"),
                )
                result: Any = True
            else:
                result = await self.sim_action(name, payload)
        except KeyError:
            return web.json_response({"error": f"unknown action {name}"}, status=404)
        return web.json_response({"ok": True, "result": result}, dumps=_dumps)


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)
