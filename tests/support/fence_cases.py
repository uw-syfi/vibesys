"""The setup/release pairs the effect-fence contract runs (``vs_runtime.api.fence_contracts``).

Each pair is one executor's create and release requests over real disk and Fakes:

* ``session-close``: EnsureSession binds a session, CloseSession releases it;
* ``turn-cancel``: DispatchTurn calls the provider, CancelTurn stops or withdraws the turn;
* ``workspace-discard``: EnsureWorkspace creates a worktree, DiscardWorkspace removes it;
* ``scope-close``: SubmitMeasurement submits an executor job, CloseAttemptScope cancels the
  scope episode's jobs;
* ``job-cancel``: SubmitMeasurement submits an executor job, CancelOwnedJob cancels it;
* ``operation-inspect``: ExecuteRegisteredOperation runs an owner's effect, and an
  InspectRequest of it that seals "never ran" is the release.

A new executor that creates and releases adds its pair to ``FENCE_PAIRS``, tagged with its
``FenceKind``; the contract's coverage test fails until every kind has exactly one pair.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from tests.support.session_lifecycle_world import (
    cancel_request,
    close_request,
    lifecycle_executor,
    open_lifecycle_host,
    released_keys,
)
from tests.support.session_world import (
    dispatch_request,
    ensure_request,
)

from vs_agent.api import AgentSessionState, DurableSessionStore, Pending
from vs_core.api import (
    AttemptId,
    AttemptRef,
    CancelOwnedJob,
    CloseAttemptScope,
    RequestId,
    ResourceId,
    SubmitMeasurement,
)
from vs_evaluation.api import EvaluationState, ExecutorObservation, ExecutorPoll, PollPhase
from vs_project.api import Project
from vs_runtime.api.core import (
    ExecutionResult,
    FenceKind,
    MeasurementRequests,
    ReceiptStore,
    SessionBinding,
    handle_for,
    owner_key,
)
from vs_runtime.api.executor_contexts import context_for
from vs_runtime.api.fence_contracts import answered_released
from vs_runtime.api.slurm_evaluation_stack import ADMISSION, SCOPE, plan
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.support.session_world import SessionHost

    from vs_core.api import RequestBase
    from vs_evaluation.api import EvaluationRequest
    from vs_project.api import StateNamespace
    from vs_runtime.api.fence_contracts import FenceHost, FenceWorld, HostLife
    from vs_sim.api import BlockingRunner

_INVOCATION = "inv-1"


@dataclass
class _Host:
    """One host: an executor and the epoch its context carries."""

    executor: Any
    epoch: int

    async def execute(self, request: RequestBase) -> ExecutionResult:
        outcome = await self.executor.execute(
            cast("Any", request), context_for(request, epoch=self.epoch)
        )
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome


class _ReleasesItself:
    """A pair whose release answers for itself: released is its own terminal release."""

    def released(self, result: ExecutionResult) -> bool:
        return answered_released(result)

    def settled(self, result: ExecutionResult) -> bool:
        return answered_released(result)


# sessions


class _SessionWorld(_ReleasesItself):
    """The lifecycle session world; the pair decides which requests set up and release."""

    def __init__(self, base: Path) -> None:
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        self._project = Project.open(base / "project")
        slot = self._project.state.state_store_namespace("sessions").slot(
            "sessions.json", AgentSessionState
        )
        self.session_host: SessionHost = open_lifecycle_host(
            base / "workspace", DurableSessionStore(slot, threads=OsThreads())
        )

    def _namespace(self) -> StateNamespace:
        return self._project.state.state_store_namespace("run")

    def store(self) -> ReceiptStore:
        return ReceiptStore(self._namespace(), threads=OsThreads())

    def host(self, epoch: int, life: HostLife, blocking: BlockingRunner) -> FenceHost:
        store = ReceiptStore(life.namespace(self._namespace()), threads=OsThreads())
        return _Host(lifecycle_executor(self.session_host, store, blocking=blocking), epoch)

    def bound(self) -> bool:
        key = f"{owner_key(ensure_request())}/{ensure_request().spec.session_id.root}"
        return self.store().load("session-bindings", "binding", key, SessionBinding) is not None


class _SessionCloseWorld(_SessionWorld):
    async def prepare(self, host: FenceHost) -> None:
        del host

    def setup(self, identity: str) -> RequestBase:
        return ensure_request(f"req-ensure-{identity}")

    def release(self, identity: str) -> RequestBase:
        return close_request(f"req-close-{identity}")

    def created(self) -> int:
        return int(self.bound())

    def held(self) -> int:
        return int(self.bound() and released_keys(self.session_host) == 0)


class _TurnCancelWorld(_SessionWorld):
    async def prepare(self, host: FenceHost) -> None:
        ensured = await host.execute(ensure_request())
        assert ensured.observation.observation.accepted, ensured

    def setup(self, identity: str) -> RequestBase:
        return dispatch_request(f"req-dispatch-{identity}", _INVOCATION)

    def release(self, identity: str) -> RequestBase:
        return cancel_request(f"req-cancel-{identity}", invocation=_INVOCATION)

    def created(self) -> int:
        return len(self.session_host.turns)

    def held(self) -> int:
        """A provider turn whose dispatch call has not returned and was never released."""
        state = self.session_host.journal.load_optional()
        row = None if state is None else state.invocations.get(_INVOCATION)
        return int(row is not None and isinstance(row.outcome, Pending) and not row.interrupted)


@dataclass
class _SessionPair:
    name: str
    world_type: type[_SessionCloseWorld | _TurnCancelWorld]
    kind: FenceKind

    @asynccontextmanager
    async def world(self) -> AsyncIterator[FenceWorld]:
        with tempfile.TemporaryDirectory() as raw:
            yield self.world_type(Path(raw))


# evaluation


_SUBMIT_STEPS = 3
"""Blocking calls a submission spends in flight before it lands."""


@dataclass
class _JobTable:
    """The executor's jobs, which outlive every host: each submitted handle, and the ended ones."""

    submitted: list[str]
    ended: set[str]


