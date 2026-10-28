"""Slimproto wire format: client frames, server frames, discovery and the gain curve.

Layouts follow squeezelite's ``slimproto.h`` and LMS ``Slim/Networking/Slimproto.pm``:

- client -> server: 4-byte opcode, u32 big-endian payload length, payload
- server -> client: u16 big-endian length (opcode + payload), 4-byte opcode, payload

Everything here is pure (no I/O) so tests and the fake server can share it.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass

SLIMPROTO_PORT = 3483

# HELO device ids (LMS Slimproto.pm @deviceids; aioslimproto DEVICE_TYPE)
DEVICE_IDS = {
    "squeezebox": 2,
    "softsqueeze": 3,
    "squeezebox2": 4,
    "transporter": 5,
    "softsqueeze3": 6,
    "receiver": 7,
    "squeezeslave": 8,
    "controller": 9,
    "boom": 10,
    "softboom": 11,
    "squeezeplay": 12,
    "squeezeesp32": 100,
}

WLAN_RECONNECT = 0x4000
STAT_STRUCT = struct.Struct("!BBBLLLLHLLLLHLL")  # 47 bytes after the 4-byte event
STRM_STRUCT = struct.Struct("!cc5sBcBcBBBLHL")  # 24 bytes, then the HTTP request
AUDG_STRUCT = struct.Struct("!LLBBLL")
CONT_STRUCT = struct.Struct("!IBH")

# strm format byte -> codec name (aioslimproto FORMAT_BYTE, LMS)
FORMATS = {
    "p": "pcm",
    "m": "mp3",
    "f": "flc",
    "w": "wma",
    "o": "ogg",
    "a": "aac",
    "l": "alc",
    "u": "ops",
    "d": "dsf",
    "?": "unknown",
}
PCM_SIZES = {"0": 8, "1": 16, "2": 24, "3": 32}  # bss:S8 (the wiki table is wrong)
PCM_RATES = {
    "0": 11025,
    "1": 22050,
    "2": 32000,
    "3": 44100,
    "4": 48000,
    "5": 8000,
    "6": 12000,
    "7": 16000,
    "8": 24000,
    "9": 96000,
    ":": 88200,
    ";": 176400,
    "<": 192000,
    "=": 352800,
    ">": 384000,
}

DSCO_REASONS = {0: "ok", 1: "local_reset", 2: "remote_reset", 3: "unreachable", 4: "timeout"}


# -- client -> server -------------------------------------------------------------------


def client_frame(op: bytes, payload: bytes = b"") -> bytes:
    assert len(op) == 4, op
    return op + struct.pack("!I", len(payload)) + payload


def mac_bytes(mac: str) -> bytes:
    return bytes(int(p, 16) for p in mac.split(":"))


def helo(
    *,
    device_id: int,
    revision: int,
    mac: str,
    uuid: bytes = b"\x00" * 16,
    reconnect: bool = False,
    bytes_received: int = 0,
    lang: bytes = b"en",
    capabilities: str = "",
) -> bytes:
    """A 36-byte HELO plus the comma-separated capability string (bss:S11)."""
    channels = WLAN_RECONNECT if reconnect else 0
    body = struct.pack(
        "!BB6s16sHLL2s",
        device_id,
        revision,
        mac_bytes(mac),
        uuid.ljust(16, b"\x00")[:16],
        channels,
        bytes_received >> 32,
        bytes_received & 0xFFFFFFFF,
        lang[:2].ljust(2, b"\x00"),
    )
    return client_frame(b"HELO", body + capabilities.encode())


@dataclass
class StatFields:
    stream_buffer_size: int = 0
    stream_buffer_fullness: int = 0
    bytes_received: int = 0
    jiffies: int = 0
    output_buffer_size: int = 0
    output_buffer_fullness: int = 0
    elapsed_ms: int = 0
    server_timestamp: int = 0
    signal_strength: int = 0xFFFF
    voltage: int = 0
    num_crlf: int = 0


def stat(event: str, f: StatFields, error_code: int | None = 0) -> bytes:
    """STAT: event + 47-byte struct (+ u16 error code; squeezelite always sends it)."""
    assert len(event) == 4, event
    body = event.encode() + STAT_STRUCT.pack(
        f.num_crlf,
        0,
        0,
        f.stream_buffer_size & 0xFFFFFFFF,
        f.stream_buffer_fullness & 0xFFFFFFFF,
        (f.bytes_received >> 32) & 0xFFFFFFFF,
        f.bytes_received & 0xFFFFFFFF,
        f.signal_strength,
        f.jiffies & 0xFFFFFFFF,
        f.output_buffer_size & 0xFFFFFFFF,
        f.output_buffer_fullness & 0xFFFFFFFF,
        (f.elapsed_ms // 1000) & 0xFFFFFFFF,
        f.voltage,
        f.elapsed_ms & 0xFFFFFFFF,
        f.server_timestamp & 0xFFFFFFFF,
    )
    if error_code is not None:
        body += struct.pack("!H", error_code)
    return client_frame(b"STAT", body)


def resp(headers: bytes) -> bytes:
    return client_frame(b"RESP", headers)


def meta(data: bytes) -> bytes:
    return client_frame(b"META", data)


def dsco(reason: int) -> bytes:
    return client_frame(b"DSCO", bytes([reason & 0xFF]))


def setd_name(name: str, nul: bool = True) -> bytes:
    """SETD id 0 reply: player name, NUL-terminated except on SqueezePlay (bss:S12)."""
    return client_frame(b"SETD", b"\x00" + name.encode() + (b"\x00" if nul else b""))


def setd_display(width: int, height: int = 0, led: int | None = None) -> bytes:
    """SETD id 0xFE reply: display size (3, 5 or 7 bytes)."""
    body = b"\xfe" + struct.pack("!H", width)
    if height or led is not None:
        body += struct.pack("!H", height)
    if led is not None:
        body += struct.pack("!H", led)
    return client_frame(b"SETD", body)


def bye(upgrade: bool = False) -> bytes:
    return client_frame(b"BYE!", bytes([1 if upgrade else 0]))


def parse_client_frames(buf: bytes) -> tuple[list[tuple[str, bytes]], bytes]:
    """Split client->server bytes into ``(opcode, payload)`` frames; returns the rest."""
    out: list[tuple[str, bytes]] = []
    while len(buf) >= 8:
        (length,) = struct.unpack("!I", buf[4:8])
        if len(buf) < 8 + length:
            break
        out.append((buf[:4].decode("latin-1"), buf[8 : 8 + length]))
        buf = buf[8 + length :]
    return out, buf


# -- server -> client -------------------------------------------------------------------


def server_frame(op: bytes, payload: bytes = b"") -> bytes:
    assert len(op) == 4, op
    return struct.pack("!H", len(payload) + 4) + op + payload


class ServerFrameReader:
    """Incremental parser of server->client frames (several may share one read)."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> Iterator[tuple[str, bytes]]:
        self._buf += data
        while len(self._buf) >= 2:
            (length,) = struct.unpack("!H", self._buf[:2])
            if len(self._buf) < 2 + length:
                return
            frame, self._buf = self._buf[2 : 2 + length], self._buf[2 + length :]
            yield frame[:4].decode("latin-1"), frame[4:]


