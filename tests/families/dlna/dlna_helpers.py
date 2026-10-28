"""Small DLNA control-point helpers for tests: SOAP calls and a GENA callback server.

Imported as ``import dlna_helpers`` (pytest puts this directory on sys.path; there is
no package ``__init__.py`` here on purpose).
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from xml.sax.saxutils import escape

import aiohttp
from aiohttp import web

from seamer.families.dlna import upnp
from seamer.simkit.kits.http import HttpServer

STYPE = {k: v[0] for k, v in upnp.SERVICES.items()}


class Soap:
    def __init__(self, dev: Any) -> None:
        self.dev = dev
        self.session = aiohttp.ClientSession()
        self.last_raw = b""
        self.last_status = 0

    async def close(self) -> None:
        await self.session.close()

    async def __aenter__(self) -> Soap:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def call(self, svc: str, action: str, **args: Any) -> tuple[int, dict[str, str]]:
        st = STYPE[svc]
        inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
        body = (
            '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{st}">{inner}</u:{action}></s:Body></s:Envelope>'
        )
        headers = {"SOAPACTION": f'"{st}#{action}"', "CONTENT-TYPE": 'text/xml; charset="utf-8"'}
        url = f"{self.dev.base_url}/upnp/{svc}/control"
        async with self.session.post(url, data=body, headers=headers) as r:
            raw = await r.read()
            self.last_raw, self.last_status = raw, r.status
            text = raw.decode("utf-8", "replace")
            try:
                out = upnp.soap_args(text) if text.startswith("<") else {}
            except Exception:
                out = {}
            return r.status, out

    async def avt(self, action: str, **args: Any) -> dict[str, str]:
        status, out = await self.call("avt", action, InstanceID=0, **args)
        assert status == 200, (action, status, out)
        assert "errorCode" not in out, (action, out)
        return out

    async def rc(self, action: str, **args: Any) -> dict[str, str]:
        status, out = await self.call("rc", action, InstanceID=0, Channel="Master", **args)
        assert status == 200, (action, status, out)
        assert "errorCode" not in out, (action, out)
        return out

    async def fault(self, svc: str, action: str, **args: Any) -> int:
        """Call and return the UPnP errorCode (asserts there is one)."""
        if svc in ("avt", "rc"):
            args = {"InstanceID": 0, **args}
        _status, out = await self.call(svc, action, **args)
        assert "errorCode" in out, (action, out)
        return int(out["errorCode"])

    async def load(self, url: str, title: str = "T", duration: str = "0:00:02.000") -> None:
        await self.avt(
            "SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=didl(title, duration, url)
        )

    async def state(self) -> str:
        return (await self.avt("GetTransportInfo"))["CurrentTransportState"]


def didl(title: str, duration: str = "0:00:02.000", uri: str = "", mime: str = "audio/wav") -> str:
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="1" parentID="0" '
        f'restricted="1"><dc:title>{title}</dc:title><upnp:artist>Test Artist</upnp:artist>'
        f'<upnp:class>object.item.audioItem.musicTrack</upnp:class><res duration="{duration}" '
        f'protocolInfo="http-get:*:{mime}:*">{escape(uri)}</res></item></DIDL-Lite>'
    )


def attr(xml_text: str, tag: str) -> str | None:
    m = re.search(rf'<{tag} [^>]*val="([^"]*)"', xml_text)
    return m.group(1) if m else None


class GenaSink:
    """A NOTIFY callback server. ``bodies(service)`` returns unescaped propertysets."""

    def __init__(self) -> None:
        self.got: list[tuple[str, str, dict[str, str], bytes]] = []  # (svc, body, hdrs, raw)
        app = web.Application()
        app.router.add_route("NOTIFY", "/{svc}", self._notify)
        self.server = HttpServer(app)
        self.session = aiohttp.ClientSession()
        self.sub_replies: list[float] = []

    async def _notify(self, req: web.Request) -> web.Response:
        raw = await req.read()
        self.got.append(
            (
                req.match_info["svc"],
                unescape(raw.decode("utf-8", "replace")),
                dict(req.headers),
                raw,
            )
        )
        return web.Response()

    async def start(self) -> GenaSink:
        await self.server.start()
        return self

    async def stop(self) -> None:
        await self.session.close()
        await self.server.stop()

    async def __aenter__(self) -> GenaSink:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def subscribe(
        self, dev: Any, svc: str, timeout: str = "Second-300"
    ) -> tuple[int, dict[str, str]]:
        headers = {
            "CALLBACK": f"<{self.server.base_url}/{svc}>",
            "NT": "upnp:event",
            "TIMEOUT": timeout,
        }
        async with self.session.request(
            "SUBSCRIBE", f"{dev.base_url}/upnp/{svc}/event", headers=headers
        ) as r:
            self.sub_replies.append(len(self.got))  # NOTIFYs seen before the reply
            return r.status, dict(r.headers)

    async def renew(self, dev: Any, svc: str, sid: str) -> tuple[int, dict[str, str]]:
        headers = {"SID": sid, "TIMEOUT": "Second-300"}
        async with self.session.request(
            "SUBSCRIBE", f"{dev.base_url}/upnp/{svc}/event", headers=headers
        ) as r:
            return r.status, dict(r.headers)

    def bodies(self, svc: str) -> list[str]:
        return [b for s, b, _h, _r in self.got if s == svc]
