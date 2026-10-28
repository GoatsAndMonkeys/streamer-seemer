"""Fake audio output for Sendspin devices: byte accounting, a simulated DAC, sync metrics.

Sendspin pushes timestamped chunks; the device never fetches audio. :class:`FakeSink`
models what an ESP (sendspin-cpp) or python-cli device does with them, without decoding:

- **Byte budget** (SS21): every chunk is charged ``header + payload`` bytes against the
  advertised ``buffer_capacity`` until its end time passes on the device clock.
  A chunk that does not fit is dropped (``drop_warn``) or kills the connection.
- **Late chunks** (SS11/SS45): a chunk whose play time is already more than
  ``late_chunk_threshold_ms`` in the past is dropped undecoded.
- **Decode cost** (SS5): a single decoder works through chunks at
  ``duration * rt_factor``; when it falls behind, chunks miss their play time.
- **Simulated DAC**: plays at ``1 + dac_skew_ppm`` against the device clock. The
  corrector (cpp/spec/python_cli/none) bounds the resulting drift; the residual plus the
  time filter's error is the reported sync error.
- **Underruns**: the play head reaching the end of buffered audio while the stream is
  still open.

Chunk durations come from the format: PCM by size, FLAC from the frame header's block
size, Opus from the TOC byte; the gap to the next chunk's timestamp is the fallback.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...simkit.knobs import KnobSet
from ...simkit.recorder import Emitter

CORRECTOR_BOUND_US = {"cpp": 100.0, "spec": 100.0, "python_cli": 2000.0}


@dataclass
class StreamFormat:
    codec: str
    sample_rate: int
    channels: int
    bit_depth: int


@dataclass
class _Chunk:
    server_ts: int
    play_at: int  # device clock, us
    duration_us: int
    charge: int  # bytes held against buffer_capacity

    @property
    def end_at(self) -> int:
        return self.play_at + self.duration_us


@dataclass
class SinkMetrics:
    chunks: int = 0
    bytes: int = 0
    played_chunks: int = 0
    played_us: int = 0
    dropped_late: int = 0
    dropped_overrun: int = 0
    dropped_decode: int = 0
    underruns: int = 0
    buffer_high_water: int = 0
    first_lead_ms: float | None = None
    min_lead_ms: float | None = None
    max_sync_error_ms: float = 0.0
    streams: int = 0
    clears: int = 0
    header_sizes: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["header_sizes"] = sorted(set(self.header_sizes))
        return d


def flac_block_size(frame: bytes) -> int | None:
    """Samples per channel in a FLAC frame, from its header (None if not a frame)."""
    if len(frame) < 4 or frame[0] != 0xFF or (frame[1] & 0xFE) != 0xF8:
        return None
    code = frame[2] >> 4
    if code == 1:
        return 192
    if 2 <= code <= 5:
        return 576 << (code - 2)
    if 8 <= code <= 15:
        return 256 << (code - 8)
    if code in (6, 7):
        # after the 4 fixed bytes comes a UTF-8-style coded frame/sample number
        lead = frame[4] if len(frame) > 4 else 0
        n = 1
        while n < 7 and lead & (0x80 >> n - 1) and lead & (0x80 >> n):
            n += 1
        if lead & 0x80 == 0:
            n = 1
        pos = 4 + n
        if code == 6 and len(frame) > pos:
            return frame[pos] + 1
        if code == 7 and len(frame) > pos + 1:
            return (frame[pos] << 8 | frame[pos + 1]) + 1
    return None


_OPUS_FRAME_US = (
    [10_000, 20_000, 40_000, 60_000] * 3  # SILK NB/MB/WB (configs 0-11)
    + [10_000, 20_000] * 2  # hybrid SWB/FB (12-15)
    + [2_500, 5_000, 10_000, 20_000] * 4  # CELT (16-31)
)


def opus_packet_us(packet: bytes) -> int | None:
    """Duration of one Opus packet from its TOC byte (RFC 6716 §3.1)."""
    if not packet:
        return None
    toc = packet[0]
    frame_us = _OPUS_FRAME_US[toc >> 3]
    code = toc & 0x3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        if len(packet) < 2:
            return None
        frames = packet[1] & 0x3F
    return frame_us * frames


def chunk_duration_us(fmt: StreamFormat, payload: bytes) -> int | None:
    if fmt.codec == "pcm":
        frame_bytes = fmt.channels * ((fmt.bit_depth + 7) // 8)
        if frame_bytes <= 0 or fmt.sample_rate <= 0:
            return None
        return len(payload) // frame_bytes * 1_000_000 // fmt.sample_rate
    if fmt.codec == "flac":
        n = flac_block_size(payload)
        return None if n is None else n * 1_000_000 // fmt.sample_rate
    if fmt.codec == "opus":
        return opus_packet_us(payload)
    return None


class FakeSink:
    """Accounts and "plays" chunks against the device clock (``now_us``)."""

    def __init__(
        self,
        now_us: Callable[[], int],
        knobs: KnobSet,
        rec: Emitter,
        *,
        on_overrun_close: Callable[[], None] | None = None,
        on_underrun: Callable[[], None] | None = None,
    ) -> None:
        self.now_us = now_us
        self.knobs = knobs
        self.rec = rec
        self.on_overrun_close = on_overrun_close
        self.on_underrun = on_underrun
        self.capacity = int(knobs.get("buffer_capacity"))
        self.fmt: StreamFormat | None = None
        self.metrics = SinkMetrics()
        self._queue: deque[_Chunk] = deque()
        self._used = 0
        self._decoder_free_at = 0
        self._started_at: int | None = None  # device clock when the DAC started
        self._playing = False
        self._underrun_flagged = False
        self.last_played_server_ts: int | None = None
        self.filter_error_us = 0

    # -- stream lifecycle ------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.fmt is not None

    @property
    def used_bytes(self) -> int:
        return self._used

    @property
    def playing(self) -> bool:
        return self._playing

    def start(self, fmt: StreamFormat) -> None:
        if self.fmt is not None and self.fmt != fmt:
            self.rec.sink("format_change", old=self.fmt.__dict__, new=fmt.__dict__)
        self.fmt = fmt
        self.metrics.streams += 1
        self._underrun_flagged = False
        self.rec.sink("stream_start", **fmt.__dict__)

    def clear(self) -> None:
        self.metrics.clears += 1
        self._flush("clear")

    def end(self) -> None:
        self._flush("end")
        self.fmt = None

    def _flush(self, why: str) -> None:
        self._queue.clear()
        self._used = 0
        self._decoder_free_at = 0
        if self._playing:
            self.rec.sink("stopped", reason=why)
        self._playing = False
        self._started_at = None

    # -- chunks ----------------------------------------------------------------------

    def on_chunk(self, server_ts: int, payload: bytes, header_len: int, play_at: int) -> bool:
        """Offer one chunk; returns whether it was buffered."""
        m = self.metrics
        m.chunks += 1
        m.bytes += len(payload)
        m.header_sizes.append(header_len)
        if self.fmt is None:
            self.rec.sink("chunk_without_stream", server_ts=server_ts)
            return False
        now = self.now_us()
        self._release(now)
        lead_ms = (play_at - now) / 1000
        if m.first_lead_ms is None:
            m.first_lead_ms = round(lead_ms, 3)
            self.rec.sink("first_chunk", lead_ms=round(lead_ms, 3), header_len=header_len)
        m.min_lead_ms = lead_ms if m.min_lead_ms is None else min(m.min_lead_ms, lead_ms)
        dur = chunk_duration_us(self.fmt, payload)
        if dur is None:
            dur = self._duration_from_gap(server_ts)
        threshold_us = self.knobs.get("late_chunk_threshold_ms") * 1000
        if self.knobs.get("drop_late_chunks") and play_at < now - threshold_us:
            m.dropped_late += 1
            self.rec.sink("late_drop", server_ts=server_ts, late_ms=round(-lead_ms, 3))
            return False
        charge = header_len + len(payload)
        if self._used + charge > self.capacity:
            m.dropped_overrun += 1
            self.rec.sink("overrun", used=self._used, charge=charge, capacity=self.capacity)
            if self.knobs.get("ring_overrun_policy") == "close_connection" and (
                self.on_overrun_close is not None
            ):
                self.on_overrun_close()
            return False
        # decoding: one decoder, chunks in arrival order
        factor_knob = "opus_decode_rt_factor" if self.fmt.codec == "opus" else "decode_rt_factor"
        cost = int(dur * float(self.knobs.get(factor_knob)))
        decode_done = max(now, self._decoder_free_at) + cost
        self._decoder_free_at = decode_done
        if decode_done > play_at + threshold_us:
            m.dropped_decode += 1
            self.rec.sink(
                "decode_late", server_ts=server_ts, late_ms=round((decode_done - play_at) / 1000, 3)
            )
            return False
        self._queue.append(_Chunk(server_ts, play_at, dur, charge))
        self._used += charge
        m.buffer_high_water = max(m.buffer_high_water, self._used)
        self._underrun_flagged = False
        return True

    def _duration_from_gap(self, server_ts: int) -> int:
        if self._queue:
            gap = server_ts - self._queue[-1].server_ts
            if 0 < gap < 1_000_000:
                return gap
        return 20_000

    # -- the DAC ---------------------------------------------------------------------

    def tick(self) -> None:
        """Advance the DAC to now (call periodically)."""
        self._release(self.now_us())

    def _release(self, now: int) -> None:
        if self.knobs.get("fault_mode") == "never_consume":
            return
        if not self._playing and self._queue:
            buffered = self._queue[-1].end_at - max(now, self._queue[0].play_at)
            gate_us = self.knobs.get("start_gate_ms") * 1000
            if now >= self._queue[0].play_at and buffered >= min(gate_us, buffered + 1):
                self._playing = True
                self._started_at = now
                self.rec.sink("first_audio", server_ts=self._queue[0].server_ts)
        while self._queue and self._queue[0].end_at <= now:
            c = self._queue.popleft()
            self._used -= c.charge
            self.metrics.played_chunks += 1
            self.metrics.played_us += c.duration_us
            self.last_played_server_ts = c.server_ts + c.duration_us
        if self._playing:
            self._update_sync_error(now)
            if not self._queue and self.fmt is not None and not self._underrun_flagged:
                self._underrun_flagged = True
                self._playing = False
                self.metrics.underruns += 1
                self.rec.sink("underrun", played_ms=self.metrics.played_us // 1000)
                if self.on_underrun is not None:
                    self.on_underrun()

    def _update_sync_error(self, now: int) -> None:
        assert self._started_at is not None
        elapsed = now - self._started_at
        drift = abs(self.knobs.get("dac_skew_ppm")) * 1e-6 * elapsed
        bound = CORRECTOR_BOUND_US.get(self.knobs.get("corrector"))
        if bound is not None:
            drift = min(drift, bound)
        err_us = drift + abs(self.filter_error_us)
        err_us += abs(self.knobs.get("output_latency_error_ms")) * 1000
        self.metrics.max_sync_error_ms = max(self.metrics.max_sync_error_ms, err_us / 1000)

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "playing": self._playing,
            "format": self.fmt.__dict__ if self.fmt else None,
            "used_bytes": self._used,
            "capacity": self.capacity,
            "queued_chunks": len(self._queue),
            "metrics": self.metrics.as_dict(),
        }
