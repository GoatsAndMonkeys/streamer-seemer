"""Knobs, profiles and the knob registry.

A *knob* is one modelled device behaviour (usually a quirk), declared once with a
type, a default, the quirk IDs it reproduces and a confidence level. Device code
reads knobs through a :class:`KnobSet`; it never hard-codes a quirk.

A *profile* is a TOML bundle of knob values plus identity. Device profiles live in
``<family>/profiles/<name>.toml``; cross-family fault bundles live in
``seamer/simkit/profiles/faults/<name>.toml`` and stack with ``+``::

    wiim_ultra_5_2+faults/events_dead+faults/slow_commands

Knob names are flat per device: core knobs (family ``"core"``) plus the knobs of the
device's family. A family knob may not reuse a core knob name.
"""

from __future__ import annotations

import enum
import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .recorder import Emitter
from .rng import SeededRng

Confidence = Literal["high", "medium", "low"]
KnobType = Literal["bool", "int", "float", "str", "enum", "prob", "dict"]

CORE = "core"
FAULTS_DIR = Path(__file__).parent / "profiles" / "faults"


class KnobError(ValueError):
    pass


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Knob:
    """Declaration of one knob.

    ``type`` is one of:

    - ``bool``/``int``/``float``/``str``: plain values (int accepted for float)
    - ``enum``: a string from ``choices``
    - ``prob``: probabilistic. Value is ``false``/``true``, a probability ``0..1``, or a
      table ``{p = 0.2, ...extra params}``. Read it with :meth:`KnobSet.fires`.
    - ``dict``: free-form table
    """

    name: str
    family: str
    type: KnobType
    default: Any
    quirks: tuple[str, ...] = ()
    confidence: Confidence = "medium"
    doc: str = ""
    choices: tuple[str, ...] = ()
    unit: str = ""

    def coerce(self, value: Any) -> Any:
        """Validate ``value`` for this knob and return the normalised value."""
        t = self.type
        if isinstance(value, enum.Enum):
            value = value.value
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
                return value.lower() in ("true", "1", "yes")
        elif t == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    pass
        elif t == "float":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    pass
        elif t == "str":
            if isinstance(value, str):
                return value
        elif t == "enum":
            if isinstance(value, str) and value in self.choices:
                return value
            raise KnobError(f"knob {self.name}: {value!r} not in {self.choices}")
        elif t == "prob":
            return _coerce_prob(self.name, value)
        elif t == "dict":
            if isinstance(value, Mapping):
                return dict(value)
        raise KnobError(f"knob {self.name}: {value!r} is not a valid {t}")


def _coerce_prob(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        low = value.lower()
        if low in ("true", "false"):
            value = low == "true"
        else:
            try:
                value = float(value)
            except ValueError as err:
                raise KnobError(f"knob {name}: {value!r} is not a probability") from err
    if isinstance(value, bool):
        return {"p": 1.0 if value else 0.0}
    if isinstance(value, (int, float)):
        if not 0.0 <= float(value) <= 1.0:
            raise KnobError(f"knob {name}: probability {value} outside 0..1")
        return {"p": float(value)}
    if isinstance(value, Mapping):
        out = dict(value)
        p = out.get("p", 1.0)
        if not isinstance(p, (int, float)) or not 0.0 <= float(p) <= 1.0:
            raise KnobError(f"knob {name}: p={p!r} outside 0..1")
        out["p"] = float(p)
        return out
    raise KnobError(f"knob {name}: {value!r} is not a probability")


# (family, name) -> Knob
REGISTRY: dict[tuple[str, str], Knob] = {}


def knob(
    name: str,
    *,
    family: str,
    type: KnobType,
    default: Any,
    quirks: Iterable[str] = (),
    confidence: Confidence = "medium",
    doc: str = "",
    choices: Iterable[str] = (),
    unit: str = "",
) -> Knob:
    """Declare and register a knob. Call at import time of the family's ``knobs`` module.

    Re-registering an identical knob is a no-op (module reloads); a conflicting one
    raises :class:`KnobError`.
    """
    k = Knob(
        name=name,
        family=family,
        type=type,
        default=None,
        quirks=tuple(quirks),
        confidence=confidence,
        doc=doc,
        choices=tuple(choices),
        unit=unit,
    )
    k = Knob(**{**k.__dict__, "default": k.coerce(default)})
    if family != CORE and (CORE, name) in REGISTRY:
        raise KnobError(f"knob {family}.{name} shadows core knob {name}")
    prev = REGISTRY.get((family, name))
    if prev is not None and prev != k:
        raise KnobError(f"knob {family}.{name} registered twice with different settings")
    REGISTRY[(family, name)] = k
    return k


def knobs_for(family: str) -> dict[str, Knob]:
    """All knobs a device of ``family`` has: core knobs plus the family's own."""
    from . import core_knobs  # noqa: F401  (registers core knobs)

    if family != CORE and not any(f == family for f, _ in REGISTRY):
        # importing the family package registers its knobs
        from ..families import discover

        discover()

    out = {n: k for (f, n), k in REGISTRY.items() if f == CORE}
    out.update({n: k for (f, n), k in REGISTRY.items() if f == family})
    return out


# ---------------------------------------------------------------------------
# Profiles


@dataclass
class Profile:
    """A resolved profile: identity, protocol surfaces and knob values."""

    name: str
    family: str
    identity: dict[str, str] = field(default_factory=dict)
    surfaces: list[str] = field(default_factory=list)
    knobs: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)  # files merged, in order
    description: str = ""

    def with_overrides(self, overrides: Mapping[str, Any], label: str = "override") -> Profile:
        return Profile(
            name=self.name,
            family=self.family,
            identity=dict(self.identity),
            surfaces=list(self.surfaces),
            knobs={**self.knobs, **overrides},
            sources=[*self.sources, label],
            description=self.description,
        )


