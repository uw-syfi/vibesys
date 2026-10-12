"""A world that acts on the host hears each host's shell and sees every executed request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vs_core.api.testing import Unknown
from vs_dst.api import simulate

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer
    from vs_dst.api import Enact
    from vs_runtime.api.core import CoreRuntime, ExecutionResult
    from vs_sim.api.testing import VirtualClock


class HostedTinyWorld(TinyWorld):
    """A tiny world whose host-side effects are recorded as each request is enacted."""

    def __init__(self, items: int = 2) -> None:
        super().__init__(items)
        self.hosts = 0
        self.enacted: list[str] = []

    def attach(self, shell: CoreRuntime[Any], clock: VirtualClock) -> Enact:
        del shell, clock
        self.hosts += 1

        async def enact(request: Request, result: ExecutionResult) -> ExecutionResult:
            self.enacted.append(request.kind)
            return result

        return enact


class LostWorkspaces(TinyWorld):
    """A tiny world whose workspace executor never answers."""

    def __call__(self, request: Request, core: CoreState) -> Answer:
        return Unknown() if request.kind == "ensure_workspace" else super().__call__(request, core)


def test_a_hosted_world_hears_its_host_and_sees_every_request_the_executors_ran() -> None:
    world = HostedTinyWorld(2)

    run = simulate(world)

    assert run.finished
    assert world.hosts == 1
    assert len(world.enacted) == len(run.executed_effects)
    assert "ensure_workspace" in world.enacted


def test_a_run_that_ends_the_way_it_should_is_live() -> None:
    run = simulate(TinyWorld(2))

    assert run.finished
    assert not run.liveness_violations


def test_a_run_the_executors_never_answer_is_reported_as_not_live() -> None:
    run = simulate(LostWorkspaces(1))

    assert not run.finished
    assert run.liveness_violations
