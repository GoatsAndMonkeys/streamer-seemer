"""Buffer model: decoded audio waiting to be heard, tagged by which track it is."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class OnServerClose(StrEnum):
    DRAIN = "drain"  # play out the buffer
    DROP = "drop"  # discard the buffer at EOF (bss:B5)
    REFETCH = "refetch"  # re-request the same URL (bss:B6 loop)


@dataclass
class BufferModel:
    """Seconds of audio received but not yet played.

    ``segments`` holds ``[seconds, track]`` runs in arrival order; ``drain`` consumes
    from the front and returns what was heard.
    """

    capacity_s: float = 8.0
    prebuffer_s: float = 0.5
    low_water_s: float = 0.0
    fill_s: float = 0.0
    played_s: float = 0.0
    received_s: float = 0.0
    dropped_s: float = 0.0
    segments: deque[list] = field(default_factory=deque)

    @property
    def full(self) -> bool:
        return self.fill_s >= self.capacity_s

    @property
    def ready(self) -> bool:
        return self.fill_s >= self.prebuffer_s

    def push(self, seconds: float, track: int | None = None) -> None:
        if seconds <= 0:
            return
        self.fill_s += seconds
        self.received_s += seconds
        if self.segments and self.segments[-1][1] == track:
            self.segments[-1][0] += seconds
        else:
            self.segments.append([seconds, track])

    def drain(self, seconds: float) -> list[tuple[float, int | None]]:
        """Play up to ``seconds``; returns ``[(seconds, track), ...]`` actually played."""
        out: list[tuple[float, int | None]] = []
        want = min(seconds, self.fill_s)
        while want > 1e-9 and self.segments:
            seg = self.segments[0]
            take = min(want, seg[0])
            seg[0] -= take
            want -= take
            out.append((take, seg[1]))
            if seg[0] <= 1e-9:
                self.segments.popleft()
        played = sum(s for s, _ in out)
        self.fill_s = max(0.0, self.fill_s - played)
        self.played_s += played
        return out

    def drop(self) -> float:
        """Discard everything buffered; returns the seconds lost."""
        lost = self.fill_s
        self.dropped_s += lost
        self.fill_s = 0.0
        self.segments.clear()
        return lost
