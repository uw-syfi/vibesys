"""The registered executor cases: one per receipt-backed role, over real owners and disk."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

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

from vs_core.api import (
    AttemptId,
    AttemptRef,
    BlockIntent,
    CancelOwnedJob,
    CancelOwnedResource,
    CloseAttemptScope,
    CollectEvidence,
    ExecuteRegisteredOperation,
    InspectOwnedJob,
    InspectRequest,
    ObserveOwnedJob,
    RequestId,
    ResourceId,
    SubmitMeasurement,
)
from vs_project.api import Project
from vs_runtime.api.core import (
    JournalSemanticEvents,
    MeasurementRequests,
    NamespaceOperationReceipts,
    ObservationFactory,
    ReceiptStore,
    RegisteredOperationRequests,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_core.api import RequestBase
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import ExecutionResult


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


CASES = (_OperationsCase(), _SemanticEventsCase(), _EvaluationCase())
