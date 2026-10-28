"""One JSONL timeline per run, shared by devices, sinks, the harness and scenarios.

Line shape::

    {"seq": 812, "t": 14.203, "wall": 1792000000.1, "src": "dev:wiim1",
     "kind": "cmd", "name": "SetAVTransportURI", "data": {...}}

``kind`` is one of :data:`KINDS`. ``t`` is seconds since the recorder started, on the
recorder's clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from .clock import Clock, RealClock

KINDS = (
    "cmd",  # request into a device
    "resp",  # its reply
    "event",  # pushed out: NOTIFY, ws push, long-poll wake
    "state",  # truth transition
    "report",  # a lensed value that differs from truth
    "sink",  # open, first_audio, underrun, eof, dropped, drained, track_change, ...
    "knob",  # a knob fired or was changed
    "sample",  # harness read of MA state
    "step",  # scenario step boundaries
    "log",  # free-form note
)


@dataclass(slots=True)
class Event:
    seq: int
    t: float
    wall: float
    src: str
    kind: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))

    def get(self, key: str, default: Any = None) -> Any:
        """Look up a top-level field or a ``data`` key."""
        if key in ("seq", "t", "wall", "src", "kind", "name"):
            return getattr(self, key)
        return self.data.get(key, default)

    def short(self) -> str:
        data = json.dumps(self.data, default=str, separators=(",", ":"))
        if len(data) > 160:
            data = data[:157] + "..."
        return f"#{self.seq:<5} {self.t:9.3f} {self.src:<16} {self.kind:<7} {self.name:<22} {data}"


Matcher = Callable[[Event], bool]


def _match(ev: Event, pred: Matcher | None, match: dict[str, Any]) -> bool:
    if pred is not None and not pred(ev):
        return False
    for key, want in match.items():
        have = ev.data.get(key[6:], _MISSING) if key.startswith("data__") else ev.get(key, _MISSING)
        if callable(want):
            if have is _MISSING or not want(have):
                return False
        elif isinstance(want, str) and key in ("src", "name") and want.endswith("*"):
            if have is _MISSING or not str(have).startswith(want[:-1]):
                return False
        elif have != want:
            return False
    return True


_MISSING = object()


class Timeline:
    """A read-only, queryable list of events.

    ``where`` keywords match top-level fields (``src``, ``kind``, ``name``) or data keys.
    Use the ``data__`` prefix to force a data key. A value may be a callable
    predicate, and a ``src``/``name`` string ending in ``*`` is a prefix match.
    """

    def __init__(self, events: Iterable[Event]) -> None:
        self.events: list[Event] = list(events)

    @classmethod
    def load(cls, path: str | Path) -> Timeline:
        events = []
        with Path(path).open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(Event(**json.loads(line)))
        return cls(events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, i: int) -> Event:
        return self.events[i]

    def where(self, pred: Matcher | None = None, /, **match: Any) -> Timeline:
        return Timeline(e for e in self.events if _match(e, pred, match))

    def between(self, t0: float | None, t1: float | None) -> Timeline:
        return Timeline(
            e for e in self.events if (t0 is None or e.t >= t0) and (t1 is None or e.t <= t1)
        )

    def first(self, pred: Matcher | None = None, /, **match: Any) -> Event | None:
        return next((e for e in self.events if _match(e, pred, match)), None)

    def last(self, pred: Matcher | None = None, /, **match: Any) -> Event | None:
        return next((e for e in reversed(self.events) if _match(e, pred, match)), None)

    def names(self) -> list[str]:
        return [e.name for e in self.events]

    def series(self, src: str, field: str, **match: Any) -> list[tuple[float, Any]]:
        """``(t, data[field])`` for every event from ``src`` that has ``field``."""
        return [
            (e.t, e.data[field])
            for e in self.events
            if _match(e, None, {"src": src, **match}) and field in e.data
        ]

    def context(self, around: Event | None, lines: int = 20) -> str:
        """The ``lines`` events around ``around`` (or the last ones), for failure output."""
        if not self.events:
            return "(empty timeline)"
        if around is None:
            chunk = self.events[-lines:]
        else:
            idx = next((i for i, e in enumerate(self.events) if e.seq == around.seq), 0)
            lo = max(0, idx - lines // 2)
            chunk = self.events[lo : lo + lines]
        out = []
        for e in chunk:
            mark = ">>" if around is not None and e.seq == around.seq else "  "
            out.append(f"{mark} {e.short()}")
        return "\n".join(out)


class Recorder:
    """Writes events to memory and (optionally) a JSONL file, in arrival order."""

    def __init__(
        self, path: str | Path | None = None, clock: Clock | None = None, echo: bool = False
    ) -> None:
        self.clock: Clock = clock or RealClock()
        self.t0 = self.clock.monotonic()
        self.path = Path(path) if path else None
        self._fh: IO[str] | None = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("w", buffering=1)
        self.events: list[Event] = []
        self.echo = echo
        self._listeners: list[Callable[[Event], None]] = []

    def now(self) -> float:
        return self.clock.monotonic() - self.t0

    def emit(self, src: str, kind: str, name: str, **data: Any) -> Event:
        ev = Event(
            seq=len(self.events),
            t=round(self.now(), 4),
            wall=round(self.clock.time(), 4),
            src=src,
            kind=kind,
            name=name,
            data=data,
        )
        self.events.append(ev)
        if self._fh is not None:
            self._fh.write(ev.to_json() + "\n")
        if self.echo:
            print(ev.short(), flush=True)
        for cb in list(self._listeners):
            cb(ev)
        return ev

    def bind(self, src: str) -> Emitter:
        """An emitter that fills in ``src``."""
        return Emitter(self, src)

    def listen(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        """Call ``cb`` for every new event; returns an unsubscribe function."""
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def timeline(self) -> Timeline:
        """Snapshot of everything recorded so far."""
        return Timeline(self.events)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Emitter:
    """A recorder handle bound to one ``src``. ``emitter.cmd("Play", uri=...)`` etc."""

    def __init__(self, rec: Recorder, src: str) -> None:
        self.rec = rec
        self.src = src

    def emit(self, kind: str, name: str, **data: Any) -> Event:
        return self.rec.emit(self.src, kind, name, **data)

    def cmd(self, name: str, **data: Any) -> Event:
        return self.emit("cmd", name, **data)

    def resp(self, name: str, **data: Any) -> Event:
        return self.emit("resp", name, **data)

    def event(self, name: str, **data: Any) -> Event:
        return self.emit("event", name, **data)

    def state(self, name: str, **data: Any) -> Event:
        return self.emit("state", name, **data)

    def report(self, name: str, **data: Any) -> Event:
        return self.emit("report", name, **data)

    def sink(self, name: str, **data: Any) -> Event:
        return self.emit("sink", name, **data)

    def knob(self, name: str, **data: Any) -> Event:
        return self.emit("knob", name, **data)

    def log(self, name: str, **data: Any) -> Event:
        return self.emit("log", name, **data)

    def child(self, suffix: str) -> Emitter:
        return Emitter(self.rec, f"{self.src}/{suffix}")
