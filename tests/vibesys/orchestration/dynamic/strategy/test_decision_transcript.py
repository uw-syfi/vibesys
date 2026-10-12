"""Every decision `dynamic` makes over fixed scenarios, pinned by id and payload digest.

Decision ids and payloads are persisted in run envelopes, and core deduplicates a replayed
proposal by both. A refactor of the strategy's building blocks must therefore not change a
single byte of what it proposes. The golden file was recorded before the extraction of the
shared strategy building blocks; a change to it is a change of the persisted contract and
needs a reason of its own.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import pytest
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vs_dst.api import simulate

_GOLDEN = Path(__file__).with_name("golden_decision_transcripts.json")


def _scenarios() -> dict[str, dict[str, object]]:
    """A fixed, ordered grid of scenarios: portfolio size, parallelism, review, repair, retention."""
    grid = itertools.product(
        (1, 2, 3),  # hypotheses in the plan
        (1, 2),  # max_in_flight
        (True, False),  # every review passes
        (False, True),  # the plan reply needs one correction first
        (False, True),  # retain a commit after every turn
        (100.0, 10.0),  # benchmark: beats the input, or does not
    )
    return {
        f"h{count}-p{parallel}-{'pass' if passes else 'fail'}"
        f"-{'fix' if fix else 'ok'}-{'turn' if per_turn else 'once'}-b{int(bench)}": {
            "count": count,
            "parallel": parallel,
            "passes": passes,
            "fix": fix,
            "per_turn": per_turn,
            "bench": bench,
        }
        for count, parallel, passes, fix, per_turn, bench in grid
    }


def _transcript(spec: dict[str, object]) -> list[list[str]]:
    count = int(str(spec["count"]))
    plans = [plan_reply(*(implement(f"h{i}") for i in range(count)))]
    world = DynamicWorld(
        planner=["not json", *plans] if spec["fix"] else plans,
        implementer=[implemented() for _ in range(count)],
        judge=[reviewed(passed=bool(spec["passes"])) for _ in range(count)],
        retain_per_turn=bool(spec["per_turn"]),
        throughputs=(float(str(spec["bench"])),),
        max_in_flight=int(str(spec["parallel"])),
    )
    finished = simulate(world)
    assert finished.finished
    assert not finished.liveness_violations
    return [
        [
            type(item).__name__,
            item.decision_id.root,
            hashlib.sha256(item.model_dump_json().encode()).hexdigest()[:16],
        ]
        for item in finished.decisions
    ]


def _record() -> dict[str, list[list[str]]]:
    return {name: _transcript(spec) for name, spec in _scenarios().items()}


@pytest.mark.parametrize("name", list(_scenarios()))
def test_decisions_are_identical_to_the_recorded_transcript(name: str) -> None:
    golden = json.loads(_GOLDEN.read_text())
    assert _transcript(_scenarios()[name]) == golden[name]


if __name__ == "__main__":
    _GOLDEN.write_text(json.dumps(_record(), indent=0, sort_keys=True) + "\n")
