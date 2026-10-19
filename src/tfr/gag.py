from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping

import regex

from tfr.config import WorldsConfig
from tfr.events import Direction, Event

MAX_GAG_INPUT_CHARACTERS = 16_384
GAG_PATTERN_TIMEOUT_SECONDS = 0.002
GAG_EVENT_BUDGET_SECONDS = 0.010
GAG_TIMEOUT_QUARANTINE_THRESHOLD = 3
GAG_DECISION_CACHE_SIZE = 50_000

GagDiagnosticHandler = Callable[[str, str, int], None]


class GagPolicy:
    """A timeout-bounded, fail-open projection policy for world output."""

    def __init__(
        self,
        patterns: Mapping[str, tuple[str, ...]],
        *,
        diagnostic: GagDiagnosticHandler | None = None,
    ) -> None:
        self._patterns = {
            world: tuple(regex.compile(pattern, regex.IGNORECASE) for pattern in world_patterns)
            for world, world_patterns in patterns.items()
            if world_patterns
        }
        self._diagnostic = diagnostic
        self._timeout_counts: dict[tuple[str, int], int] = {}
        self._reported_timeouts: set[tuple[str, int]] = set()
        self._quarantined: set[tuple[str, int]] = set()
        self._decisions: OrderedDict[object, bool] = OrderedDict()

    @classmethod
    def from_configuration(
        cls,
        worlds: WorldsConfig,
        *,
        diagnostic: GagDiagnosticHandler | None = None,
    ) -> GagPolicy:
        return cls(
            {world: config.gag.patterns for world, config in worlds.worlds.items()},
            diagnostic=diagnostic,
        )

    def set_diagnostic_handler(self, diagnostic: GagDiagnosticHandler | None) -> None:
        self._diagnostic = diagnostic

    def replace_configuration(self, worlds: WorldsConfig) -> None:
        replacement = type(self).from_configuration(worlds, diagnostic=self._diagnostic)
        self._patterns = replacement._patterns
        self._timeout_counts.clear()
        self._reported_timeouts.clear()
        self._quarantined.clear()
        self._decisions.clear()

    @property
    def quarantined_patterns(self) -> frozenset[tuple[str, int]]:
        return frozenset(self._quarantined)

    def allows(self, event: Event) -> bool:
        cached = self._decisions.get(event.event_id)
        if cached is not None:
            self._decisions.move_to_end(event.event_id)
            return cached
        allowed = self._evaluate(event)
        self._decisions[event.event_id] = allowed
        if len(self._decisions) > GAG_DECISION_CACHE_SIZE:
            self._decisions.popitem(last=False)
        return allowed

    def _evaluate(self, event: Event) -> bool:
        if event.direction is not Direction.INBOUND:
            return True
        patterns = self._patterns.get(event.world)
        if not patterns:
            return True
        text = event.plain_text or event.display_text or event.canonical_text
        if text is None:
            return True
        if len(text) > MAX_GAG_INPUT_CHARACTERS:
            return True
        deadline = time.monotonic() + GAG_EVENT_BUDGET_SECONDS
        for index, pattern in enumerate(patterns):
            key = (event.world, index)
            if key in self._quarantined:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._report_once("budget_exhausted", event.world, index)
                return True
            try:
                if pattern.search(text, timeout=min(GAG_PATTERN_TIMEOUT_SECONDS, remaining)):
                    return False
            except TimeoutError:
                count = self._timeout_counts.get(key, 0) + 1
                self._timeout_counts[key] = count
                self._report_once("match_timeout", event.world, index)
                if count >= GAG_TIMEOUT_QUARANTINE_THRESHOLD:
                    self._quarantined.add(key)
                    self._report("pattern_quarantined", event.world, index)
                return True
            except Exception:
                self._report_once("match_failed", event.world, index)
                return True
        return True

    def _report_once(self, code: str, world: str, index: int) -> None:
        key = (world, index)
        if key in self._reported_timeouts:
            return
        self._reported_timeouts.add(key)
        self._report(code, world, index)

    def _report(self, code: str, world: str, index: int) -> None:
        if self._diagnostic is not None:
            self._diagnostic(code, world, index)