def profile_dirs(family_dir: Path | None) -> list[Path]:
    dirs = []
    if family_dir is not None:
        dirs.append(family_dir)
    for extra in os.environ.get("SEAMER_PROFILE_PATH", "").split(os.pathsep):
        if extra:
            dirs.append(Path(extra))
    return dirs


def _find(part: str, family: str, family_dir: Path | None) -> Path:
    if part.endswith(".toml") and Path(part).is_file():
        return Path(part)
    if part.startswith("faults/"):
        candidate = FAULTS_DIR / f"{part.removeprefix('faults/')}.toml"
        if candidate.is_file():
            return candidate
    rel = part.removeprefix(f"{family}/")
    for d in profile_dirs(family_dir):
        for candidate in (d / f"{rel}.toml", d / family / f"{rel}.toml"):
            if candidate.is_file():
                return candidate
    raise ProfileError(f"profile {part!r} not found for family {family!r}")


def _load_file(
    path: Path, family: str, family_dir: Path | None, seen: tuple[Path, ...], is_fault: bool
) -> Profile:
    if path in seen:
        raise ProfileError(f"profile cycle: {' -> '.join(map(str, (*seen, path)))}")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as err:
        raise ProfileError(f"{path}: {err}") from err
    prof = Profile(name=path.stem, family=family)
    for parent in data.get("extends", []):
        base = _load_file(
            _find(parent, family, family_dir), family, family_dir, (*seen, path), is_fault
        )
        prof.identity.update(base.identity)
        prof.surfaces = base.surfaces or prof.surfaces
        prof.knobs.update(base.knobs)
        prof.sources.extend(base.sources)
    prof.identity.update({k: str(v) for k, v in data.get("identity", {}).items()})
    surfaces = data.get("surfaces", {})
    if isinstance(surfaces, Mapping):
        surfaces = surfaces.get("list", [])
    if surfaces:
        prof.surfaces = list(surfaces)
    prof.knobs.update(data.get("knobs", {}))
    prof.description = data.get("description", prof.description)
    prof.sources.append(str(path))
    return prof


