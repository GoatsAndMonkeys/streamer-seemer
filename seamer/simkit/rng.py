"""Seeded randomness with one independent stream per (device, purpose).

Deriving each stream from a hash of ``seed:device:name`` means adding a new knob
or a new device never shifts the draws of another one.
"""

from __future__ import annotations

import hashlib
import random


class SeededRng:
    """Factory of independent ``random.Random`` streams under one seed."""

    def __init__(self, seed: int = 0, device_id: str = "") -> None:
        self.seed = seed
        self.device_id = device_id
        self._streams: dict[str, random.Random] = {}

    def stream(self, name: str) -> random.Random:
        """Return the stream for ``name`` (created on first use)."""
        rnd = self._streams.get(name)
        if rnd is None:
            digest = hashlib.sha256(f"{self.seed}:{self.device_id}:{name}".encode()).digest()
            rnd = random.Random(int.from_bytes(digest[:8], "big"))
            self._streams[name] = rnd
        return rnd

    def for_device(self, device_id: str) -> SeededRng:
        """Same seed, different device: independent streams."""
        return SeededRng(self.seed, device_id)

    def chance(self, name: str, p: float) -> bool:
        """True with probability ``p`` on stream ``name``."""
        if p <= 0:
            return False
        if p >= 1:
            return True
        return self.stream(name).random() < p

    def uniform(self, name: str, a: float, b: float) -> float:
        return self.stream(name).uniform(a, b)
