"""BluOS family: Bluesound / NAD players and their HTTP XML API on port 11000."""

from pathlib import Path

from .. import Family
from .device import BluosDevice

FAMILY = Family(
    name="bluos",
    device_cls=BluosDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="BluOS player (Bluesound, NAD): HTTP XML API, etag long-poll, grouping",
    default_profile="node_n130",
    default_port=11000,
)
