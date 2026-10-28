"""HttpSink: pulls an audio stream over HTTP the way a renderer does, at playback rate.

Two loops per stream:

- the *reader* fetches the body, decodes it to seconds (+PCM), tags each ~100 ms
  window with the track whose tone it hears, and pushes it into the
  :class:`BufferModel`. In ``realtime`` mode it stops reading while the buffer is
  full, so TCP backpressure reaches the server. In ``burst`` mode it reads freely.
- the *player* drains the buffer on the clock while PLAYING, and reports
  first_audio / underrun / rebuffered / drained.

Everything the sink does is recorded as ``kind="sink"`` events. Listeners (usually the
:class:`~seamer.simkit.transport.TransportMachine`) get ``(name, data)`` callbacks for:
``opened``, ``first_audio``, ``underrun``, ``rebuffered``, ``eof``, ``cap_hit``,
``dropped``, ``drained``, ``track_change``, ``error`` and finally ``ended`` (natural end,
data has ``reason``) or ``closed`` (client-side close).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import aiohttp

from ..clock import Clock
from ..knobs import KnobSet
from ..media import estimate_tone_hz, track_for_tone
from ..recorder import Emitter
from .buffer import BufferModel, OnServerClose
from .decoder import Decoder, DecoderError, pick_decoder

SinkListener = Callable[[str, dict[str, Any]], None]
CapPolicy = Callable[[int, Mapping[str, str], int, float], tuple[int, float]]
TONE_WINDOW_S = 0.1
MAX_REFETCH = 20


class SinkState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"


@dataclass
class AudibleTruth:
    """What the speaker has actually emitted for the current stream."""

    url: str | None = None
    offset_s: float = 0.0  # stream position the sink started at (seek)
    played_s: float = 0.0
    track: int | None = None  # track whose tone is playing now
    heard: dict[str, float] = field(default_factory=dict)  # track -> seconds heard

    @property
    def position_s(self) -> float:
        return self.offset_s + self.played_s


class HttpSink:
    def __init__(
        self,
        clock: Clock,
        rec: Emitter,
        knobs: KnobSet,
        *,
        tick_s: float = 0.02,
        tone_map: Callable[[float | None], int | None] = track_for_tone,
        session: aiohttp.ClientSession | None = None,
        read_bytes: int = 16384,
    ) -> None:
        self.clock = clock
        self.rec = rec
        self.knobs = knobs
        self.tick_s = tick_s
        self.read_bytes = read_bytes  # max bytes per socket read (bounds buffer overshoot)
        self.tone_map = tone_map
        self._session = session
        self._own_session = session is None
        self._listeners: list[SinkListener] = []
        self.state = SinkState.IDLE
        self.buffer = BufferModel()
        self.audible = AudibleTruth()
        self.bytes_read = 0
        self.decoder_name: str | None = None
        self._paused_from: SinkState | None = None
        self._reader: asyncio.Task[None] | None = None
        self._player: asyncio.Task[None] | None = None
        self._eof = False
        self._started = False
        self._end_reason: str | None = None
        self._summary_sent = True
        self._pcm_window = b""
        self._pcm_rate = 44100
        self._pcm_channels = 2
        self._generation = 0
        self._decoder: Decoder | None = None
        # optional per-response cap override: (status, headers, byte_cap, time_cap) ->
        # (byte_cap, time_cap); lets a family cap only e.g. responses with a length
        self.cap_policy: CapPolicy | None = None

    # -- public API --------------------------------------------------------------

    def add_listener(self, cb: SinkListener) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    @property
    def active(self) -> bool:
        return self.state in (
            SinkState.CONNECTING,
            SinkState.BUFFERING,
            SinkState.PLAYING,
            SinkState.PAUSED,
        )

    async def open(self, url: str, offset_s: float = 0.0) -> None:
        """Start fetching ``url`` (replacing any current stream)."""
        if self.active or self._reader or self._player:
            await self.close("replaced")
        k = self.knobs
        self._generation += 1
        self.buffer = BufferModel(
            capacity_s=k.get("buffer_capacity_s"), prebuffer_s=k.get("prebuffer_s")
        )
        self.audible = AudibleTruth(url=url, offset_s=offset_s)
        self.bytes_read = 0
        self._eof = False
        self._started = False
        self._end_reason = None
        self._summary_sent = False
        self._pcm_window = b""
        self._paused_from = None
        self._set_state(SinkState.CONNECTING)
        self._reader = asyncio.create_task(self._read_loop(url, self._generation))
        self._player = asyncio.create_task(self._play_loop(self._generation))

    def pause(self) -> None:
        if self.state in (SinkState.PLAYING, SinkState.BUFFERING, SinkState.CONNECTING):
            self._paused_from = self.state
            self._set_state(SinkState.PAUSED)
            if self.knobs.get("pause_closes_socket") and self._reader and not self._eof:
                self._reader.cancel()
                self._reader = None
                self._emit("socket_closed_on_pause")

    def resume(self) -> None:
        if self.state != SinkState.PAUSED:
            return
        if self._reader is None and not self._eof and self.audible.url:
            self._reader = asyncio.create_task(
                self._read_loop(self.audible.url, self._generation, refetch=True)
            )
        target = (
            SinkState.PLAYING if self._started and self.buffer.fill_s > 0 else (SinkState.BUFFERING)
        )
        self._paused_from = None
        self._set_state(target)

    async def close(self, reason: str = "client_close") -> None:
        """Client-side close: stop reading and playing now."""
        tasks = [t for t in (self._reader, self._player) if t is not None]
        self._reader = self._player = None
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.state not in (SinkState.IDLE, SinkState.ENDED, SinkState.ERROR):
            self._emit("closed", reason=reason, unplayed_s=round(self.buffer.fill_s, 3))
            self._summary(reason)
            self._set_state(SinkState.IDLE)

    async def aclose(self) -> None:
        await self.close("shutdown")
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def wait_ended(self, timeout: float = 60.0) -> bool:
        """Wait (on the sink's clock) until the current stream is no longer active."""
        deadline = self.clock.monotonic() + timeout
        while self.active:
            if self.clock.monotonic() >= deadline:
                return False
            await self.clock.sleep(self.tick_s)
        return True

    def snapshot(self) -> dict[str, Any]:
        b = self.buffer
        return {
            "state": self.state.value,
            "url": self.audible.url,
            "position_s": round(self.audible.position_s, 3),
            "played_s": round(b.played_s, 3),
            "fill_s": round(b.fill_s, 3),
            "received_s": round(b.received_s, 3),
            "dropped_s": round(b.dropped_s, 3),
            "track": self.audible.track,
            "heard": {k: round(v, 3) for k, v in self.audible.heard.items()},
            "bytes": self.bytes_read,
            "decoder": self.decoder_name,
        }

    # -- internals ---------------------------------------------------------------

    def _emit(self, name: str, **data: Any) -> None:
        self.rec.sink(name, **data)
        for cb in list(self._listeners):
            cb(name, data)

    def _set_state(self, state: SinkState) -> None:
        if state != self.state:
            old, self.state = self.state, state
            self.rec.state("sink_state", old=old.value, new=state.value)

    def _summary(self, reason: str) -> None:
        if self._summary_sent:
            return
        self._summary_sent = True
        b = self.buffer
        self.rec.sink(
            "heard",
            reason=reason,
            url=self.audible.url,
            played_s=round(b.played_s, 3),
            received_s=round(b.received_s, 3),
            dropped_s=round(b.dropped_s, 3),
            unplayed_s=round(b.fill_s, 3),
            bytes=self.bytes_read,
            tracks={k: round(v, 3) for k, v in self.audible.heard.items()},
            decoder=self.decoder_name,
        )

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)
            )
            self._own_session = True
        return self._session

    def _on_audio(self, seconds: float, pcm: bytes | None) -> None:
        if pcm is None:
            self.buffer.push(seconds, None)
            return
        dec = self._decoder
        if dec is not None:
            self._pcm_rate, self._pcm_channels = dec.rate, dec.channels
        bps = self._pcm_rate * self._pcm_channels * 2
        window = max(2 * self._pcm_channels, int(TONE_WINDOW_S * bps) // 4 * 4)
        self._pcm_window += pcm
        while len(self._pcm_window) >= window:
            chunk, self._pcm_window = self._pcm_window[:window], self._pcm_window[window:]
            self._push_pcm(chunk, bps)

    def _push_pcm(self, chunk: bytes, bps: int) -> None:
        hz = estimate_tone_hz(chunk, self._pcm_rate, self._pcm_channels)
        self.buffer.push(len(chunk) / bps, self.tone_map(hz))

    def _flush_pcm(self) -> None:
        if self._pcm_window:
            bps = self._pcm_rate * self._pcm_channels * 2
            chunk, self._pcm_window = self._pcm_window, b""
            self._push_pcm(chunk, bps)

    async def _probe(self, session: aiohttp.ClientSession, url: str, mode: str) -> None:
        if mode == "head":
            async with session.head(url, allow_redirects=True) as resp:
                self._emit(
                    "probe",
                    method="HEAD",
                    status=resp.status,
                    content_type=resp.headers.get("Content-Type"),
                )
        elif mode == "range0":
            async with session.get(url, headers={"Range": "bytes=0-1"}) as resp:
                await resp.content.read(2)
                self._emit(
                    "probe",
                    method="GET-range0",
                    status=resp.status,
                    content_range=resp.headers.get("Content-Range"),
                )

    async def _read_loop(self, url: str, gen: int, refetch: bool = False) -> None:
        k = self.knobs
        attempts = 0
        try:
            while True:
                reason = await self._read_once(url, probe=not refetch and attempts == 0)
                if gen != self._generation:
                    return
                if reason == "eof" and k.get("on_server_close") == OnServerClose.REFETCH:
                    attempts += 1
                    if attempts <= MAX_REFETCH:
                        self._emit("refetch", attempt=attempts)
                        continue
                break
            if reason in ("eof", "cap_hit"):
                self._eof = True
                if reason == "eof" and k.get("on_server_close") == OnServerClose.DROP:
                    lost = self.buffer.drop()
                    self._emit("dropped", seconds=round(lost, 3))
                    self._end_reason = "dropped"
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, DecoderError, OSError, SinkRejected) as err:
            self._emit("error", error=repr(err))
            self._eof = True
            self._end_reason = "error"

    async def _read_once(self, url: str, probe: bool) -> str:
        k = self.knobs
        session = await self._session_get()
        if probe and k.get("probe") != "none":
            await self._probe(session, url, k.get("probe"))
        headers = {"Range": "bytes=0-"} if k.get("require_range") else {}
        byte_cap = k.get("byte_cap")
        time_cap = k.get("time_cap_s")
        realtime = k.get("read_mode") == "realtime"
        async with session.get(url, headers=headers) as resp:
            chunked = "chunked" in resp.headers.get("Transfer-Encoding", "").lower()
            ctype = resp.headers.get("Content-Type")
            self._emit(
                "opened",
                status=resp.status,
                content_type=ctype,
                chunked=chunked,
                content_length=resp.content_length,
            )
            if self.cap_policy is not None:
                byte_cap, time_cap = self.cap_policy(resp.status, resp.headers, byte_cap, time_cap)
            if resp.status >= 400:
                raise SinkRejected(f"HTTP {resp.status}")
            if k.get("reject_chunked") and chunked:
                raise SinkRejected("chunked transfer encoding rejected")
            if (
                k.get("require_range")
                and resp.status != 206
                and (resp.headers.get("Accept-Ranges", "").lower() != "bytes")
            ):
                raise SinkRejected("server does not support Range")
            decoder: Decoder = pick_decoder(ctype, k.get("decoder"), k.get("assumed_kbps"))
            self.decoder_name = decoder.name
            self._decoder = decoder
            await decoder.start(self._on_audio)
            try:
                while True:
                    while realtime and self.buffer.full:
                        await self.clock.sleep(self.tick_s)
                    data = await resp.content.read(self.read_bytes)
                    if not data:
                        break
                    if byte_cap and self.bytes_read + len(data) > byte_cap:
                        data = data[: byte_cap - self.bytes_read]
                    self.bytes_read += len(data)
                    await decoder.feed(data)
                    if (byte_cap and self.bytes_read >= byte_cap) or (
                        time_cap and self.buffer.received_s >= time_cap
                    ):
                        await decoder.finish()
                        self._flush_pcm()
                        self._emit(
                            "cap_hit",
                            bytes=self.bytes_read,
                            received_s=round(self.buffer.received_s, 3),
                        )
                        self._end_reason = "cap_hit"
                        return "cap_hit"
                await decoder.finish()
                self._flush_pcm()
            except BaseException:
                await decoder.abort()
                raise
        self._emit(
            "eof",
            bytes=self.bytes_read,
            received_s=round(self.buffer.received_s, 3),
            unplayed_s=round(self.buffer.fill_s, 3),
        )
        return "eof"

    async def _play_loop(self, gen: int) -> None:
        last = self.clock.monotonic()
        while gen == self._generation:
            await self.clock.sleep(self.tick_s)
            now = self.clock.monotonic()
            dt, last = now - last, now
            st = self.state
            b = self.buffer
            if st == SinkState.PAUSED:
                continue
            if st in (SinkState.CONNECTING, SinkState.BUFFERING):
                if b.ready or (self._eof and b.fill_s > 0):
                    self._set_state(SinkState.PLAYING)
                    if not self._started:
                        self._started = True
                        self._emit("first_audio", fill_s=round(b.fill_s, 3))
                    else:
                        self._emit("rebuffered", fill_s=round(b.fill_s, 3))
                elif self._eof and b.fill_s <= 0:
                    self._finish()
                    return
                continue
            if st == SinkState.PLAYING:
                for secs, track in b.drain(dt):
                    key = str(track) if track is not None else "unknown"
                    self.audible.heard[key] = self.audible.heard.get(key, 0.0) + secs
                    if track != self.audible.track and track is not None:
                        prev = self.audible.track
                        self.audible.track = track
                        self._emit(
                            "track_change",
                            track=track,
                            prev=prev,
                            position_s=round(self.audible.position_s, 3),
                        )
                self.audible.played_s = b.played_s
                if b.fill_s <= b.low_water_s + 1e-9:
                    if self._eof:
                        self._finish()
                        return
                    self._set_state(SinkState.BUFFERING)
                    self._emit("underrun", played_s=round(b.played_s, 3))

    def _finish(self) -> None:
        reason = self._end_reason or "drained"
        if reason == "error":
            self._set_state(SinkState.ERROR)
        else:
            if reason != "dropped":
                self._emit("drained", played_s=round(self.buffer.played_s, 3))
            self._set_state(SinkState.ENDED)
        self._summary(reason)
        self._reader = None
        self._player = None
        self._emit("ended", reason=reason, played_s=round(self.buffer.played_s, 3))


class SinkRejected(Exception):
    """The sink refused the response (status, chunked, no Range)."""
