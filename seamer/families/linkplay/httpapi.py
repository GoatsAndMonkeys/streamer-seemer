"""The LinkPlay HTTP API: ``GET /httpapi.asp?command=<cmd>``.

Replies follow linkplay2.md "Protocol reference": JSON bodies with string values
(``Content-Type: text/html``, as on the devices), plain ``OK``/``Failed``,
``unknown command`` with HTTP 200 for anything the profile does not support.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from aiohttp import web

from ...simkit.transport import TState
from . import proto

if TYPE_CHECKING:
    from .device import LinkPlayDevice

OK = "OK"
UNKNOWN = "unknown command"

# getSoundCardModeSupportList entries: identity "outputs" = "devName:mode,..."
DEFAULT_OUTPUTS = "Line Out:AUDIO_OUTPUT_AUX_MODE,Optical Out:AUDIO_OUTPUT_SPDIF_MODE"
HW_MODE = {
    "AUDIO_OUTPUT_SPDIF_MODE": "1",
    "AUDIO_OUTPUT_AUX_MODE": "2",
    "AUDIO_OUTPUT_COAX_MODE": "3",
}


def reply(body: str) -> web.Response:
    # the devices label everything text/html, JSON included
    return web.Response(text=body, content_type="text/html")


def jreply(obj: Any) -> web.Response:
    return reply(json.dumps(obj, separators=(",", ":")))


class HttpApi:
    def __init__(self, dev: LinkPlayDevice) -> None:
        self.dev = dev
        self._last_vol_at: float | None = None

    # -- entry -------------------------------------------------------------------------

    async def handle(self, req: web.Request) -> web.Response:
        raw = req.query_string
        # LinkPlay takes the raw command (URLs inside are not percent-decoded twice)
        command = unquote(raw.split("command=", 1)[1]) if "command=" in raw else ""
        dev = self.dev
        if self._unsupported(command):
            return reply(UNKNOWN)
        dev.clear_event_mute()
        head, _, rest = command.partition(":")
        fn = getattr(self, f"c_{head.replace('-', '_')}", None)
        if fn is None:
            return reply(UNKNOWN)
        return await fn(rest)

    def _unsupported(self, command: str) -> bool:
        items = [c.strip() for c in str(self.dev.knobs.get("unsupported_commands")).split(",")]
        return any(c and (command == c or command.startswith(c + ":")) for c in items)

    # -- status --------------------------------------------------------------------------

    async def c_getStatusEx(self, _rest: str) -> web.Response:
        return jreply(self.dev.status_ex())

    c_getStatus = c_getStatusEx

    async def c_getPlayerStatusEx(self, _rest: str) -> web.Response:
        dev = self.dev
        if dev.clock.monotonic() < dev.switchmode_empty_until:
            dev.rec.knob("fired", knob="switchmode_empty_body_s", quirks=["L67"])
            return reply("")
        body = dev.player_status()
        text = json.dumps(body, separators=(",", ":"))
        if dev.knobs.fires("json_corruption"):
            # L54: a doubled comma and a trailing '#'
            text = text.replace(',"mode"', ',,"mode"', 1) + "#"
        return reply(text)

    c_getPlayerStatus = c_getPlayerStatusEx

    async def c_getMetaInfo(self, _rest: str) -> web.Response:
        dev = self.dev
        style = dev.knobs.get("metainfo")
        if style == "unsupported":
            return reply(UNKNOWN)
        slot = dev.lens.metadata()
        if slot is None or dev.transport.state == TState.NO_MEDIA:
            return reply(dev.knobs.get("failure_word"))
        title, artist, album = proto.didl_title(slot.meta)
        sp = " " if style == "trailing_space" else ""
        art = dev.artwork_for(slot)
        return jreply(
            {
                "metaData": {
                    "album": album or slot.title or "",
                    "title": title or slot.title or "",
                    "artist": artist or "",
                    f"albumArtURI{sp}": art,
                    f"sampleRate{sp}": "44100",
                    f"bitDepth{sp}": "16",
                    f"bitRate{sp}": "1411",
                    f"trackId{sp}": "",
                }
            }
        )

    # -- player commands ---------------------------------------------------------------

    async def c_setPlayerCmd(self, rest: str) -> web.Response:
        dev = self.dev
        tm = dev.transport
        sub, _, arg = rest.partition(":")
        if sub == "play" and arg:
            await dev.load_and_play(arg, meta="", via="http")
        elif sub == "add":
            if not dev.knobs.fires("http_add_is_noop"):
                await tm.set_next(arg)
        elif sub in ("pause", "onepause"):
            if sub == "onepause" and tm.state == TState.PAUSED:
                await dev.resume()
            else:
                await tm.pause()
        elif sub == "resume":
            await dev.resume()
        elif sub == "stop":
            await tm.stop()
        elif sub == "next":
            await tm.next()
        elif sub == "prev":
            await tm.seek(0.0)
        elif sub == "seek":
            try:
                await tm.seek(float(arg))
            except ValueError:
                return reply(dev.knobs.get("failure_word"))
        elif sub == "vol":
            return await self._volume(arg)
        elif sub == "mute":
            dev.set_mute(arg == "1", via="http")
        elif sub == "loopmode":
            dev.set_loop_mode(int(arg) if arg.lstrip("-").isdigit() else 0)
        elif sub == "switchmode":
            return reply(await dev.switchmode(arg))
        else:
            return reply(UNKNOWN)
        return reply(OK)

    async def _volume(self, arg: str) -> web.Response:
        dev = self.dev
        try:
            level = int(arg)
        except ValueError:
            return reply(dev.knobs.get("failure_word"))
        window = float(dev.knobs.get("vol_collision_window_s"))
        now = dev.clock.monotonic()
        last, self._last_vol_at = self._last_vol_at, now
        if window > 0 and last is not None and now - last < window:
            dev.rec.knob("fired", knob="vol_collision_window_s", quirks=["L62"], dropped=level)
            await dev.clock.sleep(window - (now - last))
            return reply(OK)  # answered OK, never applied
        dev.set_volume(level, via="http")
        return reply(OK)

    # -- multiroom ----------------------------------------------------------------------

    async def c_multiroom(self, rest: str) -> web.Response:
        dev = self.dev
        sub, _, arg = rest.partition(":")
        if sub == "getSlaveList":
            return jreply(dev.slave_list())
        if sub == "Ungroup":
            await dev.ungroup()
        elif sub == "LeaveGroup":
            await dev.leave_group()
        elif sub == "SlaveKickout":
            await dev.kick(arg)
        elif sub == "JoinGroup":
            params = dict(p.split("=", 1) for p in arg.split(":") if "=" in p)
            if not await dev.join(uuid=params.get("uuid"), ip=params.get("IP")):
                return reply(dev.knobs.get("failure_word"))
        elif sub in ("SlaveVolume", "SlaveMute"):
            ip, _, val = arg.partition(":")
            follower = dev.follower_by_ip(ip)
            if follower is not None and val.isdigit():
                if sub == "SlaveVolume":
                    follower.set_volume(int(val), via="http")
                else:
                    follower.set_mute(val == "1", via="http")
        else:
            return reply(UNKNOWN)
        return reply(OK)

    async def c_ConnectMasterAp(self, rest: str) -> web.Response:
        # ConnectMasterAp:JoinGroupMaster:eth<leader_ip>:wifi0.0.0.0
        sub, _, arg = rest.partition(":")
        if sub != "JoinGroupMaster":
            return reply(UNKNOWN)
        ip = arg.split(":")[0].removeprefix("eth")
        if not await self.dev.join(uuid=None, ip=ip):
            return reply(self.dev.knobs.get("failure_word"))
        return reply(OK)

    # -- misc ----------------------------------------------------------------------------

    async def c_MCUKeyShortClick(self, rest: str) -> web.Response:
        return reply(OK if rest.isdigit() else self.dev.knobs.get("failure_word"))

    async def c_timeSync(self, _rest: str) -> web.Response:
        return reply(OK)

    async def c_StartRebootTime(self, _rest: str) -> web.Response:
        self.dev.rec.event("reboot_requested")
        return reply(OK)

    async def c_reboot(self, _rest: str) -> web.Response:
        style = self.dev.knobs.get("reboot_reply")
        self.dev.rec.event("reboot_requested")
        if style == "json_ok":
            return jreply({"status": "OK"})
        return reply("" if style == "empty" else UNKNOWN)

    async def _eq(self, _rest: str) -> web.Response:
        return jreply({"status": "OK"}) if self.dev.knobs.get("eq_reply_json") else reply(OK)

    c_EQOn = c_EQOff = c_EQLoad = _eq

    async def c_EQGetStat(self, _rest: str) -> web.Response:
        return jreply({"EQStat": "On"})

    async def c_EQGetList(self, _rest: str) -> web.Response:
        return jreply(["Flat", "Acoustic", "Bass Booster", "Classical", "Jazz", "Pop", "Rock"])

    async def c_getShutdown(self, _rest: str) -> web.Response:
        return reply("0")

    async def c_wlanGetConnectState(self, _rest: str) -> web.Response:
        return reply("OK")

    async def c_getAudioInputCapbility(self, _rest: str) -> web.Response:
        modes = self.dev.identity.get("inputs", "wifi,bluetooth")
        return jreply({"audioInput": [{"mode": m.strip()} for m in modes.split(",") if m]})

    def _outputs(self) -> list[tuple[str, str]]:
        spec = self.dev.identity.get("outputs", DEFAULT_OUTPUTS)
        return [tuple(p.split(":", 1)) for p in spec.split(",") if ":" in p]  # type: ignore[misc]

    async def c_getSoundCardModeSupportList(self, _rest: str) -> web.Response:
        return jreply(
            [
                {"mode": mode, "soundCard": {"devName": name, "cardId": i}, "index": i}
                for i, (name, mode) in enumerate(self._outputs())
            ]
        )

    async def c_getActiveSoundCardOutputMode(self, _rest: str) -> web.Response:
        outs = self._outputs()
        i = min(self.dev.output_index, len(outs) - 1) if outs else 0
        name, mode = outs[i] if outs else ("Line Out", "AUDIO_OUTPUT_AUX_MODE")
        return jreply(
            {"mode": mode, "devName": name, "cardId": i, "index": i, "btSource": 0, "audioCast": 0}
        )

    async def c_setSoundCardOutputMode(self, rest: str) -> web.Response:
        for i, (_name, mode) in enumerate(self._outputs()):
            if rest in (mode, str(i)):
                self.dev.output_index = i
                return reply(OK)
        return reply(self.dev.knobs.get("failure_word"))

    async def c_getNewAudioOutputHardwareMode(self, _rest: str) -> web.Response:
        outs = self._outputs()
        mode = outs[min(self.dev.output_index, len(outs) - 1)][1] if outs else ""
        return jreply({"hardware": HW_MODE.get(mode, "2"), "source": "0", "audiocast": "0"})

    async def c_setAudioOutputHardwareMode(self, rest: str) -> web.Response:
        for i, (_name, mode) in enumerate(self._outputs()):
            if HW_MODE.get(mode) == rest:
                self.dev.output_index = i
        return reply(OK)
