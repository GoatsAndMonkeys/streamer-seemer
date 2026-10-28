"""The MPD command table (``command/AllCommands.cxx``): permission, argument counts,
handler. Handlers raise :class:`ProtocolError` for ACKs and write ``key: value`` lines
into the :class:`Reply`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .protocol import (
    PERM_ADD,
    PERM_ADMIN,
    PERM_CONTROL,
    PERM_NONE,
    PERM_READ,
    SUBSYSTEMS,
    Ack,
    ProtocolError,
    parse_bool,
    parse_float,
    parse_int,
    parse_unsigned,
)
from .server import CloseConnection, Conn, EnterIdle, Reply

if TYPE_CHECKING:
    from .device import MpdDevice

Handler = Callable[["MpdDevice", Conn, list[str], Reply], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    name: str
    perm: int
    min_args: int
    max_args: int  # -1 = unlimited
    handler: Handler
    since: tuple[int, int, int] = (0, 0, 0)


COMMANDS: dict[str, Command] = {}


def command(
    name: str,
    perm: int,
    min_args: int = 0,
    max_args: int = 0,
    since: tuple[int, int, int] = (0, 0, 0),
) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        COMMANDS[name] = Command(name, perm, min_args, max_args, fn, since)
        return fn

    return deco


def _range(dev: MpdDevice, arg: str) -> tuple[int, int]:
    """``POS`` or ``START:END`` (END optional) -> half-open range, MPD's ParseRange."""
    if ":" in arg:
        a, _, b = arg.partition(":")
        start = parse_unsigned(a)
        end = parse_unsigned(b) if b else len(dev.queue)
        if end < start:
            raise ProtocolError(Ack.ARG, "Bad song index")
    else:
        start = parse_unsigned(arg)
        end = start + 1
    if start >= len(dev.queue) or end > len(dev.queue):
        raise ProtocolError(Ack.ARG, "Bad song index")
    return start, end


# -- connection --------------------------------------------------------------------


@command("ping", PERM_NONE)
async def _ping(dev, conn, args, r):
    return None


@command("close", PERM_NONE, 0, -1)
async def _close(dev, conn, args, r):
    raise CloseConnection()


@command("password", PERM_NONE, 1, 1)
async def _password(dev, conn, args, r):
    want = dev.knobs.get("password")
    if not want or args[0] != want:
        raise ProtocolError(Ack.PASSWORD, "incorrect password")
    conn.perms = PERM_READ | PERM_ADD | PERM_CONTROL | PERM_ADMIN


@command("binarylimit", PERM_NONE, 1, 1, since=(0, 22, 4))
async def _binarylimit(dev, conn, args, r):
    value = parse_unsigned(args[0])
    if value < 64:
        raise ProtocolError(Ack.ARG, "Value too small")
    conn.binary_limit = value


@command("commands", PERM_NONE)
async def _commands(dev, conn, args, r):
    for name in sorted(COMMANDS):
        cmd = COMMANDS[name]
        if dev.supports(name) and cmd.perm & conn.perms == cmd.perm:
            r.add("command", name)


@command("notcommands", PERM_NONE)
async def _notcommands(dev, conn, args, r):
    for name in sorted(COMMANDS):
        cmd = COMMANDS[name]
        if dev.supports(name) and cmd.perm & conn.perms != cmd.perm:
            r.add("command", name)


@command("tagtypes", PERM_READ, 0, -1)
async def _tagtypes(dev, conn, args, r):
    if not args:
        for t in ("Artist", "Album", "Title", "Track", "Name", "Genre", "Date"):
            r.add("tagtype", t)


@command("urlhandlers", PERM_READ)
async def _urlhandlers(dev, conn, args, r):
    for scheme in dev.url_schemes():
        r.add("handler", f"{scheme}://")


