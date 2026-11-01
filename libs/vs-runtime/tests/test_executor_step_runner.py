"""``ExecutorStepRunner`` runs the thirteen executor kinds, and each is safe to run again.

Three layers. The kinds' classes are pinned to the recovery page, and the constructor refuses a kind
with none. The shared ``StepRunnerContract`` runs against every kind over a world that counts the
state a step leaves and the work still going. And every kind runs on its real executor, killed at
durable-write boundaries and run again, so "safe to repeat" is read from the executors' own disk.

The real executors keep no begun marker and replay no sealed result, so a repeat reaches the same
end only through ``ExecutionContext.repeat`` and the effect itself. The generated programs of runs,
kills, restarts and clears hold each kind to the effects of one unkilled run: it never acts twice.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest
from tests.support.executor_cases import CASES, WorkspacesWorld
from tests.support.session_lifecycle_world import close_request
from tests.support.session_world import ensure_request, session_spec

from vs_core.api import (
    AttemptId,
    AttemptRef,
    CancelOwnedJob,
    CancelTurn,
    CloseAttemptScope,
    CloseSession,
    CollectEvidence,
    ContractError,
    DiscardWorkspace,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    HostFence,
    HostId,
    InvocationId,
    InvocationRef,
    JobObserved,
    ObservationStatus,
    ObserveOwnedJob,
    RequestId,
    RequestObserved,
    ResourceId,
    RestoreRevision,
    RetainRevision,
    RunId,
    Scope,
    SessionId,
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_core.api.recovery import Finished
from vs_runtime.api.core import ExecutionResult, RequestExecutors
from vs_runtime.api.executor_contexts import RevocableLease
from vs_runtime.api.executor_contracts import ProcessKilledError
from vs_runtime.api.executor_step_runner import (
    DECLARED_REPEATS,
    ExecutorRefusedError,
    ExecutorStepRunner,
    FixedEnd,
    ReadOnly,
    ReleaseOrBind,
    Repeat,
    RetainByContent,
    StepWithoutResultError,
)
from vs_runtime.api.operation_fixtures import revision
from vs_runtime.api.step_runner import CancelSignal, Cleared
from vs_runtime.api.testing import StepRunnerContract, StepRunnerUnderTest, finished_result
from vs_sim.api import SeededRandom
from vs_sim.api.testing import VirtualClock, examples, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from _pytest.mark.structures import ParameterSet

    from vs_core.api import Request, RequestBase
    from vs_core.api.recovery import StepResult
    from vs_runtime.api.core import ExecutionContext
    from vs_runtime.api.executor_contracts import CaseWorld, ExecutorCase, Scenario

THIRTEEN = frozenset(
    {
        EnsureWorkspace,
        RestoreRevision,
        DiscardWorkspace,
        CloseAttemptScope,
        SnapshotAndRetain,
        RetainRevision,
        EnsureSession,
        CancelTurn,
        CloseSession,
        SnapshotAndRetainRun,
        ObserveOwnedJob,
        CollectEvidence,
        CancelOwnedJob,
    }
)
"""The kinds with no runner of their own, as ``docs/contributing/run-recovery.md`` lists them."""

DEADLINE = 600.0
FENCE = HostFence(host_id=HostId(root="host"), epoch=1)
SCOPE = Scope(owner=RunId(root="run"), generation=0)
HANGS = "hangs"
QUICK = "quick"
BASE = revision("a" * 40)


def tag_named(name: str) -> RequestId:
    return RequestId(root=name)


def runner_over(
    executors: RequestExecutors, repeats: Mapping[type[Request], Repeat] = DECLARED_REPEATS
) -> ExecutorStepRunner:
    return ExecutorStepRunner(
        executors, repeats, fence=FENCE, lease=RevocableLease(), clock=VirtualClock()
    )


# The declaration


def test_the_declared_kinds_are_the_thirteen_the_recovery_page_lists() -> None:
    assert set(DECLARED_REPEATS) == THIRTEEN
    assert set(BUILDERS) == THIRTEEN


def test_the_classes_follow_the_recovery_pages_table() -> None:
    by_class: dict[type, set[type]] = {}
    for kind, repeat in DECLARED_REPEATS.items():
        by_class.setdefault(type(repeat), set()).add(kind)
    assert by_class == {
        ReadOnly: {ObserveOwnedJob, CollectEvidence},
        FixedEnd: {EnsureWorkspace, RestoreRevision},
        ReleaseOrBind: {
            DiscardWorkspace,
            CloseAttemptScope,
            EnsureSession,
            CloseSession,
            CancelTurn,
            CancelOwnedJob,
        },
        RetainByContent: {SnapshotAndRetain, RetainRevision, SnapshotAndRetainRun},
    }


@pytest.mark.parametrize("kind", sorted(THIRTEEN, key=lambda kind: kind.__name__))
def test_a_kind_with_no_declared_class_is_refused_by_name(kind: type[Request]) -> None:
    declared = {other: repeat for other, repeat in DECLARED_REPEATS.items() if other is not kind}
    with pytest.raises(ContractError, match=kind.__name__):
        runner_over(RequestExecutors(), declared)


def test_a_kind_another_runner_owns_is_refused_by_name() -> None:
    declared = {**DECLARED_REPEATS, DispatchTurn: ReadOnly()}
    with pytest.raises(ContractError, match="DispatchTurn"):
        runner_over(RequestExecutors(), declared)


# The shared contract, over a world that counts what each step leaves


def _attempt(name: str) -> AttemptRef:
    return AttemptRef(attempt_id=AttemptId(root=name), generation=0)


def _invocation(name: str) -> InvocationRef:
    return InvocationRef(
        session_id=SessionId(root=name), invocation_id=InvocationId(root=name), generation=0
    )


BUILDERS: Mapping[type[Request], Callable[[RequestId, str], Request]] = {
    ObserveOwnedJob: lambda tag, name: ObserveOwnedJob(
        request_id=tag, scope=SCOPE, resource_id=ResourceId(root=name)
    ),
    CollectEvidence: lambda tag, name: CollectEvidence(
        request_id=tag, scope=SCOPE, resource_id=ResourceId(root=name)
    ),
    CancelOwnedJob: lambda tag, name: CancelOwnedJob(
        request_id=tag, scope=SCOPE, resource_id=ResourceId(root=name)
    ),
    EnsureWorkspace: lambda tag, name: EnsureWorkspace(
        request_id=tag,
        scope=SCOPE,
        attempt=_attempt(name),
        plan=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=BASE),
    ),
    RestoreRevision: lambda tag, name: RestoreRevision(
        request_id=tag, scope=SCOPE, attempt=_attempt(name), revision=BASE
    ),
    DiscardWorkspace: lambda tag, name: DiscardWorkspace(
        request_id=tag, scope=SCOPE, attempt=_attempt(name)
    ),
    CloseAttemptScope: lambda tag, name: CloseAttemptScope(
        request_id=tag, scope=SCOPE, attempt=_attempt(name)
    ),
    SnapshotAndRetain: lambda tag, name: SnapshotAndRetain(
        request_id=tag, scope=SCOPE, attempt=_attempt(name), retention="candidate"
    ),
    RetainRevision: lambda tag, name: RetainRevision(
        request_id=tag, scope=SCOPE, attempt=_attempt(name), revision=BASE, retention="wip"
    ),
    SnapshotAndRetainRun: lambda tag, name: SnapshotAndRetainRun(
        request_id=tag, scope=SCOPE, invocation=_invocation(name), retention="candidate"
    ),
    EnsureSession: lambda tag, name: EnsureSession(
        request_id=tag, scope=SCOPE, spec=session_spec(session=SessionId(root=name))
    ),
    CloseSession: lambda tag, name: CloseSession(
        request_id=tag, scope=SCOPE, session_id=SessionId(root=name)
    ),
    CancelTurn: lambda tag, name: CancelTurn(
        request_id=tag, scope=SCOPE, invocation=_invocation(name)
    ),
}


def step_of(kind: type[Request], tag: RequestId, *, hangs: bool) -> Request:
    """A step of *kind* that the world's executor finishes at once, or never finishes."""
    return BUILDERS[kind](tag, HANGS if hangs else QUICK)


