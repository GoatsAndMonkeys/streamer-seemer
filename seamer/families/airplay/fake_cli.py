"""A fake ``cliairplay`` helper: the process Music Assistant spawns for every AirPlay stream.

Standard library only, so it runs under any Python 3.11+ (MA's interpreter included).
:func:`install` writes an executable wrapper for it; point MA at that path (see
``docs/families/airplay.md``).

Contract reproduced (MA ``providers/airplay/stream.py``):

- ``--check`` prints a line with ``cliairplay`` and ``check``, exit 0.
- ``--ptp-daemon`` prints ``[PTP] daemon up`` and waits for SIGTERM.
- Otherwise: ``<bin> --protocol P --dacp ID --activeremote AR --cmdpipe FIFO --samplerate R
  --bitdepth B ... <address>``. It connects to the receiver, prints ``[STATUS] connected``
  (stderr), opens the command FIFO, reads PCM on stdin and reports progress with
  ``[STATUS]`` lines. Commands on the FIFO are ``KEY=VALUE`` lines; ``ACTION=...`` ends a
  command, ``VOLUME=``/``PROGRESS=``/``ARTWORK=`` stand alone.

The "receiver" is a seamer :class:`~seamer.families.airplay.device.AirplayDevice` at
``<address>:<port>``: instead of RTSP/RTP the helper calls its ``/_wire/*`` JSON side
channel, and serves the PCM it got on stdin as ``audio/L16`` over HTTP so the device's
sink pulls (and tone-checks) exactly what MA sent. The device's knobs decide every
answer and delay, so the helper's own timing follows the profile.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any

CHECK_LINE = "cliairplay check ok (seamer fake)"
RING_SECONDS = 3.0  # how far ahead of playout the helper buffers stdin
TICK_S = 0.5  # cadence of '[STATUS] playing elapsed_ms=' lines


def now_ms() -> int:
    return int(time.time() * 1000)


def err(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="cliairplay", add_help=False)
    p.add_argument("--check", action="store_true")
    p.add_argument("--ptp-daemon", action="store_true")
    p.add_argument("--ptp-shared", action="store_true")
    p.add_argument("--encrypt", action="store_true")
    for name in (
        "protocol",
        "dacp",
        "activeremote",
        "cmdpipe",
        "timing",
        "name",
        "hostname",
        "udn",
        "et",
        "am",
        "pk",
        "pw",
        "cn",
        "md",
        "txt",
        "auth",
        "secret",
        "password",
        "if",
        "publish-ip",
        "debug",
    ):
        p.add_argument(f"--{name}", default=None)
    p.add_argument("--samplerate", type=int, default=44100)
    p.add_argument("--bitdepth", type=int, default=16)
    p.add_argument("--clock-offset-ns", type=int, default=0)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--latency", type=int, default=0)
    p.add_argument("address", nargs="?", default=None)
    args, _unknown = p.parse_known_args(argv)
    return args


async def http_json(
    host: str, port: int, method: str, path: str, payload: Any = None, timeout: float = 15.0
) -> tuple[int, Any]:
    """Minimal HTTP/1.1 JSON request (stdlib only). Returns (status, decoded body)."""
    body = b"" if payload is None else json.dumps(payload).encode()
    req = (
        f"{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUser-Agent: AirPlay/fake\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body

    async def _do() -> tuple[int, Any]:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(req)
            await writer.drain()
            raw = await reader.read()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        head, _, rest = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        headers = {
            k.strip().lower(): v.strip()
            for k, _, v in (ln.partition(b":") for ln in head.split(b"\r\n")[1:])
        }
        if b"chunked" in headers.get(b"transfer-encoding", b""):
            rest = _dechunk(rest)
        try:
            data = json.loads(rest) if rest else None
        except ValueError:
            data = rest.decode(errors="replace")
        return status, data

    return await asyncio.wait_for(_do(), timeout)


def _dechunk(data: bytes) -> bytes:
    out_b = b""
    while data:
        size_s, _, data = data.partition(b"\r\n")
        size = int(size_s.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out_b += data[:size]
        data = data[size + 2 :]
    return out_b


class FakeCli:
    """One stream helper process."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.host = args.address or "127.0.0.1"
        self.port = args.port or 7000
        self.rate = args.samplerate
        self.in_width = 2 if args.bitdepth <= 16 else 4  # MA sends 24-bit as s32le
        self.bps = self.rate * 2 * 2  # served as s16le stereo
        self.ring = bytearray()
        self.ring_space = asyncio.Event()
        self.ring_data = asyncio.Event()
        self.stdin_eof = False
        self.fed_bytes = 0  # since the last FLUSH (s16le)
        self.gen = 0
        self.audio_armed = True
        self.session: str | None = None
        self.cfg: dict[str, Any] = {}
        self.cli: dict[str, Any] = {}
        self.anchor_ms: int | None = None  # current START anchor (audible instant)
        self.paused = False
        self.elapsed_acc = 0.0  # seconds played before the current resume
        self.resumed_at: float | None = None  # wall time playout (re)started
        self.eof_sent = False
        self.skip_bytes = 0
        self.done: asyncio.Future[int] = asyncio.get_event_loop().create_future()
        self.tasks: list[asyncio.Task[Any]] = []
        self.fifo_fds: list[int] = []
        self.pcm_server: asyncio.base_events.Server | None = None
        self.pending: dict[str, str] = {}
        self.cmd_buf = b""

    # -- wire ----------------------------------------------------------------------------

    async def wire(self, op: str, **payload: Any) -> tuple[int, Any]:
        payload.setdefault("session", self.session)
        try:
            return await http_json(self.host, self.port, "POST", f"/_wire/{op}", payload)
        except (OSError, TimeoutError, ValueError, IndexError) as exc:
            return 0, {"ok": False, "detail": repr(exc)}

    # -- lifecycle -----------------------------------------------------------------------

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._on_signal)
        self.pcm_server = await asyncio.start_server(self._serve_pcm, "127.0.0.1", 0)
        pcm_port = self.pcm_server.sockets[0].getsockname()[1]
        self.tasks.append(asyncio.create_task(self._read_stdin()))
        status, resp = await self.wire(
            "connect",
            session=None,
            protocol=self.a.protocol or "auto",
            timing=self.a.timing,
            dacp_id=self.a.dacp,
            active_remote=self.a.activeremote,
            name=self.a.name,
            samplerate=self.rate,
            bitdepth=self.a.bitdepth,
            password=self.a.password,
            has_auth=bool(self.a.auth),
            pcm_base=f"http://127.0.0.1:{pcm_port}",
            ptp_shared=self.a.ptp_shared,
        )
        if not isinstance(resp, dict) or not resp.get("ok"):
            info = resp if isinstance(resp, dict) else {}
            code = info.get("code", "connect_failed")
            http = info.get("http", status if status >= 400 else 0)
            detail = info.get("detail", "receiver refused the connection")
            err(f"[ERROR] connect to {self.host}:{self.port} failed")
            err(f'[STATUS] error code={code} http={http} detail="{detail}"')
            return 1
        self.cfg = resp
        self.cli = resp.get("cli", {})
        self.session = resp["session"]
        connected_ms = now_ms()
        err("[STATUS] connected")
        proto = resp["protocol"]
        timing = resp["timing"]
        out(
            f"[STATUS] route protocol={proto} flow=native timing={timing} "
            f"buffered={int(bool(resp.get('buffered')))}"
        )
        lead = int(resp.get("latency_ms", 2000))
        out(
            f"[STATUS] latency lead_ms={lead} device_min_frames={self.rate // 10} "
            f"device_max_frames={self.rate * 4} warm_lead_ms={min(lead, 400)}"
        )
        out(
            "[STATUS] capabilities requested=0x40000 realtime_formats=0x40000 realtime_known=1 "
            "buffered_formats=0x0 buffered_known=0"
        )
        self._open_fifo()
        self.tasks += [
            asyncio.create_task(self._clock_timeline(connected_ms)),
            asyncio.create_task(self._ticker()),
            asyncio.create_task(self._events()),
        ]
        if die := int(self.cli.get("die_after_ms", 0)):
            self.tasks.append(asyncio.create_task(self._die_after(die / 1000)))
        code = await self.done
        await self._cleanup()
        return code

    def _finish(self, code: int) -> None:
        if not self.done.done():
            self.done.set_result(code)

    def _on_signal(self) -> None:
        if self.cli.get("ignore_sigterm"):
            err("[DEBUG] SIGTERM ignored")
            return
        self._finish(0)

    async def _die_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        os._exit(1)  # no stderr, like a crashed helper (ca2:A52)

    async def _cleanup(self) -> None:
        for t in self.tasks:
            t.cancel()
        loop = asyncio.get_running_loop()
        for fd in self.fifo_fds:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        if self.pcm_server is not None:
            self.pcm_server.close()

    # -- stdin PCM -------------------------------------------------------------------------

    def _to_s16(self, chunk: bytes) -> bytes:
        if self.in_width == 2:
            return chunk
        mv = memoryview(chunk)[: len(chunk) - len(chunk) % 4]
        out_b = bytearray(len(mv) // 2)
        out_b[0::2] = mv[2::4]
        out_b[1::2] = mv[3::4]
        return bytes(out_b)

    async def _read_stdin(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=1 << 20)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(0, "rb", buffering=0)
        )
        cap = int(RING_SECONDS * self.bps)
        carry = b""
        while True:
            while len(self.ring) >= cap:
                self.ring_space.clear()
                await self.ring_space.wait()
            chunk = await reader.read(16384)
            if not chunk:
                break
            chunk = carry + chunk
            usable = len(chunk) - len(chunk) % (self.in_width * 2)
            carry = chunk[usable:]
            pcm = self._to_s16(chunk[:usable])
            if self.skip_bytes:
                cut = min(self.skip_bytes, len(pcm))
                self.skip_bytes -= cut
                pcm = pcm[cut:]
            self.ring += pcm
            self.fed_bytes += len(pcm)
            self.ring_data.set()
            self._maybe_audio_present()
        self.stdin_eof = True
        self.ring_data.set()
        self._maybe_audio_present()
        if self.cli.get("ignore_sigterm"):
            return
        if self.session is None:
            self._finish(0)

    def _maybe_audio_present(self) -> None:
        if not self.audio_armed or self.session is None:
            return
        if len(self.ring) >= self.bps // 10 or (self.stdin_eof and self.ring):
            self.audio_armed = False
            err(f"[STATUS] audio buffered_ms={len(self.ring) * 1000 // self.bps}")

    # -- PCM served to the receiver ------------------------------------------------------

    async def _serve_pcm(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            line = head.split(b"\r\n", 1)[0].decode()
            method, target = line.split(" ")[:2]
            gen = int(target.split("/")[-1].split("?")[0].removesuffix(".pcm") or "0")
            if "skip_ms=" in target:
                skip = int(target.split("skip_ms=")[1].split("&")[0])
                drop = min(len(self.ring), skip * self.bps // 1000 // 4 * 4)
                del self.ring[:drop]
                self.ring_space.set()
            ctype = f"audio/L16;rate={self.rate};channels=2"
            writer.write(
                (
                    f"HTTP/1.0 200 OK\r\nContent-Type: {ctype}\r\nCache-Control: no-cache\r\n\r\n"
                ).encode()
            )
            if method == "HEAD":
                return
            while gen == self.gen:
                if not self.ring:
                    if self.stdin_eof:
                        break
                    self.ring_data.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.ring_data.wait(), 0.5)
                    continue
                n = min(len(self.ring), 8192)
                data = bytes(self.ring[:n])
                del self.ring[:n]
                self.ring_space.set()
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError, ValueError, IndexError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # -- clock -----------------------------------------------------------------------------

    async def _clock_timeline(self, connected_ms: int) -> None:
        mode = self.cfg["timing"]
        if mode == "ntp":
            err(
                f"[STATUS] clock_ready mode=ntp state=ready streak_ms=0 exchanges=0 "
                f"ready_in_ms=0 ready_at_unix_ms={connected_ms}"
            )
            return
        first = int(self.cfg.get("first_probe_ms", 60))
        lock = max(first, int(self.cfg.get("lock_ms", 200)))
        err("[STATUS] clock_ready mode=ptp state=cold streak_ms=0 exchanges=0")
        await asyncio.sleep(first / 1000)
        if self.cfg.get("clock_stalled"):
            err(
                "[STATUS] clock_ready mode=ptp state=stalled streak_ms=0 exchanges=0 "
                "ready_in_ms=0 ready_at_unix_ms=0"
            )
            return
        ready_at = connected_ms + lock
        err(
            f"[STATUS] clock_ready mode=ptp state=probing streak_ms=0 exchanges=1 "
            f"ready_in_ms={ready_at - now_ms()} ready_at_unix_ms={ready_at}"
        )
        await asyncio.sleep(max(0.0, (ready_at - now_ms()) / 1000))
        exchanges = max(2, lock // 125)
        err(
            f"[STATUS] clock_ready mode=ptp state=ready streak_ms={lock - first} "
            f"exchanges={exchanges} ready_in_ms=0 ready_at_unix_ms={ready_at}"
        )
        err("[STATUS] clock_verified margin_ms=40")

    # -- playout accounting ----------------------------------------------------------------

    def elapsed_s(self) -> float:
        if self.resumed_at is None:
            return self.elapsed_acc
        return self.elapsed_acc + max(0.0, time.time() - self.resumed_at)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            if self.resumed_at is None or time.time() < self.resumed_at:
                continue
            el = self.elapsed_s()
            fed_s = self.fed_bytes / self.bps
            if self.stdin_eof and el >= fed_s:
                el = fed_s
            err(f"[STATUS] playing elapsed_ms={int(el * 1000)}")
            if self.stdin_eof and not self.ring and el >= fed_s and not self.eof_sent:
                self.eof_sent = True
                self.elapsed_acc, self.resumed_at = fed_s, None
                err("[STATUS] eof")
                if self.cli.get("exit_after_eof"):
                    self._finish(0)

    async def _events(self) -> None:
        """Long-poll the receiver for things it does on its own (control reset)."""
        while True:
            status, resp = await self.wire("events", timeout=20)
            if status == 0:
                await asyncio.sleep(1)
                continue
            if self.session is None:
                return
            for ev in (resp or {}).get("events", []):
                if ev == "control_reset":
                    err("[ERROR] AirPlay 2 control channel failed")
                    err('[STATUS] error code=connect_failed http=0 detail="control channel reset"')
                    self._finish(1)
                    return
                if ev == "teardown":
                    err("[STATUS] idle_timeout")
                    self._finish(0)
                    return

    # -- command FIFO ----------------------------------------------------------------------

    def _open_fifo(self) -> None:
        path = self.a.cmdpipe
        if not path:
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            err(f"[ERROR] cannot open command pipe {path}: {exc}")
            return
        self.fifo_fds.append(fd)
        # a writer of our own keeps the FIFO from reporting EOF before MA attaches
        with contextlib.suppress(OSError):
            self.fifo_fds.append(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        asyncio.get_running_loop().add_reader(fd, self._on_fifo, fd)

    def _on_fifo(self, fd: int) -> None:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            return
        self.cmd_buf += data
        while b"\n" in self.cmd_buf:
            raw, self.cmd_buf = self.cmd_buf.split(b"\n", 1)
            line = raw.decode(errors="replace").strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "ACTION":
                cmd, self.pending = dict(self.pending), {}
                self.tasks.append(asyncio.create_task(self._action(value, cmd)))
            elif key == "VOLUME":
                self.tasks.append(asyncio.create_task(self.wire("volume", volume=int(value))))
            elif key == "PROGRESS":
                self.pending.pop("DURATION", None)
            elif key == "ARTWORK":
                pass
            else:
                self.pending[key] = value

    async def _action(self, action: str, cmd: dict[str, str]) -> None:
        if action == "START":
            await self._start(int(cmd.get("START_UNIX_MS", "0") or 0), cmd.get("START_JOIN") == "1")
        elif action == "FLUSH":
            self.gen += 1
            self.ring.clear()
            self.ring_space.set()
            self.fed_bytes = 0
            self.eof_sent = False
            self.stdin_eof = False if not self.stdin_eof else self.stdin_eof
            self.elapsed_acc, self.resumed_at, self.anchor_ms = 0.0, None, None
            self.audio_armed = True
            _status, resp = await self.wire("flush")
            if (resp or {}).get("ack", True):
                err("[STATUS] flushed")
            self._maybe_audio_present()
        elif action in ("PAUSE", "STANDBY"):
            _status, resp = await self.wire("pause", standby=action == "STANDBY")
            if (resp or {}).get("ok"):
                self.elapsed_acc = self.elapsed_s()
                self.resumed_at = None
                self.paused = True
                err("[STATUS] paused")
        elif action == "PLAY":
            _status, resp = await self.wire("play")
            if (resp or {}).get("ok") and self.paused:
                self.paused = False
                self.resumed_at = time.time() + int(resp.get("delay_ms", 0)) / 1000
        elif action == "STOP":
            await self.wire("teardown")  # TEARDOWN before the process goes away
            self.session = None
            self._finish(0)
        elif action == "SENDMETA":
            await self.wire("meta", **{k.lower(): v for k, v in cmd.items()})
        elif action == "ANNOUNCE":
            at = int(cmd.get("ANNOUNCE_AT_UNIX_MS", "0") or now_ms())
            err(f"[STATUS] announce_started at_unix_ms={at} duration_ms=1000")
            await asyncio.sleep(max(0.0, (at - now_ms()) / 1000) + 1.0)
            err("[STATUS] announce_done")

    async def _start(self, requested: int, join: bool) -> None:
        requested = requested or now_ms() + int(self.cfg.get("latency_ms", 2000))
        retry_ms = int(self.cfg.get("anchor_retry_ms", 500))
        at = requested
        for attempt in range(8):
            status, resp = await self.wire(
                "start",
                anchor_unix_ms=at,
                requested_unix_ms=requested,
                stream_url=f"{self.cfg['pcm_base']}/pcm/{self.gen}.pcm",
                join=join,
                attempt=attempt,
            )
            resp = resp or {}
            if resp.get("ok"):
                at = int(resp.get("at_unix_ms", at))
                break
            if resp.get("http") != 400:
                err(
                    f"[STATUS] error code=start_failed http={resp.get('http', status)} "
                    f'detail="{resp.get("detail", "start refused")}"'
                )
                return
            err(f"[WARN] SETRATEANCHORTIME rejected (400), retrying in {retry_ms} ms")
            await asyncio.sleep(retry_ms / 1000)
            at = max(at, now_ms() + 400)
        else:
            err('[STATUS] error code=start_failed http=400 detail="SETRATEANCHORTIME rejected"')
            return
        self.anchor_ms = at
        self.paused = False
        self.elapsed_acc = 0.0
        self.resumed_at = at / 1000
        ack = self.cli.get("start_ack", "normal")
        if ack == "missing":
            return
        if ack == "late":
            await asyncio.sleep(3.0)
        err(f"[STATUS] started requested_unix_ms={requested} at_unix_ms={at}")


async def _ptp_daemon() -> int:
    err("[PTP] daemon up")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.check:
        print(CHECK_LINE, flush=True)
        return 0
    if args.ptp_daemon:
        return asyncio.run(_ptp_daemon())
    if not args.address:
        err("[ERROR] no receiver address")
        return 2

    async def _run() -> int:
        return await FakeCli(args).run()

    return asyncio.run(_run())


def install(directory: str | os.PathLike[str], name: str = "cliairplay-seamer") -> Path:
    """Write an executable wrapper that runs this fake with the current interpreter."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    src = Path(__file__).resolve()
    path.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.argv[0] = {str(path)!r}\n"
        f"runpy.run_path({str(src)!r}, run_name='__main__')\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


if __name__ == "__main__":
    sys.exit(main())
