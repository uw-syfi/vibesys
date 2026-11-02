"""An attempt whose workspace cannot be set up ends its start, and the run goes on to its end.

A workspace request the executor refuses, fails, or leaves unanswered until its retries are spent
takes nothing. The attempt that asked for it is retired with no workspace to release, the search
hears that its start did not open (``NotOpened``), and the run ends as any run does: it is neither
held by the attempt nor given up on by the loop as stalled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_core.api import EnsureWorkspace
from vs_core.api.testing import Failed, Retryable, Unknown
from vs_dst.api import StepLoopHosting, simulate

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer

UNUSABLE = (Failed(), Retryable(), Unknown())
"""What a world answers a workspace request with when it cannot set the workspace up."""


class NoWorkspaces(TinyWorld):
    """Every workspace request gets one of the answers that set nothing up."""

    def __init__(self, answer: Answer, items: int) -> None:
        super().__init__(items)
        self.answer = answer

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, EnsureWorkspace):
            return self.answer
        return super().__call__(request, core)


@pytest.mark.parametrize("items", [1, 2])
@pytest.mark.parametrize("answer", UNUSABLE, ids=type)
def test_a_workspace_that_cannot_be_set_up_does_not_stall_the_run(
    answer: Answer, items: int
) -> None:
    """The loop never gives up on the run: the attempts end, and so does the run."""
    run = simulate(NoWorkspaces(answer, items), StepLoopHosting())

    assert run.halted is None, run.halted
    assert run.finished
    assert run.liveness_violations == ()
    assert all(row.phase.value == "terminal" for row in run.core.attempts.attempts)