def load_profile(
    spec: str,
    family: str,
    family_dir: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Profile:
    """Resolve ``spec`` (``name[+faults/x][+...]``) for ``family``.

    Unknown knobs in device profiles or overrides are errors. Unknown knobs in a
    ``faults/`` bundle are dropped, since bundles are shared across families.
    """
    known = knobs_for(family)
    parts = [p for p in spec.split("+") if p]
    if not parts:
        raise ProfileError("empty profile spec")
    result: Profile | None = None
    for part in parts:
        path = _find(part, family, family_dir)
        is_fault = path.parent == FAULTS_DIR
        prof = _load_file(path, family, family_dir, (), is_fault)
        if is_fault:
            prof.knobs = {k: v for k, v in prof.knobs.items() if k in known}
        else:
            unknown = sorted(set(prof.knobs) - set(known))
            if unknown:
                raise ProfileError(f"{path}: unknown knobs for {family}: {unknown}")
        if result is None:
            result = prof
        else:
            result.identity.update(prof.identity)
            result.surfaces = prof.surfaces or result.surfaces
            result.knobs.update(prof.knobs)
            result.sources.extend(prof.sources)
    assert result is not None
    result.name = spec
    if overrides:
        unknown = sorted(set(overrides) - set(known))
        if unknown:
            raise ProfileError(f"unknown knobs for {family}: {unknown}")
        result = result.with_overrides(overrides)
        result.name = spec
    # validate every value now so a bad profile fails at load, not mid-run
    for k, v in result.knobs.items():
        known[k].coerce(v)
    return result


def list_profiles(family_dir: Path | None) -> list[str]:
    names: set[str] = set()
    for d in profile_dirs(family_dir):
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.toml") if not p.stem.startswith("_"))
    return sorted(names)


def list_faults() -> list[str]:
    return sorted(f"faults/{p.stem}" for p in FAULTS_DIR.glob("*.toml"))


# ---------------------------------------------------------------------------
# KnobSet


class KnobSet:
    """The live knob values of one device.

    ``get`` returns the value; ``fires`` draws probabilistic knobs from the device's
    seeded RNG and records a ``knob`` event every time one fires.
    """

    def __init__(
        self,
        family: str,
        values: Mapping[str, Any] | None = None,
        rng: SeededRng | None = None,
        rec: Emitter | None = None,
    ) -> None:
        self.family = family
        self.defs = knobs_for(family)
        self.rng = rng or SeededRng()
        self.rec = rec
        self._values: dict[str, Any] = {n: k.default for n, k in self.defs.items()}
        for name, value in (values or {}).items():
            self._values[name] = self._def(name).coerce(value)

    @classmethod
    def from_profile(
        cls, profile: Profile, rng: SeededRng | None = None, rec: Emitter | None = None
    ) -> KnobSet:
        return cls(profile.family, profile.knobs, rng, rec)

    def _def(self, name: str) -> Knob:
        try:
            return self.defs[name]
        except KeyError:
            raise KnobError(f"unknown knob {name!r} for family {self.family!r}") from None

    def get(self, name: str) -> Any:
        self._def(name)
        return self._values[name]

    __getitem__ = get

    def param(self, name: str, key: str, default: Any = None) -> Any:
        """An extra parameter of a ``prob``/``dict`` knob, e.g. ``window_s``."""
        val = self.get(name)
        return val.get(key, default) if isinstance(val, Mapping) else default

    def enabled(self, name: str) -> bool:
        """Truthiness without a random draw (prob knobs: ``p > 0``)."""
        val = self.get(name)
        if self.defs[name].type == "prob":
            return val["p"] > 0
        return bool(val)

    def fires(self, name: str, **context: Any) -> bool:
        """Whether the quirk happens this time. Records ``knob``/``fired`` when it does."""
        d = self._def(name)
        val = self._values[name]
        if d.type == "prob":
            hit = self.rng.chance(f"knob:{name}", val["p"])
        elif d.type == "bool":
            hit = bool(val)
        else:
            raise KnobError(f"knob {name} ({d.type}) cannot fire; use get()")
        if hit and self.rec is not None:
            self.rec.knob(
                "fired", knob=name, quirks=list(d.quirks), confidence=d.confidence, **context
            )
        return hit

    def set_live(self, name: str, value: Any) -> Any:
        """Change a knob mid-run. Records ``knob``/``set``."""
        new = self._def(name).coerce(value)
        old = self._values[name]
        self._values[name] = new
        if self.rec is not None:
            self.rec.knob("set", knob=name, old=old, new=new)
        return new

    def values(self) -> dict[str, Any]:
        return dict(self._values)

    def non_default(self) -> dict[str, Any]:
        return {n: v for n, v in self._values.items() if v != self.defs[n].default}

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": n,
                "family": k.family,
                "type": k.type,
                "value": self._values[n],
                "default": k.default,
                "quirks": list(k.quirks),
                "confidence": k.confidence,
                "doc": k.doc,
                "choices": list(k.choices),
                "unit": k.unit,
            }
            for n, k in sorted(self.defs.items())
        ]
