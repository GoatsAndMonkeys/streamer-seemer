"""Minimal discovery helpers. Off by default: harnesses normally inject devices directly.

- :class:`SsdpResponder`: answers ``M-SEARCH`` datagrams with unicast replies. It binds
  a plain UDP socket (default ``127.0.0.1``), joins no multicast group, and so never
  touches the LAN unless you bind it to a LAN address yourself.
- :class:`MdnsAdvertiser`: registers a ``_service._tcp.local.`` record via the optional
  ``zeroconf`` extra (``pip install streamer-seamer[mdns]``). Multicast: only use on an
  isolated network.
"""

from __future__ import annotations

import asyncio
import email.utils
from dataclasses import dataclass, field
from typing import Any

from ..recorder import Emitter


def ssdp_response(
    location: str,
    st: str,
    usn: str,
    server: str = "Linux UPnP/1.0 seamer/0.1",
    max_age: int = 1800,
    extra: dict[str, str] | None = None,
) -> bytes:
    lines = [
        "HTTP/1.1 200 OK",
        f"CACHE-CONTROL: max-age={max_age}",
        f"DATE: {email.utils.formatdate(usegmt=True)}",
        "EXT:",
        f"LOCATION: {location}",
        f"SERVER: {server}",
        f"ST: {st}",
        f"USN: {usn}",
    ]
    lines += [f"{k}: {v}" for k, v in (extra or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def ssdp_notify(
    location: str,
    nt: str,
    usn: str,
    nts: str = "ssdp:alive",
    server: str = "Linux UPnP/1.0 seamer/0.1",
    max_age: int = 1800,
) -> bytes:
    lines = [
        "NOTIFY * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        f"CACHE-CONTROL: max-age={max_age}",
        f"LOCATION: {location}",
        f"NT: {nt}",
        f"NTS: {nts}",
        f"SERVER: {server}",
        f"USN: {usn}",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def parse_ssdp(data: bytes) -> tuple[str, dict[str, str]]:
    text = data.decode("utf-8", "replace")
    first, *rest = text.split("\r\n")
    headers = {}
    for line in rest:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().upper()] = v.strip()
    return first, headers


@dataclass
class SsdpTarget:
    location: str
    udn: str  # "uuid:..."
    search_targets: list[str] = field(default_factory=list)  # device/service types

    def replies(self, st: str) -> list[tuple[str, str]]:
        """``(ST, USN)`` pairs answering a search for ``st``."""
        all_st = ["upnp:rootdevice", self.udn, *self.search_targets]
        out = []
        for cand in all_st:
            if st in ("ssdp:all", cand):
                usn = self.udn if cand == self.udn else f"{self.udn}::{cand}"
                out.append((cand, usn))
        return out


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: SsdpResponder) -> None:
        self.owner = owner
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        first, headers = parse_ssdp(data)
        if not first.startswith("M-SEARCH"):
            return
        st = headers.get("ST", "")
        if self.owner.rec is not None:
            self.owner.rec.cmd("ssdp_msearch", st=st, src=f"{addr[0]}:{addr[1]}")
        for target in self.owner.targets:
            for rst, usn in target.replies(st):
                assert self.transport is not None
                self.transport.sendto(ssdp_response(target.location, rst, usn), addr)


class SsdpResponder:
    def __init__(
        self,
        targets: list[SsdpTarget],
        host: str = "127.0.0.1",
        port: int = 0,
        rec: Emitter | None = None,
    ) -> None:
        self.targets = targets
        self.host, self.port = host, port
        self.rec = rec
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _SsdpProtocol(self), local_addr=(self.host, self.port)
        )
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class MdnsAdvertiser:
    """Register one mDNS service (needs the ``zeroconf`` extra)."""

    def __init__(
        self,
        service_type: str,
        name: str,
        port: int,
        addresses: list[str],
        properties: dict[str, str] | None = None,
    ) -> None:
        self.service_type = service_type  # e.g. "_musc._tcp.local."
        self.name = name
        self.port = port
        self.addresses = addresses
        self.properties = properties or {}
        self._zc: Any = None
        self._info: Any = None

    @staticmethod
    def available() -> bool:
        try:
            import zeroconf  # noqa: F401
        except ImportError:
            return False
        return True

    async def start(self) -> None:
        import ipaddress

        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        self._info = ServiceInfo(
            self.service_type,
            f"{self.name}.{self.service_type}",
            port=self.port,
            addresses=[ipaddress.ip_address(a).packed for a in self.addresses],
            properties=self.properties,
        )
        self._zc = AsyncZeroconf(interfaces=self.addresses)
        await self._zc.async_register_service(self._info)

    async def stop(self) -> None:
        if self._zc is not None:
            await self._zc.async_unregister_service(self._info)
            await self._zc.async_close()
            self._zc = None
