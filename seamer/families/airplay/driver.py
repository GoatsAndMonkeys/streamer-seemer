"""An MA-like driver for the fake ``cliairplay``: tests run the helper without MA.

It does what MA's ``AirPlayStream`` does: make the command FIFO, spawn the helper with
the same arguments, feed PCM on stdin, write ``KEY=VALUE`` / ``ACTION=`` commands and
collect the ``[STATUS]`` lines (with arrival times) from stdout and stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Any

from ...simkit.media import pcm_tone, tone_for_track
from . import fake_cli


class CliDriver:
    def __init__(self, workdir: str | os.PathLike[str], binary: str | None = None) -> None:
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.binary = binary
        self.proc: asyncio.subprocess.Process | None = None
        self.lines: list[tuple[float, str, str]] = []  # (time, stream, line)
        self._new_line = asyncio.Event()
        self._fifo_fd: int | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self.fifo = self.workdir / f"cmd-{os.getpid()}-{id(self)}.fifo"
        self.started_at = 0.0

    # -- process -------------------------------------------------------------------------

    def argv(
        self,
        host: str,
        port: int,
        *,
        protocol: str = "auto",
        samplerate: int = 44100,
        bitdepth: int = 16,
        dacp: str = "A1B2C3D4E5F60708",
        active_remote: str = "1234567890",
        extra: tuple[str, ...] = (),
    ) -> list[str]:
        cmd = [self.binary] if self.binary else [sys.executable, fake_cli.__file__]
        return [
            *cmd,
            "--protocol",
            protocol,
            "--dacp",
            dacp,
            "--activeremote",
            active_remote,
            "--cmdpipe",
            str(self.fifo),
            "--samplerate",
            str(samplerate),
            "--bitdepth",
            str(bitdepth),
            "--clock-offset-ns",
            "0",
            "--port",
            str(port),
            "--name",
            "Seamer Test",
            *extra,
            host,
        ]

    async def start(self, host: str, port: int, **kw: Any) -> None:
        if self.fifo.exists():
            self.fifo.unlink()
        os.mkfifo(self.fifo)
        self.started_at = time.time()
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(host, port, **kw),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self.proc.stdout
        assert self.proc.stderr
        self._tasks += [
            asyncio.create_task(self._collect(self.proc.stdout, "out")),
            asyncio.create_task(self._collect(self.proc.stderr, "err")),
        ]

    async def _collect(self, stream: asyncio.StreamReader, name: str) -> None:
        while line := await stream.readline():
            self.lines.append((time.time(), name, line.decode(errors="replace").rstrip()))
            self._new_line.set()

    async def wait_line(self, needle: str, timeout: float = 10.0, after: int = 0) -> str:
        """Wait for a line containing ``needle`` (searching from index ``after``)."""
        deadline = time.monotonic() + timeout
        while True:
            for _t, _s, line in self.lines[after:]:
                if needle in line:
                    return line
            left = deadline - time.monotonic()
            if left <= 0 or (self.proc is not None and self.proc.returncode is not None):
                if self.proc is not None and self.proc.returncode is not None:
                    await asyncio.sleep(0.05)  # let the readers drain
                    for _t, _s, line in self.lines[after:]:
                        if needle in line:
                            return line
                raise TimeoutError(f"no line with {needle!r}; got:\n" + self.dump())
            self._new_line.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new_line.wait(), min(left, 0.2))

    def time_of(self, needle: str) -> float | None:
        for t, _s, line in self.lines:
            if needle in line:
                return t
        return None

    def has(self, needle: str) -> bool:
        return any(needle in line for _t, _s, line in self.lines)

    def dump(self) -> str:
        return "\n".join(f"{t - self.started_at:7.3f} {s} {line}" for t, s, line in self.lines)

    async def wait_exit(self, timeout: float = 10.0) -> int:
        assert self.proc is not None
        return await asyncio.wait_for(self.proc.wait(), timeout)

    # -- commands --------------------------------------------------------------------------

    async def _open_fifo(self, timeout: float = 5.0) -> int:
        if self._fifo_fd is not None:
            return self._fifo_fd
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._fifo_fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
                return self._fifo_fd
            except OSError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.05)

    async def send(self, *lines: str) -> None:
        """One command (several lines) in a single write, as MA does."""
        fd = await self._open_fifo()
        os.write(fd, ("\n".join(lines) + "\n").encode())

    async def start_at(self, delay_ms: int = 500, join: bool = False) -> int:
        at = int(time.time() * 1000) + delay_ms
        lines = [f"START_UNIX_MS={at}"] + (["START_JOIN=1"] if join else [])
        await self.send(*lines, "ACTION=START")
        return at

    def feed(self, pcm: bytes, close: bool = False) -> asyncio.Task[None]:
        """Write PCM to the helper's stdin in the background (backpressure applies)."""
        assert self.proc is not None
        assert self.proc.stdin is not None
        stdin = self.proc.stdin

        async def _write() -> None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                for i in range(0, len(pcm), 16384):
                    stdin.write(pcm[i : i + 16384])
                    await stdin.drain()
                if close:
                    stdin.close()

        task = asyncio.create_task(_write())
        self._tasks.append(task)
        return task

    def feed_track(self, track: int, seconds: float, close: bool = False) -> asyncio.Task[None]:
        return self.feed(pcm_tone(seconds, tone_for_track(track)), close=close)

    async def stop(self, timeout: float = 5.0) -> int | None:
        """MA's stop(): ACTION=STOP, EOF on stdin, then kill if it lingers."""
        if self.proc is None:
            return None
        if self.proc.returncode is None:
            with contextlib.suppress(OSError):
                await self.send("ACTION=STOP")
            if self.proc.stdin is not None:
                with contextlib.suppress(Exception):
                    self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        await self.close()
        return self.proc.returncode

    async def close(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t
        if self._fifo_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fifo_fd)
            self._fifo_fd = None
        with contextlib.suppress(OSError):
            self.fifo.unlink()
