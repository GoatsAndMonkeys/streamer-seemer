"""AirPlay receivers (AirPlay 2 and RAOP), driven through a fake ``cliairplay`` helper."""

from pathlib import Path

from .. import Family
from .device import AirplayDevice

FAMILY = Family(
    name="airplay",
    device_cls=AirplayDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="AirPlay receivers (HomePod, Sonos, WiiM, AirPort Express, shairport-sync) "
    "plus a fake cliairplay helper for Music Assistant.",
    default_profile="sonos_era100",
    default_port=7000,
)
