"""Test media: one sine tone per track, so the sink can tell which track it hears.

Track ``n`` (1-based) is a sine at ``TONE_BASE_HZ + n * TONE_STEP_HZ`` Hz. The sink
estimates the tone of every ~100 ms of decoded audio by counting zero crossings and
maps it back with :func:`track_for_tone`.

:class:`MediaServer` serves files with HEAD and Range (aiohttp ``FileResponse``), and a
``/stream/<name>`` route that behaves like a live stream server: chunked, optionally
throttled, optionally cut short.
"""

from __future__ import annotations

import array
import asyncio
import math
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

TONE_BASE_HZ = 300.0
TONE_STEP_HZ = 100.0
TONE_TOLERANCE_HZ = 35.0
RATE = 44100
CHANNELS = 2
SAMPLE_BYTES = 2


def tone_for_track(n: int) -> float:
    return TONE_BASE_HZ + n * TONE_STEP_HZ


def track_for_tone(hz: float | None) -> int | None:
    """Inverse of :func:`tone_for_track`; None for silence or an unknown tone."""
    if hz is None or hz < TONE_BASE_HZ + TONE_STEP_HZ - TONE_TOLERANCE_HZ:
        return None
    n = round((hz - TONE_BASE_HZ) / TONE_STEP_HZ)
    if abs(hz - tone_for_track(n)) > TONE_TOLERANCE_HZ:
        return None
    return n


def pcm_tone(
    seconds: float, freq: float, rate: int = RATE, channels: int = CHANNELS, amplitude: float = 0.4
) -> bytes:
    """Signed 16-bit little-endian interleaved PCM of a sine tone."""
    frames = round(seconds * rate)
    amp = int(32767 * amplitude)
    step = 2 * math.pi * freq / rate
    mono = array.array("h", (int(amp * math.sin(step * i)) for i in range(frames)))
    if channels == 1:
        out = mono
    else:
        out = array.array("h", bytes(frames * channels * 2))
        for c in range(channels):
            out[c::channels] = mono
    if out.itemsize != 2:  # pragma: no cover
        raise RuntimeError("unexpected array itemsize")
    return out.tobytes()


def wav_header(data_len: int, rate: int = RATE, channels: int = CHANNELS, bits: int = 16) -> bytes:
    block = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_len)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits)
        + b"data"
        + struct.pack("<I", data_len)
    )


def write_tone_wav(
    path: Path, seconds: float, freq: float, rate: int = RATE, channels: int = CHANNELS
) -> Path:
    pcm = pcm_tone(seconds, freq, rate, channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_header(len(pcm), rate, channels) + pcm)
    return path


def ffmpeg_bin() -> str | None:
    """ffmpeg path from ``$FFMPEG`` or ``PATH`` (None if missing)."""
    import os

    env = os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    return shutil.which("ffmpeg")


def estimate_tone_hz(pcm: bytes, rate: int = RATE, channels: int = CHANNELS) -> float | None:
    """Frequency of channel 0 by zero crossings; None when (near) silent."""
    if len(pcm) < 4:
        return None
    usable = len(pcm) - len(pcm) % (2 * channels)
    samples = array.array("h", pcm[:usable])[::channels]
    if not samples or max(abs(min(samples)), max(samples)) < 300:
        return None
    crossings = 0
    prev = samples[0]
    for s in samples:
        if (prev < 0 <= s) or (prev >= 0 > s):
            crossings += 1
        prev = s
    return crossings / 2 / (len(samples) / rate)


@dataclass(frozen=True)
class TrackMedia:
    index: int  # 1-based
    path: Path
    seconds: float
    tone_hz: float

    @property
    def name(self) -> str:
        return self.path.name


class MediaFactory:
    """Creates (and caches) tone tracks in ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def track(self, index: int, seconds: float, fmt: str = "wav") -> TrackMedia:
        freq = tone_for_track(index)
        stem = f"track{index:02d}_{seconds:g}s"
        wav = self.root / f"{stem}.wav"
        if not wav.exists():
            write_tone_wav(wav, seconds, freq)
        path = wav
        if fmt != "wav":
            path = self.root / f"{stem}.{fmt}"
            if not path.exists():
                ff = ffmpeg_bin()
                if ff is None:
                    raise RuntimeError(f"ffmpeg needed to make .{fmt} media")
                subprocess.run(
                    [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), str(path)],
                    check=True,
                )
        return TrackMedia(index, path, seconds, freq)

    def album(self, tracks: int, seconds: float, fmt: str = "wav") -> list[TrackMedia]:
        return [self.track(i, seconds, fmt) for i in range(1, tracks + 1)]

    def concat_wav(self, tracks: list[TrackMedia], name: str = "flow.wav") -> Path:
        """One WAV of several tracks back to back (a stand-in for an MA flow stream)."""
        pcm = b"".join(t.path.read_bytes()[44:] for t in tracks if t.path.suffix == ".wav")
        out = self.root / name
        out.write_bytes(wav_header(len(pcm)) + pcm)
        return out


class MediaServer:
    """aiohttp routes that serve test media.

    - ``GET/HEAD /media/<name>``: static file with Range support.
    - ``GET /stream/<name>?rate=1.0&close_after_s=&chunked=1&delay_s=``: streamed body,
      paced at ``rate`` x real time (0 = as fast as possible), optionally cut after
      ``close_after_s`` seconds of audio. Counts bytes actually written per request in
      ``self.sent`` (so tests can see backpressure).
    """

    def __init__(self, root: Path, chunk_bytes: int = 8192) -> None:
        self.root = Path(root)
        self.chunk_bytes = chunk_bytes
        self.sent: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def routes(self) -> list[web.AbstractRouteDef]:
        return [
            web.get("/media/{name}", self._file, allow_head=True),
            web.get("/stream/{name}", self._stream, allow_head=False),
        ]

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes(self.routes())
        return app

    def _path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        return path

    async def _file(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {"method": request.method, "path": request.path, "range": request.headers.get("Range")}
        )
        return web.FileResponse(self._path(request.match_info["name"]))

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        path = self._path(request.match_info["name"])
        self.requests.append(
            {"method": request.method, "path": request.path, "range": request.headers.get("Range")}
        )
        q = request.query
        rate = float(q.get("rate", "1.0"))
        close_after = float(q["close_after_s"]) if "close_after_s" in q else None
        delay = float(q.get("delay_s", "0"))
        chunked = q.get("chunked", "1") != "0"
        data = path.read_bytes()
        bps = RATE * CHANNELS * SAMPLE_BYTES
        header = 44 if data[:4] == b"RIFF" else 0
        if close_after is not None:
            data = data[: header + int(close_after * bps)]
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        if chunked:
            resp.enable_chunked_encoding()
        else:
            resp.content_length = len(data)
        await resp.prepare(request)
        record = {"path": request.path, "bytes": 0, "done": False}
        self.sent.append(record)
        if delay:
            await asyncio.sleep(delay)
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            for off in range(0, len(data), self.chunk_bytes):
                chunk = data[off : off + self.chunk_bytes]
                await resp.write(chunk)
                record["bytes"] += len(chunk)
                if rate > 0:
                    due = start + max(0, record["bytes"] - header) / bps / rate
                    wait = due - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
            await resp.write_eof()
            record["done"] = True
        except (ConnectionResetError, asyncio.CancelledError):
            record["aborted"] = True
            raise
        return resp
