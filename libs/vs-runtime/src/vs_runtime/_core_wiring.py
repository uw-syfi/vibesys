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
from vs_runtime._workspace_requests import RuntimeWorkspaceRequests

if TYPE_CHECKING:
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_project.api import StateNamespace
    from vs_runtime._core_requests import SessionRequests
    from vs_runtime._workspaces import RuntimeWorkspaces


def new_core_state(
    run_id: str,
    facts: RunFacts,
    declaration: StrategyDeclaration,
    *,
    deadline_at: float,
    limits: Limits | None = None,
) -> CoreState:
    """Build the state of a run that has not started, validated against its declaration.

    The host offers no lifecycle capabilities or registered operations yet, so a
    strategy that requires one fails here, by name, instead of mid-run.
    """
    selected = validate_startup(declaration, Capabilities())
    return CoreState(
        registry=selected.operations,
        intents=IntentsState(recovery=RecoveryBarrier(phase=RecoveryPhase.READY)),
        run=RunState(
            run_id=RunId(root=run_id),
            now_at=0.0,
            deadline_at=deadline_at,
            facts=facts,
            capabilities=selected,
            limits=limits or Limits(),
            declaration=declaration,
        ),
    )


def core_bindings(
    *,
    receipts: StateNamespace,
    workspaces: RuntimeWorkspaces,
    evaluation: PollingEvaluationExecutor,
    operations: OperationCatalog | None = None,
    sessions: SessionRequests | None = None,
) -> CoreRuntimeBindings:
    """Bind every request role that has an owner, over one shared receipt store.

    ``sessions`` has no production implementation yet; leaving it ``None`` keeps
    the role refusing by name. ``operations`` defaults to the empty catalog, which
    serves a strategy that declares no registered operations.
    """
    store = ReceiptStore(receipts)
    catalog = operations or OperationCatalog(OperationRegistry(), ())
    executors = RequestExecutors(
        workspaces=RuntimeWorkspaceRequests(workspaces, store),
        evaluation=MeasurementRequests(evaluation, store),
        operations=RegisteredOperationRequests(
            catalog, NamespaceOperationReceipts(store), ObservationFactory(store)
        ),
        semantic_events=JournalSemanticEvents(store, receipts),
    )
    if sessions is not None:
        executors = dataclasses.replace(executors, sessions=sessions)
    return CoreRuntimeBindings(registry=catalog.registry, executors=executors)
