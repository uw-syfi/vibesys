"""Pure, deterministic fixtures for shared kernel and wave-1 traces."""

from vs_core.types.common import (
    Limits,
    RequestRetries,
    RevisionId,
    RevisionRef,
    RunFacts,
    RunId,
    StrategyId,
)
from vs_core.types.kernel import CoreState, RunState
from vs_core.types.records import RecordsState, RecoveryBarrier, RecoveryPhase
from vs_core.types.strategy import SearchDeclaration


def sample_retries(limit: int) -> RequestRetries:
    """A request-retry declaration for a test: ``limit`` re-sends, short waits."""
    return RequestRetries(limit=limit, backoff=1.0, backoff_cap=30.0)


def sample_limits(**overrides: object) -> Limits:
    """Limits for a test, every field spelled out; pass the fields the test is about."""
    values: dict[str, object] = {
        "max_attempts": 1,
        "max_turns": 1,
        "max_parallel": 1,
        "pool_capacities": (),
        "max_refunds": 0,
        "max_adoption_rounds": 1,
        "request_retries": sample_retries(0),
        "cancellation_bound": 60.0,
        "cleanup_bound": 60.0,
        "reconciliation_bound": 60.0,
        "observe_interval": 10.0,
        "observe_backoff_cap": 120.0,
        "queue_wait_budget": 43_200.0,
        "queue_notice_interval": 600.0,
    }
    return Limits.model_validate({**values, **overrides})


def initial_state() -> CoreState:
    """Build a capability-disabled run; all identities and times are supplied."""
    return CoreState(
        records=RecordsState(hosts=(), recovery=RecoveryBarrier(phase=RecoveryPhase.READY)),
        run=RunState(
            limits=sample_limits(),
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
            declaration=SearchDeclaration(strategy_id=StrategyId(root="sequential")),
        ),
    )
