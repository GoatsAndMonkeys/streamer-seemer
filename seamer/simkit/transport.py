"""Transport truth (what the speaker is really doing) and the report lens (what it says).

:class:`TransportMachine` owns the ground truth: the current/next media slots, the
transport state and the audible position. With an :class:`HttpSink` the position is
what the sink has actually played; without one (``sink=None``) it is a clock-based
counter, which is what VirtualClock tests and fake "external source" playback use.

:class:`ReportLens` turns the truth into what a protocol surface reports, applying the
generic ``report_*`` core knobs. Families subclass it for their own quirks and map
:class:`TState` to their protocol vocabulary.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .clock import Clock
from .knobs import KnobSet
from .recorder import Emitter
from .rng import SeededRng
from .sink.http_sink import HttpSink

TransportListener = Callable[[str, dict[str, Any]], None]


class TState(StrEnum):
    NO_MEDIA = "no_media"
    STOPPED = "stopped"
    LOADING = "loading"  # command accepted, stream not yet requested / transition gap
    BUFFERING = "buffering"  # stream requested, no audio yet (or underrun)
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class MediaSlot:
    uri: str
    meta: str = ""  # raw protocol metadata (DIDL, JSON, ...)
    duration_s: float | None = None
    owner: str = "ma"  # "ma" or an external source: "hdmi", "spotify", ...
    title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class TransportMachine:
    """Ground-truth transport. Commands are async; state changes are recorded.

    Listener events (``add_listener``): ``state`` {old, new}, ``track_started`` {uri},
    ``track_ended`` {uri, reason}, ``advanced`` {uri}, ``error`` {error}.
    """

    def __init__(
        self, clock: Clock, rec: Emitter, knobs: KnobSet, sink: HttpSink | None = None
    ) -> None:
        self.clock = clock
        self.rec = rec
        self.knobs = knobs
        self.sink = sink
        self.state = TState.NO_MEDIA
        self.current: MediaSlot | None = None
        self.next_slot: MediaSlot | None = None
        self.owner = "ma"
        self.last_error: str | None = None
        self.tracks_started = 0
        self._listeners: list[TransportListener] = []
        self._task: asyncio.Task[None] | None = None
        self._bg: set[asyncio.Future[Any]] = set()
        # clock-based position (sink-less mode / external sources)
        self._pos_base = 0.0
        self._playing_since: float | None = None
        self._end_timer: Any = None
        if sink is not None:
            sink.add_listener(self._on_sink)

    # -- listeners ---------------------------------------------------------------

    def add_listener(self, cb: TransportListener) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def on_change(self, cb: Callable[[TState, TState], None]) -> Callable[[], None]:
        def _wrap(name: str, data: dict[str, Any]) -> None:
            if name == "state":
                cb(data["old"], data["new"])

        return self.add_listener(_wrap)

    def _notify(self, name: str, **data: Any) -> None:
        for cb in list(self._listeners):
            cb(name, data)

    def _set_state(self, new: TState, **why: Any) -> None:
        old = self.state
        if new == old:
            return
        # keep the clock-based position counter in step with PLAYING
        now = self.clock.monotonic()
        if old == TState.PLAYING and self._playing_since is not None:
            self._pos_base += now - self._playing_since
            self._playing_since = None
        if new == TState.PLAYING and self._clock_mode:
            self._playing_since = now
        self.state = new
        self.rec.state(
            "transport",
            old=old.value,
            new=new.value,
            uri=self.current.uri if self.current else None,
            owner=self.owner,
            **why,
        )
        self._notify("state", old=old, new=new)
        self._arm_end_timer()

    @property
    def _clock_mode(self) -> bool:
        return self.sink is None or self.owner != "ma"

    # -- truth -------------------------------------------------------------------

    def truth_position(self) -> float:
        """Seconds of the current item actually heard (sink) or elapsed (clock mode)."""
        if not self._clock_mode and self.sink is not None:
            return self.sink.audible.position_s
        pos = self._pos_base
        if self._playing_since is not None:
            pos += self.clock.monotonic() - self._playing_since
        return pos

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "owner": self.owner,
            "uri": self.current.uri if self.current else None,
            "next_uri": self.next_slot.uri if self.next_slot else None,
            "duration_s": self.current.duration_s if self.current else None,
            "position_s": round(self.truth_position(), 3),
            "tracks_started": self.tracks_started,
            "last_error": self.last_error,
            "sink": self.sink.snapshot() if self.sink else None,
        }

    # -- commands ----------------------------------------------------------------

    async def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def set_uri(
        self, uri: str, meta: str = "", duration_s: float | None = None, **extra: Any
    ) -> None:
        self.rec.cmd("set_uri", uri=uri, duration_s=duration_s)
        playing = self.state in (TState.PLAYING, TState.BUFFERING, TState.LOADING, TState.PAUSED)
        if playing and self.knobs.get("set_uri_stops_playback"):
            await self._stop_media()
        self.owner = "ma"
        self.current = MediaSlot(
            uri=uri, meta=meta, duration_s=duration_s, title=extra.pop("title", None), extra=extra
        )
        self._pos_base = 0.0
        self._playing_since = None
        if not playing or self.knobs.get("set_uri_stops_playback"):
            self._set_state(TState.STOPPED, reason="set_uri")

    async def set_next(
        self, uri: str | None, meta: str = "", duration_s: float | None = None, **extra: Any
    ) -> None:
        self.rec.cmd("set_next", uri=uri, duration_s=duration_s)
        self.next_slot = (
            None
            if uri is None
            else MediaSlot(
                uri=uri,
                meta=meta,
                duration_s=duration_s,
                title=extra.pop("title", None),
                extra=extra,
            )
        )

    async def play(self) -> None:
        """Start (or resume) the current item."""
        self.rec.cmd("play")
        if self.current is None:
            self.last_error = "no media"
            self._notify("error", error="no media")
            return
        if self.state == TState.PAUSED:
            if self.sink is not None and not self._clock_mode:
                self.sink.resume()
                self._set_state(
                    TState.PLAYING if self.sink.state.value == "playing" else TState.BUFFERING,
                    reason="resume",
                )
            else:
                self._set_state(TState.PLAYING, reason="resume")
            return
        if self.state in (TState.PLAYING, TState.BUFFERING, TState.LOADING):
            return
        await self._cancel_task()
        self._task = asyncio.create_task(
            self._start_current(self.knobs.get("load_delay_s"), offset_s=0.0)
        )

    async def _start_current(self, delay_s: float, offset_s: float) -> None:
        assert self.current is not None
        self._set_state(TState.LOADING, reason="play")
        if delay_s > 0:
            await self.clock.sleep(delay_s)
        self.tracks_started += 1
        self._notify("track_started", uri=self.current.uri)
        if self._clock_mode:
            self._pos_base = offset_s
            self._set_state(TState.PLAYING, reason="started")
            return
        assert self.sink is not None
        self._set_state(TState.BUFFERING, reason="opening")
        await self.sink.open(self.current.uri, offset_s=offset_s)

    async def pause(self) -> None:
        self.rec.cmd("pause")
        if self.state not in (TState.PLAYING, TState.BUFFERING):
            return
        if self.sink is not None and not self._clock_mode:
            self.sink.pause()
        self._set_state(TState.PAUSED, reason="pause")

    async def stop(self) -> None:
        self.rec.cmd("stop")
        await self._stop_media()
        if self.current is not None:
            self._set_state(TState.STOPPED, reason="stop")

    async def _stop_media(self) -> None:
        await self._cancel_task()
        if self.sink is not None and self.sink.active:
            await self.sink.close("stop")
        self._pos_base = 0.0
        self._playing_since = None

    async def seek(self, position_s: float) -> None:
        """Seek: clock mode moves the counter; sink mode reopens the stream at an offset."""
        self.rec.cmd("seek", position_s=position_s)
        if self.current is None:
            return
        if self._clock_mode:
            was_playing = self.state == TState.PLAYING
            self._playing_since = self.clock.monotonic() if was_playing else None
            self._pos_base = position_s
            self._arm_end_timer()
            return
        if self.state in (TState.PLAYING, TState.BUFFERING, TState.PAUSED, TState.LOADING):
            await self._cancel_task()
            self._task = asyncio.create_task(self._start_current(0.0, offset_s=position_s))

    async def next(self) -> bool:
        """Skip to the queued next item. Returns False when there is none."""
        self.rec.cmd("next")
        if self.next_slot is None:
            return False
        await self._advance(gap_s=0.0)
        return True

    async def _advance(self, gap_s: float) -> None:
        assert self.next_slot is not None
        await self._stop_media()
        self.current, self.next_slot = self.next_slot, None
        self._notify("advanced", uri=self.current.uri)
        self._task = asyncio.create_task(self._start_current(gap_s, offset_s=0.0))

    def take_over(self, owner: str, *, emit_event: bool = True, title: str | None = None) -> None:
        """An external source (HDMI, Spotify, AirPlay...) takes the device. Truth: it plays.

        ``emit_event`` is recorded so the family's protocol layer can decide whether to
        tell clients (e.g. ``input_switch_without_upnp_event`` = don't).
        """
        self.rec.cmd("take_over", owner=owner, emit_event=emit_event)
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self.sink is not None and self.sink.active:
            task = asyncio.ensure_future(self.sink.close(f"take_over:{owner}"))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
        self.owner = owner
        self.current = MediaSlot(uri=f"{owner}:", owner=owner, title=title)
        self.next_slot = None
        self._pos_base = 0.0
        self._playing_since = None
        if self.state == TState.PLAYING:
            self._playing_since = self.clock.monotonic()
            self.rec.state(
                "transport",
                old="playing",
                new="playing",
                uri=self.current.uri,
                owner=owner,
                emit_event=emit_event,
            )
        else:
            self._set_state(TState.PLAYING, reason="take_over", emit_event=emit_event)

    async def close(self) -> None:
        await self._cancel_task()
        if self._end_timer is not None:
            self._end_timer.cancel()
        if self.sink is not None:
            await self.sink.aclose()

    # -- endings -----------------------------------------------------------------

    def _arm_end_timer(self) -> None:
        """Clock mode: end the item when the counter reaches its duration."""
        if self._end_timer is not None:
            self._end_timer.cancel()
            self._end_timer = None
        if not self._clock_mode or self.owner != "ma" or self.state != TState.PLAYING:
            return
        dur = self.current.duration_s if self.current else None
        if dur is None:
            return
        remaining = dur - self.truth_position()
        self._end_timer = self.clock.call_later(
            max(0.0, remaining), lambda: self._track_ended("eof")
        )

    def _track_ended(self, reason: str) -> None:
        uri = self.current.uri if self.current else None
        self.rec.state(
            "track_ended", uri=uri, reason=reason, position_s=round(self.truth_position(), 3)
        )
        self._notify("track_ended", uri=uri, reason=reason)
        if self._playing_since is not None:
            self._pos_base += self.clock.monotonic() - self._playing_since
            self._playing_since = None
        if self.next_slot is not None and self.knobs.get("auto_advance") and reason != "error":
            self.current, self.next_slot = self.next_slot, None
            self._notify("advanced", uri=self.current.uri)
            self._set_state(TState.LOADING, reason="advance")
            self._task = asyncio.create_task(
                self._start_current(self.knobs.get("transition_gap_s"), offset_s=0.0)
            )
        else:
            self._set_state(TState.STOPPED, reason=reason)

    def _on_sink(self, name: str, data: dict[str, Any]) -> None:
        if self._clock_mode:
            return
        if name in ("first_audio", "rebuffered") and self.state in (
            TState.BUFFERING,
            TState.LOADING,
        ):
            self._set_state(TState.PLAYING, reason=name)
        elif name == "underrun" and self.state == TState.PLAYING:
            self._set_state(TState.BUFFERING, reason="underrun")
        elif name == "error":
            self.last_error = str(data.get("error"))
            self._notify("error", error=self.last_error)
        elif name == "ended":
            self._track_ended(str(data.get("reason", "drained")))


class ReportLens:
    """Truth -> reported values, applying the generic ``report_*`` knobs.

    Subclass and override ``position``/``duration``/``state``/``metadata`` for family
    quirks; call ``super()`` to keep the generic ones. Every lensed value that differs
    from truth is recorded once per change as ``kind="report"``.
    """

    def __init__(
        self,
        tm: TransportMachine,
        knobs: KnobSet,
        rng: SeededRng | None = None,
        surface: str = "default",
    ) -> None:
        self.tm = tm
        self.knobs = knobs
        self.rng = rng or knobs.rng
        self.surface = surface
        self._refresh_at: float | None = None
        self._refresh_val = 0.0
        self._stale: tuple[float, float] | None = None  # (until, value)
        self._last_report: dict[str, Any] = {}
        self._prev_position = 0.0
        tm.add_listener(self._on_tm)

    def _on_tm(self, name: str, data: dict[str, Any]) -> None:
        if name in ("track_started", "advanced"):
            prev = self._prev_position
            if self.knobs.fires("report_stale_after_load", surface=self.surface):
                window = float(self.knobs.param("report_stale_after_load", "window_s", 2.0))
                self._stale = (self.tm.clock.monotonic() + window, prev)

    def _differs(self, field_: str, truth: Any, reported: Any) -> Any:
        if reported != truth:
            key = (truth, reported) if not isinstance(truth, float) else (round(truth), reported)
            if self._last_report.get(field_) != key:
                self._last_report[field_] = key
                self.tm.rec.report(field_, surface=self.surface, truth=truth, reported=reported)
        else:
            self._last_report.pop(field_, None)
        return reported

    def position(self) -> float | None:
        truth = self.tm.truth_position()
        self._prev_position = truth
        if self.tm.current is None:
            return None
        now = self.tm.clock.monotonic()
        if self._stale is not None:
            until, value = self._stale
            if now < until:
                return self._differs("position", truth, value)
            self._stale = None
        if self.knobs.get("report_position_zero") and self.tm.state == TState.PLAYING:
            return self._differs("position", truth, 0.0)
        every = self.knobs.get("report_position_refresh_s")
        if every > 0:
            if self._refresh_at is None or now >= self._refresh_at:
                self._refresh_at = now + every
                self._refresh_val = truth
            return self._differs("position", truth, self._refresh_val)
        return truth

    def duration(self) -> float | None:
        cur = self.tm.current
        truth = cur.duration_s if cur else None
        if cur is not None and self.knobs.get("report_duration_zero"):
            return self._differs("duration", truth, 0.0)
        return truth

    def state(self) -> TState:
        return self.tm.state

    def metadata(self) -> MediaSlot | None:
        return self.tm.current
