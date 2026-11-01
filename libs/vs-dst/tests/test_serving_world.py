"""A world that serves its host: a service per process, in the turn while it is in flight, closed at its end.

A tool an agent calls during its turn reaches core through the run's admission of inputs, as the
production host's evaluation bridge does. The service a world opens is the host's memory: every
host opens its own and closes it however the host ended (finished, killed, paused or stopped), and
while a turn is in flight the admission shows the turn executing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import DispatchTurn, ResumeSessionTurn, SessionPhase
from vs_dst.api import Action, Moment, StepLoopHosting, simulate

from ._turns import TurnsWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_dst.api import Admission
    from vs_runtime.api.core import ExecutionResult
    from vs_sim.api import Clock

STEP_LOOP = StepLoopHosting()


class ServedTurns(TurnsWorld):
    """The turns world with a service per host that notes what it sees while a turn is in flight."""

    def __init__(self) -> None:
        super().__init__(2)
        self.opened = 0
        self.closed = 0
        self.turns_in_flight: list[bool] = []
        """For each turn that began: whether the admission showed that turn executing."""
        self.enacted: list[str] = []

    def serve(self, admission: Admission, clock: Clock) -> _Service:
        del clock
        self.opened += 1
        return _Service(self, admission)


class _Service:
    def __init__(self, world: ServedTurns, admission: Admission) -> None:
        self._world = world
        self._admission = admission

    async def during(self, request: Request, core: CoreState) -> None:
        if not isinstance(request, DispatchTurn | ResumeSessionTurn):
            return
        invocation = request.turn.invocation_id
        executing = {
            row.invocation.invocation_id
            for row in self._admission.admitted_core.sessions.invocations
            if row.phase is SessionPhase.EXECUTING
        }
        assert core is not None
        self._world.turns_in_flight.append(invocation in executing)

    async def enacted(self, request: Request, result: ExecutionResult) -> ExecutionResult:
        self._world.enacted.append(type(request).__name__)
        return result

    async def close(self) -> None:
        self._world.closed += 1


def _served(world: object) -> ServedTurns:
    assert isinstance(world, ServedTurns)
    return world


def test_a_host_serves_each_turn_while_it_is_in_flight_and_closes_its_service_once() -> None:
    run = simulate(ServedTurns(), STEP_LOOP)

    world = _served(run.world)
    assert run.finished
    assert (world.opened, world.closed) == (1, 1)
    assert world.turns_in_flight == [True, True], "the admission shows the turn executing"
    assert world.enacted.count("DispatchTurn") + world.enacted.count("ResumeSessionTurn") == 2


def test_a_host_that_takes_over_a_killed_one_serves_on_its_own_and_the_dead_one_closes_nothing() -> (
    None
):
    normal = simulate(ServedTurns(), STEP_LOOP)

    for crash in normal.crash_points():
        recovered = _served(normal.rerun_with_crash(crash).world)
        # The killed host's memory is gone: whatever service it opened, it never closed.
        assert recovered.closed == 1 <= recovered.opened, crash
        assert all(recovered.turns_in_flight), crash


def test_every_host_of_a_run_paused_or_stopped_anywhere_closes_the_service_it_opened() -> None:
    normal = simulate(ServedTurns(), STEP_LOOP)

    for action in (Action.PAUSE, Action.STOP):
        for moment in Moment:
            for point in normal.interruptions(action, moment):
                interrupted = _served(normal.interrupt(point).world)
                assert interrupted.opened == interrupted.closed, point
                assert all(interrupted.turns_in_flight), point
