"""An implementer or judge turn that waits for its own evaluation is resumed.

Either role submits an evaluation through the production bridge and ends its turn waiting;
core resumes it once the job ends. A resumed implementer that only reads the verdict records
the same revision again, and that current invocation's checkpoint remains authoritative.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.checkpoint_faults import CHECKPOINTS, with_checkpoints
from tests.vibesys.orchestration.multi.strategy._replies import implemented, plan_reply, reviewed
from tests.vibesys.orchestration.multi.strategy._run import run_shell
from tests.vibesys.orchestration.multi.strategy._waiting_turns import (
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


def _world(
    root: Path,
    *,
    waits: tuple[bool | str, ...],
    polls: tuple[int, ...],
    rounds: int,
    roles: tuple[str, ...] = ("implementer", "judge"),
) -> tuple[WaitingTurns, HostOptions]:
    """A run whose ``roles`` may wait; a lone implementer's resumed turn leaves the tree as it was."""
    bridge = new_bridge(root)
    script = WaitingTurns(
        planner=deque(plan_reply(f"h{n}") for n in range(rounds + 4)),
        implementer=deque(implemented() for _ in range(40)),
        judge=deque(reviewed() for _ in range(40)),
        roles=roles,
        decide=lambda n: bool(waits[n % len(waits)]),
        ends=lambda n: "wait" if waits[n % len(waits)] is True else str(waits[n % len(waits)]),
        polls=list(polls),
        unchanged_after_first_retain=roles == ("implementer",),
    )
    host = HostOptions(
        max_concurrent=2,
        translator=translator_for(script, bridge),
        attach=attach_for(script, bridge),
        controlled=True,
        lifecycle=frozenset({"suspend"}),
    )
    return script, host


def _run(
    root: Path,
    *,
    waits: tuple[bool | str, ...],
    polls: tuple[int, ...],
    rounds: int,
    roles: tuple[str, ...] = ("implementer", "judge"),
) -> Run:
    """A run whose ``roles`` may wait; a lone implementer's resumed turn leaves the tree as it was."""
    script, host = _world(root, waits=waits, polls=polls, rounds=rounds, roles=roles)
    return run_shell(script, host=host, max_rounds=rounds)


def test_a_round_whose_turn_waited_adopts_the_candidate_it_checkpointed(tmp_path: Path) -> None:
    finished = _run(tmp_path, waits=(True,), polls=(2,), rounds=1, roles=("implementer",))

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
    polls=st.integers(0, 8),
    roles=st.sampled_from((("implementer", "judge"), ("judge",), ("implementer",))),
)
def test_a_suspended_turn_is_a_wait_whatever_reply_it_ends_with(
    tmp_path_factory: pytest.TempPathFactory,
    ends: tuple[str, ...],
    roles: tuple[str, ...],
    polls: int,
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
            waits=chosen,
            polls=(polls,),
            rounds=1,
            roles=roles,
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


def _run_with_checkpoints(
    root: Path,
    checkpoints: tuple[str, ...],
    polls: int,
    *,
    waits: tuple[bool, ...] = (True,),
    roles: tuple[str, ...] = ("implementer",),
) -> Run:
    """A one-round run whose turns find their checkpoints answered per ``checkpoints``."""
    script, host = _world(root, waits=waits, polls=(polls,), rounds=1, roles=roles)
    return run_shell(with_checkpoints(script, checkpoints), host=host, max_rounds=1)


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(
    checkpoints=CHECKPOINTS,
    polls=st.integers(0, 3),
    waits=st.lists(st.booleans(), min_size=1, max_size=3).map(tuple),
    roles=st.sampled_from((("implementer",), ("implementer", "judge"))),
)
def test_a_run_ends_whatever_the_checkpoint_of_a_turn_does(
    tmp_path_factory: pytest.TempPathFactory,
    checkpoints: tuple[str, ...],
    polls: int,
    waits: tuple[bool, ...],
    roles: tuple[str, ...],
) -> None:
    """A turn that asked to wait is suspended or failed with a reason, never left waiting.

    Core publishes the suspension only once the turn's workspace is checkpointed. Whether
    the turn waits or not, and whether the executor retains the workspace or fails the
    checkpoint, the run must reach terminal with every attempt closed and no wait left
    without something to end it (`run_shell` asserts the last). A role that depends on the
    write turn (the judge) is never started on a workspace core did not retain (#2167).
    """
    finished = _run_with_checkpoints(
        tmp_path_factory.mktemp("checkpoints"), checkpoints, polls, waits=waits, roles=roles
    )

    assert finished.core.run.status == RunStatus.TERMINAL
    for attempt in finished.core.attempts.attempts:
        assert attempt.phase == AttemptPhase.TERMINAL


def test_a_declined_checkpoint_fails_the_waiting_turn_instead_of_stalling_the_run(
    tmp_path: Path,
) -> None:
    """Every checkpoint fails: the turn cannot be suspended, and the run still ends."""
    finished = _run_with_checkpoints(tmp_path, ("failed",), 1)

    assert finished.core.run.status == RunStatus.TERMINAL
    assert finished.core.evaluation.continuations == ()
