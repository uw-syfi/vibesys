"""The run's non-candidate identities depend only on their own resolved input."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vibesys.run.evaluation_backend import semantic_evaluation_identity
from vs_runtime.api import RunFacts
from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView, TrustedEvaluationPlan

_plans = st.builds(
    TrustedEvaluationPlan,
    accuracy_command=st.none() | st.text(min_size=1, max_size=8),
    accuracy_timeout_seconds=st.none() | st.integers(min_value=1, max_value=999),
    benchmark_command=st.none() | st.text(min_size=1, max_size=8),
    benchmark_timeout_seconds=st.none() | st.integers(min_value=1, max_value=999),
)
_facts = st.builds(
    RunFacts,
    domain_id=st.text(min_size=1, max_size=8),
    objective=st.text(min_size=1, max_size=16),
    accuracy_configured=st.booleans(),
    benchmark_configured=st.booleans(),
)
_environments = st.builds(
    RunEnvironmentView,
    paths=st.just(AgentPaths()),
    prompt_notes=st.text(max_size=16),
    supports_parallel_candidate_evaluation=st.booleans(),
)


_inputs = st.tuples(_plans, _facts, _environments)


@given(_inputs, _inputs)
def test_each_identity_follows_only_its_own_input(
    left: tuple[TrustedEvaluationPlan, RunFacts, RunEnvironmentView],
    right: tuple[TrustedEvaluationPlan, RunFacts, RunEnvironmentView],
) -> None:
    first = semantic_evaluation_identity(*left)
    second = semantic_evaluation_identity(*right)

    assert (first.evaluator == second.evaluator) == (left[0] == right[0])
    assert (first.workload == second.workload) == (left[1] == right[1])
    assert (first.environment == second.environment) == (left[2] == right[2])
    assert semantic_evaluation_identity(*left) == first