class _HostJobs:
    """One host's client of the job table; each call runs on the host's blocking runner."""

    def __init__(self, table: _JobTable, blocking: BlockingRunner) -> None:
        self._table = table
        self._blocking = blocking

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        del request

        def submit() -> None:
            if handle_id not in self._table.submitted:
                self._table.submitted.append(handle_id)

        # A submission is in flight for longer than a poll or a cancel takes, so those can
        # run, and answer, before it lands.
        for _ in range(_SUBMIT_STEPS):
            await self._blocking.run(lambda: None)
        await self._blocking.run(submit)

    async def poll(self, handle_id: str) -> ExecutorPoll:
        def poll() -> ExecutorPoll:
            if handle_id not in self._table.submitted:
                return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
            if handle_id in self._table.ended:
                cancelled = ExecutorObservation(state=EvaluationState.CANCELED)
                return ExecutorPoll(phase=PollPhase.ENDED, terminal=cancelled)
            return ExecutorPoll(phase=PollPhase.QUEUED)

        return await self._blocking.run(poll)

    async def cancel(self, handle_id: str) -> None:
        def cancel() -> None:
            if handle_id in self._table.submitted:
                self._table.ended.add(handle_id)

        await self._blocking.run(cancel)


class _EvaluationWorld(_ReleasesItself):
    def __init__(self, base: Path, *, cancel: bool) -> None:
        (base / "project").mkdir()
        self._project = Project.open(base / "project")
        self._table = _JobTable(submitted=[], ended=set())
        self._cancel = cancel

    async def prepare(self, host: FenceHost) -> None:
        del host

    def setup(self, identity: str) -> RequestBase:
        return SubmitMeasurement(
            request_id=RequestId(root=f"submit-{identity}"),
            scope=SCOPE,
            admission_id=ADMISSION,
            plan=plan(),
        )

    def release(self, identity: str) -> RequestBase:
        if self._cancel:
            return CancelOwnedJob(
                request_id=RequestId(root=f"cancel-{identity}"),
                scope=SCOPE,
                admission_id=ADMISSION,
                resource_id=ResourceId(root=handle_for(RequestId(root="submit-a"))),
            )
        return CloseAttemptScope(
            request_id=RequestId(root=f"close-{identity}"),
            scope=SCOPE,
            admission_id=ADMISSION,
            attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
        )

    def host(self, epoch: int, life: HostLife, blocking: BlockingRunner) -> FenceHost:
        store = ReceiptStore(
            life.namespace(self._project.state.state_store_namespace("run")), threads=OsThreads()
        )
        jobs = _HostJobs(self._table, blocking)
        return _Host(MeasurementRequests(cast("Any", jobs), store), epoch)

    def _counted(self) -> list[str]:
        """Every job for a close; for a cancel, only the job it names (a late setup's is not)."""
        named = handle_for(RequestId(root="submit-a"))
        return [h for h in self._table.submitted if not self._cancel or h == named]

    def created(self) -> int:
        return len(self._counted())

    def held(self) -> int:
        return len(set(self._counted()) - self._table.ended)


@dataclass
class _EvaluationPair:
    name: str
    cancel: bool
    kind: FenceKind

    @asynccontextmanager
    async def world(self) -> AsyncIterator[FenceWorld]:
        with tempfile.TemporaryDirectory() as raw:
            yield _EvaluationWorld(Path(raw), cancel=self.cancel)


FENCE_PAIRS = (
    _SessionPair("session-close", _SessionCloseWorld, FenceKind.SESSION_EPISODE),
    _SessionPair("turn-cancel", _TurnCancelWorld, FenceKind.INVOCATION),
    _EvaluationPair("scope-close", cancel=False, kind=FenceKind.SCOPE_EPISODE),
    _EvaluationPair("job-cancel", cancel=True, kind=FenceKind.MEASUREMENT_JOB),
)

__all__ = ["FENCE_PAIRS"]
