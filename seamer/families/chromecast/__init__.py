"""Chromecast family: a CASTV2 receiver (TLS 8009), eureka_info (8008/8443) and
``_googlecast._tcp`` mDNS, driving a real HTTP fetch of each contentId. Alias: ``cast``."""

from pathlib import Path

from .. import Family
from .device import ChromecastDevice

FAMILY = Family(
    name="chromecast",
    device_cls=ChromecastDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="Google Cast receivers (Nest, Chromecast, Cast groups, vendor Cast stacks)",
    default_profile="nest_mini",
    default_port=8008,
    aliases=("cast",),
)
