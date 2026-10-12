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


def _run(
    root: Path,
    *,
    waits: tuple[bool, ...],
    polls: tuple[int, ...],
    rounds: int,
    ends: tuple[str, ...] = ("wait",),
) -> Run:
    bridge = new_bridge(root)
    script = WaitingTurns(
        planner=deque(plan_reply(f"h{n}") for n in range(rounds + 4)),
        implementer=deque(combined() for _ in range(40)),
        decide=lambda n: waits[n % len(waits)],
        ends=lambda n: ends[n % len(ends)],
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


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(
    ends=st.lists(st.sampled_from(("wait", "result", "invalid")), min_size=1, max_size=4).map(
        tuple
    ),
    rounds=st.integers(1, 2),
    polls=st.integers(0, 8),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    tmp_path_factory: pytest.TempPathFactory, ends: tuple[str, ...], rounds: int, polls: int
) -> None:
    """Core holds a turn that validated a wait as suspended, so no reply may be accepted.

    A turn that ends with its final result or an invalid reply instead of the waiting reply
    must still yield: accepting or correcting it would request a turn core refuses, which
    halts the run (`run_shell` raises the halt). So the strategy's decisions must not depend on
    which reply the suspended turn ended with: they equal those of a run whose turns all end
    with the waiting reply.
    """

    def run(chosen: tuple[str, ...]) -> Run:
        return _run(
            tmp_path_factory.mktemp("ends"),
            waits=(True,),
            polls=(polls,),
            rounds=rounds,
            ends=chosen,
        )

    finished = run(ends)

    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL
    if "invalid" not in ends:
        # Whether core records a wait on an invalid reply is core's own rule, so only the other
        # replies are compared.
        assert finished.core.evaluation.continuations != ()
        assert [type(item).__name__ for item in finished.decisions] == [
            type(item).__name__ for item in run(("wait",)).decisions
        ]
