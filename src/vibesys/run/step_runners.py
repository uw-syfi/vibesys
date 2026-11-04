"""The step runners of one run: which runner performs each kind of step, over the run's executors.

``build_run_runner`` is the one place the run's services meet ``vs_runtime``'s step runners. Each
runner adapts an executor the run already owns, so a step runs on the same code it ran on before
the step loop, and ``clear`` is what the step loop adds. A step whose environment has no runner
(an evaluation on SkyPilot or Modal runs inside the agent's turn) is declared unserved; the
router refuses to be built with a kind that is neither served nor declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_runtime.api.adoption_step_runner import AdoptionStepRunner
from vs_runtime.api.agent_step_runners import (
    AgentTurnStepRunner,
    TurnPaths,
    TurnState,
)
from vs_runtime.api.core import JobBooks, JobRegistry
from vs_runtime.api.executor_step_runner import DECLARED_REPEATS, ExecutorStepRunner
from vs_runtime.api.operation_step_runner import (
    OperationStepRunner,
)
from vs_runtime.api.step_router import NoRunnerError, StepRouter, StepRunnerParts, build_step_runner
from vs_runtime.api.step_runner import Adopted, Cleared

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import ClientAgentSessions
    from vs_core.api import ExecuteRegisteredOperation, Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_runtime.api.agent_step_runners import OperationTurns
    from vs_runtime.api.core import CoreExecutors, OperationCatalog
    from vs_runtime.api.evaluation_step_runners import StageWorkReaper
    from vs_runtime.api.infrastructure import RuntimeWorkspaces
    from vs_runtime.api.step_runner import CancelSignal, StepRunner
    from vs_sim.api import BlockingRunner, Clock


@dataclass(frozen=True)
class RunnerSeams:
    """What a run's environment decides about its runners; the rest is the run's executors."""

    agent_work: StageWorkReaper
    """Ends the agent processes of one step by its tag, in the environment the agent runs in."""
    jobs: Callable[[Clock, JobBooks], StepRunner] | None
    """Builds the runner of ``SubmitMeasurement`` and owned-job operations on the run clock and
    the books the run keeps of its jobs (where each is, what each read);
    ``None`` where the agent runs the evaluation helper inside its turn (SkyPilot, Modal)."""


@dataclass(frozen=True)
class RunnerServices:
    """The run's own services the runners are built on."""

    run_id: str
    executors: CoreExecutors
    catalog: OperationCatalog
    workspaces: RuntimeWorkspaces
    sessions: ClientAgentSessions | None
    blocking: BlockingRunner


class NoOperationTurns:
    """The turns of registered operations: no production operation runs as an agent turn."""

    def turn(self, operation: ExecuteRegisteredOperation) -> None:
        """Every operation is no turn, so a ``SESSION_TURN`` operation is refused by name."""
        del operation


class UnservedRunner:
    """The runner of a kind this run has no executor for: a step of it never starts."""

    def __init__(self, name: str) -> None:
        """Name the kind the run cannot serve."""
        self._name = name

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Refuse by name: nothing the run composed performs this step."""
        del step, tag, attempt, deadline_at, cancel
        raise NoRunnerError(self._name)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """There is nothing to clear: the step never started."""
        del step, tag
        return Cleared()


def build_run_runner(services: RunnerServices, seams: RunnerSeams, *, clock: Clock) -> StepRouter:
    """The router of a run, whose runners read the run clock ``clock``.

    The run's lock is the only writer, so no runner carries an authority of its own.
    """
    bound = services.executors
    return build_step_runner(
        StepRunnerParts(
            executors=ExecutorStepRunner(bound.executors, DECLARED_REPEATS, clock=clock),
            turns=_turn_runner(services, seams, clock),
            adoption=AdoptionStepRunner(
                services.workspaces,
                bound.evidence,
                bound.observations,
                clock=clock,
            ),
            operations=OperationStepRunner(services.catalog, bound.observations, clock=clock),
            jobs=None
            if seams.jobs is None
            else seams.jobs(
                clock,
                JobBooks(JobRegistry(bound.store), bound.evidence, bound.progress, bound.decisions),
            ),
        )
    )


def _turn_runner(services: RunnerServices, seams: RunnerSeams, clock: Clock) -> StepRunner:
    """The agent turn runner, or the refusal of a run that declares no agent role."""
    session = services.executors.sessions
    if session is None or services.sessions is None:
        return UnservedRunner("an agent turn: the run declares no agent role")
    return AgentTurnStepRunner(
        TurnPaths(dispatch=session.turns, resume=session.lifecycle, operations=_NO_TURNS),
        TurnState(
            sessions=services.sessions, store=services.executors.store, blocking=services.blocking
        ),
        clock,
        services.run_id,
        seams.agent_work,
    )


_NO_TURNS: OperationTurns = NoOperationTurns()


__all__ = [
    "NoOperationTurns",
    "RunnerSeams",
    "RunnerServices",
    "UnservedRunner",
    "build_run_runner",
]
