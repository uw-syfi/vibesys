"""The registered executor cases: one per receipt-backed role, over real owners and disk."""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from tests.support.executor_context import RevocableLease, context_for
from tests.support.executor_harness import FaultingNamespace, Scenario
from tests.support.runtime_evaluation import (
    ADMISSION,
    ScenarioCluster,
    Stack,
    build_stack,
    submission,
)
from tests.support.runtime_evaluation import (
    SCOPE as EVALUATION_SCOPE,
)
from tests.support.runtime_operations import SCOPE, catalog_of, execute_request, scenarios
from tests.support.workspace_world import WorkspaceEnv, open_workspace_env

from vs_core.api import (
    AdoptRevision,
    AttemptId,
    AttemptRef,
    BlockIntent,
    CancelOwnedJob,
    CancelOwnedResource,
    CloseAttemptScope,
    CollectEvidence,
    DecisionId,
    DiscardWorkspace,
    EnsureWorkspace,
    ExecuteRegisteredOperation,
    InspectOwnedJob,
    InspectRequest,
    InvocationId,
    InvocationRef,
    ObserveOwnedJob,
    RequestId,
    ResourceId,
    RestoreRevision,
    RetainedCandidate,
    RetainRevision,
    RunId,
    Scope,
    SessionId,
    SettlementId,
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    SubmitMeasurement,
    VerifyAdoption,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_project.api import Project, run_git
from vs_runtime.api.core import (
    ExecutionResult,
    JournalSemanticEvents,
    MeasurementRequests,
    NamespaceOperationReceipts,
    ObservationFactory,
    ReceiptStore,
    RegisteredOperationRequests,
    RuntimeWorkspaceRequests,
    revision_ref,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from vs_core.api import Request, RequestBase, RevisionRef
    from vs_project.api import StateNamespace


class _World:
    """Shared plumbing: a Project directory and a namespace that can die mid-write."""

    def __init__(self, base: Path) -> None:
        self.base = base
        (base / "project").mkdir()
        self._project = Project.open(base / "project")
        self._writes = 0

    def real_namespace(self) -> StateNamespace:
        return self._project.state.state_store_namespace("run")

    def faulting(self, crash_at: int | None) -> FaultingNamespace:
        return FaultingNamespace(self.real_namespace(), crash_at)

    def store(self, faulting: FaultingNamespace) -> ReceiptStore:
        return ReceiptStore(cast("StateNamespace", faulting))

    def writes(self) -> int:
        return self._writes


# operations


class _OperationsWorld(_World):
    def _items(self):  # noqa: ANN202  # lint-waiver: LW-0D3-10 [ANN202]; the tuple type is the support module's own scenario type.
        return scenarios(self.base / "owners", self.real_namespace())

    async def prepare(self, scenario: Scenario) -> RequestBase:
        catalog = catalog_of(self._items())
        echo = next(item for item in self._items() if item.name == "echo")
        if scenario.kind is ExecuteRegisteredOperation:
            return execute_request(catalog, echo.request, "req-exec")
        seed = execute_request(catalog, echo.request, "req-seed")
        await self.execute(seed, lease=RevocableLease(), crash_at=None)
        if scenario.kind is InspectRequest:
            return InspectRequest(
                request_id=RequestId(root="req-inspect"),
                scope=SCOPE,
                deadline_at=100.0,
                target=RequestId(root="req-seed"),
                resource_id=None,
            )
        return CancelOwnedResource(
            request_id=RequestId(root="req-cancel"),
            scope=SCOPE,
            deadline_at=100.0,
            resource_id=ResourceId(root="resource"),
            target=RequestId(root="req-seed"),
        )

    async def execute(
        self,
        request: RequestBase,
        *,
        lease: RevocableLease,
        crash_at: int | None,
        digest: str | None = None,
    ) -> ExecutionResult:
        faulting = self.faulting(crash_at)
        store = self.store(faulting)
        runner = RegisteredOperationRequests(
            catalog_of(self._items()), NamespaceOperationReceipts(store), ObservationFactory(store)
        )
        context = context_for(request, lease=lease)
        if digest is not None:
            context = context.model_copy(update={"payload_digest": digest})
        try:
            return await runner.execute(cast("ExecuteRegisteredOperation", request), context)
        finally:
            self._writes = faulting.writes

    def effects(self) -> int:
        return next(item for item in self._items() if item.name == "echo").effects()


class _OperationsCase:
    name = "operations"
    scenarios = (
        Scenario("execute", ExecuteRegisteredOperation, effectful=True),
        Scenario("inspect", InspectRequest, effectful=False),
        Scenario("cancel", CancelOwnedResource, effectful=True),
    )

    @asynccontextmanager
    async def world(self) -> AsyncIterator[_OperationsWorld]:
        with tempfile.TemporaryDirectory() as raw:
            yield _OperationsWorld(Path(raw))


# semantic events


class _SemanticWorld(_World):
    async def prepare(self, scenario: Scenario) -> RequestBase:
        del scenario
        return BlockIntent(
            request_id=RequestId(root="block"),
            scope=SCOPE,
            deadline_at=100.0,
            target=RequestId(root="target"),
            diagnostic="stuck",
        )

    async def execute(
        self,
        request: RequestBase,
        *,
        lease: RevocableLease,
        crash_at: int | None,
        digest: str | None = None,
    ) -> ExecutionResult:
        faulting = self.faulting(crash_at)
        events = JournalSemanticEvents(self.store(faulting), cast("StateNamespace", faulting))
        context = context_for(request, lease=lease)
        if digest is not None:
            context = context.model_copy(update={"payload_digest": digest})
        try:
            return await events.execute(cast("BlockIntent", request), context)
        finally:
            self._writes = faulting.writes

    def effects(self) -> int:
        real = self.real_namespace()
        return len(JournalSemanticEvents(ReceiptStore(real), real).read())


class _SemanticEventsCase:
    name = "semantic_events"
    scenarios = (Scenario("block", BlockIntent, effectful=True),)

    @asynccontextmanager
    async def world(self) -> AsyncIterator[_SemanticWorld]:
        with tempfile.TemporaryDirectory() as raw:
            yield _SemanticWorld(Path(raw))


# evaluation


class _EvaluationWorld(_World):
    def __init__(self, base: Path, stack: Stack) -> None:
        super().__init__(base)
        self.stack = stack

    async def prepare(self, scenario: Scenario) -> RequestBase:
        if scenario.kind is SubmitMeasurement:
            return submission("sub", candidate=self.stack.snapshot)
        seed = await self.execute(
            submission("seed", candidate=self.stack.snapshot),
            lease=RevocableLease(),
            crash_at=None,
        )
        resource = seed.observation.observation.resource_id
        assert resource is not None
        while resource.root not in self.stack.cluster.submissions:
            await self.stack.executor.wait_for_change(resource.root, 30.0)
        if scenario.kind is CloseAttemptScope:
            return CloseAttemptScope(
                request_id=RequestId(root="close"),
                scope=EVALUATION_SCOPE,
                admission_id=ADMISSION,
                deadline_at=100.0,
                attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
            )
        kind = scenario.kind
        assert kind in (ObserveOwnedJob, InspectOwnedJob, CollectEvidence, CancelOwnedJob)
        return kind(  # type: ignore[call-arg]  # the four job queries share one constructor shape
            request_id=RequestId(root=scenario.name),
            scope=EVALUATION_SCOPE,
            admission_id=ADMISSION,
            deadline_at=100.0,
            resource_id=resource,
        )

    async def execute(
        self,
        request: RequestBase,
        *,
        lease: RevocableLease,
        crash_at: int | None,
        digest: str | None = None,
    ) -> ExecutionResult:
        faulting = self.faulting(crash_at)
        requests = MeasurementRequests(self.stack.executor, self.store(faulting))
        context = context_for(request, lease=lease)
        if digest is not None:
            context = context.model_copy(update={"payload_digest": digest})
        try:
            return await requests.execute(cast("SubmitMeasurement", request), context)
        finally:
            self._writes = faulting.writes

    def effects(self) -> int:
        return len(self.stack.cluster.submissions)


class _EvaluationCase:
    name = "evaluation"
    scenarios = (
        Scenario("submit", SubmitMeasurement, effectful=True),
        Scenario("observe", ObserveOwnedJob, effectful=False),
        Scenario("inspect", InspectOwnedJob, effectful=False),
        Scenario("collect", CollectEvidence, effectful=False),
        Scenario("cancel", CancelOwnedJob, effectful=True),
        Scenario("close", CloseAttemptScope, effectful=True),
    )

    @asynccontextmanager
    async def world(self) -> AsyncIterator[_EvaluationWorld]:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            stack = await build_stack(base / "stack", ScenarioCluster())
            try:
                yield _EvaluationWorld(base, stack)
            finally:
                await stack.executor.close()


# workspaces


_ATTEMPT = AttemptRef(attempt_id=AttemptId(root="a1"), generation=0)


def _attempt_request(name: str) -> dict[str, Any]:
    return {
        "request_id": RequestId(root=name),
        "scope": Scope(owner=_ATTEMPT.attempt_id, generation=_ATTEMPT.generation),
        "admission_id": DecisionId(root="admit"),
        "deadline_at": 100.0,
        "attempt": _ATTEMPT,
    }


def _run_request(name: str) -> dict[str, Any]:
    return {
        "request_id": RequestId(root=name),
        "scope": Scope(owner=RunId(root="run-1"), generation=0),
        "deadline_at": 100.0,
    }


class _WorkspacesWorld:
    def __init__(self, env: WorkspaceEnv) -> None:
        self.env = env
        self._writes = 0

    @property
    def _root(self) -> Path:
        return self.env.hosts[0].root.path

    def _git(self, *args: str) -> str:
        result = run_git(list(args), cwd=self._root)
        assert result.returncode == 0, result.stderr
        return result.stdout.decode().strip()

    def _candidate(self) -> Path:
        paths = [Path(line.split()[0]) for line in self._git("worktree", "list").splitlines()]
        (candidate,) = [path for path in paths if path != self._root.resolve()]
        return candidate

    async def _seed(self, request: RequestBase) -> ExecutionResult:
        return await self.execute(request, lease=RevocableLease(), crash_at=None)

    async def prepare(self, scenario: Scenario) -> RequestBase:
        base = self.env.hosts[0].root.revision
        assert base is not None
        kind = scenario.kind
        if kind is SnapshotAndRetainRun:
            (self._root / "candidate.py").write_text("VALUE = 9\n", encoding="utf-8")
            return SnapshotAndRetainRun(
                request_id=RequestId(root="run-snap"),
                scope=Scope(owner=RunId(root="run-1"), generation=3),
                deadline_at=100.0,
                invocation=InvocationRef(
                    session_id=SessionId(root="s1"),
                    invocation_id=InvocationId(root="i1"),
                    generation=3,
                ),
                retention="candidate",
            )
        plan = WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=revision_ref(base))
        if kind is EnsureWorkspace:
            return EnsureWorkspace(**_attempt_request("ensure"), plan=plan)
        await self._seed(EnsureWorkspace(**_attempt_request("seed-ensure"), plan=plan))
        return await self._after_ensure(kind, plan.base)

    async def _after_ensure(self, kind: type[RequestBase], base: RevisionRef) -> RequestBase:
        if kind is RestoreRevision:
            return RestoreRevision(**_attempt_request("restore"), revision=base)
        if kind is RetainRevision:
            return RetainRevision(**_attempt_request("retain"), revision=base, retention="wip")
        if kind is DiscardWorkspace:
            return DiscardWorkspace(**_attempt_request("discard"))
        (self._candidate() / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        if kind is SnapshotAndRetain:
            return SnapshotAndRetain(**_attempt_request("snapshot"), retention="candidate")
        seeded = await self._seed(
            SnapshotAndRetain(**_attempt_request("seed-snap"), retention="candidate")
        )
        assert seeded.observation.revision is not None
        return await self._adoption(kind, seeded.observation.revision)

    async def _adoption(self, kind: type[RequestBase], winner: RevisionRef) -> RequestBase:
        selection = RetainedCandidate(settlement_id=SettlementId(root="settle"), revision=winner)
        if kind is AdoptRevision:
            return AdoptRevision(**_run_request("adopt"), selection=selection)
        assert kind is VerifyAdoption
        await self._seed(AdoptRevision(**_run_request("seed-adopt"), selection=selection))
        return VerifyAdoption(**_run_request("verify"), selection=selection)

    async def execute(
        self,
        request: RequestBase,
        *,
        lease: RevocableLease,
        crash_at: int | None,
        digest: str | None = None,
    ) -> ExecutionResult:
        faulting = FaultingNamespace(self.env.receipts_namespace(), crash_at)
        store = ReceiptStore(cast("StateNamespace", faulting))
        executor = RuntimeWorkspaceRequests(self.env.start_host(), store)
        context = context_for(request, lease=lease)
        if digest is not None:
            context = context.model_copy(update={"payload_digest": digest})
        try:
            outcome = await executor.execute(cast("Request", request), context)
        finally:
            self._writes = faulting.writes
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome

    def writes(self) -> int:
        return self._writes

    def effects(self) -> int:
        """Worktrees, commits and refs, plus one once the adopted content is in the root."""
        worktrees = len(self._git("worktree", "list").splitlines())
        commits = int(self._git("rev-list", "--all", "--count"))
        refs = len(self._git("for-each-ref", "--count=1000").splitlines())
        adopted = (self._root / "candidate.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        return worktrees + commits + refs + int(adopted)


class _WorkspacesCase:
    name = "workspaces"
    scenarios = (
        Scenario("ensure", EnsureWorkspace, effectful=True),
        Scenario("restore", RestoreRevision, effectful=True),
        Scenario("snapshot", SnapshotAndRetain, effectful=True),
        Scenario("retain", RetainRevision, effectful=True),
        Scenario("discard", DiscardWorkspace, effectful=True),
        Scenario("run-snapshot", SnapshotAndRetainRun, effectful=True),
        Scenario("adopt", AdoptRevision, effectful=True),
        Scenario("verify", VerifyAdoption, effectful=False),
    )

    @asynccontextmanager
    async def world(self) -> AsyncIterator[_WorkspacesWorld]:
        with (
            tempfile.TemporaryDirectory() as raw,
            _state_home(Path(raw) / "operator-state"),
            open_workspace_env(Path(raw)) as env,
        ):
            try:
                yield _WorkspacesWorld(env)
            finally:
                for host in reversed(env.hosts):
                    await host.close()


@contextmanager
def _state_home(path: Path) -> Iterator[None]:
    """Point the Project state home at *path* for one world (restored on exit)."""
    previous = os.environ.get("VIBESYS_STATE_HOME")
    os.environ["VIBESYS_STATE_HOME"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            del os.environ["VIBESYS_STATE_HOME"]
        else:
            os.environ["VIBESYS_STATE_HOME"] = previous


CASES = (_OperationsCase(), _SemanticEventsCase(), _EvaluationCase(), _WorkspacesCase())
