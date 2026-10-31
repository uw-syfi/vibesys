"""The searches the crash tests run through `vs_dst`: each a world of scripted replies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.dynamic_world import DynamicWorld
from tests.vibesys.evolve_world import EvolveWorld
from tests.vibesys.orchestration.issue_queue._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.world import IssueQueueWorld
from tests.vibesys.serial_world import SerialWorld

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_dst.api import World

WORLDS: dict[str, Callable[[], World]] = {
    "dynamic": DynamicWorld,
    "dynamic-parallel": lambda: DynamicWorld(max_in_flight=2),
    "evolve": EvolveWorld,
    "single": lambda: SerialWorld("single", max_rounds=2),
    "multi": lambda: SerialWorld("multi", max_rounds=2),
    "issue-queue": lambda: IssueQueueWorld(
        implementer=[implemented(f"turn {n}") for n in range(12)],
        judge=[judged(passed=False), *[judged() for _ in range(11)]],
        perf=[evaluated("first", "second"), evaluated("third"), *[evaluated() for _ in range(6)]],
    ),
}
"""Each searched world; the issue queue files three issues, and the judge rejects one attempt."""
