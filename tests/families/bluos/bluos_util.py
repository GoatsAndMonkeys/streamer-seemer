"""Helpers shared by the BluOS family tests (imported by module name; no package)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import aiohttp

PROLOG = '<?xml version="1.0" encoding="UTF-8"?>\n'
FAST = {"connect_delay_s": {"min": 0.0, "max": 0.0}}


@dataclass
class Reply:
    status: int
    text: str
    content_type: str

    @property
    def body(self) -> str:
        return self.text.removeprefix(PROLOG)

    @property
    def xml(self) -> ET.Element:
        return ET.fromstring(self.body)

    def field(self, tag: str) -> str | None:
        el = self.xml.find(tag)
        return None if el is None else (el.text or "")


async def get(dev: Any, path: str, client_timeout: float = 10.0, **params: Any) -> Reply:
    q = {k: str(v) for k, v in params.items()}
    async with (
        aiohttp.ClientSession() as s,
        s.get(
            dev.base_url + path, params=q, timeout=aiohttp.ClientTimeout(total=client_timeout)
        ) as r,
    ):
        return Reply(r.status, await r.text(), r.headers.get("Content-Type", ""))


def _own(dev: Any) -> list[Any]:
    """Events from this device only (a scene's devices share one recorder)."""
    src = dev.rec.src
    return [e for e in dev.rec.rec.events if e.src == src or e.src.startswith(src + "/")]


def fired(dev: Any, knob: str) -> int:
    return sum(
        1
        for e in _own(dev)
        if e.kind == "knob" and e.name == "fired" and e.data.get("knob") == knob
    )


def events(dev: Any, name: str, kind: str = "event") -> list[dict[str, Any]]:
    return [e.data for e in _own(dev) if e.kind == kind and e.name == name]
