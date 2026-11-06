"""Deterministic clock for replay."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


class ManualClock:
    """A clock that only moves when told to. `now` is seconds on an arbitrary monotonic axis."""

    def __init__(self, start: float = 0.0, epoch_offset: float = 1_700_000_000.0) -> None:
        self.now = start
        self.epoch_offset = epoch_offset

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.epoch_offset + self.now

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.time(), tz=timezone.utc)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self.now += seconds

    def set(self, now: float) -> None:
        if now < self.now:
            raise ValueError("clock cannot go backwards")
        self.now = now


@contextmanager
def patched_time(module, clock: ManualClock) -> Iterator[ManualClock]:
    """Make `module.time.monotonic()` / `module.time.time()` read `clock` for the duration.

    Production code does `import time; time.monotonic()`, so we bind a private shim as `module.time`,
    leaving the real `time` module untouched for everything else.
    """
    class _TimeShim:
        monotonic = staticmethod(clock.monotonic)
        time = staticmethod(clock.time)

    original = module.time
    module.time = _TimeShim
    try:
        yield clock
    finally:
        module.time = original
