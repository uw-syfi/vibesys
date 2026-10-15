"""A run's candidate workspaces outlive a host only while core still references them.

Core references an attempt generation's workspace while the attempt has not ended or a
request scoped to it is still open, and none once the run's result is final. A host's start
binds its workspaces to that reading of core's committed state.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptsState,
    AttemptView,
    CoreState,
    DecisionId,
    DiscardWorkspace,
    ItemId,
    LifecycleClass,
    RecordsState,
    RecoveryBarrier,
    RecoveryPhase,
    RequestId,
    RequestPhase,
    RequestRecord,
    RunStatus,
    Scope,
    Until,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
)
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    RunLoopConfig,
    revision_ref,
    start_core_awaiting_lease,
)
from vs_runtime.api.core_shell_fixtures import CounterConfig, counter
from vs_runtime.api.infrastructure import WorkspaceHolds, core_workspace_holds
from vs_runtime.api.testing import FakePublicationDelivery
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable

_ATTEMPTS = ("a0", "a1", "a2")


def _attempt(name: str, generation: int) -> AttemptRef:
    return AttemptRef(attempt_id=AttemptId(root=name), generation=generation)


def _view(attempt: AttemptRef, phase: AttemptPhase) -> AttemptView:
    return AttemptView(
        attempt_id=attempt.attempt_id,
        item_id=ItemId(root=attempt.attempt_id.root),
        generation=attempt.generation,
        phase=phase,
        workspace=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=revision_ref("0" * 40)),
        budget=AttemptBudget(),
    )


def _discard(index: int, attempt: AttemptRef, phase: RequestPhase) -> RequestRecord:
    request_id = RequestId(root=f"discard-{index}")
    request = DiscardWorkspace(
        request_id=request_id,
        scope=Scope(owner=attempt.attempt_id, generation=attempt.generation),
        admission_id=DecisionId(root="admit"),
        attempt=attempt,
    )
    return RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        until=Until(at=100.0),
        addressed_by=(),
    )


def _state(
    views: tuple[AttemptView, ...], records: tuple[RequestRecord, ...], status: RunStatus
) -> CoreState:
    core = initial_state()
    return core.model_copy(
        update={
            "run": core.run.model_copy(
                update={"declaration": counter.declaration, "status": status}
            ),
            "attempts": AttemptsState(attempts=views),
            "records": RecordsState(
                hosts=(), records=records, recovery=RecoveryBarrier(phase=RecoveryPhase.READY)
            ),
        }
    )


_refs = st.builds(_attempt, st.sampled_from(_ATTEMPTS), st.integers(0, 2))


@given(
    views=st.dictionaries(_refs, st.sampled_from(AttemptPhase), max_size=4),
    records=st.lists(st.tuples(_refs, st.sampled_from(RequestPhase)), max_size=4),
    status=st.sampled_from(RunStatus),
)
def test_core_holds_an_attempt_workspace_until_the_attempt_and_its_requests_end(
    views: dict[AttemptRef, AttemptPhase],
    records: list[tuple[AttemptRef, RequestPhase]],
    status: RunStatus,
) -> None:
    """Held: an attempt not ended, or one an open request is scoped to; none once final."""
    core = _state(
        tuple(_view(attempt, phase) for attempt, phase in views.items()),
        tuple(_discard(index, attempt, phase) for index, (attempt, phase) in enumerate(records)),
        status,
    )
    holds = core_workspace_holds(core)
    assert holds.final == (status == RunStatus.TERMINAL)
    expected = (
        frozenset()
        if holds.final
        else frozenset(
            {attempt for attempt, phase in views.items() if phase != AttemptPhase.TERMINAL}
            | {attempt for attempt, phase in records if phase != RequestPhase.COMPLETED}
        )
    )
    assert holds.attempts == expected
    assert holds.workspace_ids() == WorkspaceHolds(final=False, attempts=expected).workspace_ids()
    assert len(holds.workspace_ids()) == len(expected)


class _Following:
    """Run workspaces that record what they follow and when they sweep."""

    def __init__(self) -> None:
        self.holds: Callable[[], WorkspaceHolds] | None = None
        self.swept: list[WorkspaceHolds] = []

    def follow(self, holds: Callable[[], WorkspaceHolds]) -> None:
        self.holds = holds

    async def release_unheld(self) -> None:
        assert self.holds is not None, "a sweep before core is followed keeps nothing correct"
        self.swept.append(self.holds())


class _InterruptedFollowing(_Following):
    """Run workspaces whose selected recovery step interrupts startup."""

    def __init__(self, phase: str, error: BaseException) -> None:
        super().__init__()
        self._phase = phase
        self._error = error

    def follow(self, holds: Callable[[], WorkspaceHolds]) -> None:
        super().follow(holds)
        if self._phase == "follow":
            raise self._error

    async def release_unheld(self) -> None:
        if self._phase == "release":
            raise self._error
        await super().release_unheld()


def test_a_start_sweeps_by_and_then_follows_the_recovered_core_state() -> None:
    """A host's start sweeps by what recovered core references, and close reads it live."""
    open_attempt, ended = _attempt("a0", 0), _attempt("a1", 0)
    state = _state(
        (_view(open_attempt, AttemptPhase.ACTIVE), _view(ended, AttemptPhase.TERMINAL)),
        (),
        RunStatus.RUNNING,
    )
    shell = CoreRuntime(
        FakeStateStore(), counter.driver(CounterConfig()), state, config=CounterConfig()
    )
    clock = VirtualClock(1.0)
    workspaces = _Following()
    host = CoreRunHost(shell, FakePublicationDelivery(), clock, workspaces=workspaces)
    config = RunLoopConfig(host_id="follow", lease_duration=100.0)
    run_virtual(clock, start_core_awaiting_lease(host, config))
    assert workspaces.swept == [WorkspaceHolds(final=False, attempts=frozenset({open_attempt}))]
    assert workspaces.holds is not None
    assert workspaces.holds() == core_workspace_holds(shell.record.envelope.core)


@pytest.mark.parametrize(
    ("phase", "error_type"),
    [("follow", RuntimeError), ("release", asyncio.CancelledError)],
)
def test_an_interrupted_workspace_recovery_releases_the_new_lease(
    phase: str, error_type: type[BaseException]
) -> None:
    """A failed or cancelled post-acquisition step leaves no lease for a restart to wait out."""
    store = FakeStateStore()
    state = _state((), (), RunStatus.RUNNING)
    clock = VirtualClock(1.0)
    first = CoreRuntime(store, counter.driver(CounterConfig()), state, config=CounterConfig())
    host = CoreRunHost(
        first,
        FakePublicationDelivery(),
        clock,
        workspaces=_InterruptedFollowing(phase, error_type("recovery interrupted")),
    )
    config = RunLoopConfig(host_id="interrupted", lease_duration=60.0)

    with pytest.raises(error_type, match="recovery interrupted"):
        run_virtual(clock, start_core_awaiting_lease(host, config))

    assert not first.holds_lease(now_at=clock.now())
    second = CoreRuntime(store, counter.driver(CounterConfig()), state, config=CounterConfig())
    run_virtual(
        clock,
        start_core_awaiting_lease(
            CoreRunHost(second, FakePublicationDelivery(), clock),
            RunLoopConfig(host_id="restart", lease_duration=60.0),
        ),
    )
    assert clock.now() == 1.0
    assert second.holds_lease(now_at=clock.now())
