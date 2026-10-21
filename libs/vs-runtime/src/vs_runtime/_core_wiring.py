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
    Limits,
    OperationRegistry,
    RecordsState,
    RecoveryBarrier,
    RecoveryPhase,
    RunFacts,
    RunId,
    RunState,
    SearchDeclaration,
    validate_startup,
)
from vs_runtime._core_loop import CoreRuntimeBindings
from vs_runtime._core_requests import RequestExecutors
from vs_runtime._evaluation_requests import MeasurementRequests
from vs_runtime._evidence_ledger import ReceiptEvidenceLedger
from vs_runtime._measurement_progress import (
    IgnoreMeasurement,
    MeasurementDecisions,
    MeasurementObserver,
)
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
    from vs_runtime._run_control import RunControlChannel
    from vs_runtime._session_requests import SessionResolver, TurnYields
    from vs_runtime._workspaces import RuntimeWorkspaces
    from vs_sim.api import BlockingRunner, Threads


@dataclasses.dataclass(frozen=True)
class MeasurementServices:
    """What the EVALUATION role runs on: the executor and a display-only progress listener."""

    executor: PollingEvaluationExecutor
    observer: MeasurementObserver = dataclasses.field(default_factory=IgnoreMeasurement)
    """Hears each measurement stage start and end; it never changes a result."""
    decisions: MeasurementDecisions = dataclasses.field(default_factory=MeasurementDecisions)
    """Learns which decision each measurement answers; share it with the stage output opener."""
    control: RunControlChannel | None = None


@dataclasses.dataclass(frozen=True)
class SessionServices:
    """What the SESSIONS role runs on.

    ``agent_sessions`` is the durable provider-conversation client. ``resolver`` maps a
    turn's declared role, schema and prompts to concrete agent configuration.
    """

    agent_sessions: ClientAgentSessions
    resolver: SessionResolver
    yields: TurnYields | None = None
    """Turns that asked to wait; None when no tool lets an agent yield."""


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
    limits: Limits
    duration_seconds: float | None = None
    lifecycle: frozenset[LifecycleCapability] = frozenset()
    requirements: EvidenceRequirements = dataclasses.field(default_factory=EvidenceRequirements)


def new_core_state(
    run_id: str,
    facts: RunFacts,
    declaration: SearchDeclaration,
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
        records=RecordsState(hosts=(), recovery=RecoveryBarrier(phase=RecoveryPhase.READY)),
        run=RunState(
            run_id=RunId(root=run_id),
            now_at=0.0,
            deadline_at=startup.deadline_at,
            duration_seconds=startup.duration_seconds,
            facts=facts,
            capabilities=selected,
            limits=startup.limits,
            declaration=declaration,
            requirements=startup.requirements,
        ),
    )


def core_bindings(  # noqa: PLR0913  # lint-waiver: LW-260141 [PLR0913]; the run's stores and services plus the two vs_sim roles its executors run on.
    # > Bundling threads and blocking into one value would be a second ``Platform``, which
    # > only entrypoints and the server runtime take; folding them into ``SessionServices``
    # > would hide the workspace executor's runner inside the session bundle.
    *,
    receipts: StateNamespace,
    workspaces: RuntimeWorkspaces,
    measurement: MeasurementServices,
    sessions: SessionServices,
    threads: Threads,
    blocking: BlockingRunner,
    operations: OperationCatalog | None = None,
    allow_adoption: bool = True,
) -> CoreRuntimeBindings:
    """Bind every request role over one shared receipt store.

    The session role is ``session_executors``: EnsureSession, DispatchTurn and
    InspectTurn go to its ``turns`` executor, CancelTurn, CloseSession and
    ResumeSessionTurn to its ``lifecycle`` executor, both over one access settlement.
    The same agent sessions also prove, for the workspace executor, that a run turn's
    writer ended (settled, or interrupted and released) before the run is snapshotted.
    ``operations`` defaults to the empty catalog, which serves a strategy that declares
    no registered operations; pass the same catalog to ``new_core_state(offered=)``.
    ``allow_adoption=False`` protects the operator root after an explicit Resume; ordinary
    writer recovery keeps publication authority.
    ``threads`` locks the receipt store; ``blocking`` runs the executors' synchronous
    journal, provider and disk calls off the event loop.
    """
    store = ReceiptStore(receipts, threads)
    catalog = operations or empty_catalog()
    session = session_executors(
        sessions.agent_sessions,
        sessions.resolver,
        store,
        sessions.yields,
        blocking,
    )
    proof = ReleasedRunInvocations(
        JournalRunInvocations(sessions.agent_sessions, store), sessions.agent_sessions, store
    )
    workspace_requests = RuntimeWorkspaceRequests(
        workspaces, store, proof, blocking, ReceiptEvidenceLedger(store)
    )
    if not allow_adoption:
        workspace_requests.protect_adoption()
    executors = RequestExecutors(
        workspaces=workspace_requests,
        sessions=SessionRequestRouter(session.turns, session.lifecycle),
        evaluation=MeasurementRequests(
            measurement.executor,
            store,
            measurement.observer,
            measurement.decisions,
            control=measurement.control,
        ),
        operations=RegisteredOperationRequests(
            catalog, NamespaceOperationReceipts(store), ObservationFactory(store)
        ),
        semantic_events=JournalSemanticEvents(store, receipts),
    )
    return CoreRuntimeBindings(
        registry=catalog.registry, executors=executors, steering=session.turns
    )
