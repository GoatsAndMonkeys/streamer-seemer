"""pytest fixtures for seamer (auto-loaded through the ``pytest11`` entry point).

Fixtures:

- ``sim_seed``: from ``--sim-seed`` (default 0)
- ``virtual_clock``: a fresh :class:`VirtualClock`
- ``recorder``: an in-memory :class:`Recorder` (real clock)
- ``media``: a :class:`MediaFactory` in a session-wide cache dir
- ``scene``: an async :class:`Scene` (real clock) writing to ``tmp_path``; the timeline
  path is printed when the test fails. ``--sim-keep DIR`` copies timelines there.
- ``make_device``: async factory ``await make_device("example/basic", **knobs)`` that
  stops devices at teardown (uses the ``scene``).
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from .clock import VirtualClock
from .device import SimDevice
from .media import MediaFactory
from .recorder import Recorder
from .scenario import Scene


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("seamer")
    group.addoption("--sim-seed", type=int, default=0, help="seed for simulated devices")
    group.addoption("--sim-keep", default=None, help="copy scenario timelines to this dir")
    group.addoption("--sim-echo", action="store_true", help="print timeline events live")


@pytest.fixture
def sim_seed(request: pytest.FixtureRequest) -> int:
    return int(request.config.getoption("--sim-seed"))


@pytest.fixture
def virtual_clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> MediaFactory:
    return MediaFactory(tmp_path_factory.mktemp("seamer-media"))


@pytest_asyncio.fixture
async def scene(
    request: pytest.FixtureRequest, tmp_path: Path, sim_seed: int, media: MediaFactory
) -> AsyncIterator[Scene]:
    s = Scene(
        tmp_path / "scene",
        seed=sim_seed,
        echo=request.config.getoption("--sim-echo"),
        name=request.node.name,
        media_dir=media.root,
    )
    try:
        yield s
    finally:
        await s.close()
        keep = request.config.getoption("--sim-keep")
        if keep:
            dest = Path(keep) / f"{request.node.name}.jsonl"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(s.timeline_path, dest)
        rep = getattr(request.node, "rep_call", None)
        if rep is not None and rep.failed:
            print(f"\n[seamer] timeline: {s.timeline_path}")


@pytest.fixture
def make_device(scene: Scene) -> Callable[..., Awaitable[SimDevice]]:
    async def _make(profile: str, **knobs: Any) -> SimDevice:
        return await scene.device(profile, **knobs)

    return _make


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    rep = yield
    if rep.when == "call":
        item.rep_call = rep  # type: ignore[attr-defined]
    return rep