def hangs(step: Request) -> bool:
    return HANGS in step.model_dump_json()


@dataclass
class EffectWorld:
    """What the steps left, by tag, and the executions still going.

    A write is a plain assignment of the end state the step's class names, so a repeat leaves the
    state it left the first time, and the world holds no record that would make a repeat free.
    """

    left: dict[str, str] = field(default_factory=dict)
    working: dict[str, int] = field(default_factory=dict)


class EffectExecutor:
    """Every role's executor over an ``EffectWorld``."""

    def __init__(self, world: EffectWorld) -> None:
        self.world = world

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        del context
        assert request.request_id is not None
        tag = request.request_id
        self.world.working[tag.root] = self.world.working.get(tag.root, 0) + 1
        try:
            if hangs(request):
                await asyncio.get_running_loop().create_future()
            repeat = DECLARED_REPEATS[type(request)]
            if not isinstance(repeat, ReadOnly):
                self.world.left[tag.root] = type(repeat).__name__
        finally:
            self.world.working[tag.root] -= 1
        return ExecutionResult(
            observation=RequestObserved(observation=finished_result(tag).observation)
        )


def contract_for(kind: type[Request]) -> type[StepRunnerContract]:
    """The shared contract, run against *kind* over a fresh world."""

    class KindContract(StepRunnerContract):
        def step_runner_under_test(self) -> StepRunnerUnderTest:
            world = EffectWorld()
            executor = EffectExecutor(world)
            clock = VirtualClock()
            runner = ExecutorStepRunner(
                RequestExecutors(workspaces=executor, sessions=executor, evaluation=executor),
                DECLARED_REPEATS,
                fence=FENCE,
                lease=RevocableLease(),
                clock=clock,
            )

            async def left_running(tag: RequestId) -> int:
                return world.working.get(tag.root, 0)

            return StepRunnerUnderTest(
                runner=runner,
                run=lambda main: run_virtual(clock, main),
                finishing_step=lambda tag: step_of(kind, tag, hangs=False),
                running_step=lambda tag: step_of(kind, tag, hangs=True),
                left_running=left_running,
            )

    KindContract.__name__ = KindContract.__qualname__ = f"Test{kind.__name__}"
    return KindContract


