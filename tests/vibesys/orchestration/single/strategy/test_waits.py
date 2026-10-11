"""A combined turn that waits for its own evaluation is resumed, and its checkpoint is the candidate.

The implementer submits an evaluation through the production bridge and ends its turn waiting;
core resumes it once the job ends. The resumed turn only reads the verdict, so it retains
nothing of its own: the round's candidate is what the turn checkpointed before it waited.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.single.strategy._replies import combined, plan_reply
from tests.vibesys.orchestration.single.strategy._run import run_shell
from tests.vibesys.orchestration.single.strategy._waiting_turns import (
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


def _run(root: Path, *, waits: tuple[bool, ...], polls: tuple[int, ...], rounds: int) -> Run:
    bridge = new_bridge(root)
    script = WaitingTurns(
        planner=deque(plan_reply(f"h{n}") for n in range(rounds + 4)),
        implementer=deque(combined() for _ in range(40)),
        decide=lambda n: waits[n % len(waits)],
        polls=list(polls),
        unchanged_after_first_retain=True,
    )
    host = HostOptions(
        max_concurrent=2,
        translator=translator_for(script, bridge),
        attach=attach_for(script, bridge),
        controlled=True,
        lifecycle=frozenset({"suspend"}),
    )
    return run_shell(script, host=host, max_rounds=rounds)


def test_a_round_whose_turn_waited_adopts_the_candidate_it_checkpointed(tmp_path: Path) -> None:
    finished = _run(tmp_path, waits=(True,), polls=(2,), rounds=1)

    assert finished.core.run.status == RunStatus.TERMINAL
    assert finished.core.run.result is not None
    assert finished.core.run.result.outcome == "success"
    assert finished.core.evaluation.continuations != ()
    (settle,) = [
        item.disposition
        for item in finished.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    assert settle.candidate is not None


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(
    waits=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    polls=st.lists(st.integers(0, 3), min_size=1, max_size=3).map(tuple),
    rounds=st.integers(1, 2),
)
def test_a_run_ends_whatever_its_turns_wait_for(
    tmp_path_factory: pytest.TempPathFactory,
    waits: tuple[bool, ...],
    polls: tuple[int, ...],
    rounds: int,
) -> None:
    finished = _run(tmp_path_factory.mktemp("waits"), waits=waits, polls=polls, rounds=rounds)

    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL
