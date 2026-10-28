"""simkit: the shared core every device family builds on.

Stable public surface (import from here):
"""

from . import core_knobs  # noqa: F401  (registers core knobs)
from .clock import Clock, RealClock, VirtualClock, patch_time, wait_with_timeout
from .device import DeviceContext, SimDevice
from .expect import Expect, ExpectationError, wait_for
from .kits.http import HttpServer, LongPoll, command_middleware
from .knobs import Knob, KnobError, KnobSet, Profile, ProfileError, knob, load_profile
from .media import MediaFactory, MediaServer, TrackMedia, tone_for_track, track_for_tone
from .recorder import Emitter, Event, Recorder, Timeline
from .rng import SeededRng
from .scenario import ScenarioResult, Scene, run_scenario, scenario
from .sink import BufferModel, HttpSink, OnServerClose, SinkState
from .transport import MediaSlot, ReportLens, TransportMachine, TState

__all__ = [
    "BufferModel",
    "Clock",
    "DeviceContext",
    "Emitter",
    "Event",
    "Expect",
    "ExpectationError",
    "HttpServer",
    "HttpSink",
    "Knob",
    "KnobError",
    "KnobSet",
    "LongPoll",
    "MediaFactory",
    "MediaServer",
    "MediaSlot",
    "OnServerClose",
    "Profile",
    "ProfileError",
    "RealClock",
    "Recorder",
    "ReportLens",
    "ScenarioResult",
    "Scene",
    "SeededRng",
    "SimDevice",
    "SinkState",
    "TState",
    "Timeline",
    "TrackMedia",
    "TransportMachine",
    "VirtualClock",
    "command_middleware",
    "knob",
    "load_profile",
    "patch_time",
    "run_scenario",
    "scenario",
    "tone_for_track",
    "track_for_tone",
    "wait_for",
    "wait_with_timeout",
]
