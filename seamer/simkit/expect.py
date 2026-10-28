"""Assertions over a :class:`~seamer.simkit.recorder.Timeline`.

All checks are post hoc (run them after the scenario has played out). Each failure
raises :class:`ExpectationError` with the timeline lines around the first violation.
For live waiting use ``Scene.wait_for`` / ``Expect.wait_event``.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Sequence
from typing import Any

from .clock import Clock, RealClock
from .recorder import Event, Matcher, Recorder, Timeline


class ExpectationError(AssertionError):
    pass


class Expect:
    """Assertion helpers. Pass a Recorder (live, re-snapshotted per check) or a Timeline."""

    def __init__(self, source: Recorder | Timeline, context_lines: int = 20) -> None:
        self._source = source
        self.context_lines = context_lines

    @property
    def tl(self) -> Timeline:
        src = self._source
        return src.timeline() if isinstance(src, Recorder) else src

    def _fail(self, msg: str, at: Event | None, tl: Timeline | None = None) -> None:
        tl = tl or self.tl
        raise ExpectationError(f"{msg}\n--- timeline ---\n{tl.context(at, self.context_lines)}")

    # -- existence / absence -------------------------------------------------

    def eventually(
        self,
        pred: Matcher | None = None,
        /,
        *,
        within: float | None = None,
        after: float | None = None,
        **match: Any,
    ) -> Event:
        """An event matching ``pred``/``match`` exists in ``[after, after+within]``."""
        t0 = after or 0.0
        t1 = None if within is None else t0 + within
        ev = self.tl.between(t0, t1).first(pred, **match)
        if ev is None:
            self._fail(f"no event matching {match or pred} in [{t0}, {t1}]", None)
        assert ev is not None
        return ev

    def never(
        self,
        pred: Matcher | None = None,
        /,
        *,
        during: tuple[float | None, float | None] | None = None,
        **match: Any,
    ) -> None:
        """No event matching ``pred``/``match`` (optionally within ``during``)."""
        tl = self.tl if during is None else self.tl.between(*during)
        ev = tl.first(pred, **match)
        if ev is not None:
            self._fail(f"unexpected event matching {match or pred}", ev)

    def count(
        self,
        pred: Matcher | None = None,
        /,
        *,
        at_most: int | None = None,
        at_least: int | None = None,
        exactly: int | None = None,
        **match: Any,
    ) -> int:
        hits = self.tl.where(pred, **match)
        n = len(hits)
        bad = (
            (at_most is not None and n > at_most)
            or (at_least is not None and n < at_least)
            or (exactly is not None and n != exactly)
        )
        if bad:
            at = hits[at_most] if at_most is not None and n > at_most else (hits[-1] if n else None)
            self._fail(
                f"count of {match or pred} is {n} "
                f"(at_most={at_most}, at_least={at_least}, exactly={exactly})",
                at,
            )
        return n

    def order(self, *matches: dict[str, Any]) -> list[Event]:
        """Events matching each dict occur in this order (not necessarily adjacent)."""
        found: list[Event] = []
        events = self.tl.events
        i = 0
        for m in matches:
            while i < len(events) and not _match_dict(events[i], m):
                i += 1
            if i >= len(events):
                self._fail(
                    f"expected {m} after {found[-1].short() if found else 'start'}",
                    found[-1] if found else None,
                )
            found.append(events[i])
            i += 1
        return found

    # -- series ----------------------------------------------------------------

    def max_jump(
        self, series: Sequence[tuple[float, float]], limit: float, *, rate: float = 1.0
    ) -> float:
        """Values never jump by more than ``limit`` beyond the expected ``rate*dt`` drift."""
        worst = 0.0
        for (ta, va), (tb, vb) in itertools.pairwise(series):
            if va is None or vb is None:
                continue
            jump = abs((vb - va) - rate * (tb - ta))
            worst = max(worst, jump)
            if jump > limit:
                ev = self.tl.first(t=tb)
                self._fail(f"jump of {jump:.3f} at t={tb:.3f} ({va} -> {vb}) > {limit}", ev)
        return worst

    # -- audible truth ---------------------------------------------------------

    def audible_coverage(self, track: int, at_least_s: float, src: str | None = None) -> float:
        """The sink reported at least ``at_least_s`` seconds of ``track`` as heard.

        Reads the last ``sink``/``heard`` summary event (written when a sink closes)
        from ``src`` (any sink if None).
        """
        match: dict[str, Any] = {"kind": "sink", "name": "heard"}
        if src is not None:
            match["src"] = src
        heard = 0.0
        for ev in self.tl.where(**match):
            heard += float(ev.data.get("tracks", {}).get(str(track), 0.0))
        if heard < at_least_s:
            self._fail(
                f"track {track} heard for {heard:.2f}s < {at_least_s}s", self.tl.last(**match)
            )
        return heard

    def played_close_to(
        self, expected_s: float, tolerance_s: float, src: str | None = None
    ) -> float:
        """Total seconds played by sinks is within ``tolerance_s`` of ``expected_s``."""
        match: dict[str, Any] = {"kind": "sink", "name": "heard"}
        if src is not None:
            match["src"] = src
        played = sum(float(e.data.get("played_s", 0.0)) for e in self.tl.where(**match))
        if abs(played - expected_s) > tolerance_s:
            self._fail(
                f"played {played:.3f}s, expected {expected_s}±{tolerance_s}s", self.tl.last(**match)
            )
        return played

    # -- live waiting ----------------------------------------------------------

    async def wait_event(
        self,
        pred: Matcher | None = None,
        /,
        *,
        timeout: float,
        clock: Clock | None = None,
        poll: float = 0.02,
        **match: Any,
    ) -> Event:
        """Wait until an event matching ``pred``/``match`` has been recorded."""
        if not isinstance(self._source, Recorder):
            raise TypeError("wait_event needs a live Recorder")
        clock = clock or self._source.clock or RealClock()
        deadline = clock.monotonic() + timeout
        while True:
            ev = self.tl.first(pred, **match)
            if ev is not None:
                return ev
            if clock.monotonic() >= deadline:
                self._fail(f"timed out after {timeout}s waiting for {match or pred}", None)
            await clock.sleep(poll)


def _match_dict(ev: Event, m: dict[str, Any]) -> bool:
    return Timeline([ev]).first(**m) is not None


async def wait_for(
    pred: Callable[[], bool],
    timeout: float,
    clock: Clock | None = None,
    poll: float = 0.02,
    what: str = "condition",
) -> None:
    """Poll ``pred`` until true; raise ``ExpectationError`` after ``timeout``."""
    clock = clock or RealClock()
    deadline = clock.monotonic() + timeout
    while not pred():
        if clock.monotonic() >= deadline:
            raise ExpectationError(f"timed out after {timeout}s waiting for {what}")
        await clock.sleep(poll)
        await asyncio.sleep(0)
