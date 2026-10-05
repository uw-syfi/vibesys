"""Production builders for a fresh core run: its initial state and its executor bindings.

Nothing else in production builds these pieces yet (``vs_core`` ships only a
capability-disabled test builder, and each executor is constructed by its own
tests). The launch that starts a core run calls these two functions, so the
composition test exercises the same path. Roles without an owner are left
unbound: the shell then returns a typed refusal naming the role, never a guess.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from vs_core.api import (
    Capabilities,
    CoreState,
    EvidenceRequirements,
    IntentsState,
    Limits,
    OperationRegistry,
    RecoveryBarrier,
    RecoveryPhase,
    RunFacts,
    RunId,
    RunState,
    StrategyDeclaration,
    validate_startup,
)
from vs_runtime._core_loop import CoreRuntimeBindings
from vs_runtime._core_requests import RequestExecutors
from vs_runtime._evaluation_requests import MeasurementRequests
from vs_runtime._observation_factory import ObservationFactory
from vs_runtime._operation_catalog import OperationCatalog
from vs_runtime._operation_receipts import NamespaceOperationReceipts
from vs_runtime._operation_requests import RegisteredOperationRequests
from vs_runtime._receipt_store import ReceiptStore
from vs_runtime._semantic_events import JournalSemanticEvents
from vs_runtime._session_lifecycle_requests import ReleasedRunInvocations, SessionRequestRouter
from vs_runtime._session_requests import JournalRunInvocations
from vs_runtime._session_resolver import session_executors
from vs_runtime._workspace_requests import RuntimeWorkspaceRequests

if TYPE_CHECKING:
    from vs_agent.api import ClientAgentSessions
    from vs_core.api import LifecycleCapability
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_project.api import StateNamespace
    from vs_runtime._session_requests import SessionResolver
    from vs_runtime._workspaces import RuntimeWorkspaces


@dataclasses.dataclass(frozen=True)
class SessionServices:
    """What the SESSIONS role runs on.

    ``agent_sessions`` is the durable provider-conversation client. ``resolver`` maps a
    turn's declared role, schema and prompts to concrete agent configuration.
    """

    agent_sessions: ClientAgentSessions
    resolver: SessionResolver


def empty_catalog() -> OperationCatalog:
    """The catalog of a host that registers no operations."""
    return OperationCatalog(OperationRegistry(), ())


@dataclasses.dataclass(frozen=True)
class CoreStartup:
    """The run-wide bounds a new core state starts with.

    ``deadline_at`` is on the run clock's timeline. ``lifecycle`` names the lifecycle
    capabilities (suspend, park, ...) the host's executors serve; none is offered unless
    the caller says so.
    """

    deadline_at: float
    limits: Limits = dataclasses.field(default_factory=Limits)
    lifecycle: frozenset[LifecycleCapability] = frozenset()
    requirements: EvidenceRequirements = dataclasses.field(default_factory=EvidenceRequirements)


def new_core_state(
    run_id: str,
    facts: RunFacts,
    declaration: StrategyDeclaration,
    *,
    offered: OperationCatalog,
    startup: CoreStartup,
) -> CoreState:
    """Build the state of a run that has not started, validated against its declaration.

    ``offered`` is the catalog the run's executors are bound from (``core_bindings``
    takes the same object), so the operations core is told it may use are exactly the
    ones with an owner; there is no default. ``startup`` carries the run's deadline and
    limits and the lifecycle capabilities the host's executors serve. A strategy that
    requires something not offered fails here, naming it, instead of mid-run.
    """
    selected = validate_startup(
        declaration,
        Capabilities(lifecycle=startup.lifecycle, operations=offered.offered_operations),
    )
    return CoreState(
        registry=selected.operations,
        intents=IntentsState(recovery=RecoveryBarrier(phase=RecoveryPhase.READY)),
        run=RunState(
            run_id=RunId(root=run_id),
            now_at=0.0,
            deadline_at=startup.deadline_at,
            facts=facts,
            capabilities=selected,
            limits=startup.limits,
            declaration=declaration,
            requirements=startup.requirements,
        ),
    )


def core_bindings(
    *,
    receipts: StateNamespace,
    workspaces: RuntimeWorkspaces,
    evaluation: PollingEvaluationExecutor,
    sessions: SessionServices,
    operations: OperationCatalog | None = None,
) -> CoreRuntimeBindings:
    """Bind every request role over one shared receipt store.

    The session role is ``session_executors``: EnsureSession, DispatchTurn and
    InspectTurn go to its ``turns`` executor, CancelTurn, CloseSession and
    ResumeSessionTurn to its ``lifecycle`` executor, both over one access settlement.
    The same agent sessions also prove, for the workspace executor, that a run turn's
    writer ended (settled, or interrupted and released) before the run is snapshotted.
    ``operations`` defaults to the empty catalog, which serves a strategy that declares
    no registered operations; pass the same catalog to ``new_core_state(offered=)``.
    """
    store = ReceiptStore(receipts)
    catalog = operations or empty_catalog()
    session = session_executors(sessions.agent_sessions, sessions.resolver, store)
    proof = ReleasedRunInvocations(
        JournalRunInvocations(sessions.agent_sessions, store), sessions.agent_sessions, store
    )
    executors = RequestExecutors(
        workspaces=RuntimeWorkspaceRequests(workspaces, store, proof),
        sessions=SessionRequestRouter(session.turns, session.lifecycle),
        evaluation=MeasurementRequests(evaluation, store),
        operations=RegisteredOperationRequests(
            catalog, NamespaceOperationReceipts(store), ObservationFactory(store)
        ),
        semantic_events=JournalSemanticEvents(store, receipts),
    )
    return CoreRuntimeBindings(registry=catalog.registry, executors=executors)
