"""The policy built from a real run request is what core startup and the owners accept."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    COMMIT,
    bounds_of,
    options_of,
    ports,
    resolved,
    run_request,
)
from tests.vibesys.orchestration.dynamic.strategy._run import config as _config

from vibesys.dynamic_core import (
    ResolvedRun,
    dynamic_operation_catalog,
    resolve_core_policy,
)
from vibesys.orchestration.dynamic import REGISTRATION, DynamicOptions
from vibesys.orchestration.dynamic.core_policy.api import (
    UNBOUNDED_DEADLINE_AT,
    RunBounds,
    run_deadline_at,
)
from vibesys.orchestration.dynamic.strategy.api import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PLANNER_REPLY,
    PROFILER_REPLY,
    DynamicStrategy,
)
from vibesys.run.evaluation_backend import semantic_evaluation_identity
from vs_core.api import Capabilities, RevisionRef, RunFacts, RunId, RunState, validate_startup
from vs_runtime.api.infrastructure import TrustedEvaluationPlan

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

    from vibesys.orchestration.dynamic.core_policy.api import DynamicCorePolicy
    from vs_core.api import OperationDescriptor, SchemaRef


_PACING = {
    "queue_allowance_seconds": 900,
    "observe_interval_seconds": 10,
    "observe_backoff_cap_seconds": 120,
}


def _policy(
    root: Path,
    options: dict[str, JsonValue] | None = None,
    config: dict[str, object] | None = None,
    *,
    profiler_id: str = "none",
    plan: TrustedEvaluationPlan | None = None,
) -> tuple[DynamicCorePolicy, ResolvedRun]:
    request = run_request(root, options, config)
    run = resolved(root, profiler_id=profiler_id, plan=plan)
    parsed = options_of(request)
    assert isinstance(parsed, DynamicOptions)
    return resolve_core_policy(parsed, bounds_of(request), run), run


def _core_run_state(policy: DynamicCorePolicy, offered: Capabilities) -> RunState:
    """The run state a shell builds from the policy: what core accepts at startup."""
    return RunState(
        run_id=RunId(root="policy"),
        now_at=0.0,
        deadline_at=policy.deadline_at,
        facts=policy.facts,
        capabilities=validate_startup(policy.strategy.declaration, offered),
        limits=policy.limits,
        declaration=policy.strategy.declaration,
    )


def test_policy_from_a_real_request_starts_on_the_production_catalog(tmp_path: Path) -> None:
    policy, run = _policy(tmp_path, {"max_rounds": 3, "max_in_flight": 2})
    catalog = dynamic_operation_catalog(ports(tmp_path, run))
    declaration = policy.strategy.declaration

    catalog.require_owned(declaration)
    state = _core_run_state(policy, Capabilities(operations=catalog.offered_operations))

    assert {item.kind for item in declaration.required_operations} <= {
        item.kind for item in state.capabilities.operations
    }
    assert state.facts.baseline == RevisionRef.of_git_commit(COMMIT)
    assert state.limits.max_attempts == 6
    assert state.limits.max_parallel == 2


@settings(max_examples=25, deadline=None)
@given(
    rounds=st.integers(1, 6),
    in_flight=st.integers(1, 4),
    retries=st.integers(1, 3),
    judge_every=st.integers(1, 3),
)
def test_limits_follow_the_requested_budgets(
    tmp_path_factory: pytest.TempPathFactory,
    rounds: int,
    in_flight: int,
    retries: int,
    judge_every: int,
) -> None:
    root = tmp_path_factory.mktemp("limits")
    options: dict[str, JsonValue] = {
        "max_rounds": rounds,
        "max_in_flight": in_flight,
        "max_retries_per_round": retries,
        "judge_every": judge_every,
    }
    policy, _ = _policy(root, options)

    assert policy.limits.max_attempts == rounds * in_flight == policy.strategy.config.start_budget
    assert policy.limits.max_parallel == in_flight
    assert policy.limits.max_turns >= policy.limits.max_attempts
    _core_run_state(policy, Capabilities(operations=_offered(root)))


def _offered(root: Path) -> tuple[OperationDescriptor, ...]:
    run = resolved(root)
    return dynamic_operation_catalog(ports(root, run)).offered_operations


def test_run_facts_use_the_evaluation_identity_the_backend_uses(tmp_path: Path) -> None:
    policy, run = _policy(tmp_path)
    identity = semantic_evaluation_identity(run.evaluation_plan, run.facts, run.environment)

    assert policy.facts.objective == run.facts.objective
    assert policy.facts.evaluator_digest == identity.evaluator.value
    assert policy.facts.workload_digest == identity.workload.value
    assert policy.facts.environment_digest == identity.environment.value


def test_the_recipe_is_the_stored_evaluation_plan(tmp_path: Path) -> None:
    policy, run = _policy(tmp_path)
    recipe = policy.strategy.config.recipe

    assert run.artifacts.contains(run.evaluation_plan.model_dump_json().encode())
    assert recipe.digest in {path.name for path in tmp_path.rglob("*") if path.is_file()}


def test_capabilities_of_the_run_override_the_config_defaults(tmp_path: Path) -> None:
    plan = TrustedEvaluationPlan(
        accuracy_command="true",
        accuracy_timeout_seconds=11,
        profile_command="profile",
        profile_timeout_seconds=13,
    )
    policy, _ = _policy(
        tmp_path,
        config={"evaluation": {"queue_allowance_seconds": 77}},
        profiler_id="nsys",
        plan=plan,
    )
    config = policy.strategy.config

    assert config.profiling
    assert config.profile_measurement
    assert config.queue_allowance_seconds == 77
    assert config.accuracy_seconds == 11
    assert config.profile_seconds == 13
    assert config.benchmark_seconds == 3600.0  # the plan declares none: the strategy default
    assert policy.limits.queue_allowance == 77


def test_no_profiler_means_no_profiling(tmp_path: Path) -> None:
    config = _policy(tmp_path)[0].strategy.config

    assert not config.profiling
    assert not config.profile_measurement


def test_an_unset_run_budget_is_an_unbounded_deadline(tmp_path: Path) -> None:
    assert _policy(tmp_path)[0].deadline_at == UNBOUNDED_DEADLINE_AT


@given(seconds=st.none() | st.integers(1, 10**9))
def test_core_accepts_the_deadline_and_the_state_round_trips(seconds: int | None) -> None:
    deadline = run_deadline_at(RunBounds(**_PACING, max_run_seconds=seconds).max_run_seconds)
    facts = RunFacts(
        objective="o",
        baseline=RevisionRef.of_git_commit(COMMIT),
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="v",
    )
    declaration = DynamicStrategy(config=_config()).declaration
    state = RunState(
        run_id=RunId(root="r"),
        now_at=0.0,
        deadline_at=deadline,
        facts=facts,
        declaration=declaration,
    )

    assert deadline == (UNBOUNDED_DEADLINE_AT if seconds is None else float(seconds))
    assert RunState.model_validate_json(state.model_dump_json()) == state


def test_the_deadline_comes_from_the_config_field(tmp_path: Path) -> None:
    policy, _ = _policy(tmp_path, config={"run": {"max_run_seconds": 3600}})

    assert policy.deadline_at == 3600.0


@pytest.mark.parametrize("seconds", [0, -5, True, 1.5, "60"])
def test_a_bad_deadline_is_rejected_by_name(seconds: object) -> None:
    with pytest.raises(ValidationError, match="max_run_seconds"):
        RunBounds.model_validate({**_PACING, "max_run_seconds": seconds})


def test_unknown_bounds_are_rejected_by_name() -> None:
    with pytest.raises(ValidationError, match="max_run_second"):
        RunBounds.model_validate({**_PACING, "max_run_second": 60})


def test_unknown_dynamic_options_are_rejected_by_name(tmp_path: Path) -> None:
    request = run_request(tmp_path, {"max_roundz": 3})

    with pytest.raises(ValidationError, match="max_roundz"):
        REGISTRATION.parse_options(request.orchestration)


def test_every_reply_schema_the_strategy_asks_for_has_a_reply_type(tmp_path: Path) -> None:
    policy, _ = _policy(tmp_path)

    assert set(policy.reply_schemas) == {
        PLANNER_REPLY,
        IMPLEMENTER_REPLY,
        PROFILER_REPLY,
        JUDGE_REPLY,
    }
    assert all(issubclass(model, BaseModel) for model in policy.reply_schemas.values())


@pytest.mark.parametrize("ref", [IMPLEMENTER_REPLY, PROFILER_REPLY, JUDGE_REPLY])
def test_a_wait_reply_validates_and_an_unknown_field_does_not(
    tmp_path: Path, ref: SchemaRef
) -> None:
    model = _policy(tmp_path)[0].reply_schemas[ref]

    assert model.model_validate_json('{"kind":"waiting_for_evaluation","handles":["h1"]}')
    with pytest.raises(ValidationError):
        model.model_validate_json('{"kind":"waiting_for_evaluation","handles":["h1"],"extra":1}')


@settings(max_examples=25, deadline=None)
@given(interval=st.integers(1, 600), extra=st.integers(0, 600))
def test_limits_carry_the_configured_observe_pacing(
    tmp_path_factory: pytest.TempPathFactory, interval: int, extra: int
) -> None:
    root = tmp_path_factory.mktemp("pacing")
    config = {
        "evaluation": {
            "observe_interval_seconds": interval,
            "observe_backoff_cap_seconds": interval + extra,
        }
    }
    policy, _ = _policy(root, config=config)

    assert policy.limits.observe_interval == interval
    assert policy.limits.observe_backoff_cap == interval + extra


def test_default_observe_pacing_comes_from_the_config_not_the_core(tmp_path: Path) -> None:
    policy, _ = _policy(tmp_path)

    assert (policy.limits.observe_interval, policy.limits.observe_backoff_cap) == (10.0, 120.0)
