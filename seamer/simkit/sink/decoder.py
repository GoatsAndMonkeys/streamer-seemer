"""Turn stream bytes into seconds of audio (and PCM, when we can decode it).

- :class:`PcmDecoder`: exact for WAV and raw PCM (``audio/L16``), parses the RIFF header.
- :class:`FfmpegDecoder`: pipes the body through ``ffmpeg -f s16le`` and counts frames;
  exact for FLAC/MP3/AAC/... Honours ``$FFMPEG`` then ``PATH``.
- :class:`BitrateEstimate`: bytes x assumed bitrate; no PCM, so no tone detection.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable

from ..media import CHANNELS, RATE, ffmpeg_bin

# seconds of audio, plus s16le interleaved PCM when available
AudioCallback = Callable[[float, bytes | None], None]


class DecoderError(Exception):
    pass


class Decoder(ABC):
    name = "base"
    rate = RATE
    channels = CHANNELS

    def __init__(self) -> None:
        self._on_audio: AudioCallback | None = None

    async def start(self, on_audio: AudioCallback) -> None:
        self._on_audio = on_audio

    def _emit(self, seconds: float, pcm: bytes | None) -> None:
        if self._on_audio is not None and seconds > 0:
            self._on_audio(seconds, pcm)

    @abstractmethod
    async def feed(self, data: bytes) -> None: ...

    async def finish(self) -> None:
        """End of input: flush everything still inside the decoder."""

    async def abort(self) -> None:
        """Stop now, discarding anything not yet emitted."""


class PcmDecoder(Decoder):
    """WAV (auto-detected by ``RIFF``) or raw s16le PCM."""

    name = "pcm"

    def __init__(
        self,
        rate: int = RATE,
        channels: int = CHANNELS,
        bits: int = 16,
        expect_wav: bool | None = None,
    ) -> None:
        super().__init__()
        self.rate, self.channels, self.bits = rate, channels, bits
        self._expect_wav = expect_wav
        self._header = b""
        self._in_data = False
        self._data_left: int | None = None
        self._carry = b""

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.bits // 8

    def _parse_header(self) -> bool:
        """Consume RIFF chunks from ``_header`` until ``data``. False if more bytes needed."""
        buf = self._header
        if len(buf) < 12:
            return False
        if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
            raise DecoderError("not a RIFF/WAVE stream")
        pos = 12
        while True:
            if len(buf) < pos + 8:
                return False
            cid = buf[pos : pos + 4]
            size = struct.unpack("<I", buf[pos + 4 : pos + 8])[0]
            if cid == b"data":
                self._in_data = True
                # streaming servers often write 0 or 0xFFFFFFFF as the data size
                self._data_left = None if size in (0, 0xFFFFFFFF) else size
                rest = buf[pos + 8 :]
                self._header = b""
                if rest:
                    self._pcm(rest)
                return True
            if len(buf) < pos + 8 + size:
                return False
            if cid == b"fmt ":
                fmt, ch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", buf[pos + 8 : pos + 24])
                if fmt not in (1, 0xFFFE):
                    raise DecoderError(f"unsupported WAV format {fmt}")
                self.channels, self.rate, self.bits = ch, rate, bits
            pos += 8 + size + (size & 1)

    def _pcm(self, data: bytes) -> None:
        if self._data_left is not None:
            data = data[: self._data_left]
            self._data_left -= len(data)
        data = self._carry + data
        usable = len(data) - len(data) % self.frame_bytes
        self._carry = data[usable:]
        if usable:
            pcm = data[:usable]
            secs = usable / (self.frame_bytes * self.rate)
            self._emit(secs, pcm if self.bits == 16 else None)

    async def feed(self, data: bytes) -> None:
        if self._in_data:
            self._pcm(data)
            return
        if self._expect_wav is False:
            self._in_data = True
            self._pcm(data)
            return
        self._header += data
        if self._expect_wav is None and len(self._header) >= 4 and self._header[:4] != b"RIFF":
            self._in_data, data, self._header = True, self._header, b""
            self._pcm(data)
            return
        self._parse_header()


class BitrateEstimate(Decoder):
    name = "bitrate"

    def __init__(self, kbps: float = 320.0) -> None:
        super().__init__()
        self.bytes_per_s = kbps * 1000 / 8

    async def feed(self, data: bytes) -> None:
        self._emit(len(data) / self.bytes_per_s, None)


class FfmpegDecoder(Decoder):
    name = "ffmpeg"

    def __init__(
        self,
        ffmpeg: str | None = None,
        rate: int = RATE,
        channels: int = CHANNELS,
        read_bytes: int = 8820,
    ) -> None:
        super().__init__()
        exe = ffmpeg or ffmpeg_bin()
        if exe is None:
            raise DecoderError("ffmpeg not found (set $FFMPEG or PATH)")
        self.exe = exe
        self.rate, self.channels = rate, channels
        self.read_bytes = read_bytes
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._carry = b""

    async def start(self, on_audio: AudioCallback) -> None:
        await super().start(on_audio)
        self._proc = await asyncio.create_subprocess_exec(
            self.exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-probesize",
            "32768",
            # single-threaded decode and per-packet flushes keep the audio ffmpeg holds
            # back small: frame threading kept ~2 s of a paced FLAC stream until EOF,
            # which a drop-on-close sink then counted as dropped
            "-threads",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ar",
            str(self.rate),
            "-ac",
            str(self.channels),
            "-flush_packets",
            "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        fb = self.channels * 2
        while chunk := await self._proc.stdout.read(self.read_bytes):
            data = self._carry + chunk
            usable = len(data) - len(data) % fb
            self._carry = data[usable:]
            if usable:
                self._emit(usable / (fb * self.rate), data[:usable])

    async def feed(self, data: bytes) -> None:
        assert self._proc is not None
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as err:
            raise DecoderError(f"ffmpeg exited: {err!r}") from err

    async def finish(self) -> None:
        if self._proc is None:
            return
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        if self._reader is not None:
            await self._reader
        await self._proc.wait()

    async def abort(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()


PCM_TYPES = ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/l16", "audio/pcm")


def pick_decoder(content_type: str | None, choice: str = "auto", kbps: float = 320.0) -> Decoder:
    """Pick a decoder for a response ``Content-Type`` (or a forced ``choice``)."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if choice == "pcm" or (choice == "auto" and ctype in PCM_TYPES):
        if ctype == "audio/l16":
            params = dict(
                p.strip().split("=", 1) for p in (content_type or "").split(";")[1:] if "=" in p
            )
            return PcmDecoder(
                rate=int(params.get("rate", RATE)),
                channels=int(params.get("channels", 1)),
                expect_wav=False,
            )
        return PcmDecoder()
    if choice == "bitrate":
        return BitrateEstimate(kbps)
    if choice == "ffmpeg" or ffmpeg_bin() is not None:
        return FfmpegDecoder()
    return BitrateEstimate(kbps)
