"""Clocks, id factories and hashing, injectable so tests and evals are deterministic."""

from __future__ import annotations

import hashlib
import itertools
import time
import uuid
from datetime import datetime, timedelta, timezone


def sha256_hex(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part if isinstance(part, bytes) else part.encode())
        h.update(b"\x1f")
    return h.hexdigest()


class Clock:
    """Wall clock. `now()` is UTC ISO 8601; `monotonic()` is for latency."""

    def now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock(Clock):
    """Starts at a fixed time and advances one second per `now()` call. `sleep` advances without waiting."""

    def __init__(self, start: str = "2026-09-01T12:00:00Z"):
        self._t = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self._mono = 0.0
        self.slept: list[float] = []

    def now(self) -> str:
        self._t += timedelta(seconds=1)
        return self._t.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)
        self._mono += seconds

    def monotonic(self) -> float:
        return self._mono + time.monotonic()

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


class Ids:
    """Sequential ids per prefix (R-1001, A-1001, ...). Starts high to avoid the seeded ids."""

    def __init__(self, start: int = 1001):
        self._counters: dict[str, itertools.count] = {}
        self._start = start

    def new(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(self._start))
        return f"{prefix}-{next(counter)}"


class UuidIds(Ids):
    def new(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"
