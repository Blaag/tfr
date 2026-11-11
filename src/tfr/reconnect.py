from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 120.0
    window_seconds: float = 7_200.0
    stable_reset_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not 0 < self.initial_delay_seconds <= 300:
            raise ValueError("reconnect initial delay must be positive and at most 300 seconds")
        if not self.initial_delay_seconds <= self.max_delay_seconds <= 3_600:
            raise ValueError("reconnect maximum delay must be at least the initial delay")
        if not 60 <= self.window_seconds <= 86_400:
            raise ValueError("reconnect window must be between 60 and 86400 seconds")
        if not 1 <= self.stable_reset_seconds <= 86_400:
            raise ValueError("reconnect stable reset must be between 1 and 86400 seconds")


@dataclass(frozen=True, slots=True)
class ReconnectSchedule:
    attempt: int
    delay_seconds: float
    elapsed_seconds: float
    remaining_seconds: float


class ReconnectBackoff:
    def __init__(
        self,
        policy: ReconnectPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.policy = policy
        self.clock = clock
        self.jitter = jitter
        self.started_at: float | None = None
        self.attempts = 0

    def reset(self) -> None:
        self.started_at = None
        self.attempts = 0

    def observe_connection(self, connected_seconds: float) -> bool:
        if connected_seconds >= self.policy.stable_reset_seconds:
            self.reset()
            return True
        return False

    def next(self) -> ReconnectSchedule | None:
        now = self.clock()
        if self.started_at is None:
            self.started_at = now
        elapsed = max(0.0, now - self.started_at)
        remaining = max(0.0, self.policy.window_seconds - elapsed)
        if remaining <= 0:
            return None
        factors = (1, 2, 4, 8, 16, 30, 60)
        factor = (
            factors[self.attempts]
            if self.attempts < len(factors)
            else 120 * (2 ** min(self.attempts - len(factors), 23))
        )
        raw_delay = min(
            self.policy.max_delay_seconds,
            self.policy.initial_delay_seconds * factor,
        )
        delay = min(remaining, max(0.001, self.jitter(raw_delay * 0.8, raw_delay * 1.2)))
        self.attempts += 1
        return ReconnectSchedule(
            attempt=self.attempts,
            delay_seconds=delay,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
        )

    def status(self) -> dict[str, object]:
        now = self.clock()
        elapsed = max(0.0, now - self.started_at) if self.started_at is not None else 0.0
        return {
            "attempt": self.attempts,
            "retry_elapsed_seconds": elapsed,
            "retry_remaining_seconds": max(0.0, self.policy.window_seconds - elapsed),
            "exhausted": self.started_at is not None and elapsed >= self.policy.window_seconds,
        }
