"""A request that runs for many lease durations does not cost the run its lease.

The loop renews the lease beside the requests it executes. A request that takes several lease
durations of run time (an agent turn) holds the loop at its await; without the heartbeat the
next commit is stamped after the lease lapsed and the run dies on a fence conflict. This runs
the production shell and loop on a virtual clock over a one-step search, so it costs nothing in
real time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import (
    AttemptBudget,
    AttemptId,
    EnsureWorkspace,
    Flow,
    FlowState,
    ItemId,
    OperationRegistry,
    ResourceId,
    RevisionRef,
    RunEnvelope,
    RunFacts,
    RunStatus,
    Value,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
)
from vs_core.api.testing import Harness, Succeeded, sample_limits, sample_retries
from vs_core.flows import search
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    RunLoopConfig,
    start_core,
    start_core_awaiting_lease,
)
from vs_runtime.api.core_shell_fixtures import CounterConfig, counter
from vs_runtime.api.testing import (
    LEASE,
    FakePublicationDelivery,
    HostOptions,
    Launch,
    ScriptedExecutors,
    scripted_host,
)
from vs_sim.api.testing import VirtualClock, examples, run_virtual

if TYPE_CHECKING:
    from vs_project.api import CommitOutcome, StoredEnvelope, StoreFence, StoreRecord
    from vs_runtime.api.core import ExecutionContext, ExecutionResult

BASELINE = RevisionRef.of_git_commit("abc123")
FACTS = RunFacts(
    objective="open one workspace",
    baseline=BASELINE,
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)


class OneStepConfig(Value):
    """The one-step search takes no settings."""


@search("one-step", config=OneStepConfig)
async def one_step(ctx: Flow, config: OneStepConfig) -> None:
    """Open one workspace and leave; the run then ends on the input."""
    del config
    async with ctx.workspaces.open(
        attempt=AttemptId(root="attempt:0"),
        item=ItemId(root="item:0"),
        plan=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=ctx.facts.baseline),
        budget=AttemptBudget(),
    ):
        pass


def _answer(request: object, core: object) -> Succeeded:
    del core
    if isinstance(request, EnsureWorkspace):
        return Succeeded(resource_id=ResourceId(root="workspace:0"), revision=request.plan.base)
    return Succeeded()


@settings(max_examples=examples(pr=3, nightly=20), deadline=None, derandomize=True, database=None)
@given(thirds=st.integers(min_value=1, max_value=30))
def test_a_request_longer_than_the_lease_does_not_lose_it(thirds: int) -> None:
    """Run time of ``thirds`` third-lease steps passes inside one request; the run still ends."""
    clocks: list[VirtualClock] = []

    class LongExecutors(ScriptedExecutors):
        async def execute(self, request: object, context: ExecutionContext) -> ExecutionResult:
            if isinstance(request, EnsureWorkspace):
                for _ in range(thirds):
                    await clocks[0].sleep(LEASE / 3)
            return await super().execute(request, context)  # ty: ignore[invalid-argument-type]

    harness = Harness(
        registry=OperationRegistry(()),
        facts=FACTS,
        limits=sample_limits(
            max_attempts=2, max_turns=2, max_parallel=1, request_retries=sample_retries(2)
        ),
        envelope_type=RunEnvelope[FlowState],
    )
    host = scripted_host(
        Launch(one_step, OneStepConfig()),
        _answer,
        harness,
        {},
        HostOptions(translator=LongExecutors),
    )
    clocks.append(host.clock)

    run = host.run()

    assert run.halted is None
    assert run.core.run.status == RunStatus.TERMINAL


class _SlowStateStore(FakeStateStore):
    """The memory store, but every commit holds the loop for ``cost`` seconds of run time.

    A production commit rewrites and fsyncs the whole run record on the loop's thread, so its
    cost grows with the record (seconds after hours of run time) and nothing else on the loop
    runs meanwhile. Moving the virtual clock inside the call, with no await, is exactly that.
    """

    def __init__(self, cost: float) -> None:
        super().__init__()
        self.cost = cost
        self.clock: VirtualClock | None = None

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        if self.clock is not None:
            self.clock.at += self.cost
        return super().commit(expected_revision, envelope, fence, now)


@settings(max_examples=examples(pr=3, nightly=20), deadline=None, derandomize=True, database=None)
@given(cost=st.floats(min_value=LEASE / 5, max_value=LEASE / 4))
def test_slow_commits_in_one_settle_do_not_starve_the_lease(cost: float) -> None:
    """Commits that each hold the loop for up to a quarter lease never cost the run its lease.

    ``start`` and one settle commit the recovery, the queue, the finished requests and the
    publications back to back, for longer than the lease, so the lease must be renewed between
    them, on a schedule that counts from when the store granted it, not from when the loop
    began. The loop renews between the steps of a settle, and a step commits at most twice
    here, so any commit shorter than a third of a lease keeps it.
    """
    harness = Harness(
        registry=OperationRegistry(()),
        facts=FACTS,
        limits=sample_limits(
            max_attempts=2, max_turns=2, max_parallel=1, request_retries=sample_retries(2)
        ),
        envelope_type=RunEnvelope[FlowState],
    )
    store = _SlowStateStore(cost)
    host = scripted_host(Launch(one_step, OneStepConfig()), _answer, harness, {}, store=store)
    store.clock = host.clock

    run = host.run()

    assert run.halted is None
    assert run.core.run.status == RunStatus.TERMINAL


class FanOutConfig(Value):
    """How many workspaces the fan-out search opens at once."""

    width: int


@search("fan-out", config=FanOutConfig)
async def fan_out(ctx: Flow, config: FanOutConfig) -> None:
    """Open ``width`` workspaces at once, so their requests are all prepared in one step."""

    async def open_one(task: Flow, index: int) -> None:
        async with task.workspaces.open(
            attempt=AttemptId(root=f"attempt:{index}"),
            item=ItemId(root=f"item:{index}"),
            plan=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=task.facts.baseline),
            budget=AttemptBudget(),
        ):
            pass

    async with ctx.group() as tasks:
        for index in range(config.width):
            tasks.start(f"open:{index}", open_one, index)


def _answer_each(request: object, core: object) -> Succeeded:
    """Answer as `_answer`, with a workspace of its own for every request."""
    del core
    if isinstance(request, EnsureWorkspace):
        identity = request.request_id.root if request.request_id is not None else "workspace"
        return Succeeded(resource_id=ResourceId(root=identity), revision=request.plan.base)
    return Succeeded()


_WIDTH = 8


@settings(max_examples=examples(pr=3, nightly=20), deadline=None, derandomize=True, database=None)
@given(cost=st.floats(min_value=LEASE / 5, max_value=LEASE / 4))
def test_a_burst_of_slow_dispatch_commits_does_not_starve_the_lease(cost: float) -> None:
    """``_WIDTH`` requests started in one settle, each start a quarter-lease commit, keep the lease.

    Each start commits its authorization before its executor runs, and starts run back to back
    while slots are free (a cancellation needs no slot at all), so the starts alone hold the loop
    for longer than the lease unless it is renewed between them.
    """
    harness = Harness(
        registry=OperationRegistry(()),
        facts=FACTS,
        limits=sample_limits(
            max_attempts=_WIDTH + 1,
            max_turns=2,
            max_parallel=_WIDTH,
            request_retries=sample_retries(2),
        ),
        envelope_type=RunEnvelope[FlowState],
    )
    store = _SlowStateStore(cost)
    host = scripted_host(
        Launch(fan_out, FanOutConfig(width=_WIDTH)),
        _answer_each,
        harness,
        {},
        HostOptions(max_concurrent=_WIDTH),
        store=store,
    )
    store.clock = host.clock

    run = host.run()

    assert run.halted is None
    assert run.core.run.status == RunStatus.TERMINAL


class _SlowDecodeStore(FakeStateStore):
    """The memory store, where each newly written record takes ``cost`` seconds to take in.

    A restart decodes and validates the whole stored record before it takes the lease, on the
    loop's thread: tens of seconds after hours of run time. A shell decodes each store revision
    once, so only the first load of a revision moves the clock, with no await.
    """

    def __init__(self, cost: float) -> None:
        super().__init__()
        self.cost = cost
        self.clock: VirtualClock | None = None
        self._seen: set[int] = set()

    def load(self) -> StoreRecord | None:
        record = super().load()
        revision = getattr(record, "revision", None)
        if self.clock is not None and revision is not None and revision not in self._seen:
            self._seen.add(revision)
            self.clock.at += self.cost
        return record


@settings(max_examples=examples(pr=3, nightly=20), deadline=None, derandomize=True, database=None)
@given(cost=st.floats(min_value=1.0, max_value=59.0))
def test_a_restart_holds_its_lease_a_full_term_after_taking_it(cost: float) -> None:
    """However long decoding the stored run takes, the new lease runs a full term from its taking.

    A restart that read the clock before decoding stamped its lease with that earlier time, so
    the decode ate into the term it had to renew within.
    """
    lease = 60.0
    clock = VirtualClock(1.0)
    store = _SlowDecodeStore(cost)
    core = initial_state()
    core = core.model_copy(
        update={"run": core.run.model_copy(update={"declaration": counter.declaration})}
    )

    def shell() -> CoreRuntime:
        return CoreRuntime(store, counter.driver(CounterConfig()), core, config=CounterConfig())

    start_core(
        CoreRunHost(shell(), FakePublicationDelivery(), clock),
        RunLoopConfig(host_id="crashed", lease_duration=lease),
    )
    clock.at += lease
    store.clock = clock
    restarted = shell()
    run_virtual(
        clock,
        start_core_awaiting_lease(
            CoreRunHost(restarted, FakePublicationDelivery(), clock),
            RunLoopConfig(host_id="restart", lease_duration=lease),
        ),
    )

    assert restarted.holds_lease(now_at=clock.now() + lease - 1.0)
