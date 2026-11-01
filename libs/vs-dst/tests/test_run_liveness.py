"""A simulated run is live when it ends as it should, and is reported as not live when it cannot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api.testing import Unknown
from vs_dst.api import simulate

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer


class LostWorkspaces(TinyWorld):
    """A tiny world whose workspace executor never answers."""

    def __call__(self, request: Request, core: CoreState) -> Answer:
        return Unknown() if request.kind == "ensure_workspace" else super().__call__(request, core)


def test_a_run_that_ends_the_way_it_should_is_live() -> None:
    run = simulate(TinyWorld(2))

    assert run.finished
    assert not run.liveness_violations


def test_a_run_the_executors_never_answer_is_reported_as_not_live() -> None:
    run = simulate(LostWorkspaces(1))

    assert not run.finished
    assert run.liveness_violations
