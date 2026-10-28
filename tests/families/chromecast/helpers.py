"""An asyncio CASTV2 sender for tests: one TLS socket, requestId bookkeeping, inbox."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from typing import Any

from seamer.families.chromecast import protocol as p

SENDER = "sender-0"


class CastClient:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.inbox: list[tuple[str, str, dict[str, Any]]] = []  # (ns, source, payload)
        self.closed: str | None = None  # why the socket ended
        self._ids = itertools.count(1)
        self._waiters: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._new = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, dev: Any) -> CastClient:
        c = cls(dev.cast.host, dev.cast.port)
        await c.open()
        return c

    async def open(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, ssl=p.client_ssl_context()
        )
        self.closed = None
        self._task = asyncio.create_task(self._read())
        await self.send(p.NS_CONNECTION, {"type": "CONNECT"}, dest=p.PLATFORM_ID)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self.writer is not None:
            await p.close_writer(self.writer)

    async def __aenter__(self) -> CastClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _read(self) -> None:
        assert self.reader is not None
        try:
            while True:
                msg = await p.read_frame(self.reader)
                data = msg.json()
                self.inbox.append((msg.namespace, msg.source_id, data))
                rid = data.get("requestId")
                fut = self._waiters.pop(rid, None) if rid else None
                if fut is not None and not fut.done():
                    fut.set_result(data)
                self._new.set()
        except asyncio.IncompleteReadError:
            self.closed = "eof"
        except (ConnectionError, OSError) as err:
            self.closed = repr(err)
        self._new.set()

    async def send(self, ns: str, payload: dict[str, Any], dest: str = p.PLATFORM_ID) -> None:
        assert self.writer is not None
        self.writer.write(p.frame(p.json_message(SENDER, dest, ns, payload)))
        await self.writer.drain()

    async def request(
        self, ns: str, payload: dict[str, Any], dest: str = p.PLATFORM_ID, timeout: float = 3.0
    ) -> dict[str, Any]:
        rid = next(self._ids) + 100
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        await self.send(ns, {**payload, "requestId": rid}, dest)
        return await asyncio.wait_for(fut, timeout)

    async def receiver(self, mtype: str, timeout: float = 3.0, **kw: Any) -> dict[str, Any]:
        return await self.request(p.NS_RECEIVER, {"type": mtype, **kw}, timeout=timeout)

    async def launch(self, app_id: str = p.APP_MEDIA_RECEIVER) -> str:
        """LAUNCH and CONNECT to the app; returns its transportId."""
        st = await self.receiver("LAUNCH", appId=app_id)
        assert st["type"] == "RECEIVER_STATUS", st
        app = next(a for a in st["status"]["applications"] if a["appId"] == app_id)
        await self.send(p.NS_CONNECTION, {"type": "CONNECT"}, dest=app["transportId"])
        self.transport_id = app["transportId"]
        return app["transportId"]

    async def media(self, mtype: str, timeout: float = 3.0, **kw: Any) -> dict[str, Any]:
        return await self.request(
            p.NS_MEDIA, {"type": mtype, **kw}, dest=self.transport_id, timeout=timeout
        )

    def messages(self, mtype: str | None = None, ns: str | None = None) -> list[dict[str, Any]]:
        return [
            d
            for n, _s, d in self.inbox
            if (ns is None or n == ns) and (mtype is None or d.get("type") == mtype)
        ]

    def media_states(self) -> list[tuple[str, str | None]]:
        out = []
        for d in self.messages("MEDIA_STATUS"):
            for e in d.get("status") or []:
                out.append((e["playerState"], e.get("idleReason")))
        return out

    async def wait(self, pred: Any, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not pred():
            left = end - loop.time()
            if left <= 0:
                raise AssertionError(f"timed out; inbox tail: {self.inbox[-6:]}")
            self._new.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new.wait(), min(left, 0.1))


def media_item(url: str, content_type: str = "audio/wav", title: str = "Track") -> dict[str, Any]:
    return {
        "contentId": url,
        "contentType": content_type,
        "streamType": "BUFFERED",
        "metadata": {"metadataType": 3, "title": title},
    }
