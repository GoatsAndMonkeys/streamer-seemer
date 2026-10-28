"""Shared boot code for the Sendspin bug hunt: a real MA dev server, loopback only.

Runs inside MA's interpreter (``PYTHONPATH=$MA_REPO:<seamer repo>``). Each scenario
gets a throwaway data dir and free high ports (never 8095/8097/8927/8928). MA's own
mDNS and aiosendspin's mDNS are switched off; devices are reached through the
provider's "manual discovery IPs" list, or through :meth:`Hunt.mdns_announce`, which
feeds aiosendspin's own zeroconf handlers with a synthetic service record (no
multicast leaves the process).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

FORBIDDEN_PORTS = {8095, 8097, 8927, 8928}


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS and port > 20000:
            return port


class _FakeServiceInfo:
    """Stands in for zeroconf's AsyncServiceInfo inside aiosendspin's mDNS handlers."""

    registry: dict[str, tuple[str, int, str]] = {}

    def __init__(self, service_type: str, name: str) -> None:
        self.name = name
        rec = self.registry.get(name)
        self._addr, self.port, path = rec if rec else (None, None, None)
        self.properties = {b"path": path.encode()} if path else {}

    def load_from_cache(self, _zc: Any) -> bool:
        return self._addr is not None

    async def async_request(self, _zc: Any, _timeout: int) -> bool:
        return self._addr is not None

    def parsed_addresses(self) -> list[str]:
        return [self._addr] if self._addr else []


def _patch_ma(mdns_emulation: bool) -> None:
    import aiosendspin.server.server as ss_server
    from aiosendspin.server.server import SendspinServer
    from music_assistant.controllers.discovery.controller import DiscoveryController
    from zeroconf import IPVersion
    from zeroconf.asyncio import AsyncZeroconf

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    DiscoveryController._register_mass_service = _noop  # type: ignore[method-assign]
    DiscoveryController._setup_mdns_browser = _noop  # type: ignore[method-assign]
    DiscoveryController._schedule_periodic_upnp_discovery = lambda self: None  # type: ignore[method-assign]
    DiscoveryController._create_aiozc = lambda self, config: AsyncZeroconf(  # type: ignore[method-assign]
        interfaces=["127.0.0.1"], ip_version=IPVersion.V4Only
    )
    SendspinServer._start_mdns_advertising = _noop  # type: ignore[method-assign]
    SendspinServer._start_mdns_discovery = _noop  # type: ignore[method-assign]
    if mdns_emulation:
        ss_server.AsyncServiceInfo = _FakeServiceInfo  # type: ignore[misc,assignment]


def host_time(dev: Any, dev_us: int) -> float:
    """Ground truth: the host monotonic time at which device clock reads ``dev_us``."""
    c = dev.dev_clock
    return c._t0 + (dev_us - c._origin_us - c.offset_us) / (1 + c.skew_ppm * 1e-6) / 1e6


class Hunt:
    """One scenario run: MA, a Scene with devices, helpers and the result dict."""

    def __init__(self, name: str, *, mdns_emulation: bool = False) -> None:
        self.name = name
        self.mdns_emulation = mdns_emulation
        self.result: dict[str, Any] = {"scenario": name}
        self.t0 = time.monotonic()
        self.work = Path(tempfile.mkdtemp(prefix=f"seamer-hunt-{name}-"))
        self.data_dir = self.work / "data"
        self.mass: Any = None
        self.scene: Any = None
        self.devices: list[Any] = []
        self.manual: list[Any] = []
        self.taps: dict[str, list[tuple[int, float, float, int]]] = {}
        self._scene_cm: Any = None

    # -- logging / waiting -------------------------------------------------------------

    def log(self, msg: str) -> None:
        print(f"[{self.name} {time.monotonic() - self.t0:6.1f}s] {msg}", flush=True)

    @staticmethod
    async def until(pred: Callable[[], Any], timeout: float, poll: float = 0.25) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(poll)
        return bool(pred())

    # -- boot --------------------------------------------------------------------------

    async def __aenter__(self) -> Hunt:
        import music_assistant.providers.sendspin.provider as ss_provider

        from seamer.simkit.scenario import Scene

        _patch_ma(self.mdns_emulation)
        self.web_port, self.stream_port, self.ss_port = free_port(), free_port(), free_port()
        assert not {self.web_port, self.stream_port, self.ss_port} & FORBIDDEN_PORTS
        ss_provider.SENDSPIN_SERVER_PORT = self.ss_port
        self.data_dir.mkdir()
        (self.data_dir / "settings.json").write_text(
            json.dumps(
                {
                    "core": {
                        "webserver": {
                            "domain": "webserver",
                            "values": {"bind_port": self.web_port, "bind_ip": "127.0.0.1"},
                        },
                        "streams": {
                            "domain": "streams",
                            "values": {
                                "bind_port": self.stream_port,
                                "bind_ip": "127.0.0.1",
                                "publish_ip": "127.0.0.1",
                            },
                        },
                    }
                }
            )
        )
        logging.basicConfig(
            level=logging.INFO,
            filename=self.work / "server.log",
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            force=True,
        )
        logging.getLogger("music_assistant.providers.sendspin").setLevel(logging.DEBUG)
        logging.getLogger("aiosendspin").setLevel(logging.DEBUG)
        self._scene_cm = Scene(self.work / "scene", name=self.name)
        self.scene = await self._scene_cm.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self.mass is not None:
                await self.mass.stop()
        finally:
            await self._scene_cm.__aexit__(*exc)
            if os.environ.get("SEAMER_KEEP"):
                self.result["work_dir"] = str(self.work)
            else:
                shutil.rmtree(self.work, ignore_errors=True)

    async def device(self, profile: str, *, manual: bool = True, **knobs: Any) -> Any:
        n = len(self.devices) + 1
        dev = await self.scene.device(profile, device_id=f"hunt{n}", **knobs)
        self.devices.append(dev)
        if manual:
            self.manual.append(dev)
        self.log(f"device {n} {profile} {dev.client_id[:12]} on port {dev.port} {knobs}")
        return dev

    @staticmethod
    def ws_url(dev: Any) -> str:
        return f"ws://127.0.0.1:{dev.port}/sendspin"

    async def start_ma(self) -> None:
        from music_assistant.mass import MusicAssistant

        self.mass = MusicAssistant(str(self.data_dir), str(self.work / "cache"), safe_mode=True)
        await self.mass.start()
        if self.manual and not self.result.get("_manual_saved"):
            prov = self.provider()
            # what a user types into "manual discovery IPs"; saving reloads the provider
            await self.mass.config.save_provider_config(
                "sendspin",
                {"manual_discovery_ip_addresses": [self.ws_url(d) for d in self.manual]},
                instance_id=prov.instance_id,
            )
            self.result["_manual_saved"] = True

    async def restart_ma(self, down_s: float = 0.0) -> None:
        """Stop MA and start a fresh instance on the same data dir and ports."""
        await self.mass.stop()
        self.mass = None
        await asyncio.sleep(down_s)
        await self.start_ma()

    def provider(self) -> Any:
        prov = self.mass.get_provider("sendspin")
        assert prov is not None, "sendspin provider not loaded"
        return prov

    def server(self) -> Any:
        return self.provider().server_api

    # -- players -----------------------------------------------------------------------

    def player(self, pid: str) -> Any:
        return self.mass.players.get_player(pid)

    async def registered(self, dev: Any, timeout: float = 30) -> bool:
        def ok() -> bool:
            p = self.player(dev.client_id)
            return p is not None and p.available

        return await self.until(ok, timeout)

    async def visible(self, dev: Any, timeout: float = 40) -> str:
        """The universal player MA wraps a MAC-carrying protocol player in."""

        def parent() -> Any:
            p = self.player(dev.client_id)
            return p.protocol_parent_id if p is not None else None

        await self.until(lambda: parent() is not None, timeout)
        return str(parent() or dev.client_id)

    def ma_state(self, pid: str) -> str:
        p = self.player(pid)
        return str(p.state.playback_state.value) if p is not None else "missing"

    async def play(self, queue_id: str, seconds: float = 90.0, **track_kw: Any) -> str | None:
        from music_assistant_models.errors import MusicAssistantError, PlayerUnavailableError

        await self.scene.media_server()
        url = self.scene.media_url(self.scene.media.track(1, seconds, **track_kw).path)
        await self.until(lambda: self.mass.player_queues.get(queue_id) is not None, 15)
        err: str | None = None
        for _ in range(40):
            try:
                await self.mass.player_queues.play_media(queue_id, url)
                return None
            except PlayerUnavailableError as e:
                err = repr(e)
                await asyncio.sleep(0.25)
            except MusicAssistantError as e:
                return repr(e)
        return err

    # -- probes ------------------------------------------------------------------------

    def tap(self, dev: Any) -> list[tuple[int, float, float, int]]:
        """Record (server_ts, true host play time, host arrival time, payload bytes)."""
        rows: list[tuple[int, float, float, int]] = []
        orig = dev.on_audio

        def tapped(server_ts: int, payload: bytes, header_len: int, play_at: int) -> None:
            rows.append((server_ts, host_time(dev, play_at), time.monotonic(), len(payload)))
            orig(server_ts, payload, header_len, play_at)

        dev.on_audio = tapped
        self.taps[dev.client_id] = rows
        return rows

    def count_commands(self, dev: Any) -> list[tuple[float, str, Any]]:
        """Record every server/command the device handles."""
        rows: list[tuple[float, str, Any]] = []
        orig = dev.on_command

        async def counted(command: Any, volume: Any, mute: Any, delay_ms: Any) -> None:
            rows.append((round(time.monotonic() - self.t0, 3), command, volume or mute))
            await orig(command, volume, mute, delay_ms)

        dev.on_command = counted
        return rows

    def loop_lag_monitor(self, period: float = 0.05) -> dict[str, float]:
        stats = {"max_lag_ms": 0.0}

        async def run() -> None:
            while True:
                t = time.monotonic()
                await asyncio.sleep(period)
                lag = (time.monotonic() - t - period) * 1000
                stats["max_lag_ms"] = max(stats["max_lag_ms"], round(lag, 1))

        self._lag_task = asyncio.ensure_future(run())
        return stats

    def mdns_announce(self, dev: Any, change: str) -> None:
        """Feed aiosendspin the zeroconf event a real device would cause.

        ``change`` is Added, Updated or Removed. Removed also drops the cached record.
        """
        from zeroconf import ServiceStateChange

        stype = "_sendspin._tcp.local."
        name = f"{dev.name}.{stype}"
        if change == "Removed":
            _FakeServiceInfo.registry.pop(name, None)
        else:
            _FakeServiceInfo.registry[name] = ("127.0.0.1", dev.port, "/sendspin")
        self.server()._on_mdns_service_state_change(
            None, stype, name, getattr(ServiceStateChange, change)
        )

    def server_log(self, *needles: str) -> dict[str, int]:
        text = (self.work / "server.log").read_text(errors="replace")
        return {n: text.count(n) for n in needles}


async def run(name: str, fn: Callable[[Hunt], Any], **kw: Any) -> dict[str, Any]:
    async with Hunt(name, **kw) as h:
        try:
            await fn(h)
        except Exception as e:
            h.result["error"] = repr(e)
            import traceback

            traceback.print_exc()
    h.result.pop("_manual_saved", None)
    return h.result
