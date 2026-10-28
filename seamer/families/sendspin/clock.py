"""The device's own clock: an imperfect crystal, as aiosendspin's ``Clock`` (``now_us``).

The real device measures time with its own oscillator. :class:`SkewedClock` runs at
``1 + skew_ppm * 1e-6`` against the simkit clock, starts at an offset and adds uniform
read jitter, while staying monotonic. aiosendspin's time filter has to track it, the
same way it has to track a real ESP32 crystal (SS10, SS32).
"""

from __future__ import annotations

import random

from ...simkit.clock import Clock


class SkewedClock:
    def __init__(
        self,
        base: Clock,
        *,
        skew_ppm: float = 0.0,
        offset_ms: float = 0.0,
        jitter_us: int = 0,
        rng: random.Random | None = None,
    ) -> None:
        self.base = base
        self.skew_ppm = skew_ppm
        self.offset_us = offset_ms * 1000
        self.jitter_us = jitter_us
        self.rng = rng or random.Random(0)
        self._t0 = base.monotonic()
        # start well above zero so offsets never make the clock negative
        self._origin_us = 1_000_000_000
        self._last = 0

    def ideal_us(self) -> float:
        """Device time without jitter (what a perfect read would return)."""
        elapsed = (self.base.monotonic() - self._t0) * 1e6
        return self._origin_us + self.offset_us + elapsed * (1 + self.skew_ppm * 1e-6)

    def now_us(self) -> int:
        t = self.ideal_us()
        if self.jitter_us:
            t += self.rng.uniform(-self.jitter_us, self.jitter_us)
        v = max(int(t), self._last + 1)
        self._last = v
        return v