@dataclass
class Strm:
    command: str
    autostart: int
    format: str
    pcm_size: str
    pcm_rate: str
    pcm_channels: str
    pcm_endian: str
    threshold_kb: int
    spdif: str
    trans_period: int
    trans_type: str
    flags: int
    output_threshold: int
    replay_gain: int
    server_port: int
    server_ip: int
    request: bytes

    @property
    def https(self) -> bool:
        return bool(self.flags & 0x20)

    @property
    def pcm_bits(self) -> int | None:
        return PCM_SIZES.get(self.pcm_size)

    def request_line(self) -> tuple[str, str]:
        """``(method, path)`` from the embedded HTTP request."""
        first = self.request.split(b"\r\n", 1)[0].decode("latin-1")
        parts = first.split(" ")
        return (parts[0], parts[1]) if len(parts) >= 2 else ("GET", "/")

    def header(self, name: str) -> str | None:
        for line in self.request.split(b"\r\n")[1:]:
            key, _, val = line.decode("latin-1").partition(":")
            if key.strip().lower() == name.lower():
                return val.strip()
        return None


def parse_strm(data: bytes) -> Strm:
    f = STRM_STRUCT.unpack(data[: STRM_STRUCT.size])
    codec = f[2].decode("latin-1")
    auto = f[1].decode("latin-1")
    return Strm(
        command=f[0].decode("latin-1"),
        autostart=int(auto) if auto.isdigit() else 0,
        format=codec[0],
        pcm_size=codec[1],
        pcm_rate=codec[2],
        pcm_channels=codec[3],
        pcm_endian=codec[4],
        threshold_kb=f[3],
        spdif=f[4].decode("latin-1"),
        trans_period=f[5],
        trans_type=f[6].decode("latin-1"),
        flags=f[7],
        output_threshold=f[8],
        replay_gain=f[10],
        server_port=f[11],
        server_ip=f[12],
        request=data[STRM_STRUCT.size :],
    )


