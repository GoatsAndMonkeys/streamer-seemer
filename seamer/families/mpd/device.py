"""The MPD device: a Music Player Daemon (or MPD-protocol clone) on a TCP port.

Surfaces:

- the MPD line protocol (``mpd_port`` knob; real servers use 6600), see
  :mod:`.server` and :mod:`.commands`;
- the usual ``/_sim`` HTTP control API on the device's base URL.

Audio: ``add`` + ``play`` makes the sink GET the song's HTTP URL (with MPD's
``User-Agent``), so a Music Assistant flow stream is really consumed. Queue state
(songs, ids, playlist version, current position) lives here; playback truth is the
shared :class:`TransportMachine`, read back through :class:`MpdLens`.

Sim actions (``POST /_sim/action/<name>``):

- ``external`` ``{"cmd": "pause 1"}`` or ``{"cmds": [...]}``: run commands as another
  MPD client (a phone app); every client gets the idle events (MP1 tests).
- ``drop`` ``{"which": "command"|"idle"|"all"}``: close sockets by role (MP12).
- ``renderer`` ``{"renderer": "airplay"|"upnp"|...|"none"}``: moOde/Volumio renderer takes
  the audio device: MPD stops and further play fails (MO1, VO7, MO5).
- ``connections``: list open client connections.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiohttp
from aiohttp import web

from ...simkit.device import SimDevice
from ...simkit.transport import ReportLens, TState
from . import knobs as _knobs  # noqa: F401  (registers the family's knobs)
from .commands import COMMANDS
from .protocol import PERM_ALL, Ack, ProtocolError, parse_version
from .server import Conn, MpdServer, Reply

MPD_STATES = {
    TState.NO_MEDIA: "stop",
    TState.STOPPED: "stop",
    TState.LOADING: "play",
    TState.BUFFERING: "play",
    TState.PLAYING: "play",
    TState.PAUSED: "pause",
}

# what MPD's decoders report for MA's output codecs: (kbit/s, "audio:" format)
CODEC_INFO = {
    "mp3": (320, "44100:24:2"),  # libmad decodes to 24 bit
    "aac": (256, "44100:f:2"),  # ffmpeg decodes to float
    "wav": (1411, "44100:16:2"),
    "flac": (1000, "44100:16:2"),
}


def codec_of(content_type: str | None, uri: str) -> str | None:
    ct = (content_type or "").split(";")[0].strip().lower()
    for key, names in (
        ("mp3", ("audio/mpeg", "audio/mp3")),
        ("aac", ("audio/aac", "audio/aacp", "audio/mp4", "audio/x-m4a")),
        ("wav", ("audio/wav", "audio/x-wav", "audio/wave", "audio/l16")),
        ("flac", ("audio/flac", "audio/x-flac")),
    ):
        if ct in names:
            return key
    path = uri.split("?", 1)[0].lower()
    for ext in ("mp3", "aac", "wav", "flac"):
        if path.endswith("." + ext):
            return ext
    return None


@dataclass
class Song:
    id: int
    uri: str
    version: int  # playlist version when added
    added: float  # wall clock
    name: str | None = None  # icy-name header
    title: str | None = None
    duration: float | None = None
    content_length: int | None = None
    codec: str | None = None
    opened: bool = False


class MpdLens(ReportLens):
    def mpd_state(self) -> str:
        return MPD_STATES[self.state()]


class MpdDevice(SimDevice):
    family = "mpd"
    lens_cls = MpdLens
    lens: MpdLens

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.queue: list[Song] = []
        self.current = -1  # MPD's "song" pointer
        self.playlist_version = 1
        self._next_id = 1
        self.options = {
            "repeat": "0",
            "random": "0",
            "single": "0",
            "consume": "1" if self.knobs.get("initial_consume") else "0",
        }
        self.xfade = 0
        self.volume = int(self.identity.get("initial_volume", 50))
        self.error: str | None = None
        self.outputs = [
            {
                "id": 0,
                "name": self.identity.get("output_name", "ALSA Default"),
                "plugin": self.identity.get("output_plugin", "alsa"),
                "enabled": True,
            }
        ]
        self.renderer: str | None = None
        self.started_at = self.clock.monotonic()
        self._play_started: float | None = None
        self._playtime = 0.0
        self._pause_after_seek = False
        self._resp_headers: dict[str, dict[str, str]] = {}
        self._bg: set[asyncio.Future[Any]] = set()
        self.mpd = MpdServer(self)
        self._ext = Conn(id=0, peer="external", perms=PERM_ALL, role="external")
        self.transport.add_listener(self._on_transport)
        if self.sink is not None:
            self.sink.add_listener(self._on_sink)

    # -- identity / version ------------------------------------------------------------

    @property
    def protocol_version(self) -> str:
        return self.knobs.get("version")

    @property
    def version(self) -> tuple[int, int, int]:
        return parse_version(self.protocol_version)

    @property
    def mopidy(self) -> bool:
        return self.knobs.get("status_dialect") == "mopidy"

    @property
    def mpd_port(self) -> int:
        return self.mpd.port

    def supports(self, name: str) -> bool:
        cmd = COMMANDS.get(name)
        if cmd is None or self.version < cmd.since:
            return False
        unsupported = {c.strip() for c in self.knobs.get("unsupported_commands").split(",")}
        return name not in unsupported

    def url_schemes(self) -> list[str]:
        return ["http", "https"]

    # -- lifecycle ------------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = self.make_app()
        app.router.add_get("/", self._index)
        return app

    async def _index(self, _req: web.Request) -> web.Response:
        return web.json_response(
            {"family": "mpd", "mpd": f"{self.ctx.host}:{self.mpd.port}", "id": self.id}
        )

    async def on_start(self) -> None:
        await self.mpd.start(self.ctx.host, self.knobs.get("mpd_port"))
        if self.sink is not None:
            ua = self.identity.get("user_agent", "Music Player Daemon {version}").format(
                version=self.protocol_version
            )
            headers = {"User-Agent": ua, "Accept": "*/*"}
            if self.knobs.get("icy_metadata_header"):
                headers["Icy-MetaData"] = "1"
            trace = aiohttp.TraceConfig()
            trace.on_request_start.append(self._on_http_start)
            trace.on_request_end.append(self._on_http_end)
            # the core sink creates its session lazily; hand it one that speaks like MPD
            self.sink._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60),
                trace_configs=[trace],
            )
            self.sink._own_session = True
        self.rec.log(
            "listening",
            url=f"mpd://{self.ctx.host}:{self.mpd.port}",
            proto="mpd",
            version=self.protocol_version,
        )

    async def on_stop(self) -> None:
        await self.mpd.stop()
        for t in list(self._bg):
            t.cancel()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # -- idle helper ----------------------------------------------------------------

    def idle(self, *subsystems: str) -> None:
        self.mpd.idle_add(*subsystems)

    def _fired(self, knob: str, **data: Any) -> None:
        """Record a non-bool knob (enum/str/int) taking effect."""
        d = self.knobs.defs[knob]
        self.rec.knob("fired", knob=knob, quirks=list(d.quirks), confidence=d.confidence, **data)

    # -- transport / sink events ---------------------------------------------------------

    def _on_transport(self, name: str, data: dict[str, Any]) -> None:
        if name == "state":
            old, new = MPD_STATES[data["old"]], MPD_STATES[data["new"]]
            now = self.clock.monotonic()
            if data["new"] == TState.PLAYING and self._play_started is None:
                self._play_started = now
            elif data["new"] != TState.PLAYING and self._play_started is not None:
                self._playtime += now - self._play_started
                self._play_started = None
            if data["new"] == TState.PLAYING and self._pause_after_seek:
                self._pause_after_seek = False
                self._spawn(self.transport.pause())
            if old != new:
                self.idle("player")
        elif name == "advanced":
            self._spawn(self._advanced())
        elif name == "track_ended":
            self._spawn(self._ended(str(data.get("reason"))))
        elif name == "error":
            err = str(data.get("error"))
            if "HTTP " in err:
                err = "got HTTP status " + err.rsplit("HTTP ", 1)[1].split()[0]
            song = self.current_song()
            self.error = f"Failed to open {song.uri!r}: {err}" if song else err
            self.idle("player")

    async def _advanced(self) -> None:
        cur = self.transport.current
        sid = cur.extra.get("song_id") if cur else None
        pos = self.pos_of_id(sid) if sid is not None else None
        prev = self.current_song()
        if prev is not None and self.options["consume"] != "0" and prev.id != sid:
            self._remove_song(prev)
            if self.options["consume"] == "oneshot":
                self.options["consume"] = "0"
                self.idle("options")
            pos = self.pos_of_id(sid) if sid is not None else None
        if pos is not None:
            self.current = pos
        self._sync_next()
        self.idle("player")

    async def _ended(self, reason: str) -> None:
        await asyncio.sleep(0)
        if self.transport.state != TState.STOPPED:
            return  # it advanced to the next song
        song = self.current_song()
        if self.options["single"] == "oneshot":
            self.options["single"] = "0"
            self.idle("options")
        if song is not None and self.options["consume"] != "0" and reason != "error":
            self._remove_song(song)
            self.current = -1
            if self.options["consume"] == "oneshot":
                self.options["consume"] = "0"
                self.idle("options")

    def _on_sink(self, name: str, data: dict[str, Any]) -> None:
        if name != "opened":
            return
        song = self.current_song()
        if song is None:
            return
        song.opened = True
        song.content_length = data.get("content_length")
        song.codec = codec_of(data.get("content_type"), song.uri)
        hdrs = self._resp_headers.get(song.uri, {})
        if hdrs.get("icy-name"):
            song.name = hdrs["icy-name"]
        song.duration = None
        if (
            song.content_length
            and self.knobs.get("report_duration") == "content_length"
            and song.codec in CODEC_INFO
        ):
            kbps = CODEC_INFO[song.codec][0]
            song.duration = song.content_length * 8 / (kbps * 1000)
        elif song.content_length is None and self.knobs.get("report_duration") == "none":
            pass
        fail = {c.strip() for c in self.knobs.get("codec_probe_fail").split(",") if c.strip()}
        if song.codec in fail and not song.content_length:
            self._fired("codec_probe_fail", codec=song.codec, uri=song.uri)
            self.error = f"Failed to decode {song.uri}"
            self._spawn(self.transport.stop())
        self.idle("player")

    async def _on_http_start(self, _session: Any, _ctx: Any, params: Any) -> None:
        self.rec.event(
            "http_request",
            method=params.method,
            url=str(params.url),
            headers={k: v for k, v in params.headers.items() if k.lower() != "host"},
        )

    async def _on_http_end(self, _session: Any, _ctx: Any, params: Any) -> None:
        h = params.response.headers
        keep = {
            k.lower(): v
            for k, v in h.items()
            if k.lower() in ("content-type", "content-length", "icy-name", "icy-metaint")
        }
        self._resp_headers[str(params.url)] = keep
        self.rec.event("http_response", url=str(params.url), status=params.response.status, **keep)

    # -- state readers ---------------------------------------------------------------

    def mpd_state(self) -> str:
        return self.lens.mpd_state()

    def current_song(self) -> Song | None:
        if 0 <= self.current < len(self.queue):
            return self.queue[self.current]
        return None

    def pos_of_id(self, sid: int) -> int | None:
        for i, s in enumerate(self.queue):
            if s.id == sid:
                return i
        return None

    def elapsed(self) -> float | None:
        if self.mpd_state() == "stop":
            return None
        return self.lens.position() or 0.0

    def playtime(self) -> float:
        extra = self.clock.monotonic() - self._play_started if self._play_started else 0.0
        return self._playtime + extra

    def mixer_volume(self) -> int | None:
        return None if self.knobs.get("mixer") == "none" else self.volume

    def _next_pos(self) -> int | None:
        if self.current < 0 or self.options["single"] != "0":
            return None
        nxt = self.current + 1
        if nxt < len(self.queue):
            return nxt
        if self.options["repeat"] == "1" and self.queue:
            return 0
        return None

    def status_into(self, r: Reply) -> None:
        mopidy = self.mopidy
        vol = self.mixer_volume()
        if vol is not None:
            r.add("volume", vol)
        elif self.knobs.get("volume_unavailable") == "minus_one":
            r.add("volume", -1)
        for k in ("repeat", "random", "single", "consume"):
            r.add(k, self.options[k])
        if not mopidy and self.version >= (0, 22, 0):
            r.add("partition", "default")
        r.add("playlist", self.playlist_version)
        r.add("playlistlength", len(self.queue))
        if mopidy:
            r.add("xfade", self.xfade)
        else:
            r.add("mixrampdb", "0" if self.version >= (0, 23, 0) else "0.000000")
        state = self.mpd_state()
        r.add("state", state)
        if not mopidy and self.xfade > 0:
            r.add("xfade", self.xfade)
        song = self.current_song()
        if song is not None:
            r.add("song", self.current)
            r.add("songid", song.id)
        nxt = self._next_pos()
        if mopidy and nxt is not None:
            r.add("nextsong", nxt)
            r.add("nextsongid", self.queue[nxt].id)
        if state != "stop":
            elapsed = self.elapsed() or 0.0
            dur = (self.lens.duration() or song.duration) if song is not None else None
            kbps, fmt = CODEC_INFO.get(song.codec or "", (0, "")) if song else (0, "")
            r.add("time", f"{round(elapsed)}:{round(dur) if dur else 0}")
            r.add("elapsed", f"{elapsed:.3f}")
            r.add("bitrate", kbps if song and song.opened else 0)
            if not mopidy:
                if dur:
                    r.add("duration", f"{dur:.3f}")
                if fmt and song and song.opened:
                    r.add("audio", fmt)
        if self.error and not mopidy:
            r.add("error", self.error)
        if not mopidy and nxt is not None:
            r.add("nextsong", nxt)
            r.add("nextsongid", self.queue[nxt].id)

    def song_into(self, r: Reply, song: Song, pos: int) -> None:
        r.add("file", song.uri)
        if self.mopidy:
            r.add("Time", round(song.duration) if song.duration else 0)
            if song.name:
                r.add("Name", song.name)
            r.add("Pos", pos)
            r.add("Id", song.id)
            return
        if song.name:
            r.add("Name", song.name)
        if song.title:
            r.add("Title", song.title)
        if song.duration:
            r.add("Time", round(song.duration))
            r.add("duration", f"{song.duration:.3f}")
        r.add("Pos", pos)
        r.add("Id", song.id)
        if self.version >= (0, 24, 0):
            import time as _time

            r.add("Added", _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(song.added)))

    # -- queue -------------------------------------------------------------------------

    def parse_insert_pos(self, arg: str) -> int:
        from .protocol import parse_int

        n = len(self.queue)
        if arg[:1] in ("+", "-"):
            if self.current < 0:
                raise ProtocolError(Ack.ARG, "No current song")
            rel = parse_int(arg)
            pos = self.current + 1 + rel if arg[0] == "+" else self.current + rel
        else:
            pos = parse_int(arg)
        if pos < 0 or pos > n:
            raise ProtocolError(Ack.ARG, "Bad song index")
        return pos

    def add_uri(self, uri: str, pos: int | None = None) -> Song:
        if "://" in uri:
            scheme = uri.split("://", 1)[0].lower()
            if scheme not in self.url_schemes():
                raise ProtocolError(Ack.NO_EXIST, "Unsupported URI scheme")
        else:
            raise ProtocolError(Ack.NO_EXIST, "Not found")
        self.playlist_version += 1
        song = Song(
            id=self._next_id, uri=uri, version=self.playlist_version, added=self.clock.time()
        )
        self._next_id += 1
        if pos is None or pos >= len(self.queue):
            self.queue.append(song)
        else:
            self.queue.insert(pos, song)
            if self.current >= pos:
                self.current += 1
        self.idle("playlist")
        self._sync_next()
        if self.knobs.fires("add_probe_get", uri=uri):
            self._spawn(self._probe_get(uri, "add_probe"))
        return song

    def _remove_song(self, song: Song) -> None:
        pos = self.queue.index(song)
        self.queue.pop(pos)
        if self.current > pos:
            self.current -= 1
        elif self.current == pos:
            self.current = -1
        self.playlist_version += 1
        self.idle("playlist")

    async def clear(self) -> None:
        if self.transport.state not in (TState.NO_MEDIA, TState.STOPPED):
            await self.transport.stop()
        await self.transport.set_next(None)
        self.queue.clear()
        self.current = -1
        self.playlist_version += 1
        self.idle("playlist")

    async def delete_range(self, start: int, end: int) -> None:
        playing_deleted = start <= self.current < end and self.mpd_state() != "stop"
        if start <= self.current < end:
            if playing_deleted:
                await self.transport.stop()
            self.current = -1
        elif self.current >= end:
            self.current -= end - start
        del self.queue[start:end]
        self.playlist_version += 1
        self.idle("playlist")
        if playing_deleted and start < len(self.queue):
            await self.play_pos(start)
        else:
            self._sync_next()

    def _sync_next(self) -> None:
        nxt = self._next_pos()
        playing_id = self.transport.current.extra.get("song_id") if self.transport.current else None
        cur = self.current_song()
        if nxt is None or cur is None or playing_id != cur.id:
            self._spawn(self.transport.set_next(None))
            return
        song = self.queue[nxt]
        self._spawn(self.transport.set_next(song.uri, title=song.title, song_id=song.id))

    # -- playback ---------------------------------------------------------------------

    async def play_pos(self, pos: int) -> None:
        song = self.queue[pos]
        self.error = None
        self.current = pos
        if self.transport.state not in (TState.NO_MEDIA, TState.STOPPED):
            await self.transport.stop()
        if self.knobs.fires("output_busy", renderer=self.renderer):
            # MPD accepts `play`, then fails to open the output: no audio, error line
            self.error = self.knobs.get("output_error_text")
            self.idle("player")
            return
        await self.transport.set_uri(song.uri, title=song.title, song_id=song.id)
        self._sync_next()
        await self.transport.play()
        self.idle("player")

    async def play_current_or_first(self) -> None:
        state = self.mpd_state()
        if state == "pause":
            await self.transport.play()
            self.idle("player")
            return
        if state == "play" or not self.queue:
            return
        await self.play_pos(self.current if self.current >= 0 else 0)

    async def pause(self, want: bool | None) -> None:
        state = self.mpd_state()
        if state == "stop":
            if want is False and not self.knobs.get("pause_in_stop_noop"):
                await self.play_current_or_first()
                return
            self.knobs.fires("pause_in_stop_noop", arg=want)
            return
        if want is None:
            want = state == "play"
        if want and state == "play":
            await self.transport.pause()
        elif not want and state == "pause":
            await self.transport.play()
        self.idle("player")

    async def stop_playback(self) -> None:
        if self.transport.state not in (TState.NO_MEDIA, TState.STOPPED):
            await self.transport.stop()
        self.idle("player")

    async def next_song(self) -> None:
        if self.mpd_state() == "stop":
            return
        nxt = self.current + 1
        if nxt < len(self.queue):
            await self.play_pos(nxt)
        elif self.options["repeat"] == "1" and self.queue:
            await self.play_pos(0)
        else:
            await self.stop_playback()

    async def previous_song(self) -> None:
        if self.mpd_state() == "stop" or self.current < 0:
            return
        await self.play_pos(max(0, self.current - 1))

    async def seek_pos(self, pos: int, t: float) -> None:
        song = self.queue[pos] if 0 <= pos < len(self.queue) else None
        if song is None:
            raise ProtocolError(Ack.ARG, "Bad song index")
        if pos != self.current or self.mpd_state() == "stop":
            await self.play_pos(pos)
            if t > 0:
                await self.transport.seek(t)
            return
        if (
            song.opened
            and not song.content_length
            and self.knobs.fires("not_seekable_without_length", uri=song.uri)
        ):
            # pympd#166: the ACK names no command
            raise ProtocolError(Ack.UNKNOWN, "Not seekable", command="")
        paused = self.mpd_state() == "pause"
        await self.transport.seek(t)
        self._pause_after_seek = paused
        self.idle("player")

    def set_volume(self, level: int) -> None:
        mixer = self.knobs.get("mixer")
        if mixer in ("none", "failing"):
            self._fired("mixer", mixer=mixer, level=level)
            raise ProtocolError(Ack.SYSTEM, self.knobs.get("setvol_error"))
        self.volume = level
        self.idle("mixer")

    def set_option(self, name: str, value: str) -> None:
        self.options[name] = value
        self._sync_next()
        self.idle("options")

    # -- HTTP side effects (MP9, MPY1) -------------------------------------------------------

    async def _probe_get(self, url: str, why: str, max_bytes: int = 32768) -> tuple[int, bytes]:
        if self.sink is None:
            return 0, b""
        session = await self.sink._session_get()
        try:
            async with session.get(url) as resp:
                data = await resp.content.read(max_bytes) if resp.status < 400 else b""
                self.rec.event(why, url=url, status=resp.status, bytes=len(data))
                return resp.status, data
        except Exception as err:
            self.rec.event(why, url=url, error=f"{type(err).__name__}: {err}")
            return 0, b""

    async def fetch_art(self, uri: str, kind: str) -> bytes | None:
        if not uri.lower().startswith(("http://", "https://")):
            return None
        if not self.knobs.fires("art_probe_get", command=kind, uri=uri):
            return None
        if kind == "readpicture":
            await self._probe_get(uri, "art_probe")
            return None
        status, data = await self._probe_get(urljoin(uri, "cover.png"), "art_probe", 1 << 20)
        return data if status == 200 and data else None

    # -- sim hooks ----------------------------------------------------------------------

    async def external(self, line: str) -> str:
        """Run one command as another MPD client; returns the raw reply."""
        res = await self.mpd._run_one(self._ext, 0, line)
        if res is None:
            return ""
        data, ok = res
        return (data + (b"OK\n" if ok else b"")).decode("utf-8", errors="replace")

    def status(self) -> dict[str, Any]:
        return {
            "mpd_port": self.mpd.port,
            "version": self.protocol_version,
            "connections": [c.info() for c in self.mpd.conns.values()],
            "refused": self.mpd.refused,
            "queue": [{"id": s.id, "uri": s.uri, "duration": s.duration} for s in self.queue],
            "current": self.current,
            "playlist_version": self.playlist_version,
            "mpd_state": self.mpd_state(),
            "volume": self.mixer_volume(),
            "error": self.error,
            "options": dict(self.options),
            "renderer": self.renderer,
        }

    async def sim_action(self, name: str, payload: dict[str, Any]) -> Any:
        if name == "external":
            cmds = payload.get("cmds") or [payload["cmd"]]
            return [await self.external(c) for c in cmds]
        if name == "drop":
            return self.mpd.drop(str(payload.get("which", "all")))
        if name == "renderer":
            return await self.set_renderer(payload.get("renderer"))
        if name == "connections":
            return [c.info() for c in self.mpd.conns.values()]
        raise KeyError(name)

    async def set_renderer(self, name: str | None) -> dict[str, Any]:
        """moOde/Volumio: another renderer takes (or releases) the audio device."""
        if not name or name == "none":
            self.renderer = None
            self.knobs.set_live("output_busy", False)
            return {"renderer": None}
        self.renderer = name
        self.knobs.set_live("output_busy", True)
        await self.stop_playback()
        if name == "upnp" and self.knobs.fires("ownqueue_clears_mpd"):
            await self.clear()
        return {"renderer": name, "queue": len(self.queue)}


__all__ = ["MpdDevice", "MpdLens", "Song"]
