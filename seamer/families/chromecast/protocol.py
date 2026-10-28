"""CASTV2 wire format: length-prefixed protobuf ``CastMessage`` frames over TLS.

Hand-rolled proto2 codec for ``cast_channel.proto`` (the module pychromecast ships as
``generated/cast_channel_pb2.py``), so the simulator needs no protobuf dependency::

    CastMessage { 1 protocol_version (enum, 0 = CASTV2_1_0)  2 source_id  3 destination_id
                  4 namespace  5 payload_type (0 STRING, 1 BINARY)  6 payload_utf8
                  7 payload_binary }

Each frame on the socket is a 4-byte big-endian length followed by the message; the
maximum is 64 KiB (cast_airplay2.md section 2a).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import ssl
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FRAME = 64 * 1024

NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_DEVICEAUTH = "urn:x-cast:com.google.cast.tp.deviceauth"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
NS_MULTIZONE = "urn:x-cast:com.google.cast.multizone"
NS_CAC = "urn:x-cast:com.google.cast.cac"
NS_DEBUGOVERLAY = "urn:x-cast:com.google.cast.debugoverlay"
NS_MA = "urn:x-cast:io.music-assistant.cast"
NS_SENDSPIN = "urn:x-cast:sendspin"

PLATFORM_ID = "receiver-0"
BROADCAST_ID = "*"

APP_MEDIA_RECEIVER = "CC1AD845"  # Default Media Receiver
APP_MA = "C35B0678"  # Music Assistant's custom receiver
APP_SENDSPIN = "DD107DDB"

PAYLOAD_STRING = 0
PAYLOAD_BINARY = 1


class FrameError(Exception):
    """A frame that cannot be decoded (bad length, truncated, bad varint)."""


@dataclass
class CastMessage:
    source_id: str
    destination_id: str
    namespace: str
    payload_utf8: str | None = None
    payload_binary: bytes | None = None
    protocol_version: int = 0

    @property
    def payload_type(self) -> int:
        return PAYLOAD_BINARY if self.payload_binary is not None else PAYLOAD_STRING

    def json(self) -> dict[str, Any]:
        """The payload as a JSON object ({} when it is not one)."""
        if self.payload_utf8 is None:
            return {}
        try:
            data = json.loads(self.payload_utf8)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


# -- protobuf varint / field codec -------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise FrameError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise FrameError("varint too long")


def _field_bytes(number: int, data: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def encode(msg: CastMessage) -> bytes:
    """Serialize a CastMessage (no length prefix). Field order follows the .proto."""
    out = bytearray(_varint(1 << 3 | 0) + _varint(msg.protocol_version))
    out += _field_bytes(2, msg.source_id.encode())
    out += _field_bytes(3, msg.destination_id.encode())
    out += _field_bytes(4, msg.namespace.encode())
    out += _varint(5 << 3 | 0) + _varint(msg.payload_type)
    if msg.payload_binary is not None:
        out += _field_bytes(7, msg.payload_binary)
    else:
        out += _field_bytes(6, (msg.payload_utf8 or "").encode())
    return bytes(out)


def decode(buf: bytes) -> CastMessage:
    fields: dict[int, Any] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            fields[number], pos = _read_varint(buf, pos)
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise FrameError("truncated field")
            fields[number] = buf[pos : pos + length]
            pos += length
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            raise FrameError(f"unsupported wire type {wire}")
    utf8 = fields.get(6)
    binary = fields.get(7)
    return CastMessage(
        protocol_version=int(fields.get(1, 0)),
        source_id=bytes(fields.get(2, b"")).decode(errors="replace"),
        destination_id=bytes(fields.get(3, b"")).decode(errors="replace"),
        namespace=bytes(fields.get(4, b"")).decode(errors="replace"),
        payload_utf8=bytes(utf8).decode(errors="replace") if utf8 is not None else None,
        payload_binary=bytes(binary) if binary is not None and utf8 is None else None,
    )


def frame(msg: CastMessage) -> bytes:
    body = encode(msg)
    return struct.pack(">I", len(body)) + body


def json_message(source: str, dest: str, namespace: str, payload: dict[str, Any]) -> CastMessage:
    return CastMessage(
        source_id=source,
        destination_id=dest,
        namespace=namespace,
        payload_utf8=json.dumps(payload, separators=(",", ":")),
    )


async def read_frame(reader: asyncio.StreamReader) -> CastMessage:
    """Read one frame. Raises IncompleteReadError at EOF, FrameError on garbage."""
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME:
        raise FrameError(f"frame of {length} bytes exceeds {MAX_FRAME}")
    return decode(await reader.readexactly(length))


# -- TLS ----------------------------------------------------------------------------------

_CERT_DIR = Path(tempfile.gettempdir()) / f"seamer-cast-cert-{os.getuid()}"


def _make_cert(cert: Path, key: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl:
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "3650",
                "-subj",
                "/CN=seamer-cast",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        return
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    pkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "seamer-cast")])
    now = datetime.datetime.now(datetime.UTC)
    crt = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(pkey.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(pkey, hashes.SHA256())
    )
    key.write_bytes(
        pkey.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))


def cert_paths() -> tuple[Path, Path]:
    """A cached self-signed certificate + key (pychromecast never verifies it)."""
    cert, key = _CERT_DIR / "cert.pem", _CERT_DIR / "key.pem"
    if not (cert.exists() and key.exists()):
        _CERT_DIR.mkdir(mode=0o700, exist_ok=True)
        tmp_cert, tmp_key = cert.with_suffix(".tmp"), key.with_suffix(".tmp")
        _make_cert(tmp_cert, tmp_key)
        os.replace(tmp_key, key)
        os.replace(tmp_cert, cert)
    return cert, key


def server_ssl_context() -> ssl.SSLContext:
    cert, key = cert_paths()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def client_ssl_context() -> ssl.SSLContext:
    """What pychromecast uses: TLS client, no hostname check, no verification."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def close_writer(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 1.0)