TestObserveOwnedJob = contract_for(ObserveOwnedJob)
TestCollectEvidence = contract_for(CollectEvidence)
TestEnsureWorkspace = contract_for(EnsureWorkspace)
TestRestoreRevision = contract_for(RestoreRevision)
TestDiscardWorkspace = contract_for(DiscardWorkspace)
TestCloseAttemptScope = contract_for(CloseAttemptScope)
TestEnsureSession = contract_for(EnsureSession)
TestCloseSession = contract_for(CloseSession)
TestCancelTurn = contract_for(CancelTurn)
TestCancelOwnedJob = contract_for(CancelOwnedJob)
TestSnapshotAndRetain = contract_for(SnapshotAndRetain)
TestRetainRevision = contract_for(RetainRevision)
TestSnapshotAndRetainRun = contract_for(SnapshotAndRetainRun)


def test_there_is_a_contract_row_for_each_declared_kind() -> None:
    rows = {name for name in globals() if name.startswith("Test")}
    assert rows == {f"Test{kind.__name__}" for kind in THIRTEEN}


class Undecided:
    """An executor that cannot conclude: it answers an observation that is not final."""

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        del context
        assert request.request_id is not None
        observation = finished_result(request.request_id).observation.model_copy(
            update={"terminal": False, "status": ObservationStatus.UNKNOWN}
        )
        return ExecutionResult(observation=RequestObserved(observation=observation))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(THIRTEEN, key=lambda kind: kind.__name__))
