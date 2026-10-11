"""A mutator or judge turn that waits for its own evaluation is resumed.

Either role submits an evaluation through the production bridge and ends its turn waiting;
core resumes it once the job ends. A resumed turn that only reads the verdict records the same
revision again, and that current invocation's checkpoint is authoritative.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve.strategy._replies import mutated, reviewed
from tests.vibesys.orchestration.evolve.strategy._run import run_shell
from tests.vibesys.orchestration.evolve.strategy._waiting_turns import (
    WaitingTurns,
    attach_for,
    new_bridge,
    translator_for,
)

from vs_core.api import AttemptPhase, RunStatus, Settle, Withdraw
from vs_runtime.api.testing import HostOptions, Run

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _run(
    root: Path,
    *,
    waits: tuple[bool, ...],
    polls: tuple[int, ...],
    parallelism: int,
    roles: tuple[str, ...] = ("mutator", "judge"),
) -> Run:
    """A run whose ``roles`` may wait; a lone mutator's resumed turn leaves the tree as it was."""
    bridge = new_bridge(root)
    script = WaitingTurns(
        mutator=deque(mutated() for _ in range(40)),
        judge=deque(reviewed() for _ in range(40)),
        roles=roles,
        decide=lambda n: waits[n % len(waits)],
        polls=list(polls),
        unchanged_after_first_retain=roles == ("mutator",),
    )
    host = HostOptions(
        max_concurrent=2,
        translator=translator_for(script, bridge),
        attach=attach_for(script, bridge),
        controlled=True,
        lifecycle=frozenset({"suspend"}),
    )
    return run_shell(
        script,
        host=host,
        max_generations=1,
        children_per_generation=2,
        max_parallelism=parallelism,
    )


def test_a_child_whose_turn_waited_adopts_the_candidate_it_checkpointed(tmp_path: Path) -> None:
    finished = _run(tmp_path, waits=(True,), polls=(2,), parallelism=1, roles=("mutator",))

    assert finished.core.run.status == RunStatus.TERMINAL
    assert finished.core.run.result is not None
    assert finished.core.run.result.outcome == "success"
    assert finished.core.evaluation.continuations != ()
    settles = [
        item.disposition
        for item in finished.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    assert settles
    assert all(item.candidate is not None for item in settles)


@settings(max_examples=4, deadline=None, derandomize=True, database=None)
@given(
    waits=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    polls=st.lists(st.integers(0, 3), min_size=1, max_size=3).map(tuple),
    parallelism=st.integers(1, 2),
)
def test_a_run_ends_whatever_its_turns_wait_for(
    tmp_path_factory: pytest.TempPathFactory,
    waits: tuple[bool, ...],
    polls: tuple[int, ...],
    parallelism: int,
) -> None:
    finished = _run(
        tmp_path_factory.mktemp("waits"), waits=waits, polls=polls, parallelism=parallelism
    )

    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL
