"""``OperationStepRunner`` passes the StepRunner contract under each rerun policy.

The world a test builds counts the real effects of the owners and the work still going, so
"a rerun repeats no effect" and "clear leaves nothing running" are read from the world, not
from the runner. The shared operation scenarios run the real owners through the same cases.
"""

from __future__ import annotations

import asyncio
import itertools
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, NoReturn

import pytest

from vs_core.api import (
    ContractError,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    OperationDescriptor,
    OperationId,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    PoolId,
    RequestId,
    SchemaRef,
    Value,
)
from vs_core.api.recovery import Cancelled as CancelledStep
from vs_core.api.recovery import CancelReason, Finished
from vs_project.api import Project
from vs_runtime.api.core import (
    Applied,
    CancellableOwner,
    Cancelled,
    ExecutionResult,
    Indeterminate,
    NotApplied,
    ObservationFactory,
    OperationCatalog,
    OperationEntry,
    ReceiptStore,
)
from vs_runtime.api.observation_contracts import assert_core_accepts
from vs_runtime.api.operation_fixtures import (
    SCENARIO_NAMES,
    SCOPE,
    OperationScenario,
    SimulatedCrashError,
    catalog_of,
    execute_request,
    scenarios,
)
from vs_runtime.api.operation_step_runner import (
    ClearedWork,
    KeyedWrite,
    OperationStepRunner,
    ReadOnly,
    RerunPolicy,
)
from vs_runtime.api.step_runner import Adopted, CancelSignal, Cleared, ClearUnavailableError
from vs_runtime.api.testing import StepRunnerContract, StepRunnerUnderTest
from vs_sim.api import SeededRandom
from vs_sim.api.testing import (
    HANG_GUARD_S,
    SimThreads,
    VirtualClock,
    examples,
    run_virtual,
    yield_until,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic import BaseModel

    from vs_project.api import StateNamespace
    from vs_runtime.api.core import ExecutionContext, Inspection, OperationOwner


class WorkOutcome(Value):
    status: Literal["succeeded"] = "succeeded"
    name: str


class WorkRequest(OperationRequest):
    outcome_model: ClassVar[type[BaseModel]] = WorkOutcome
    name: str
    hangs: bool = False


class QueryWork(WorkRequest):
    kind: Literal["test.query-work"] = "test.query-work"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY


class OwnedWork(WorkRequest):
    kind: Literal["test.owned-work"] = "test.owned-work"
    lifecycle: Literal[LifecycleClass.OWNED_JOB] = LifecycleClass.OWNED_JOB


class WriteWork(WorkRequest):
    kind: Literal["test.write-work"] = "test.write-work"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE


type Work = QueryWork | WriteWork | OwnedWork


def registration(request: type[Work], lifecycle: LifecycleClass) -> OperationRegistration:
    stem = request.model_fields["kind"].default.replace(".", "-")
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind=request.model_fields["kind"].default,
            request_schema=SchemaRef(name=stem, version=1),
            outcome_schema=SchemaRef(name=f"{stem}-outcome", version=1),
            lifecycle=lifecycle,
            inspect=True,
            resource_pool=PoolId(root="pool") if lifecycle is LifecycleClass.OWNED_JOB else None,
        ),
        request_model=request,
        outcome_model=WorkOutcome,
    )


@dataclass
class World:
    """What the owners did: the effect each name has had, and the work still going."""

    process_outlives_task: bool
    effects: list[str] = field(default_factory=list)
    working: dict[str, int] = field(default_factory=dict)
    owner_blind: bool = False
    dies_after_effect: bool = False
    process_table_down: bool = False


