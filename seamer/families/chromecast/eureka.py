"""Device-info HTTP endpoint (``/setup/eureka_info`` on 8008/8443) and mDNS TXT records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from .device import ChromecastDevice

# 1x1 transparent PNG for /setup/icon.png
ICON_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def dashed(uuid_hex: str) -> str:
    h = uuid_hex.replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def eureka_body(dev: ChromecastDevice, params: set[str], detail: bool) -> dict[str, Any]:
    ident = dev.identity
    body: dict[str, Any] = {}
    if not params or "name" in params:
        body["name"] = dev.friendly_name
    if not params or "device_info" in params:
        body["device_info"] = {
            "manufacturer": ident.get("manufacturer", "Google Inc."),
            "model_name": dev.model_name,
            "capabilities": {
                "display_supported": dev.cast_type == "cast",
                "multizone_supported": str(ident.get("multizone_supported", True)) != "False",
            },
            "ssdp_udn": dashed(dev.uuid_hex),
        }
    if "multizone" in params:
        body["multizone"] = multizone_body(dev)
    if detail:
        body["mac_address"] = ident.get("mac", "02:00:00:00:00:01")
        body["cast_build_revision"] = ident.get("firmware", "")
    return body


def multizone_body(dev: ChromecastDevice) -> dict[str, Any]:
    groups = []
    for grp in dev.ident_list("groups"):
        entry: dict[str, Any] = {"name": grp.get("name", "Group"), "uuid": dashed(grp["uuid"])}
        if grp.get("multichannel_group"):
            entry["multichannel_group"] = True
            leader = "self" if grp.get("leader", True) else grp.get("leader_ip", "192.0.2.50")
            if dev.knobs.get("pair_schema") == "legacy":
                entry["cast_port"] = int(grp.get("cast_port", 32000))
                entry["elected_leader"] = leader
            else:
                entry["leader"] = leader
        else:
            entry["multichannel_group"] = False
        groups.append(entry)
    return {"groups": groups, "dynamic_groups": dev.ident_list("dynamic_groups")}


def txt_records(dev: ChromecastDevice) -> dict[str, str]:
    """``_googlecast._tcp`` TXT (id must be 32 hex chars or pychromecast drops it)."""
    app = dev.app
    return {
        "id": dev.uuid_hex,
        "cd": dev.uuid_hex.upper(),
        "rm": "",
        "ve": "05",
        "md": dev.model_name,
        "ic": "/setup/icon.png",
        "fn": dev.friendly_name,
        "ca": str(dev.identity.get("ca", 4101)),
        "st": "1" if app is not None else "0",
        "bs": dev.uuid_hex[:12].upper(),
        "nf": "1",
        "rs": app.status_text if app is not None else "",
    }


def mdns_instance_name(dev: ChromecastDevice) -> str:
    return f"{dev.model_name.replace(' ', '-')}-{dev.uuid_hex}"


def add_routes(app: web.Application, dev: ChromecastDevice) -> None:
    async def eureka(req: web.Request) -> web.Response:
        mode = dev.knobs.get("eureka_info")
        dev.rec.cmd("eureka_info", query=dict(req.query), mode=mode)
        if mode == "refused":  # nothing listens: drop the connection without a reply
            if req.transport is not None:
                req.transport.close()
            raise web.HTTPServiceUnavailable()
        if mode in ("404", "tls_fail"):  # C37: 8443 handshake fails, 8008 answers 404
            return web.Response(status=404, text="")
        if mode == "empty":
            return web.json_response({})
        params = {p for p in req.query.get("params", "").split(",") if p}
        detail = "detail" in req.query.get("options", "")
        return web.json_response(eureka_body(dev, params, detail))

    async def icon(_req: web.Request) -> web.Response:
        return web.Response(body=ICON_PNG, content_type="image/png")

    app.router.add_get("/setup/eureka_info", eureka)
    app.router.add_get("/setup/icon.png", icon)
