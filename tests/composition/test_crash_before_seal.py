"""A host crash after an effect ran and before its result was sealed.

``run_once`` writes ``begun``, runs the effect, then seals the result. The other crash
tests crash after a write lands, so they never leave an effect that ran with no sealed
result: the restart always finds either no effect or its sealed result. Here the sealed
write never lands. The restart finds the request begun and unsealed, re-runs it as
resumed, and the run must still end exactly as the straight run does. This is the state
a host killed in the middle of a job poll or an agent turn leaves behind.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import all_crossings, rule, straight_run
from tests.support.skeleton_faults import RECEIPT_SEALED
from tests.support.skeleton_sim import Simulation, simulate

from vs_faults.api import Boundary, FaultGate, FaultPlan, HostCrashError

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_faults.api import Crossing


class LostSealGate(FaultGate):
    """A gate whose planned crash at a sealed write comes before the write lands."""

    def around[T](self, boundary: Boundary, target: str, call: Callable[[], T]) -> T:
        """Crash instead of writing when the plan names this sealed write."""
        ordinal = 1 + sum(1 for c in self.calls if (c.boundary, c.target) == (boundary, target))
        if target != RECEIPT_SEALED or self.plan.match(boundary, target, ordinal) is None:
            return super().around(boundary, target, call)

        def lost() -> T:
            raise HostCrashError(boundary, target, ordinal)

        return super().around(boundary, target, lost)


_RECOVERY_CHECK_STALLS = (
    "a turn whose reply seal was lost is inspected by InspectTurn, which never resolves "
    "the recovery check; fixed in the next PR of the stack"
)


def _sealed_writes() -> list[object]:
    """Each sealed write of the straight run, named by the request kind that wrote it."""
    params = []
    kind = "?"
    for crossing in all_crossings():
        if crossing.boundary == Boundary.EXECUTOR_REQUEST:
            kind = crossing.target
        elif crossing.target == RECEIPT_SEALED:
            marks = (
                [pytest.mark.xfail(strict=True, reason=_RECOVERY_CHECK_STALLS)]
                if kind == "dispatch_turn"
                else []
            )
            params.append(
                pytest.param(crossing, id=f"{kind}:sealed#{crossing.ordinal}", marks=marks)
            )
    return params


def _run(seal: Crossing) -> Simulation:
    plan = FaultPlan(seed=seal.ordinal, rules=(rule(seal),))
    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(simulate(Path(tmp), plan, gate=LostSealGate(plan)))


@pytest.mark.parametrize("seal", _sealed_writes())
def test_a_crash_before_a_result_is_sealed_converges(seal: Crossing) -> None:
    summary = _run(seal).summary
    straight = straight_run().summary
    replay = f"lost seal at {seal}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.crashes == 1, replay
    assert summary.outcome == straight.outcome, replay
    assert summary.adopted_tree == straight.adopted_tree, replay
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay
