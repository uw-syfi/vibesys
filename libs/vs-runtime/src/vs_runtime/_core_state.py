"""The state a core run starts from: its run-wide bounds and the state built from them."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from vs_core.api import (
    Capabilities,
    CoreState,
    EvidenceRequirements,
    Limits,
    RecordsState,
    RunFacts,
    RunId,
    RunState,
    SearchDeclaration,
    validate_startup,
)

if TYPE_CHECKING:
    from vs_core.api import LifecycleCapability
    from vs_runtime._operation_catalog import OperationCatalog


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

    ``offered`` is the catalog the run's executors are bound from (``core_executors``
    takes the same object as ``operations``), so the operations core is told it may use are exactly the
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
        records=RecordsState(),
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
