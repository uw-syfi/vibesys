"""A resumed implementer settles only after its own checkpoint outcome is known."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vs_core.api import Settle, Withdraw
from vs_dst.api import SimulatedRun, simulate

if TYPE_CHECKING:
    from vs_core.api import AttemptCheckpoint


def _run_resumed(*, unchanged: bool) -> SimulatedRun:
    world = DynamicWorld(
        planner=[plan_reply(implement(f"h{n}")) for n in range(7)],
        implementer=[implemented("supported") for _ in range(60)],
        judge=[reviewed(passed=True) for _ in range(60)],
        waits=(True,),
        polls=(2,),
        roles=("implementer",),
        retain_per_turn=True,
        unchanged_after_first_retain=unchanged,
        max_rounds=1,
        max_in_flight=1,
    )
    return simulate(world)


def _settled(finished: SimulatedRun) -> tuple[Settle, tuple[AttemptCheckpoint, ...]]:
    assert finished.finished
    assert not finished.liveness_violations
    (settle,) = [
        item.disposition
        for item in finished.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ]
    (attempt,) = finished.core.attempts.attempts
    turn_checkpoints = tuple(item for item in attempt.checkpoints if item.invocation is not None)
    return settle, turn_checkpoints


def test_an_unchanged_resume_records_and_uses_its_current_revision() -> None:
    settle, checkpoints = _settled(_run_resumed(unchanged=True))

    assert len(checkpoints) == 2
    assert checkpoints[0].revision == checkpoints[1].revision
    assert settle.candidate == checkpoints[1].revision


def test_a_changed_resume_uses_its_own_newer_checkpoint() -> None:
    settle, checkpoints = _settled(_run_resumed(unchanged=False))

    assert len(checkpoints) == 2
    assert checkpoints[0].revision != checkpoints[1].revision
    assert settle.candidate == checkpoints[1].revision