@command("decoders", PERM_READ)
async def _decoders(dev, conn, args, r):
    for plugin, suffixes, mimes in (
        ("mad", ("mp3", "mp2"), ("audio/mpeg",)),
        ("flac", ("flac",), ("audio/flac", "audio/x-flac")),
        ("pcm", (), ("audio/L16", "audio/wav", "audio/x-wav")),
        ("ffmpeg", ("aac", "m4a", "ogg", "opus"), ("audio/aac", "audio/mp4", "audio/ogg")),
    ):
        r.add("plugin", plugin)
        for s in suffixes:
            r.add("suffix", s)
        for m in mimes:
            r.add("mime_type", m)


@command("config", PERM_ADMIN)
async def _config(dev, conn, args, r):
    # MPD only answers `config` on a local (unix socket) connection
    raise ProtocolError(Ack.PERMISSION, "Command only permitted to local clients")


@command("idle", PERM_READ, 0, -1)
async def _idle(dev, conn, args, r):
    for a in args:
        if a not in SUBSYSTEMS:
            raise ProtocolError(Ack.ARG, f"Unrecognized idle event: {a}")
    raise EnterIdle(frozenset(args))


# -- status ------------------------------------------------------------------------


@command("status", PERM_READ)
async def _status(dev, conn, args, r):
    dev.status_into(r)


@command("currentsong", PERM_READ)
async def _currentsong(dev, conn, args, r):
    song = dev.current_song()
    if song is not None:
        dev.song_into(r, song, dev.queue.index(song))


@command("stats", PERM_READ)
async def _stats(dev, conn, args, r):
    up = int(dev.clock.monotonic() - dev.started_at)
    for k, v in (
        ("uptime", up),
        ("playtime", int(dev.playtime())),
        ("artists", 0),
        ("albums", 0),
        ("songs", 0),
        ("db_playtime", 0),
        ("db_update", 0),
    ):
        r.add(k, v)


@command("replay_gain_status", PERM_READ)
async def _rg(dev, conn, args, r):
    r.add("replay_gain_mode", "off")


@command("clearerror", PERM_CONTROL)
async def _clearerror(dev, conn, args, r):
    if dev.error is not None:
        dev.error = None
        dev.idle("player")


# -- mixer --------------------------------------------------------------------------


@command("setvol", PERM_CONTROL, 1, 1)
async def _setvol(dev, conn, args, r):
    level = parse_unsigned(args[0], 100)
    dev.set_volume(level)


@command("volume", PERM_CONTROL, 1, 1)
async def _volume(dev, conn, args, r):
    delta = parse_int(args[0])
    if not -100 <= delta <= 100:
        raise ProtocolError(Ack.ARG, "Number too large")
    cur = dev.mixer_volume()
    if cur is None:
        raise ProtocolError(Ack.SYSTEM, "No mixer")
    dev.set_volume(max(0, min(100, cur + delta)))


@command("getvol", PERM_READ, since=(0, 23, 0))
async def _getvol(dev, conn, args, r):
    vol = dev.mixer_volume()
    if vol is not None:
        r.add("volume", vol)


# -- playback ---------------------------------------------------------------------------


@command("play", PERM_CONTROL, 0, 1)
async def _play(dev, conn, args, r):
    if args:
        pos = parse_int(args[0])
        if pos < 0:
            await dev.play_current_or_first()
            return
        if pos >= len(dev.queue):
            raise ProtocolError(Ack.ARG, "Bad song index")
        await dev.play_pos(pos)
    else:
        await dev.play_current_or_first()


@command("playid", PERM_CONTROL, 0, 1)
async def _playid(dev, conn, args, r):
    if not args or parse_int(args[0]) < 0:
        await dev.play_current_or_first()
        return
    sid = parse_int(args[0])
    pos = dev.pos_of_id(sid)
    if pos is None:
        raise ProtocolError(Ack.NO_EXIST, "No such song")
    await dev.play_pos(pos)