async def test_an_executor_that_cannot_conclude_gives_the_step_no_result(
    kind: type[Request],
) -> None:
    undecided = Undecided()
    runner = runner_over(
        RequestExecutors(workspaces=undecided, sessions=undecided, evaluation=undecided)
    )
    tag = tag_named("undecided")
    with pytest.raises(StepWithoutResultError):
        await runner.run(step_of(kind, tag, hangs=False), tag, 1, DEADLINE, CancelSignal())


class Eventful:
    """An executor that tells the owner of the job something the report does not say."""

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        del context
        assert request.request_id is not None
        observation = finished_result(request.request_id).observation
        event = JobObserved(resource_id=ResourceId(root="job"), observation=observation)
        return ExecutionResult(
            observation=RequestObserved(observation=observation), owner_events=(event,)
        )


@pytest.mark.asyncio
async def test_the_owner_events_an_executor_returns_beside_the_report_are_in_the_result() -> None:
    eventful = Eventful()
    runner = runner_over(
        RequestExecutors(workspaces=eventful, sessions=eventful, evaluation=eventful)
    )
    tag = tag_named("eventful")
    done = await runner.run(
        step_of(CollectEvidence, tag, hangs=False), tag, 1, DEADLINE, CancelSignal()
    )
    assert isinstance(done, Finished)
    assert [event.kind for event in done.owner_events] == ["job_observed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(THIRTEEN, key=lambda kind: kind.__name__))
async def test_a_role_with_no_executor_bound_gives_the_step_no_result(kind: type[Request]) -> None:
    tag = tag_named("unbound")
    with pytest.raises(ExecutorRefusedError):
        await runner_over(RequestExecutors()).run(
            step_of(kind, tag, hangs=False), tag, 1, DEADLINE, CancelSignal()
        )


@pytest.mark.asyncio
async def test_a_step_of_a_kind_the_runner_does_not_serve_or_under_another_tag_is_refused() -> None:
    runner = runner_over(RequestExecutors())
    tag = tag_named("tag")
    with pytest.raises(ContractError, match="tag"):
        await runner.clear(step_of(CloseSession, tag_named("other"), hangs=False), tag)
    foreign = DispatchTurn.model_construct(request_id=tag)
    with pytest.raises(ContractError, match="DispatchTurn"):
        await runner.clear(foreign, tag)


# Every kind on its real executor, killed at durable-write boundaries


class Rig:
    """A case world, and runners over it that die at a durable-write boundary on demand."""

    def __init__(self, world: CaseWorld) -> None:
        self.world = world
        self.lease = RevocableLease()
        self.crash_at: int | None = None

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        # The world builds the context a shell would, for its own host; the runner's say is `repeat`.
        return await self.world.execute(
            request, lease=self.lease, crash_at=self.crash_at, repeat=context.repeat
        )

    def runner(self) -> ExecutorStepRunner:
        """A runner with no memory of an earlier one: a restarted process."""
        return ExecutorStepRunner(
            RequestExecutors(workspaces=self, sessions=self, evaluation=self),
            DECLARED_REPEATS,
            fence=FENCE,
            lease=self.lease,
            clock=VirtualClock(),
        )


def _params(kinds: Collection[type[RequestBase]]) -> list[ParameterSet]:
    return [
        pytest.param(case, scenario, id=f"{case.name}-{scenario.name}")
        for case in CASES
        for scenario in case.scenarios
        if scenario.kind in kinds
    ]


REAL = _params(DECLARED_REPEATS)
SNAPSHOTS = _params({SnapshotAndRetain, SnapshotAndRetainRun})


def test_every_declared_kind_has_a_real_executor_scenario() -> None:
    kinds = {scenario.kind for case in CASES for scenario in case.scenarios}
    assert kinds & set(DECLARED_REPEATS) == THIRTEEN


async def prepared(world: CaseWorld, scenario: Scenario) -> Request:
    """The scenario's request, which is of a declared kind: the world only seeds those."""
    request = await world.prepare(scenario)
    assert type(request) in DECLARED_REPEATS, type(request).__name__
    return cast("Request", request)


@dataclass(frozen=True)
class Result:
    """What a step's result says about the world it left, without the ids of one run."""

    status: ObservationStatus
    terminal: bool
    accepted: bool
    released: bool
    children_complete: bool

    @classmethod
    def of(cls, done: StepResult) -> Result:
        assert isinstance(done, Finished)
        seen = done.observation
        return cls(seen.status, seen.terminal, seen.accepted, seen.released, seen.children_complete)


@dataclass(frozen=True)
class Reference:
    """What an unkilled run of a scenario leaves."""

    result: Result
    effects: int
    writes: int


async def reference_of(case: ExecutorCase, scenario: Scenario) -> Reference:
    async with case.world() as world:
        request = await prepared(world, scenario)
        assert request.request_id is not None
        done = (
            await Rig(world).runner().run(request, request.request_id, 1, DEADLINE, CancelSignal())
        )
        result = Result.of(done)
        assert result.terminal
        return Reference(result, world.effects(), world.writes())


def sampled(boundaries: int) -> list[int]:
    """Every boundary of a short step, else the first two, the middle and the last two."""
    if boundaries <= 6:
        return list(range(boundaries))
    return sorted({0, 1, boundaries // 2, boundaries - 2, boundaries - 1})


async def killed_then_run_again(
    case: ExecutorCase, scenario: Scenario, boundary: int, reference: Reference
) -> None:
    async with case.world() as world:
        request = await prepared(world, scenario)
        assert request.request_id is not None
        tag = request.request_id
        rig = Rig(world)
        rig.crash_at = boundary
        with pytest.raises(ProcessKilledError):
            await rig.runner().run(request, tag, 1, DEADLINE, CancelSignal())
        rig.crash_at = None
        restarted = rig.runner()
        assert await restarted.clear(request, tag) == Cleared()
        result = await restarted.run(request, tag, 2, DEADLINE, CancelSignal())
        assert Result.of(result) == reference.result, f"boundary {boundary}"
        assert world.effects() == reference.effects, f"boundary {boundary}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "scenario"), REAL)
async def test_a_step_killed_at_a_durable_write_runs_again_to_the_effects_of_an_unkilled_run(
    case: ExecutorCase, scenario: Scenario
) -> None:
    reference = await reference_of(case, scenario)
    for boundary in sampled(2 * reference.writes):
        await killed_then_run_again(case, scenario, boundary, reference)


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "scenario"), REAL)
async def test_a_step_that_finished_runs_again_to_the_same_result_and_effects(
    case: ExecutorCase, scenario: Scenario
) -> None:
    async with case.world() as world:
        request = await prepared(world, scenario)
        assert request.request_id is not None
        tag = request.request_id
        rig = Rig(world)
        first = await rig.runner().run(request, tag, 1, DEADLINE, CancelSignal())
        effects = world.effects()
        restarted = rig.runner()
        assert await restarted.clear(request, tag) == Cleared()
        again = await restarted.run(request, tag, 2, DEADLINE, CancelSignal())
        assert Result.of(again) == Result.of(first)
        assert world.effects() == effects


# A snapshot is taken once, whatever the tree does between attempts


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "scenario"), SNAPSHOTS)
async def test_a_snapshot_that_ran_again_after_the_tree_moved_returns_the_snapshot_it_took(
    case: ExecutorCase, scenario: Scenario
) -> None:
    """The repeat finds the snapshot by its label; committing again would retain the moved tree."""
    async with case.world() as world:
        assert isinstance(world, WorkspacesWorld)
        request = await prepared(world, scenario)
        assert request.request_id is not None
        tag = request.request_id
        rig = Rig(world)
        first = await rig.runner().run(request, tag, 1, DEADLINE, CancelSignal())
        effects = world.effects()
        world.drift(request)
        again = await rig.runner().run(request, tag, 2, DEADLINE, CancelSignal())
        assert isinstance(first, Finished)
        assert isinstance(again, Finished)
        assert again.report.revision == first.report.revision
        assert world.effects() == effects


