"""HTTP kit: run aiohttp apps, record every protocol request, inject latency/faults,
and hold long-polls.

- :class:`HttpServer`: start/stop an ``aiohttp`` app on host:port (port 0 = pick one),
  optional TLS (bring your own ``ssl.SSLContext``).
- :func:`command_middleware`: records ``cmd``/``resp`` events for every request and
  applies the core ``latency_s``/``jitter_s``/``http_error``/``hang``/``offline`` knobs.
  Paths under ``/_sim`` and ``exempt_prefixes`` are passed through untouched.
- :class:`LongPoll`: etag-versioned resource; ``wait(etag, timeout)`` returns at once
  when the client's etag is stale, else holds until the next ``bump()`` or timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiohttp import web

from ..clock import Clock, wait_with_timeout
from ..knobs import KnobSet
from ..recorder import Emitter

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class HttpServer:
    """One listening aiohttp app."""

    def __init__(
        self,
        app: web.Application,
        host: str = "127.0.0.1",
        port: int = 0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self._runner: web.AppRunner | None = None

    @property
    def scheme(self) -> str:
        return "https" if self.ssl_context else "http"

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    async def start(self) -> None:
        self._runner = web.AppRunner(
            self.app, access_log=None, handle_signals=False, shutdown_timeout=1.0
        )
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self.host, self.port, ssl_context=self.ssl_context, reuse_address=True
        )
        await site.start()
        if self.port == 0:
            server = site._server
            assert server is not None
            assert server.sockets
            self.port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


def command_middleware(
    rec: Emitter,
    knobs: KnobSet,
    clock: Clock,
    exempt_prefixes: Iterable[str] = ("/_sim", "/media", "/stream"),
    record_body: bool = True,
) -> Any:
    """aiohttp middleware that records and perturbs protocol requests."""
    exempt = tuple(exempt_prefixes)
    counter = {"n": 0}

    @web.middleware
    async def _mw(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path.startswith(exempt):
            return await handler(request)
        counter["n"] += 1
        conn = f"r{counter['n']}"
        data: dict[str, Any] = {
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "conn": conn,
        }
        if record_body and request.can_read_body:
            body = await request.read()
            if body:
                data["body"] = body[:4096].decode("utf-8", "replace")
        rec.cmd(f"{request.method} {request.path}", **data)
        if knobs.get("offline"):
            rec.resp(f"{request.method} {request.path}", status=503, conn=conn, fault="offline")
            raise web.HTTPServiceUnavailable()
        delay = knobs.get("latency_s")
        jitter = knobs.get("jitter_s")
        if jitter > 0:
            delay += knobs.rng.uniform("jitter", 0.0, jitter)
        if delay > 0:
            await clock.sleep(delay)
        if knobs.fires("hang", path=request.path):
            await clock.sleep(float(knobs.param("hang", "hold_s", 60.0)))
            rec.resp(f"{request.method} {request.path}", status=None, conn=conn, fault="hang")
            raise web.HTTPGatewayTimeout()
        if knobs.fires("http_error", path=request.path):
            status = int(knobs.param("http_error", "status", 500))
            rec.resp(
                f"{request.method} {request.path}", status=status, conn=conn, fault="http_error"
            )
            return web.Response(status=status, text="error")
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            rec.resp(f"{request.method} {request.path}", status=exc.status, conn=conn)
            raise
        out: dict[str, Any] = {"status": resp.status, "conn": conn}
        if record_body and isinstance(resp, web.Response) and resp.body is not None:
            body = resp.body if isinstance(resp.body, bytes) else str(resp.body).encode()
            out["body"] = body[:2048].decode("utf-8", "replace")
        rec.resp(f"{request.method} {request.path}", **out)
        return resp

    return _mw


class LongPoll:
    """An etag-versioned resource clients can long-poll.

    ``etag`` changes on every :meth:`bump`. :meth:`wait` returns ``True`` right away if
    the client's etag differs from the current one, otherwise waits for a bump
    (``True``) or the timeout (``False``). With ``min_interval_s`` set, a client
    (identified by ``client``) polling again sooner is recorded as a
    ``longpoll_rate_violation`` event (BluOS-style rate rules, BL-01/BL-02).
    """

    def __init__(
        self,
        clock: Clock,
        name: str = "status",
        rec: Emitter | None = None,
        min_interval_s: float = 0.0,
    ) -> None:
        self.clock = clock
        self.name = name
        self.rec = rec
        self.min_interval_s = min_interval_s
        self.version = 0
        self._cond = asyncio.Condition()
        self._last_seen: dict[str, float] = {}

    @property
    def etag(self) -> str:
        return f"{self.name}-{self.version}"

    def bump(self) -> None:
        self.version += 1
        if self.rec is not None:
            self.rec.event("longpoll_bump", resource=self.name, etag=self.etag)

        async def _notify() -> None:
            async with self._cond:
                self._cond.notify_all()

        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(_notify())

    async def wait(self, etag: str | None, timeout: float, client: str | None = None) -> bool:
        if client is not None and self.min_interval_s > 0:
            now = self.clock.monotonic()
            last = self._last_seen.get(client)
            if last is not None and now - last < self.min_interval_s and self.rec is not None:
                self.rec.event(
                    "longpoll_rate_violation",
                    resource=self.name,
                    client=client,
                    interval_s=round(now - last, 3),
                )
        changed = await self._wait(etag, timeout)
        if client is not None:
            # interval is measured from the previous *answer* to the next request
            self._last_seen[client] = self.clock.monotonic()
        return changed

    async def _wait(self, etag: str | None, timeout: float) -> bool:
        if etag is None or etag != self.etag:
            return True
        if timeout <= 0:
            return False
        start_version = self.version

        async def _until_bump() -> None:
            async with self._cond:
                await self._cond.wait_for(lambda: self.version != start_version)

        changed, _ = await wait_with_timeout(self.clock, _until_bump(), timeout)
        if changed and self.rec is not None:
            self.rec.event("longpoll_wake", resource=self.name, etag=self.etag)
        return changed


def text(body: str, status: int = 200, content_type: str = "text/plain") -> web.Response:
    return web.Response(text=body, status=status, content_type=content_type)


def xml(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/xml", charset="utf-8")


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)
