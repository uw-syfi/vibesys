"""Pure, deterministic fixtures for shared kernel and wave-1 traces."""

from vs_core.types.common import RevisionId, RevisionRef, RunFacts, RunId, SchemaRef, StrategyId
from vs_core.types.intents import IntentsState, RecoveryBarrier, RecoveryPhase
from vs_core.types.kernel import CoreState, RunState
from vs_core.types.strategy import StrategyDeclaration


def initial_state() -> CoreState:
    """Build a capability-disabled run; all identities and times are supplied."""
    return CoreState(
        intents=IntentsState(recovery=RecoveryBarrier(phase=RecoveryPhase.READY)),
        run=RunState(
            run_id=RunId(root="run"),
            now_at=0.0,
            deadline_at=1000.0,
            facts=RunFacts(
                objective="two opaque items",
                baseline=RevisionRef(revision_id=RevisionId(root="base"), digest="base-digest"),
                evaluator_digest="evaluator",
                workload_digest="workload",
                environment_digest="environment",
            ),
            declaration=StrategyDeclaration(
                strategy_id=StrategyId(root="sequential"),
                state_schema=SchemaRef(name="sequential", version=1),
            ),
        ),
    )