# EnsureSession, the original reported failure


def case_named(name: str) -> ExecutorCase:
    """The registered case; the concrete cases satisfy the protocol, which types ``scenarios`` writable."""
    return cast("ExecutorCase", next(case for case in CASES if case.name == name))


@pytest.mark.asyncio
async def test_a_session_ensure_killed_at_any_durable_write_runs_again_and_binds_the_session_once() -> (
    None
):
    case = case_named("sessions")
    scenario = next(scenario for scenario in case.scenarios if scenario.kind is EnsureSession)
    reference = await reference_of(case, scenario)
    assert reference.result.status is ObservationStatus.SUCCEEDED
    for boundary in range(2 * reference.writes):
        await killed_then_run_again(case, scenario, boundary, reference)


@pytest.mark.asyncio
async def test_an_ensure_whose_session_ended_without_acceptance_runs_again_to_a_result() -> None:
    """#3458: the session was ensured and closed with no turn accepted, then the ensure's result was lost."""
    case = case_named("session-lifecycle")
    async with case.world() as world:
        rig = Rig(world)
        ensure, close = ensure_request(), close_request()
        assert ensure.request_id is not None
        runner = rig.runner()
        first = await runner.run(ensure, ensure.request_id, 1, DEADLINE, CancelSignal())
        assert isinstance(first, Finished)
        assert first.observation.status is ObservationStatus.SUCCEEDED
        assert close.request_id is not None
        closed = await runner.run(close, close.request_id, 1, DEADLINE, CancelSignal())
        assert isinstance(closed, Finished)
        assert closed.observation.terminal
        restarted = rig.runner()
        assert await restarted.clear(ensure, ensure.request_id) == Cleared()
        again = await restarted.run(ensure, ensure.request_id, 2, DEADLINE, CancelSignal())
        assert isinstance(again, Finished)
        assert again.observation.terminal
        # The run continues: the session closes again without a result going missing.
        assert close.request_id is not None
        closed_again = await restarted.run(close, close.request_id, 2, DEADLINE, CancelSignal())
        assert isinstance(closed_again, Finished)
        assert closed_again.observation.terminal