class WorkOwner:
    """Performs a request's effect once it is done; a hanging request never gets there."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def execute(self, request: OperationRequest, context: ExecutionContext) -> WorkOutcome:
        del context
        assert isinstance(request, WorkRequest)
        world = self.world
        world.working[request.name] = world.working.get(request.name, 0) + 1
        try:
            if request.hangs:
                await asyncio.get_running_loop().create_future()
            world.effects.append(request.name)
            if world.dies_after_effect:
                world.dies_after_effect = False
                raise SimulatedCrashError
        finally:
            if not world.process_outlives_task or not request.hangs:
                world.working[request.name] -= 1
        return WorkOutcome(name=request.name)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        del context
        assert isinstance(request, WorkRequest)
        if self.world.owner_blind:
            return Indeterminate("the owner cannot read its effect")
        if request.name in self.world.effects:
            return Applied(WorkOutcome(name=request.name))
        return NotApplied()


class WorldCleaner:
    """Ends the work a killed attempt left in the world."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def clear(self, tag: RequestId, request: OperationRequest) -> None:
        del request
        if self.world.process_table_down:
            raise ClearUnavailableError(tag, "the process table")
        self.world.working[tag.root] = 0


@contextmanager
def namespace_in(root: Path) -> Iterator[StateNamespace]:
    root.mkdir(parents=True, exist_ok=True)
    project = Project.open(root)
    yield project.state.state_store_namespace("run")


@dataclass
class Harness:
    """One world, the catalog over it, and the runner under test."""

    world: World
    catalog: OperationCatalog
    store: ReceiptStore
    clock: VirtualClock
    policy: RerunPolicy
    request: type[Work]

    @property
    def kind(self) -> str:
        return self.request.model_fields["kind"].default

    def runner(self) -> OperationStepRunner:
        """A runner with no memory of an earlier one: a restarted process."""
        return OperationStepRunner(
            self.catalog,
            {self.kind: self.policy},
            ObservationFactory(self.store),
            clock=self.clock,
        )

    def step(self, tag: RequestId, *, hangs: bool) -> ExecuteRegisteredOperation:
        request = self.request(name=tag.root, hangs=hangs)
        return ExecuteRegisteredOperation(
            request_id=tag,
            scope=SCOPE,
            operation_id=OperationId(root=f"op:{tag.root}"),
            operation=self.catalog.registry.encode(request),
            retry_limit=0,
        )


def harness(root: Path, request: type[Work], *, cleared: bool, keyed: bool) -> Harness:
    world = World(process_outlives_task=cleared)
    lifecycle = request.model_fields["lifecycle"].default
    entry = OperationEntry.owned(registration(request, lifecycle), WorkOwner(world))
    catalog = OperationCatalog(OperationRegistry((entry.registration,)), (entry,))
    namespace_cm = namespace_in(root)
    namespace = namespace_cm.__enter__()
    policy: RerunPolicy = (
        ClearedWork(WorldCleaner(world)) if cleared else KeyedWrite("name") if keyed else ReadOnly()
    )
    return Harness(
        world, catalog, ReceiptStore(namespace, SimThreads()), VirtualClock(), policy, request
    )


class OperationRunnerContract(StepRunnerContract):
    request: ClassVar[type[Work]]
    cleared: ClassVar[bool] = False
    keyed: ClassVar[bool] = False
    _worlds: Iterator[int]

    @pytest.fixture(autouse=True)
    def _world_directory(self) -> Iterator[None]:
        with tempfile.TemporaryDirectory() as raw:
            self._root = Path(raw)
            self._worlds = itertools.count()
            yield

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        built = harness(
            self._root / f"world-{next(self._worlds)}",
            self.request,
            cleared=self.cleared,
            keyed=self.keyed,
        )
        runner = built.runner()

        async def left_running(tag: RequestId) -> int:
            return built.world.working.get(tag.root, 0)

        @contextmanager
        def unreachable() -> Iterator[None]:
            """The backend of a keyed write is its owner; the backend of cleared work is the host."""
            flag = "process_table_down" if self.cleared else "owner_blind"
            setattr(built.world, flag, True)
            try:
                yield
            finally:
                setattr(built.world, flag, False)

        return StepRunnerUnderTest(
            runner=runner,
            run=lambda main: run_virtual(built.clock, main),
            finishing_step=lambda tag: built.step(tag, hangs=False),
            running_step=lambda tag: built.step(tag, hangs=True),
            left_running=left_running,
            unreachable=unreachable,
        )


class TestKeyedWriteOperationStepRunner(OperationRunnerContract):
    request = WriteWork
    keyed = True


