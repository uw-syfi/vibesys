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

from vs_core.api import HostFence, HostId, LifecycleClass
from vs_runtime.api.adoption_step_runner import AdoptionStepRunner
from vs_runtime.api.agent_step_runners import (
    AgentTurnStepRunner,
    RunWriter,
    TurnPaths,
    TurnState,
)
from vs_runtime.api.core import OperationRole
from vs_runtime.api.executor_step_runner import DECLARED_REPEATS, ExecutorStepRunner, ReadOnly
from vs_runtime.api.operation_step_runner import (
    ClearedWork,
    KeyedWrite,
    OperationStepRunner,
)
from vs_runtime.api.step_router import NoRunnerError, StepRouter, StepRunnerParts, build_step_runner
from vs_runtime.api.step_runner import Adopted, Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_agent.api import ClientAgentSessions
    from vs_core.api import ExecuteRegisteredOperation, OperationRequest, Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_runtime.api.agent_step_runners import OperationTurns
    from vs_runtime.api.core import CoreExecutors, OperationCatalog
    from vs_runtime.api.evaluation_step_runners import StageWorkReaper
    from vs_runtime.api.infrastructure import RuntimeWorkspaces
    from vs_runtime.api.operation_step_runner import RerunPolicy, WorkCleaner
    from vs_runtime.api.step_runner import CancelSignal, StepRunner
    from vs_sim.api import BlockingRunner, Clock


@dataclass(frozen=True)
class RunnerSeams:
    """What a run's environment decides about its runners; the rest is the run's executors."""

    agent_work: StageWorkReaper
    """Ends the agent processes of one step by its tag, in the environment the agent runs in."""
    jobs: Callable[[Clock], StepRunner] | None
    """Builds the runner of ``SubmitMeasurement`` and owned-job operations on the run clock;
    ``None`` where the agent runs the evaluation helper inside its turn (SkyPilot, Modal)."""


@dataclass(frozen=True)
class RunnerServices:
    """The run's own services the runners are built on."""

    run_id: str
    executors: CoreExecutors
    catalog: OperationCatalog
    policies: dict[str, RerunPolicy]
    """What a rerun of each owned operation may repeat, by operation kind."""
    workspaces: RuntimeWorkspaces
    sessions: ClientAgentSessions | None
    blocking: BlockingRunner
    resuming: bool
    """Whether an earlier process ran this run: an adoption then keeps the operator's tree."""


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


def build_run_runner(
    services: RunnerServices, seams: RunnerSeams, *, clock: Clock, epoch: int
) -> StepRouter:
    """The router of a run in process ``epoch``, whose runners read the run clock ``clock``.

    The fence names the writer of this epoch. The run's lock is the only fence, so it carries no
    authority of its own: it is monotone per epoch because the executors still stamp it.
    """
    fence = HostFence(host_id=HostId(root=f"run:{services.run_id}"), epoch=epoch)
    bound = services.executors
    return build_step_runner(
        StepRunnerParts(
            executors=ExecutorStepRunner(
                bound.executors,
                DECLARED_REPEATS,
                fence=fence,
                lease=_AlwaysHeld(),
                clock=clock,
            ),
            turns=_turn_runner(services, seams, fence, clock),
            adoption=AdoptionStepRunner(
                services.workspaces,
                bound.evidence,
                bound.observations,
                clock=clock,
                publishes=not services.resuming,
            ),
            operations=OperationStepRunner(
                services.catalog, services.policies, bound.observations, clock=clock
            ),
            jobs=None if seams.jobs is None else seams.jobs(clock),
        )
    )


def _turn_runner(
    services: RunnerServices, seams: RunnerSeams, fence: HostFence, clock: Clock
) -> StepRunner:
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
        RunWriter(run_id=services.run_id, fence=fence),
        seams.agent_work,
    )


_NO_TURNS: OperationTurns = NoOperationTurns()


class _AlwaysHeld:
    """The execution lease of a run whose lock is held by this process for as long as it runs."""

    def renew(self, *, now_at: float, lease_duration: float) -> None:
        """Nothing expires: the kernel drops the lock when the process ends."""
        del now_at, lease_duration

    def verify(self, *, now_at: float) -> bool:
        """The lock is held, or this process would not be running the step."""
        del now_at
        return True


class UnwiredWorkCleaner:
    """The cleaner of a command operation no environment can clear: it says so, and the run waits.

    The two command operations run a process in a workspace, and nothing ends it by the step's
    tag yet. A rerun after a crash therefore waits visibly on ``clear`` instead of repeating a
    command whose process nobody ended.
    """

    async def clear(self, tag: RequestId, request: OperationRequest) -> None:
        """Report that nothing here can end the processes of ``tag``."""
        del request
        raise ClearUnavailableError(tag, "no work cleaner is wired for command operations")


_ROLE_RERUN: Mapping[OperationRole, ReadOnly | KeyedWrite] = {
    OperationRole.RENDER_ARTIFACTS: KeyedWrite("the digest of the rendered artifact"),
    OperationRole.VERIFY_REVISION: ReadOnly(),
    OperationRole.INTERPRET_EVIDENCE: ReadOnly(),
    OperationRole.RETAIN_REVISION: KeyedWrite("the retention label of the request"),
    OperationRole.EXPORT_PATCH: ReadOnly(),
}


def rerun_policies(
    roles: Mapping[str, OperationRole], catalog: OperationCatalog, cleaner: WorkCleaner
) -> dict[str, RerunPolicy]:
    """What a rerun of each owned operation may repeat, by operation kind.

    A role fixes it for the runtime's own operations (``roles`` names the role of each kind the
    strategy registered); a command role runs a process, so its policy is ``cleaner``. An
    operation outside the roles is read only when it is a query and keyed when it is a write.
    """
    policies: dict[str, RerunPolicy] = {}
    for entry in catalog.entries:
        kind = entry.schema.kind
        if entry.owner is None or entry.schema.lifecycle not in _RERUNNABLE:
            continue
        role = roles.get(kind)
        if role is not None and role in _ROLE_RERUN:
            policies[kind] = _ROLE_RERUN[role]
        elif role is not None:
            policies[kind] = ClearedWork(cleaner)
        elif entry.schema.lifecycle is LifecycleClass.QUERY:
            policies[kind] = ReadOnly()
        else:
            policies[kind] = KeyedWrite("the key the owner's inspect reads back")
    return policies


_RERUNNABLE = frozenset({LifecycleClass.QUERY, LifecycleClass.IDEMPOTENT_WRITE})
"""The lifecycles the operation runner serves; a job or a turn is another runner's."""


__all__ = [
    "NoOperationTurns",
    "RunnerSeams",
    "RunnerServices",
    "UnservedRunner",
    "UnwiredWorkCleaner",
    "build_run_runner",
    "rerun_policies",
]
