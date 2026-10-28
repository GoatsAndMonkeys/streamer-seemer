"""squeezelite family (alias ``slimproto``): a slimproto client that connects to the server."""

from pathlib import Path

from .. import Family
from .device import SqueezeliteDevice

FAMILY = Family(
    name="squeezelite",
    aliases=("slimproto",),
    device_cls=SqueezeliteDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="Slimproto player (squeezelite, piCorePlayer, ESP32, Squeezebox, WiiM)",
    default_profile="squeezelite_linux",
)
