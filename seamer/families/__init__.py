"""Device family registry.

A family is a subpackage ``seamer/families/<name>/`` whose ``__init__.py`` defines
``FAMILY = Family(...)``. Families are discovered automatically (no central list to
edit), and third-party packages can add more through the ``seamer.families``
entry-point group (value: a module path that defines ``FAMILY``).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ..simkit.clock import Clock, RealClock
from ..simkit.device import DeviceContext, SimDevice
from ..simkit.knobs import Profile, list_profiles, load_profile
from ..simkit.recorder import Recorder


@dataclass(frozen=True)
class Family:
    name: str
    device_cls: type[SimDevice]
    profiles_dir: Path
    description: str = ""
    default_profile: str = "default"
    default_port: int = 0  # suggested port for `seamer run` when --port is omitted
    aliases: tuple[str, ...] = ()  # other names get_family accepts (e.g. "wiim")

    def profiles(self) -> list[str]:
        return list_profiles(self.profiles_dir)

    def load_profile(
        self, spec: str | None = None, overrides: Mapping[str, Any] | None = None
    ) -> Profile:
        return load_profile(spec or self.default_profile, self.name, self.profiles_dir, overrides)


class FamilyError(LookupError):
    pass


_CACHE: dict[str, Family] | None = None
_ERRORS: dict[str, str] = {}


def discover(refresh: bool = False) -> dict[str, Family]:
    """All families: built-in subpackages plus ``seamer.families`` entry points.

    A family that fails to import is skipped and its error kept in :func:`errors`.
    """
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    found: dict[str, Family] = {}
    _ERRORS.clear()
    modules = [f"{__name__}.{m.name}" for m in pkgutil.iter_modules(__path__) if m.ispkg]
    modules += [ep.value for ep in entry_points(group="seamer.families")]
    for modname in modules:
        try:
            mod = importlib.import_module(modname)
        except Exception as err:
            _ERRORS[modname] = repr(err)
            continue
        fam = getattr(mod, "FAMILY", None)
        if isinstance(fam, Family):
            found[fam.name] = fam
    _CACHE = found
    return found


def errors() -> dict[str, str]:
    return dict(_ERRORS)


def get_family(name: str) -> Family:
    fams = discover()
    if name not in fams:
        for fam in fams.values():
            if name in fam.aliases:
                return fam
        hint = f"; import errors: {_ERRORS}" if _ERRORS else ""
        raise FamilyError(f"unknown family {name!r} (have: {sorted(fams)}){hint}")
    return fams[name]


def build_device(
    family: str,
    profile: str | None = None,
    *,
    recorder: Recorder | None = None,
    clock: Clock | None = None,
    seed: int = 0,
    device_id: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    overrides: Mapping[str, Any] | None = None,
    **options: Any,
) -> SimDevice:
    """Create (but do not start) a device of ``family`` with ``profile``."""
    fam = get_family(family)
    prof = fam.load_profile(profile, overrides)
    clock = clock or RealClock()
    ctx = DeviceContext(
        device_id=device_id or f"{fam.name}1",
        profile=prof,
        recorder=recorder or Recorder(clock=clock),
        clock=clock,
        seed=seed,
        host=host,
        port=port,
        options=dict(options),
    )
    return fam.device_cls(ctx)


__all__ = ["Family", "FamilyError", "build_device", "discover", "errors", "get_family"]