class TestClearedWorkOperationStepRunner(OperationRunnerContract):
    request = QueryWork
    cleared = True


DEADLINE = 600.0


def tag_named(name: str) -> RequestId:
    return RequestId(root=name)


async def start_hanging(
    built: Harness, tag: RequestId
) -> tuple[asyncio.Task[object], CancelSignal]:
    cancel = CancelSignal()
    task = asyncio.ensure_future(
        built.runner().run(built.step(tag, hangs=True), tag, 1, DEADLINE, cancel)
    )

    async def working() -> int:
        return built.world.working.get(tag.root, 0)

    await yield_until(working, lambda count: count > 0 or task.done(), tag.root)
    return task, cancel


@pytest.mark.parametrize("cleared", [False, True])
def test_a_cancelled_attempt_returns_cancelled_for_the_reason_and_leaves_no_work(
    tmp_path: Path, *, cleared: bool
) -> None:
    built = harness(tmp_path, QueryWork, cleared=cleared, keyed=False)

    async def main() -> None:
        tag = tag_named("cancelled")
        task, cancel = await start_hanging(built, tag)
        cancel.set(CancelReason.DEADLINE)
        assert await asyncio.wait_for(task, HANG_GUARD_S) == CancelledStep(
            reason=CancelReason.DEADLINE
        )
        assert built.world.working[tag.root] == 0

    run_virtual(built.clock, main())


def test_a_read_only_operation_has_nothing_to_clear_and_runs_again_after_a_kill(
    tmp_path: Path,
) -> None:
    built = harness(tmp_path, QueryWork, cleared=False, keyed=False)
    tag = tag_named("reread")

    async def main() -> None:
        task, _ = await start_hanging(built, tag)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert built.world.working[tag.root] == 0
        assert await built.runner().clear(built.step(tag, hangs=False), tag) == Cleared()
        result = await built.runner().run(
            built.step(tag, hangs=False), tag, 2, DEADLINE, CancelSignal()
        )
        assert isinstance(result, Finished)
        assert built.world.effects == [tag.root]

    run_virtual(built.clock, main())


def test_a_write_that_landed_before_the_attempt_died_is_adopted_and_never_written_twice(
    tmp_path: Path,
) -> None:
    built = harness(tmp_path, WriteWork, cleared=False, keyed=True)
    tag = tag_named("landed")

    async def main() -> None:
        await built.runner().run(built.step(tag, hangs=False), tag, 1, DEADLINE, CancelSignal())
        restarted = built.runner()
        adopted = await restarted.clear(built.step(tag, hangs=False), tag)
        assert isinstance(adopted, Adopted)
        assert isinstance(adopted.result, Finished)
        again = await restarted.run(built.step(tag, hangs=False), tag, 2, DEADLINE, CancelSignal())
        assert again == adopted.result
        assert built.world.effects == [tag.root]

    run_virtual(built.clock, main())


def test_an_owner_that_cannot_read_its_effect_makes_clear_unavailable_not_a_result(
    tmp_path: Path,
) -> None:
    built = harness(tmp_path, WriteWork, cleared=False, keyed=True)
    tag = tag_named("blind")

    async def main() -> None:
        await built.runner().run(built.step(tag, hangs=False), tag, 1, DEADLINE, CancelSignal())
        built.world.owner_blind = True
        with pytest.raises(ClearUnavailableError) as refused:
            await built.runner().clear(built.step(tag, hangs=False), tag)
        assert refused.value.tag == tag
        with pytest.raises(ClearUnavailableError):
            await built.runner().run(built.step(tag, hangs=False), tag, 2, DEADLINE, CancelSignal())
        built.world.owner_blind = False
        assert isinstance(await built.runner().clear(built.step(tag, hangs=False), tag), Adopted)

    run_virtual(built.clock, main())


def test_a_cleaner_that_cannot_reach_the_process_table_makes_clear_unavailable(
    tmp_path: Path,
) -> None:
    built = harness(tmp_path, QueryWork, cleared=True, keyed=False)
    tag = tag_named("stuck")

    async def main() -> None:
        task, _ = await start_hanging(built, tag)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        built.world.process_table_down = True
        with pytest.raises(ClearUnavailableError):
            await built.runner().clear(built.step(tag, hangs=False), tag)
        assert built.world.working[tag.root] == 1
        built.world.process_table_down = False
        assert await built.runner().clear(built.step(tag, hangs=True), tag) == Cleared()
        assert built.world.working[tag.root] == 0

    run_virtual(built.clock, main())


