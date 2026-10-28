"""Generic DLNA/UPnP renderer family: AVTransport, RenderingControl, ConnectionManager
with GENA, plus optional OpenHome (Product/Info/Time/Volume). What MA's ``dlna``
provider drives through async_upnp_client."""

from pathlib import Path

from .. import Family
from .device import DlnaDevice

FAMILY = Family(
    name="dlna",
    device_cls=DlnaDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="Generic DLNA/UPnP MediaRenderer (gmrender, LUMIN/TEAC, Eversolo, Samsung TV)",
    default_profile="generic_cheap",
)
