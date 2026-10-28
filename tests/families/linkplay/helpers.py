"""Small LinkPlay clients for tests: httpapi.asp over the device's TLS setup, and SOAP."""

from __future__ import annotations

import json
import re
from typing import Any
from xml.sax.saxutils import escape

import aiohttp

from seamer.families.linkplay import upnp

STYPE = {k: v[0] for k, v in upnp.SERVICES.items()}


class Client:
    def __init__(self, dev: Any, with_cert: bool = True) -> None:
        self.dev = dev
        self.ssl = dev.client_ssl_context(with_cert) if "https" in dev.api_urls else None
        self.session = aiohttp.ClientSession()

    async def close(self) -> None:
        await self.session.close()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def cmd(self, command: str, base: str | None = None) -> tuple[int, str]:
        url = f"{base or self.dev.api_url}/httpapi.asp?command={command}"
        async with self.session.get(url, ssl=self.ssl if url.startswith("https") else None) as r:
            return r.status, await r.text()

    async def cmdj(self, command: str) -> Any:
        status, text = await self.cmd(command)
        assert status == 200, text
        return json.loads(text)

    async def soap(self, svc: str, action: str, **args: Any) -> tuple[int, dict[str, str]]:
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
            text = await r.text()
            return r.status, (upnp.soap_args(text) if text.startswith("<") else {})

    async def avt(self, action: str, **args: Any) -> dict[str, str]:
        status, out = await self.soap("avt", action, InstanceID=0, **args)
        assert status == 200, (action, out)
        return out


def didl(title: str, duration: str = "0:00:02.000", uri: str = "") -> str:
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="1" parentID="0" '
        f'restricted="1"><dc:title>{title}</dc:title><upnp:artist>Test Artist</upnp:artist>'
        f'<upnp:class>object.item.audioItem.musicTrack</upnp:class><res duration="{duration}" '
        f'protocolInfo="http-get:*:audio/wav:*">{escape(uri)}</res></item></DIDL-Lite>'
    )


def attr(xml_text: str, tag: str) -> str | None:
    m = re.search(rf'<{tag} [^>]*val="([^"]*)"', xml_text)
    return m.group(1) if m else None


class GenaSink:
    """A NOTIFY callback server; ``bodies(service)`` returns the unescaped propertysets."""

    def __init__(self) -> None:
        from aiohttp import web

        from seamer.simkit.kits.http import HttpServer

        self.got: list[tuple[str, str]] = []  # (path, body)
        app = web.Application()
        app.router.add_route("NOTIFY", "/{svc}", self._notify)
        self.server = HttpServer(app)
        self.session = aiohttp.ClientSession()

    async def _notify(self, req: Any) -> Any:
        from html import unescape

        from aiohttp import web

        self.got.append((req.match_info["svc"], unescape(await req.text())))
        return web.Response()

    async def start(self) -> GenaSink:
        await self.server.start()
        return self

    async def stop(self) -> None:
        await self.session.close()
        await self.server.stop()

    async def subscribe(self, dev: Any, svc: str) -> tuple[int, dict[str, str]]:
        headers = {
            "CALLBACK": f"<{self.server.base_url}/{svc}>",
            "NT": "upnp:event",
            "TIMEOUT": "Second-300",
        }
        async with self.session.request(
            "SUBSCRIBE", f"{dev.base_url}/upnp/{svc}/event", headers=headers
        ) as r:
            return r.status, dict(r.headers)

    def bodies(self, svc: str) -> list[str]:
        return [b for s, b in self.got if s == svc]
