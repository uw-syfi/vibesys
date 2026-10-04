"""Pure, deterministic fixtures for shared kernel and wave-1 traces."""

from vs_core.types.common import RevisionId, RevisionRef, RunFacts, RunId, SchemaRef, StrategyId
from vs_core.types.kernel import CoreState, RunState
from vs_core.types.strategy import StrategyDeclaration


def initial_state() -> CoreState:
    """Build a capability-disabled run; all identities and times are supplied."""
    return CoreState(
        run=RunState(
            run_id=RunId("run"),
            now_at=0.0,
            deadline_at=1000.0,
            facts=RunFacts(
                objective="two opaque items",
                baseline=RevisionRef(revision_id=RevisionId("base"), digest="base-digest"),
                evaluator_digest="evaluator",
                workload_digest="workload",
                environment_digest="environment",
            ),
            declaration=StrategyDeclaration(
                strategy_id=StrategyId("sequential"),
                state_schema=SchemaRef(name="sequential", version=1),
            ),
        )
    )
