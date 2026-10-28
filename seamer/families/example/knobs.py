"""Knobs of the example family. Importing this module registers them."""

from __future__ import annotations

from ...simkit.knobs import knob

FAMILY = "example"

knob(
    "ignore_pause",
    family=FAMILY,
    type="prob",
    default=False,
    quirks=("EX-01",),
    confidence="low",
    doc="Answers OK to pause but keeps playing (illustrative quirk).",
)
knob(
    "no_state_events",
    family=FAMILY,
    type="bool",
    default=False,
    quirks=("EX-02",),
    doc="State changes do not wake long-polls; clients only see them on timeout.",
)
knob(
    "report_state_names",
    family=FAMILY,
    type="enum",
    default="plain",
    choices=("plain", "upper"),
    doc="How state strings are spelled in /api/status.",
)