# Generated programs of runs, kills, restarts and clears


ACTIONS = ("run", "kill", "restart", "clear")


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "scenario"), REAL)
async def test_whatever_the_order_of_runs_kills_restarts_and_clears_a_step_ends_as_an_unkilled_one(
    case: ExecutorCase, scenario: Scenario
) -> None:
    reference = await reference_of(case, scenario)
    for seed in range(examples(pr=3, nightly=30)):
        rng = SeededRandom(seed)
        async with case.world() as world:
            request = await prepared(world, scenario)
            assert request.request_id is not None
            tag = request.request_id
            before = world.effects()
            between = range(min(before, reference.effects), max(before, reference.effects) + 1)
            rig = Rig(world)
            runner = rig.runner()
            attempt = 0
            for _ in range(rng.randint(1, 8)):
                attempt += 1
                match rng.choice(ACTIONS):
                    case "run":
                        done = await runner.run(request, tag, attempt, DEADLINE, CancelSignal())
                        assert Result.of(done) == reference.result, f"seed {seed}"
                    case "kill":
                        rig.crash_at = rng.randint(0, 2 * reference.writes - 1)
                        with contextlib.suppress(ProcessKilledError):
                            await runner.run(request, tag, attempt, DEADLINE, CancelSignal())
                        rig.crash_at = None
                    case "restart":
                        runner = rig.runner()
                    case "clear":
                        assert await runner.clear(request, tag) == Cleared()
                assert world.effects() in between, f"seed {seed}"
            runner = rig.runner()
            assert await runner.clear(request, tag) == Cleared()
            done = await runner.run(request, tag, attempt + 1, DEADLINE, CancelSignal())
            assert Result.of(done) == reference.result, f"seed {seed}"
            assert world.effects() == reference.effects, f"seed {seed}"