# Registration: an owned operation declares a rerun policy its lifecycle allows.

ALLOWED = {
    (QueryWork, ReadOnly),
    (QueryWork, ClearedWork),
    (WriteWork, KeyedWrite),
    (WriteWork, ClearedWork),
}


def policy_of(kind: type[RerunPolicy], world: World) -> RerunPolicy:
    if kind is KeyedWrite:
        return KeyedWrite("name")
    if kind is ClearedWork:
        return ClearedWork(WorldCleaner(world))
    return ReadOnly()


@pytest.mark.parametrize("policy_type", [ReadOnly, KeyedWrite, ClearedWork, None])
@pytest.mark.parametrize("request_type", [QueryWork, WriteWork, OwnedWork])
def test_an_operation_registers_only_with_a_policy_its_lifecycle_allows(
    tmp_path: Path, request_type: type[Work], policy_type: type[RerunPolicy] | None
) -> None:
    built = harness(tmp_path, request_type, cleared=False, keyed=False)
    policies = {} if policy_type is None else {built.kind: policy_of(policy_type, built.world)}

    def register() -> OperationStepRunner:
        return OperationStepRunner(
            built.catalog,
            policies,
            ObservationFactory(built.store),
            clock=built.clock,
        )

    if (request_type, policy_type) in ALLOWED:
        register()
    else:
        with pytest.raises(ContractError, match=built.kind):
            register()


def test_a_policy_for_an_operation_the_catalog_does_not_own_is_refused(tmp_path: Path) -> None:
    built = harness(tmp_path, QueryWork, cleared=False, keyed=False)
    with pytest.raises(ContractError, match=r"test\.stray"):
        OperationStepRunner(
            built.catalog,
            {built.kind: ReadOnly(), "test.stray": ReadOnly()},
            ObservationFactory(built.store),
            clock=built.clock,
        )


def test_a_keyed_write_names_its_key() -> None:
    with pytest.raises(ContractError, match="key"):
        KeyedWrite(" ")


# The real owners of the shared operation scenarios: a rerun repeats no effect.


class DyingOwner:
    """Wraps a real owner and kills the process before or after its effect."""

    def __init__(self, inner: OperationOwner, *, after_effect: bool) -> None:
        self.inner = inner
        self.after_effect = after_effect

    async def execute(self, request: OperationRequest, context: ExecutionContext) -> NoReturn:
        if not self.after_effect:
            raise SimulatedCrashError
        await self.inner.execute(request, context)
        raise SimulatedCrashError

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        return await self.inner.inspect(request, context)

    async def cancel(self, request: OperationRequest, context: ExecutionContext) -> Cancelled:
        assert isinstance(self.inner, CancellableOwner)
        return await self.inner.cancel(request, context)


def policies_of(items: tuple[OperationScenario, ...]) -> dict[str, RerunPolicy]:
    return {
        item.entry.schema.kind: ReadOnly()
        if item.allows_repeat
        else KeyedWrite("the owner's own key")
        for item in items
        if item.entry.owner is not None
    }


@contextmanager
def scenario_world(
    died: str | None = None, only: str | None = None
) -> Iterator[tuple[tuple[OperationScenario, ...], ReceiptStore, VirtualClock]]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "project").mkdir()
        namespace = Project.open(root / "project").state.state_store_namespace("run")
        items = scenarios(root, namespace)
        if died is not None:
            items = tuple(
                replace(
                    item,
                    entry=OperationEntry.owned(
                        item.entry.registration,
                        DyingOwner(item.entry.owner, after_effect=died == "after_effect"),
                    ),
                )
                if item.name == only and item.entry.owner is not None
                else item
                for item in items
            )
        yield items, ReceiptStore(namespace, SimThreads()), VirtualClock()


