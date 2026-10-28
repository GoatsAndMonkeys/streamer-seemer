"""The example device: a tiny JSON-over-HTTP player. Copy this to start a family.

Protocol (all GET, JSON replies):

- ``/api/status[?etag=E&timeout=S]``: state, position, duration, uri, title, volume.
  With ``etag`` it long-polls until something changes or ``timeout`` passes.
- ``/api/play?url=U[&duration=D&title=T]``: load and play ``U``.
- ``/api/queue_next?url=U[&duration=D]``: set the next item (gapless advance).
- ``/api/pause``, ``/api/resume``, ``/api/stop``, ``/api/skip``, ``/api/seek?pos=S``,
  ``/api/volume?level=0..100``.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ...simkit.device import SimDevice
from ...simkit.kits.http import LongPoll
from ...simkit.transport import ReportLens, TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)

STATE_NAMES = {
    TState.NO_MEDIA: "none",
    TState.STOPPED: "stop",
    TState.LOADING: "loading",
    TState.BUFFERING: "loading",
    TState.PLAYING: "play",
    TState.PAUSED: "pause",
}


class ExampleLens(ReportLens):
    """Maps truth to the example protocol's vocabulary."""

    def state_name(self) -> str:
        name = STATE_NAMES[self.state()]
        return name.upper() if self.knobs.get("report_state_names") == "upper" else name


class ExampleDevice(SimDevice):
    family = "example"
    lens_cls = ExampleLens
    lens: ExampleLens

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.volume = 30
        self.status_poll = LongPoll(self.clock, "status", self.rec.child("status"))
        # push truth changes to long-poll clients unless the quirk says not to
        self.transport.add_listener(self._on_transport)

    def _on_transport(self, name: str, _data: dict[str, Any]) -> None:
        if name in ("state", "advanced") and not self.knobs.get("no_state_events"):
            self.status_poll.bump()

    # -- protocol ------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app()
        app.router.add_get("/api/status", self._status)
        app.router.add_get("/api/{cmd}", self._command)
        return app

    def status_body(self) -> dict[str, Any]:
        meta = self.lens.metadata()
        return {
            "state": self.lens.state_name(),
            "position": self.lens.position(),
            "duration": self.lens.duration(),
            "uri": meta.uri if meta else None,
            "title": meta.title if meta else None,
            "source": self.transport.owner,
            "volume": self.volume,
            "etag": self.status_poll.etag,
            "name": self.identity.get("name", self.id),
        }

    async def _status(self, req: web.Request) -> web.Response:
        if "etag" in req.query:
            timeout = float(req.query.get("timeout", "30"))
            await self.status_poll.wait(req.query["etag"], timeout, client=req.remote)
        return web.json_response(self.status_body())

    async def _command(self, req: web.Request) -> web.Response:
        cmd = req.match_info["cmd"]
        q = req.query
        tm = self.transport
        if cmd == "play":
            if "url" not in q:
                return web.json_response({"error": "url required"}, status=400)
            dur = float(q["duration"]) if "duration" in q else None
            await tm.set_uri(q["url"], duration_s=dur, title=q.get("title"))
            await tm.play()
        elif cmd == "queue_next":
            dur = float(q["duration"]) if "duration" in q else None
            await tm.set_next(q.get("url"), duration_s=dur, title=q.get("title"))
        elif cmd == "pause":
            if not self.knobs.fires("ignore_pause"):
                await tm.pause()
        elif cmd == "resume":
            await tm.play()
        elif cmd == "stop":
            await tm.stop()
        elif cmd == "skip":
            if not await tm.next():
                return web.json_response({"error": "nothing queued"}, status=409)
        elif cmd == "seek":
            await tm.seek(float(q.get("pos", "0")))
        elif cmd == "volume":
            self.volume = max(0, min(100, int(q.get("level", self.volume))))
            self.status_poll.bump()
        else:
            return web.json_response({"error": f"unknown command {cmd}"}, status=404)
        return web.json_response({"ok": True})

    # -- hooks ---------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {"volume": self.volume, "etag": self.status_poll.etag}

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "press_button":  # a physical button on the device
            await self.transport.pause()
            return "paused"
        raise KeyError(name)
