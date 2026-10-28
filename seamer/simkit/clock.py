"""Clocks: the real one for e2e runs, a virtual one for fast in-process tests.

Every piece of simkit takes a :class:`Clock` instead of calling ``time``/``asyncio.sleep``
directly, so the same device code runs against wall time or against a
:class:`VirtualClock` that a test advances by hand.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time as _time
import types
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class Cancellable(Protocol):
    """What ``call_later`` returns: something with ``cancel()``."""

    def cancel(self) -> None: ...


@runtime_checkable
class Clock(Protocol):
    """Time source used by devices, sinks and the recorder."""

    def time(self) -> float:
        """Wall-clock seconds since the epoch (for protocol timestamps)."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds (for durations and positions)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep on this clock."""
        ...

    def call_later(self, seconds: float, cb: Callable[[], Any]) -> Cancellable:
        """Run ``cb`` after ``seconds`` on this clock."""
        ...


class RealClock:
    """Wall time. Default for every e2e run."""

    def time(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    def call_later(self, seconds: float, cb: Callable[[], Any]) -> Cancellable:
        return asyncio.get_running_loop().call_later(max(0.0, seconds), cb)


class _VTimer:
    __slots__ = ("cancelled", "cb", "when")

    def __init__(self, when: float, cb: Callable[[], Any]) -> None:
        self.when = when
        self.cb = cb
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class VirtualClock:
    """A clock that only moves when the test calls :meth:`advance`.

    Sleepers and ``call_later`` callbacks fire in time order. After each timer
    fires, the event loop gets a few turns so the woken coroutines can run and
    schedule their next sleep before time moves on.
    """

    def __init__(
        self, start: float = 0.0, wall_start: float = 1_800_000_000.0, settle_turns: int = 8
    ) -> None:
        self._now = start
        self._wall_offset = wall_start - start
        self._heap: list[tuple[float, int, _VTimer]] = []
        self._seq = itertools.count()
        self.settle_turns = settle_turns

    def time(self) -> float:
        return self._now + self._wall_offset

    def monotonic(self) -> float:
        return self._now

    def call_later(self, seconds: float, cb: Callable[[], Any]) -> Cancellable:
        timer = _VTimer(self._now + max(0.0, seconds), cb)
        heapq.heappush(self._heap, (timer.when, next(self._seq), timer))
        return timer

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:  # like asyncio.sleep(0): just yield, don't wait for advance()
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def _wake() -> None:
            if not fut.done():
                fut.set_result(None)

        timer = self.call_later(seconds, _wake)
        try:
            await fut
        finally:
            timer.cancel()

    @property
    def pending(self) -> int:
        """Number of live timers."""
        return sum(1 for _, _, t in self._heap if not t.cancelled)

    def next_deadline(self) -> float | None:
        while self._heap and self._heap[0][2].cancelled:
            heapq.heappop(self._heap)
        return self._heap[0][0] if self._heap else None

    async def settle(self) -> None:
        """Give the event loop a few turns without moving time."""
        for _ in range(self.settle_turns):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Move time forward by ``seconds``, firing due timers in order."""
        target = self._now + max(0.0, seconds)
        await self.settle()
        while True:
            when = self.next_deadline()
            if when is None or when > target:
                break
            _, _, timer = heapq.heappop(self._heap)
            self._now = max(self._now, timer.when)
            timer.cb()
            await self.settle()
        self._now = target
        await self.settle()

    async def run_until(
        self, pred: Callable[[], bool], max_s: float, step: float | None = None
    ) -> bool:
        """Advance until ``pred()`` is true or ``max_s`` virtual seconds pass.

        Jumps straight to the next timer when ``step`` is None. Returns the final
        value of ``pred()``.
        """
        end = self._now + max_s
        await self.settle()
        while not pred() and self._now < end:
            if step is None:
                nxt = self.next_deadline()
                delta = (end - self._now) if nxt is None else max(0.0, min(nxt, end) - self._now)
            else:
                delta = min(step, end - self._now)
            await self.advance(delta)
        return pred()


async def wait_with_timeout[T](
    clock: Clock, aw: Awaitable[T], timeout: float
) -> tuple[bool, T | None]:
    """Await ``aw`` but give up after ``timeout`` seconds on ``clock``.

    Returns ``(True, result)`` or ``(False, None)`` on timeout. Works with
    :class:`VirtualClock` (unlike ``asyncio.wait_for``, which uses the loop clock).
    """
    task = asyncio.ensure_future(aw)
    sleeper = asyncio.ensure_future(clock.sleep(timeout))
    try:
        done, _ = await asyncio.wait({task, sleeper}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        sleeper.cancel()
        raise
    if task in done:
        sleeper.cancel()
        return True, task.result()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return False, None


class _TimeShim(types.SimpleNamespace):
    """Stands in for the ``time`` module inside patched modules."""


@contextmanager
def patch_time(clock: Clock, modules: Sequence[str | types.ModuleType]) -> Iterator[None]:
    """Make ``time.time``/``time.monotonic`` inside ``modules`` read from ``clock``.

    Only the module-level name ``time`` is replaced (``import time`` style). Other
    attributes of the real module are passed through. Each module must already be
    imported and must have a ``time`` attribute, or this raises ``AttributeError``
    so a rename upstream fails loudly.
    """
    import importlib

    saved: list[tuple[types.ModuleType, Any]] = []
    try:
        for mod in modules:
            m = importlib.import_module(mod) if isinstance(mod, str) else mod
            if not hasattr(m, "time"):
                raise AttributeError(f"{m.__name__} has no 'time' attribute to patch")
            shim = _TimeShim(**{k: getattr(_time, k) for k in dir(_time) if not k.startswith("_")})
            shim.time = clock.time
            shim.monotonic = clock.monotonic
            saved.append((m, m.time))
            m.time = shim
        yield
    finally:
        for m, orig in reversed(saved):
            m.time = orig