def scenario_runner(
    items: tuple[OperationScenario, ...], store: ReceiptStore, clock: VirtualClock
) -> OperationStepRunner:
    return OperationStepRunner(
        catalog_of(items),
        policies_of(items),
        ObservationFactory(store),
        clock=clock,
    )


def owned(name: str) -> bool:
    return name not in ("interpret", "retain")


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_a_step_of_every_scenario_runs_once_and_runs_again_with_the_same_effect(name: str) -> None:
    with scenario_world() as (items, store, clock):
        item = next(item for item in items if item.name == name)
        request = execute_request(catalog_of(items), item.request, f"step-{name}")
        tag = request.request_id
        assert tag is not None

        async def main() -> None:
            first = await scenario_runner(items, store, clock).run(
                request, tag, 1, DEADLINE, CancelSignal()
            )
            assert isinstance(first, Finished)
            effects = item.effects()
            assert_core_accepts([ExecutionResult(observation=first.report)], expect_retry=False)
            if not owned(name):
                assert first.observation.status is ObservationStatus.REJECTED
                return
            assert first.observation.status is ObservationStatus.SUCCEEDED
            again = await scenario_runner(items, store, clock).run(
                request, tag, 2, DEADLINE, CancelSignal()
            )
            assert again == first or item.allows_repeat
            assert item.effects() == effects

        run_virtual(clock, main())


@pytest.mark.parametrize("name", [name for name in SCENARIO_NAMES if owned(name)])
@pytest.mark.parametrize("died", ["before_effect", "after_effect"])
def test_a_process_that_died_around_the_effect_leaves_it_done_once_after_clear_and_run(
    name: str, died: str
) -> None:
    with scenario_world(died, name) as (items, store, clock):
        item = next(item for item in items if item.name == name)
        request = execute_request(catalog_of(items), item.request, f"died-{name}")
        tag = request.request_id
        assert tag is not None

        async def main() -> None:
            with pytest.raises(SimulatedCrashError):
                await scenario_runner(items, store, clock).run(
                    request, tag, 1, DEADLINE, CancelSignal()
                )
            landed = item.effects()
            restarted = scenario_runner(items, store, clock)
            cleared = await restarted.clear(request, tag)
            if item.allows_repeat or died == "before_effect":
                assert cleared == Cleared()
            else:
                assert isinstance(cleared, Adopted)
                assert isinstance(cleared.result, Finished)
                assert item.effects() == landed

        run_virtual(clock, main())


# Generated programs: whatever the order of runs, kills, restarts and deaths, a keyed write
# lands once.

STEPS = ("run", "kill", "restart", "die", "clear")


def program(seed: int) -> tuple[str, ...]:
    rng = SeededRandom(seed)
    return tuple(rng.choice(STEPS) for _ in range(rng.randint(1, 10)))


@pytest.mark.parametrize("seed", range(examples(pr=8, nightly=80)))
def test_whatever_the_order_of_kills_deaths_and_restarts_a_keyed_write_lands_once(
    tmp_path: Path, seed: int
) -> None:
    built = harness(tmp_path, WriteWork, cleared=False, keyed=True)
    tag = tag_named("program")
    runner = built.runner()

    async def run_once(attempt: int) -> None:
        outcome = await asyncio.gather(
            runner.run(built.step(tag, hangs=False), tag, attempt, DEADLINE, CancelSignal()),
            return_exceptions=True,
        )
        if isinstance(outcome[0], SimulatedCrashError):
            return
        assert isinstance(outcome[0], Finished)

    async def main() -> None:
        nonlocal runner
        for attempt, action in enumerate(program(seed), start=1):
            match action:
                case "run":
                    await run_once(attempt)
                case "kill":
                    task, _ = await start_hanging(built, tag)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                case "restart":
                    runner = built.runner()
                case "die":
                    built.world.dies_after_effect = True
                    await run_once(attempt)
                case "clear":
                    await runner.clear(built.step(tag, hangs=False), tag)
            assert built.world.effects.count(tag.root) <= 1
        await runner.clear(built.step(tag, hangs=False), tag)
        await run_once(2)
        assert built.world.effects.count(tag.root) == 1

    run_virtual(built.clock, main())
