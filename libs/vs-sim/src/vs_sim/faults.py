"""The generic part of a seeded fault plan: which call a rule fires on, and reproducible draws.

A fault plan schedules faults by *ordinal*: the n-th call at a named boundary on a named
target. Calls are counted, never timed, so a schedule is the same on every run. What a
boundary is and what a fault does there belong to the domain that injects it; these helpers
own the counting, the matching and the seeded stream that generated plans and generated
replies draw from.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import TYPE_CHECKING, Protocol

from vs_sim.randomness import SeededRandom

if TYPE_CHECKING:
    from collections.abc import Iterable


class ScheduledRule(Protocol):
    """A rule that fires on the ``at``-th call (1-based) at ``boundary`` matching ``target``."""

    @property
    def boundary(self) -> str:
        """The boundary the rule is injected at."""
        ...

    @property
    def target(self) -> str | None:
        """What at the boundary the rule matches; ``None`` matches every target."""
        ...

    @property
    def at(self) -> int:
        """The ordinal of the matching call the rule fires on."""
        ...


def match_rule[R: ScheduledRule](
    rules: Iterable[R], boundary: str, target: str, ordinal: int
) -> R | None:
    """The first rule for the ``ordinal``-th call at ``boundary`` on ``target``, if any."""
    for rule in rules:
        if (
            rule.boundary == boundary
            and rule.at == ordinal
            and (rule.target is None or rule.target == target)
        ):
            return rule
    return None


class CallCounter:
    """Numbers the calls at each (boundary, target), from 1, safely across threads."""

    def __init__(self) -> None:
        """Start with no calls counted."""
        self._counts: Counter[tuple[str, str]] = Counter()
        self._lock = threading.Lock()

    def next(self, boundary: str, target: str) -> int:
        """Count one more call at ``boundary`` on ``target`` and return its ordinal."""
        with self._lock:
            self._counts[boundary, target] += 1
            return self._counts[boundary, target]


def fault_stream(seed: int, *scope: object) -> SeededRandom:
    """The stream for ``scope`` under ``seed``: the same pair always draws the same values.

    Streams of different scopes are independent, so adding a consumer never moves another's draws.
    The stream of a (seed, scope) pair is the one fault plans have always drawn, so a seed recorded
    from an earlier failure still replays the same scenario.
    """
    return SeededRandom(repr((seed, *scope)))
