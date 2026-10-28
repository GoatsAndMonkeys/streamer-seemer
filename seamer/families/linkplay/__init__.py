"""LinkPlay / WiiM family: the HTTP API (httpapi.asp) plus the UPnP renderer MA's wiim
provider drives (AVTransport, RenderingControl, PlayQueue, GENA). Alias: ``wiim``."""

from pathlib import Path

from .. import Family
from .device import LinkPlayDevice

FAMILY = Family(
    name="linkplay",
    device_cls=LinkPlayDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="WiiM / LinkPlay (Audio Pro, Arylic): httpapi.asp + UPnP AV with GENA",
    default_profile="wiim_pro",
    aliases=("wiim",),
)
