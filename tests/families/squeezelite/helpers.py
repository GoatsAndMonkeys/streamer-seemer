"""Shared setup: a FakeSlimServer plus a connected squeezelite sim."""

from __future__ import annotations

from typing import Any

from seamer.simkit.scenario import Scene

from .fake_server import FakeSlimServer


async def rig(
    scene: Scene, profile: str = "squeezelite_linux", heartbeat_s: float = 0.0, **knobs: Any
) -> tuple[Any, FakeSlimServer]:
    srv = await FakeSlimServer(heartbeat_s=heartbeat_s).start()
    scene._servers.append(_Closer(srv))  # closed with the scene
    knobs.setdefault("server", f"127.0.0.1:{srv.port}")
    dev = await scene.device(f"squeezelite/{profile}", **knobs)
    await srv.wait("HELO")
    await srv.wait("SETD")
    return dev, srv


class _Closer:
    """Adapts FakeSlimServer to the Scene's server list (``await stop()``)."""

    def __init__(self, srv: FakeSlimServer) -> None:
        self.srv = srv

    async def stop(self) -> None:
        await self.srv.close()


async def track_url(scene: Scene, n: int = 1, secs: float = 2.0, route: str = "media") -> str:
    await scene.media_server()
    t = scene.media.track(n, secs)
    return scene.media_url(t.path, route=route)
