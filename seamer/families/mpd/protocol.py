"""MPD text-protocol pieces: argument tokenizer, ACK codes, argument parsers.

Everything here follows MPD's own wording (``src/protocol/*``, ``src/command/*``) so a
client sees byte-identical replies. ``ProtocolError`` is raised by handlers and turned
into ``ACK [errno@index] {command} message`` by the server.
"""

from __future__ import annotations

import re
from enum import IntEnum


class Ack(IntEnum):
    """``enum ack`` from MPD's ``protocol/Ack.hxx`` (also python-mpd2 FailureResponseCode)."""

    NOT_LIST = 1
    ARG = 2
    PASSWORD = 3
    PERMISSION = 4
    UNKNOWN = 5
    NO_EXIST = 50
    PLAYLIST_MAX = 51
    SYSTEM = 52
    PLAYLIST_LOAD = 53
    UPDATE_ALREADY = 54
    PLAYER_SYNC = 55
    EXIST = 56


class ProtocolError(Exception):
    """An ACK reply. ``command`` None means "the command being run"; "" means ``{}``."""

    def __init__(self, code: Ack, message: str, command: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.command = command


def ack_line(code: int, index: int, command: str, message: str) -> str:
    return f"ACK [{int(code)}@{index}] {{{command}}} {message}\n"


# Idle subsystems in MPD's own order (``IdleFlags.cxx`` idle_names); a ``changed:``
# reply lists them in this order.
SUBSYSTEMS: tuple[str, ...] = (
    "database",
    "stored_playlist",
    "playlist",
    "player",
    "mixer",
    "output",
    "options",
    "sticker",
    "update",
    "subscription",
    "message",
    "neighbor",
    "mount",
    "partition",
)

# Permission bits (``client/Permission.hxx``)
PERM_NONE = 0
PERM_READ = 1
PERM_ADD = 2
PERM_CONTROL = 4
PERM_ADMIN = 8
PERM_ALL = PERM_READ | PERM_ADD | PERM_CONTROL | PERM_ADMIN
PERM_NAMES = {"read": PERM_READ, "add": PERM_ADD, "control": PERM_CONTROL, "admin": PERM_ADMIN}


def parse_permissions(text: str) -> int:
    perms = 0
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        if part not in PERM_NAMES:
            raise ValueError(f"unknown permission {part!r}")
        perms |= PERM_NAMES[part]
    return perms


class TokenizeError(Exception):
    pass


def _unquoted_ok(c: str) -> bool:
    # MPD's IsUnquotedChar: ASCII alphanumerics and a few punctuation marks
    return (c.isascii() and c.isalnum()) or c in "_-+.:/"


def tokenize(line: str) -> list[str]:
    """Split a command line like MPD's ``Tokenizer``.

    The first word is the command (``[a-z_0-9]+``). Arguments are unquoted words or
    ``"..."`` strings where ``\\`` escapes the next character.
    """
    out: list[str] = []
    i, n = 0, len(line)
    while True:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            return out
        if line[i] == '"':
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise TokenizeError("Missing closing '\"'")
                c = line[i]
                if c == "\\":
                    i += 1
                    if i >= n:
                        raise TokenizeError("Missing closing '\"'")
                    buf.append(line[i])
                elif c == '"':
                    i += 1
                    break
                else:
                    buf.append(c)
                i += 1
            if i < n and line[i] not in " \t":
                raise TokenizeError("Space expected after closing '\"'")
            out.append("".join(buf))
        else:
            j = i
            while j < n and line[j] not in " \t":
                if not _unquoted_ok(line[j]):
                    raise TokenizeError("Invalid unquoted character")
                j += 1
            out.append(line[i:j])
            i = j


# -- argument parsers (``protocol/ArgParser.cxx``) -----------------------------------

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def parse_int(s: str) -> int:
    if not _INT.match(s):
        raise ProtocolError(Ack.ARG, f"Integer expected: {s}")
    return int(s)


def parse_unsigned(s: str, max_value: int | None = None) -> int:
    value = parse_int(s)
    if value < 0:
        raise ProtocolError(Ack.ARG, f"Number is negative: {s}")
    if max_value is not None and value > max_value:
        raise ProtocolError(Ack.ARG, f"Number too large: {s}")
    return value


def parse_bool(s: str) -> bool:
    if s not in ("0", "1"):
        raise ProtocolError(Ack.ARG, f"Boolean (0/1) expected: {s}")
    return s == "1"


def parse_float(s: str) -> float:
    if not _FLOAT.match(s):
        raise ProtocolError(Ack.ARG, f"Float expected: {s}")
    return float(s)


def parse_version(text: str) -> tuple[int, int, int]:
    parts = [int(p) for p in re.findall(r"\d+", text)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]
