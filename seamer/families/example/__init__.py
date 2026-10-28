"""Example family: the smallest complete device, as a template for new families."""

from pathlib import Path

from .. import Family
from .device import ExampleDevice

FAMILY = Family(
    name="example",
    device_cls=ExampleDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="Tiny JSON-over-HTTP player (template for new families)",
    default_profile="basic",
)