@command("pause", PERM_CONTROL, 0, 1)
async def _pause(dev, conn, args, r):
    want = parse_bool(args[0]) if args else None
    await dev.pause(want)


@command("stop", PERM_CONTROL)
async def _stop(dev, conn, args, r):
    await dev.stop_playback()


@command("next", PERM_CONTROL)
async def _next(dev, conn, args, r):
    await dev.next_song()


@command("previous", PERM_CONTROL)
async def _previous(dev, conn, args, r):
    await dev.previous_song()


def _time_arg(dev: MpdDevice, arg: str) -> tuple[float, bool]:
    rel = arg[:1] in ("+", "-")
    value = parse_float(arg)
    return value, rel


@command("seekcur", PERM_CONTROL, 1, 1)
async def _seekcur(dev, conn, args, r):
    value, rel = _time_arg(dev, args[0])
    if dev.mpd_state() == "stop":
        raise ProtocolError(Ack.PLAYER_SYNC, "Not playing")
    target = (dev.elapsed() or 0.0) + value if rel else value
    await dev.seek_pos(dev.current, max(0.0, target))


@command("seek", PERM_CONTROL, 2, 2)
async def _seek(dev, conn, args, r):
    pos = parse_unsigned(args[0])
    if pos >= len(dev.queue):
        raise ProtocolError(Ack.ARG, "Bad song index")
    value = parse_float(args[1])
    await dev.seek_pos(pos, max(0.0, value))


@command("seekid", PERM_CONTROL, 2, 2)
async def _seekid(dev, conn, args, r):
    pos = dev.pos_of_id(parse_unsigned(args[0]))
    if pos is None:
        raise ProtocolError(Ack.NO_EXIST, "No such song")
    await dev.seek_pos(pos, max(0.0, parse_float(args[1])))


# -- options ------------------------------------------------------------------------------


@command("repeat", PERM_CONTROL, 1, 1)
async def _repeat(dev, conn, args, r):
    dev.set_option("repeat", "1" if parse_bool(args[0]) else "0")


@command("random", PERM_CONTROL, 1, 1)
async def _random(dev, conn, args, r):
    dev.set_option("random", "1" if parse_bool(args[0]) else "0")


@command("single", PERM_CONTROL, 1, 1)
async def _single(dev, conn, args, r):
    if args[0] == "oneshot" and dev.version >= (0, 21, 0):
        dev.set_option("single", "oneshot")
        return
    dev.set_option("single", "1" if parse_bool(args[0]) else "0")


@command("consume", PERM_CONTROL, 1, 1)
async def _consume(dev, conn, args, r):
    if args[0] == "oneshot" and dev.version >= (0, 24, 0):
        dev.set_option("consume", "oneshot")
        return
    dev.set_option("consume", "1" if parse_bool(args[0]) else "0")


@command("crossfade", PERM_CONTROL, 1, 1)
async def _crossfade(dev, conn, args, r):
    dev.xfade = parse_unsigned(args[0])
    dev.idle("options")


# -- queue -------------------------------------------------------------------------------


@command("add", PERM_ADD, 1, 2)
async def _add(dev, conn, args, r):
    if len(args) > 1 and dev.version < (0, 23, 3):
        raise ProtocolError(Ack.ARG, 'too many arguments for "add"')
    pos = dev.parse_insert_pos(args[1]) if len(args) > 1 else None
    dev.add_uri(args[0], pos)


@command("addid", PERM_ADD, 1, 2)
async def _addid(dev, conn, args, r):
    pos = dev.parse_insert_pos(args[1]) if len(args) > 1 else None
    song = dev.add_uri(args[0], pos)
    r.add("Id", song.id)


@command("clear", PERM_CONTROL)
async def _clear(dev, conn, args, r):
    await dev.clear()


@command("delete", PERM_CONTROL, 1, 1)
async def _delete(dev, conn, args, r):
    start, end = _range(dev, args[0])
    await dev.delete_range(start, end)


