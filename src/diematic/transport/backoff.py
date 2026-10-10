from __future__ import annotations

import random
from collections.abc import Callable


class Backoff:
    """Exponential delay with proportional jitter, for reconnect loops."""

    def __init__(
        self,
        initial: float = 1.0,
        maximum: float = 30.0,
        factor: float = 2.0,
        jitter: float = 0.2,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._initial = initial
        self._maximum = maximum
        self._factor = factor
        self._jitter = jitter
        self._rng = rng
        self._current = initial

    def next_delay(self) -> float:
        base = self._current
        self._current = min(self._current * self._factor, self._maximum)
        return base * (1 + self._jitter * (2 * self._rng() - 1))

    def reset(self) -> None:
        self._current = self._initial
