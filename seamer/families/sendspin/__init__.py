"""Sendspin family: ESPHome/ESP32 speakers, Voice PE and the Linux Sendspin clients."""

from pathlib import Path

from .. import Family
from .device import SendspinDevice

FAMILY = Family(
    name="sendspin",
    device_cls=SendspinDevice,
    profiles_dir=Path(__file__).parent / "profiles",
    description="Sendspin players MA dials over WebSocket (G2 Noise via aiosendspin, G0 legacy)",
    default_profile="esphome_s3_psram",
    default_port=8928,
)