def build_strm(
    command: str,
    *,
    autostart: str = "0",
    codec: str = "p1321",
    threshold_kb: int = 0,
    flags: int = 0,
    output_threshold: int = 0,
    replay_gain: int = 0,
    server_port: int = 0,
    server_ip: int = 0,
    request: bytes = b"",
) -> bytes:
    """Server-side strm payload (used by tests and the fake server)."""
    return (
        STRM_STRUCT.pack(
            command.encode(),
            autostart.encode(),
            codec.encode(),
            threshold_kb,
            b"0",
            0,
            b"0",
            flags,
            output_threshold,
            0,
            replay_gain,
            server_port,
            server_ip,
        )
        + request
    )


@dataclass
class Audg:
    old_left: int
    old_right: int
    adjust: int  # dvc: 0 = the player should not apply digital gain (hardware volume)
    preamp: int
    left: int  # 16.16 fixed point
    right: int


def parse_audg(data: bytes) -> Audg:
    return Audg(*AUDG_STRUCT.unpack(data[: AUDG_STRUCT.size]))


def parse_cont(data: bytes) -> tuple[int, int, int]:
    """``(metaint, loop, guid_count)``."""
    if len(data) >= CONT_STRUCT.size:
        return CONT_STRUCT.unpack(data[: CONT_STRUCT.size])
    return (struct.unpack("!I", data[:4])[0] if len(data) >= 4 else 0), 0, 0


# -- volume curve (LMS Squeezebox2.pm / aioslimproto volume.py) -------------------------

OLD_MAP = [
    0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 5, 5, 6, 6, 7, 8, 9, 9, 10, 11, 12, 13, 14, 15, 16, 16,
    17, 18, 19, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 32, 33, 34, 35, 37, 38, 39, 40, 42,
    43, 44, 46, 47, 48, 50, 51, 53, 54, 56, 57, 59, 60, 61, 63, 65, 66, 68, 69, 71, 72, 74,
    75, 77, 79, 80, 82, 84, 85, 87, 89, 90, 92, 94, 96, 97, 99, 101, 103, 104, 106, 108,
    110, 112, 113, 115, 117, 119, 121, 123, 125, 127, 128,
]  # fmt: skip

# (total range dB, step point, step fraction): hardware players and software players
CURVES = ((-50, -1, 1.0), (-74, 25, 0.5))


def volume_db(volume: int, total: int = -50, step_point: int = -1, fraction: float = 1.0) -> float:
    step_db = total * fraction
    if volume > step_point:
        return (0 - step_db) / (100.0 - step_point) * (volume - 100)
    return (step_db - total) / (step_point - 0.0) * volume + total


def new_gain(volume: int, curve: tuple[int, int, float] = CURVES[0]) -> int:
    if volume <= 0:
        return 0
    db = volume_db(volume, *curve)
    mult = 10 ** (db / 20.0)
    if -30 <= db <= 0:
        return int(mult * (1 << 8) + 0.5) * (1 << 8)
    return int(mult * (1 << 16) + 0.5)


def old_gain(volume: int) -> int:
    return 0 if volume <= 0 else OLD_MAP[min(100, volume)]


def gain_to_volume(old: int, new: int) -> int:
    """Invert an audg pair to the server's 0..100 volume (either curve)."""
    if new == 0:
        return 0
    for curve in CURVES:
        for v in range(1, 101):
            if new_gain(v, curve) == new and old_gain(v) == old:
                return v
    # unknown curve: nearest -50 dB curve point by dB
    db = 20 * math.log10(new / 65536)
    return max(0, min(100, round(100 + db * 2)))


# -- discovery (UDP 3483) ---------------------------------------------------------------


def discovery_request_tlv(keys: tuple[str, ...] = ("NAME", "IPAD", "JSON", "VERS")) -> bytes:
    """New-style ``e`` request: 4-char keys with zero-length values."""
    return b"e" + b"".join(k.encode() + b"\x00" for k in keys)


def discovery_request_legacy(device_id: int, revision: int, mac: str) -> bytes:
    """Old-style 18-byte ``d`` request (Squeezebox firmware)."""
    return struct.pack("!cxBB8x6s", b"d", device_id, revision, mac_bytes(mac))


def parse_discovery_reply(data: bytes) -> dict[str, str]:
    """``E`` TLV reply -> dict, or ``D`` + 16-byte host -> {"IPAD": host}."""
    if data[:1] == b"D":
        return {"IPAD": data[1:17].split(b"\x00", 1)[0].decode("latin-1")}
    out: dict[str, str] = {}
    i = 1
    while i + 5 <= len(data):
        key = data[i : i + 4].decode("latin-1")
        n = data[i + 4]
        out[key] = data[i + 5 : i + 5 + n].decode("utf-8", errors="replace")
        i += 5 + n
    return out