@command("deleteid", PERM_CONTROL, 1, 1)
async def _deleteid(dev, conn, args, r):
    pos = dev.pos_of_id(parse_unsigned(args[0]))
    if pos is None:
        raise ProtocolError(Ack.NO_EXIST, "No such song")
    await dev.delete_range(pos, pos + 1)


@command("playlistinfo", PERM_READ, 0, 1)
async def _playlistinfo(dev, conn, args, r):
    start, end = _range(dev, args[0]) if args else (0, len(dev.queue))
    for pos in range(start, end):
        dev.song_into(r, dev.queue[pos], pos)


@command("playlistid", PERM_READ, 0, 1)
async def _playlistid(dev, conn, args, r):
    if args:
        pos = dev.pos_of_id(parse_unsigned(args[0]))
        if pos is None:
            raise ProtocolError(Ack.NO_EXIST, "No such song")
        dev.song_into(r, dev.queue[pos], pos)
        return
    for pos, song in enumerate(dev.queue):
        dev.song_into(r, song, pos)


@command("playlist", PERM_READ)
async def _playlist(dev, conn, args, r):
    for pos, song in enumerate(dev.queue):
        r.add(str(pos), f"file: {song.uri}")


@command("plchanges", PERM_READ, 1, 2)
async def _plchanges(dev, conn, args, r):
    version = parse_unsigned(args[0])
    for pos, song in enumerate(dev.queue):
        if song.version > version:  # approximation: moves are not tracked
            dev.song_into(r, song, pos)


# -- outputs ------------------------------------------------------------------------------


@command("outputs", PERM_READ)
async def _outputs(dev, conn, args, r):
    for out in dev.outputs:
        r.add("outputid", out["id"])
        r.add("outputname", out["name"])
        r.add("plugin", out["plugin"])
        r.add("outputenabled", 1 if out["enabled"] else 0)


async def _set_output(dev: MpdDevice, arg: str, mode: str) -> None:
    oid = parse_unsigned(arg)
    for out in dev.outputs:
        if out["id"] == oid:
            out["enabled"] = (not out["enabled"]) if mode == "toggle" else mode == "on"
            dev.idle("output")
            return
    raise ProtocolError(Ack.NO_EXIST, "No such audio output")


@command("enableoutput", PERM_ADMIN, 1, 1)
async def _enableoutput(dev, conn, args, r):
    await _set_output(dev, args[0], "on")


@command("disableoutput", PERM_ADMIN, 1, 1)
async def _disableoutput(dev, conn, args, r):
    await _set_output(dev, args[0], "off")


@command("toggleoutput", PERM_ADMIN, 1, 1)
async def _toggleoutput(dev, conn, args, r):
    await _set_output(dev, args[0], "toggle")


# -- album art (MP9) ----------------------------------------------------------------------


@command("albumart", PERM_READ, 2, 2, since=(0, 21, 0))
async def _albumart(dev, conn, args, r):
    offset = parse_unsigned(args[1])
    data = await dev.fetch_art(args[0], "albumart")
    if data is None:
        raise ProtocolError(Ack.NO_EXIST, "No file exists")
    _binary_chunk(r, data, offset, conn.binary_limit, None)


@command("readpicture", PERM_READ, 2, 2, since=(0, 22, 0))
async def _readpicture(dev, conn, args, r):
    offset = parse_unsigned(args[1])
    data = await dev.fetch_art(args[0], "readpicture")
    if data:  # no embedded picture: empty OK reply
        _binary_chunk(r, data, offset, conn.binary_limit, "image/png")


def _binary_chunk(r: Reply, data: bytes, offset: int, limit: int, mime: str | None) -> None:
    if offset > len(data):
        raise ProtocolError(Ack.ARG, "Bad file offset")
    r.add("size", len(data))
    if mime:
        r.add("type", mime)
    r.binary(data[offset : offset + limit])
