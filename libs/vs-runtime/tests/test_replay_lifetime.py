"""One launch lifetime commits its epoch before setup and serves live controls."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_core.api.replay import applied_control
from vs_project.api import CommitFault, FakeStateStore, RequestState, StoredEnvelope
from vs_project.api.testing import FakeControlInbox
from vs_proto.api import Ended, Outcome, Record, ReplayStore, Span, core_common, replay
from vs_proto.api import replay_workspace as workspace
from vs_runlog.api import current_epoch
from vs_runtime.api.replay import (
    SETUP_FAILED,
    FakeReplayExecutor,
    ReplayCommitError,
    ReplayCompleted,
    ReplayEnding,
    ReplayExecutor,
    ReplayLaunchResources,
    ReplayLifetime,
    ReplayOperationBinding,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayParked,
    ReplayPlan,
    ReplayRecoveryRequiredError,
    ReplayResult,
    ReplayRunHost,
    ReplayRuntime,
    drive_replay,
    open_replay,
    replay_lifetime,
)
from vs_sim.api.testing import Sim

from .test_replay_controls import control, two_turns
from .test_replay_run import Reply, call, launch
from .test_replay_turn_dependencies import result

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from protobuf import Message
    from pydantic import BaseModel

    from vs_core.api import OperationRequest
    from vs_core.api.replay import ReplayFlow
    from vs_runtime.api.replay import ReplayWriterBarrier

Setup = Literal["opens", "enter_fails", "binding_fails"]
TURN_SECONDS = 10


def ending(_: object) -> ReplayEnding:
    return ReplayEnding(Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True))


async def echo(ctx: ReplayFlow) -> object:
    return (await call(ctx, "echo")).root


def records(store: FakeStateStore) -> tuple[Record, ...]:
    stored = store.load()
    if stored is None:
        return ()
    assert isinstance(stored, StoredEnvelope)
    return tuple(ReplayStore.from_binary(stored.payload).records)


def endings(store: FakeStateStore) -> list[Ended]:
    return [
        record.body.value.state.value
        for record in records(store)
        if record.body is not None
        and record.body.field == "lifecycle"
        and record.body.value.state is not None
        and record.body.value.state.field == "ended"
    ]


class Projector:
    name = "seen"

    def __init__(self) -> None:
        self.seen: list[int] = []

    def project(self, records: tuple[Record, ...]) -> None:
        self.seen.append(len(records))


class Environment:
    """A run environment recording what was durable when it was asked to open."""

    def __init__(self, store: FakeStateStore, executor: ReplayExecutor) -> None:
        self.store, self.owner = store, executor
        self.epochs_at_open: list[int] = []
        self.adoption: list[bool] = []
        self.setup: Setup = "opens"
        self.writer_barrier: ReplayWriterBarrier | None = None

    def executor(
        self, plan: ReplayPlan, host: ReplayOperationHost, *, allow_adoption: bool
    ) -> ReplayExecutor:
        del plan, host
        if self.setup == "binding_fails":
            problem = "replay operation owner is absent"
            raise ReplayOperationError(problem)
        self.adoption.append(allow_adoption)
        return self.owner

    @asynccontextmanager
    async def open(self) -> AsyncIterator[Environment]:
        self.epochs_at_open.append(current_epoch(records(self.store)).number)
        if self.setup == "enter_fails":
            problem = "the run environment did not open"
            raise OSError(problem)
        yield self


def plan(projectors: tuple[Projector, ...] = ()) -> ReplayPlan:
    return ReplayPlan(echo, replay.ReplayAllowance(iterations=10), ending, (), (), projectors)


def resources(
    store: FakeStateStore, environment: Environment, sim: Sim, inbox: FakeControlInbox | None = None
) -> ReplayLaunchResources:
    return ReplayLaunchResources(store, sim.clock, environment.open, inbox, 1)


def echoing() -> FakeReplayExecutor:
    return FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))


async def launched(
    plan: ReplayPlan, request: str, store: FakeStateStore, environment: Environment, sim: Sim
) -> ReplayCompleted | ReplayParked:
    async with open_replay(plan, launch(request), resources(store, environment, sim)) as lifetime:
        return await drive_replay(lifetime)


@pytest.mark.regression
@example(setup="enter_fails", epoch_fault=None, ending_fault=CommitFault.UNKNOWN_AFTER)
@given(
    setup=st.sampled_from(["opens", "enter_fails", "binding_fails"]),
    epoch_fault=st.none() | st.sampled_from(list(CommitFault)),
    ending_fault=st.none() | st.sampled_from(list(CommitFault)),
)
def test_setup_follows_the_durable_epoch_and_a_failed_setup_leaves_it_interrupted(
    setup: Setup, epoch_fault: CommitFault | None, ending_fault: CommitFault | None
) -> None:
    sim = Sim(seed=0)
    # The launch's second commit is its interruption only when its epoch and not its setup held.
    faults = (epoch_fault,) if setup == "opens" or epoch_fault else (None, ending_fault)
    store = FakeStateStore(fault_plan=faults)
    environment = Environment(store, echoing())
    environment.setup = setup

    async def scenario() -> None:
        if epoch_fault is None and setup == "opens":
            assert await launched(plan(), "first", store, environment, sim) == ReplayCompleted(
                "echo"
            )
        else:
            with pytest.raises((ReplayCommitError, ReplayOperationError, OSError)):
                await launched(plan(), "first", store, environment, sim)
        if epoch_fault is not None:
            # No setup runs on an epoch this launch could not confirm.
            assert environment.epochs_at_open == []
        else:
            assert environment.epochs_at_open == [1]
        if epoch_fault is None and setup != "opens":
            (ended,) = endings(store) or (None,)
            if ending_fault is None:
                assert ended is not None
                assert ended.outcome is Outcome.INTERRUPTED
                assert ended.interruption_reason == SETUP_FAILED
                assert ended.resumable
        environment.setup = "opens"
        resumed = await launched(plan(), "resume", store, environment, sim)
        assert resumed == ReplayCompleted("echo")
        epoch = current_epoch(records(store)).number
        assert environment.epochs_at_open[-1] == epoch
        # Only a run's first epoch may adopt; every later epoch is an operator Resume.
        assert environment.adoption[-1] is (epoch == 1)
        for prior in endings(store)[:-1]:
            assert prior.outcome is Outcome.INTERRUPTED or prior.outcome is Outcome.FINISHED
            assert prior.resumable

    sim.run(scenario())


def test_a_lifetime_drives_once_and_exists_only_after_its_epoch(sim: Sim) -> None:
    store = FakeStateStore()
    runtime = ReplayRuntime(store, echo, replay.ReplayAllowance(iterations=10))
    host = ReplayRunHost(runtime, echoing(), sim.clock, ending)

    async def scenario() -> None:
        with pytest.raises(TypeError, match="open a replay lifetime"):
            ReplayLifetime(host, launch("first"), object())
        async with replay_lifetime(host, launch("first")) as lifetime:
            assert await drive_replay(lifetime) == ReplayCompleted("echo")
            committed = records(store)
            with pytest.raises(RuntimeError, match="drives its epoch once"):
                await drive_replay(lifetime)
            assert records(store) == committed
        assert current_epoch(records(store)).number == 1

    sim.run(scenario())


def test_projectors_fold_the_committed_prefix_when_the_lifetime_closes(sim: Sim) -> None:
    store = FakeStateStore()
    projector = Projector()

    async def scenario() -> None:
        environment = Environment(store, echoing())
        assert await launched(plan((projector,)), "first", store, environment, sim) == (
            ReplayCompleted("echo")
        )
        assert projector.seen == [len(records(store))]

    sim.run(scenario())


def never_decoded(value: Message) -> OperationRequest:
    raise AssertionError(value)


def never_encoded(value: BaseModel) -> Message:
    raise AssertionError(value)


def binding(
    operation: str, request: type[Message] = core_common.Identity
) -> ReplayOperationBinding:
    return ReplayOperationBinding(
        operation,
        "kind",
        request,
        core_common.Identity,
        never_decoded,
        never_encoded,
        1,
        paid=False,
    )


@pytest.mark.parametrize(
    ("bindings", "projectors", "problem"),
    [
        ((binding("a"), binding("a")), (), "duplicate replay plan operation binding: a"),
        ((), (Projector(), Projector()), "duplicate replay plan projector: seen"),
        (
            (binding("a", workspace.ReplayWorkspaceRequest),),
            (),
            "cannot replace lifecycle owner",
        ),
    ],
)
def test_a_plan_is_closed_before_any_launch(
    bindings: tuple[ReplayOperationBinding, ...], projectors: tuple[Projector, ...], problem: str
) -> None:
    with pytest.raises(ReplayOperationError, match=problem):
        ReplayPlan(echo, replay.ReplayAllowance(), ending, (), bindings, projectors)


def turns_owner(sim: Sim, cancelled: list[str]) -> FakeReplayExecutor:
    async def perform(span: Span) -> ReplayResult:
        if span.replay_physical is not None:
            try:
                await sim.clock.sleep(TURN_SECONDS)
            except asyncio.CancelledError:
                cancelled.append(span.id)
                raise
        return result(span)

    return FakeReplayExecutor(perform)


@pytest.mark.regression
@example(posted=1)
@example(posted=20)
@given(posted=st.integers(min_value=0, max_value=20))
def test_live_pause_now_withdraws_then_cancels_and_parks(posted: int) -> None:
    sim = Sim(seed=0)
    store, inbox, cancelled = FakeStateStore(), FakeControlInbox(), []
    owner = turns_owner(sim, cancelled)
    environment = Environment(store, owner)
    flow = ReplayPlan(two_turns(30, 30), replay.ReplayAllowance(), ending, (), (), ())

    async def post() -> None:
        await sim.clock.sleep(posted)
        inbox.post("control", control("pause_now").to_binary(), ended=lambda: None)

    async def scenario() -> None:
        poster = asyncio.create_task(post())
        opened = open_replay(flow, launch("first"), resources(store, environment, sim, inbox))
        async with opened as lifetime:
            outcome = await drive_replay(lifetime)
            archive = lifetime.host.runtime.archive
        await poster
        assert inbox.served_format() is not None
        status = inbox.status("control", ended=None)
        assert status is not None
        if isinstance(outcome, ReplayCompleted):
            assert status.state is RequestState.PENDING
            assert cancelled == []
            return
        assert outcome == ReplayParked()
        assert status.state is RequestState.APPLIED
        disposition = archive.controls.disposition("control")
        assert disposition is not None
        applied = applied_control(disposition)
        assert applied is not None
        # Every named child was withdrawn and none accepted.
        for identity in applied.withdrawn:
            child = archive.span(identity)
            assert child.replay_physical is not None
            assert child.replay_physical.has_field("withdrawn_sequence")
            assert not child.has_field("ended_at")
            parent = archive.span(child.replay_physical.logical)
            assert parent.replay is not None
            assert parent.replay.outcome is None
        assert set(cancelled) <= set(applied.withdrawn)
        if 0 < posted < TURN_SECONDS:
            # Both turns were running at the next control poll: withdrawn, then cancelled.
            assert len(applied.withdrawn) == 2
            assert sorted(cancelled) == sorted(applied.withdrawn)
        invocations = owner.invocations
        # A later launch inspects withdrawn children and never redispatches them blindly.
        with pytest.raises(ReplayRecoveryRequiredError) as raised:
            await launched(flow, "resume", store, environment, sim)
        assert {span.id for span in raised.value.spans} >= set(applied.withdrawn)
        assert owner.invocations == invocations

    sim.run(scenario())
