"""MPD family: Music Player Daemon and MPD-protocol servers (moOde, Volumio, Mopidy)."""

from pathlib import Path

from .. import Family
from .device import MpdDevice

FAMILY = Family(
    name="mpd",
    device_cls=MpdDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="MPD text protocol over TCP (MPD 0.23/0.24, moOde, Volumio, Mopidy-MPD)",
    default_profile="mpd_0_23",
)
