"""Production builder for the executors of a core run: every request role over one receipt store.

Roles without an owner are left unbound: the shell then returns a typed refusal naming the
role, never a guess. ``core_executors`` is the one place the request roles are composed, so
the launch that starts a core run and the composition test exercise the same path.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from vs_core.api import OperationRegistry
from vs_runtime._core_requests import ExecutorRole, RefusingRequestExecution, RequestExecutors
from vs_runtime._evaluation_requests import MeasurementRequests
from vs_runtime._evidence_ledger import ReceiptEvidenceLedger
from vs_runtime._measurement_progress import (
    IgnoreMeasurement,
    MeasurementDecisions,
    MeasurementObserver,
    MeasurementProgress,
)
from vs_runtime._observation_factory import ObservationFactory
from vs_runtime._operation_catalog import OperationCatalog
from vs_runtime._receipt_store import ReceiptStore
from vs_runtime._session_lifecycle_requests import ReleasedRunInvocations, SessionRequestRouter
from vs_runtime._session_requests import JournalRunInvocations
from vs_runtime._session_resolver import SessionExecutors, session_executors
from vs_runtime._workspace_requests import RuntimeWorkspaceRequests

if TYPE_CHECKING:
    from vs_agent.api import ClientAgentSessions
    from vs_core.api import Continuation, DispatchTurn, SnapshotAndRetainRun
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
    yields: TurnYields[DispatchTurn, Continuation] | None = None
    """Turns that asked to wait; None when no tool lets an agent yield."""


class _NoRunInvocations:
    """Refuse a run snapshot that claims an invocation when sessions are not bound."""

    def unproven(self, request: SnapshotAndRetainRun) -> str:
        del request
        return "the run has no bound session executor, so no invocation can be proven terminal"


def empty_catalog() -> OperationCatalog:
    """The catalog of a host that registers no operations."""
    return OperationCatalog(OperationRegistry(), ())


@dataclasses.dataclass(frozen=True)
class CoreExecutors:
    """The executors of one run, over the one receipt store they share."""

    executors: RequestExecutors
    """One translator per request role."""
    sessions: SessionExecutors | None
    """The turn and lifecycle executors behind the SESSIONS role; ``None`` for a run with no roles."""
    store: ReceiptStore
    """The receipt store every executor of the run writes through."""
    observations: ObservationFactory
    """Builds the observation a runner reports for an operation or an adoption."""
    evidence: ReceiptEvidenceLedger
    """The run's evidence ledger, which an adoption reads."""
    progress: MeasurementProgress
    """Tells the run's observer each stage of a job that a step runner reported ended."""
    decisions: MeasurementDecisions
    """Which decision each submitted job answers, shared with the observer that names its spans."""


def core_executors(  # noqa: PLR0913  # lint-waiver: LW-260141 [PLR0913]; the run's stores and services plus the two vs_sim roles its executors run on.
    # > Bundling threads and blocking into one value would be a second ``Platform``, which
    # > only entrypoints and the server runtime take; folding them into ``SessionServices``
    # > would hide the workspace executor's runner inside the session bundle.
    *,
    receipts: StateNamespace,
    workspaces: RuntimeWorkspaces,
    measurement: MeasurementServices,
    sessions: SessionServices | None = None,
    threads: Threads,
    blocking: BlockingRunner,
) -> CoreExecutors:
    """Bind every request role over one shared receipt store.

    When ``sessions`` is supplied, the session role is ``session_executors``:
    EnsureSession, DispatchTurn and InspectTurn go to its ``turns`` executor,
    CancelTurn, CloseSession and ResumeSessionTurn to its ``lifecycle`` executor,
    both over one access settlement. The same agent sessions also prove, for the
    workspace executor, that a run turn's writer ended (settled, or interrupted and
    released) before the run is snapshotted. When ``sessions`` is absent, session
    requests use the runtime's explicit unbound-role refusal and invocation snapshots
    have no terminal-writer proof. This is the production composition for a policy
    that declares no agent roles.
    ``threads`` locks the receipt store; ``blocking`` runs the executors' synchronous
    journal, provider and disk calls off the event loop.
    """
    store = ReceiptStore(receipts, threads)
    if sessions is None:
        session = None
        session_requests = RefusingRequestExecution(ExecutorRole.SESSIONS)
        proof = _NoRunInvocations()
    else:
        session = session_executors(
            sessions.agent_sessions, sessions.resolver, store, sessions.yields, blocking
        )
        session_requests = SessionRequestRouter(session.turns, session.lifecycle)
        proof = ReleasedRunInvocations(
            JournalRunInvocations(sessions.agent_sessions, store), sessions.agent_sessions, store
        )
    observations = ObservationFactory(store)
    evidence = ReceiptEvidenceLedger(store)
    executors = RequestExecutors(
        workspaces=RuntimeWorkspaceRequests(workspaces, store, proof, blocking, evidence),
        sessions=session_requests,
        evaluation=MeasurementRequests(
            measurement.executor,
            store,
            measurement.observer,
            measurement.decisions,
            control=measurement.control,
        ),
    )
    return CoreExecutors(
        executors,
        session,
        store,
        observations,
        evidence,
        MeasurementProgress(measurement.observer),
        measurement.decisions,
    )
