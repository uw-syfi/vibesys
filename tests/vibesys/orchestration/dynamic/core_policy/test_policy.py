"""The plan the dynamic policy makes from a real run's resolved inputs is what core accepts.

The host resolves a run (its facts, evaluation plan and environment) and hands the policy a
`CoreRunContext`; the policy answers with the search, its config, the task's terms, the run's
limits and the evidence the judge vouches for. These tests plan runs from generated launch
options and generated evaluation plans and check what the plan says about each.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    COMMIT,
    EVALUATION_CAPACITY,
    OPTIONS,
    candidate_environment,
    handoff,
    launch_of,
    plan_of,
    ports,
    resolved,
)

from vibesys.candidates.api import UNBOUNDED_DEADLINE_AT, Gates, Measuring, terms_of
from vibesys.core_wiring import ResolvedRun
from vibesys.dynamic_core import dynamic_operation_catalog
from vibesys.errors import ConfigurationError
from vibesys.metrics import MetricSpace, Objective
from vibesys.orchestration.dynamic.core_policy.api import requirements_for
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vibesys.orchestration.dynamic.search.api import JUDGE, DynamicConfig, dynamic
from vibesys.run.evaluation_identity import semantic_evaluation_identity
from vs_core.api import ArtifactId, ArtifactRef, AssessmentKind, RevisionRef
from vs_runtime.api.core import CoreStartup, new_core_state
from vs_runtime.api.infrastructure import (
    ClusterEvaluationFacts,
    RunEnvironmentView,
    TrustedEvaluationPlan,
)
from vs_sandbox.api.slurm import (
    SlurmEvaluationPlan,
    load_slurm_operator_settings,
    trusted_profile_command,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import CoreState
    from vs_runtime.api import CorePlan

_SETTINGS = settings(
    max_examples=examples(pr=10, nightly=25),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
_SEARCH_KEYS = {
    "max_rounds": st.integers(1, 6),
    "max_in_flight": st.integers(1, 4),
    "judge_every": st.integers(1, 3),
    "max_unmeasured_turns": st.integers(1, 4),
    "max_continuation_rounds": st.integers(0, 4),
}
_LAUNCHES = st.fixed_dictionaries({}, optional=_SEARCH_KEYS)
_WIDEST = 32
"""The most trusted evaluations a config may hold at once (its ``evaluation_slots`` bound)."""
_RECIPE = ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe")


def _config(plan: CorePlan) -> DynamicConfig:
    assert isinstance(plan.config, DynamicConfig)
    return plan.config


def _started(plan: CorePlan, root: Path, run: ResolvedRun) -> CoreState:
    """The state a shell builds from the plan on the production catalog: what core accepts."""
    catalog = dynamic_operation_catalog(ports(root, run), handoff())
    catalog.require_owned(plan.search.declaration)
    return new_core_state(
        "policy",
        plan.facts,
        plan.search.declaration,
        offered=catalog,
        startup=CoreStartup(
            deadline_at=plan.deadline_seconds,
            limits=plan.limits,
            lifecycle=frozenset(),
            requirements=plan.requirements,
        ),
    )


def test_a_plan_from_a_real_run_starts_on_the_production_catalog(tmp_path: Path) -> None:
    run = resolved(tmp_path)
    plan = plan_of(run, {"max_rounds": 3, "max_in_flight": 2})

    state = _started(plan, tmp_path, run)

    assert state.run.facts.baseline == RevisionRef.of_git_commit(COMMIT)
    assert plan.search.name == dynamic.name == "dynamic"


@_SETTINGS
@given(options=_LAUNCHES)
def test_the_limits_admit_exactly_the_attempts_the_config_schedules(
    tmp_path_factory: pytest.TempPathFactory, options: dict[str, int]
) -> None:
    root = tmp_path_factory.mktemp("limits")
    run = resolved(root)
    plan = plan_of(run, options)
    config = _config(plan)

    assert config == launch_of(options).search_config(
        profiling=False, evaluation_capacity=EVALUATION_CAPACITY
    )
    assert plan.limits.max_attempts == config.max_rounds * config.max_in_flight
    # agents work in max_in_flight attempts; the parked ones beside them stay admitted
    parked = min(config.backlog, plan.limits.max_attempts - config.max_in_flight)
    assert plan.limits.max_parallel == config.max_in_flight + parked
    assert plan.limits.max_turns >= plan.limits.max_attempts
    _started(plan, root, run)


@settings(max_examples=examples(pr=8, nightly=100))
@given(accuracy=st.booleans(), benchmark=st.booleans())
def test_the_judge_may_vouch_and_each_measured_gate_is_required(
    *, accuracy: bool, benchmark: bool
) -> None:
    """Core grants no role implicit authority, so an undeclared judge makes no candidate eligible."""
    terms = terms_of(
        _RECIPE, Gates(accuracy=accuracy, benchmark=benchmark), MetricSpace(), Measuring({}, 1.0)
    )

    requirements = requirements_for(terms)

    assert [
        (item.kind, item.role_id.root, item.output_schema)
        for item in requirements.assessment_authorities
    ] == [
        (AssessmentKind.LOCAL_VALIDATION, dynamic.ids.role_id(JUDGE.name).root, JUDGE.reply_schema)
    ]
    assert set(requirements.required_assessments) == {
        *((AssessmentKind.CORRECTNESS,) if accuracy else ()),
        *((AssessmentKind.BENCHMARK,) if benchmark else ()),
    }


@_SETTINGS
@given(accuracy=st.booleans(), benchmark=st.booleans())
def test_the_plan_requires_the_gates_the_task_configures(
    tmp_path_factory: pytest.TempPathFactory, *, accuracy: bool, benchmark: bool
) -> None:
    root = tmp_path_factory.mktemp("gates")
    run = resolved(root)
    facts = run.facts.model_copy(
        update={"accuracy_configured": accuracy, "benchmark_configured": benchmark}
    )

    plan = plan_of(
        ResolvedRun(
            facts=facts,
            evaluation_plan=run.evaluation_plan,
            environment=run.environment,
            baseline_commit=run.baseline_commit,
            artifacts=run.artifacts,
        )
    )

    assert plan.facts.task is not None
    assert set(plan.facts.task.measured) == {
        *(("accuracy",) if accuracy else ()),
        *(("benchmark",) if benchmark else ()),
    }
    assert plan.requirements == requirements_for(plan.facts.task)


def test_run_facts_use_the_evaluation_identity_the_backend_uses(tmp_path: Path) -> None:
    run = resolved(tmp_path)
    plan = plan_of(run)
    identity = semantic_evaluation_identity(run.evaluation_plan, run.facts, run.environment)

    assert plan.facts.objective == run.facts.objective
    assert plan.facts.evaluator_digest == identity.evaluator.value
    assert plan.facts.workload_digest == identity.workload.value
    assert plan.facts.environment_digest == identity.environment.value


def test_the_recipe_is_the_stored_evaluation_plan(tmp_path: Path) -> None:
    run = resolved(tmp_path)
    plan = plan_of(run)

    assert plan.facts.task is not None
    assert run.artifacts.contains(run.evaluation_plan.model_dump_json().encode())
    assert plan.facts.task.recipe.digest in {
        path.name for path in tmp_path.rglob("*") if path.is_file()
    }


_SECONDS = st.none() | st.integers(1, 10**5)


@_SETTINGS
@given(
    accuracy=_SECONDS,
    benchmark=_SECONDS,
    profile=_SECONDS,
    queue=st.integers(0, 10**4),
)
def test_the_stage_limits_come_from_the_evaluation_plan_and_the_queue_from_the_run(
    tmp_path_factory: pytest.TempPathFactory,
    accuracy: int | None,
    benchmark: int | None,
    profile: int | None,
    queue: int,
) -> None:
    root = tmp_path_factory.mktemp("stages")
    plan = TrustedEvaluationPlan(
        accuracy_command="true",
        accuracy_timeout_seconds=accuracy,
        benchmark_command="python benchmark.py",
        benchmark_timeout_seconds=benchmark,
        profile_command="profile" if profile else None,
        profile_timeout_seconds=profile,
    )

    terms = plan_of(resolved(root, plan=plan), queue_allowance_seconds=queue).facts.task

    assert terms is not None
    declared = {item.stage_id: item.execution_seconds for item in terms.stages}
    assert (declared["accuracy"] == accuracy) if accuracy else declared["accuracy"] > 0
    assert (declared["benchmark"] == benchmark) if benchmark else declared["benchmark"] > 0
    assert (declared["profile"] == profile) if profile else declared["profile"] > 0
    assert terms.queue_allowance == queue


@_SETTINGS
@given(
    names=st.lists(st.sampled_from(("throughput", "latency", "memory")), unique=True, max_size=3),
    noise=st.sampled_from((0.0, 0.01, 0.2)),
)
def test_the_metric_space_the_operator_chose_is_the_space_candidates_are_ranked_in(
    tmp_path_factory: pytest.TempPathFactory, names: list[str], noise: float
) -> None:
    root = tmp_path_factory.mktemp("space")
    space = MetricSpace(
        objectives=tuple(
            Objective(name, "max" if name == "throughput" else "min") for name in names
        ),
        relative_noise=noise,
    )

    terms = plan_of(resolved(root), {"metric_space": space.model_dump(mode="json")}).facts.task

    assert terms is not None
    assert [(item.name, item.direction) for item in terms.objectives] == [
        (item.name, item.direction) for item in space.objectives
    ]
    assert terms.relative_noise == noise


def test_no_profiler_means_no_profiling(tmp_path: Path) -> None:
    plan = plan_of(resolved(tmp_path))

    assert not _config(plan).profiling
    assert plan.facts.task is not None
    assert plan.facts.task.profile_fields == ()


_OPERATOR = """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/runs"
job_timeout_seconds = 1800

[slurm.transport]
kind = "ssh"
host = "cluster"

[vibesys]
remote_python = "/remote/venv/bin/python"
setup_script = "/remote/setup.sh"
"""
_SERVICE = """
[vibesys.service]
command = ["python", "-m", "engine.server", "--port", "VIBESYS_DYNAMIC_PORT"]
readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health"
startup_timeout_seconds = 700
"""


def _slurm_view(
    root: Path,
    *,
    service: bool,
    profile_command: tuple[str, ...] | None = ("profile",),
    sbatch_arguments: tuple[str, ...] = (),
    dedicated_measurement_resources: bool = False,
) -> RunEnvironmentView:
    """The view of a real Slurm run whose capture command comes from the production wiring."""
    path = root / "slurm.toml"
    operator = _OPERATOR.replace(
        "job_timeout_seconds = 1800\n",
        f"job_timeout_seconds = 1800\nsbatch_arguments = {json.dumps(list(sbatch_arguments))}\n"
        f"dedicated_measurement_resources = {json.dumps(dedicated_measurement_resources)}\n",
    )
    path.write_text(operator + (_SERVICE if service else ""), encoding="utf-8")
    settings = load_slurm_operator_settings(path)
    capture = trusted_profile_command(
        settings.config, settings.policy, profile_command, profiler_tree="profiler"
    )
    plan = SlurmEvaluationPlan(config_path=path, profile_command=capture)
    return candidate_environment(
        env_kind="slurm", cluster_evaluation=ClusterEvaluationFacts(settings, plan)
    )


def _environments(root: Path) -> dict[str, RunEnvironmentView]:
    return {
        "host": candidate_environment(),
        "slurm-no-command": _slurm_view(root, service=False, profile_command=None),
        "slurm-command-only": _slurm_view(root, service=False),
        "slurm-service": _slurm_view(root, service=True),
    }


@pytest.mark.parametrize(
    "environment", ["host", "slurm-no-command", "slurm-command-only", "slurm-service"]
)
@pytest.mark.parametrize("profiler_id", ["none", "rocprof"])
@pytest.mark.parametrize("manifest_command", [None, "profile"])
def test_profile_work_follows_the_evaluation_plan_and_the_capture_the_run_offers(
    tmp_path: Path, environment: str, profiler_id: str, manifest_command: str | None
) -> None:
    """Profiling is on exactly when a profiler is chosen and the plan has a profile command.

    A run that wants profile work but cannot capture a profile is refused at launch with an
    error that names the option; it never plans a profile workstream nothing can satisfy.
    """
    view = _environments(tmp_path)[environment]
    plan = TrustedEvaluationPlan(
        accuracy_command="true",
        accuracy_timeout_seconds=11,
        profile_command=manifest_command,
        profile_timeout_seconds=13 if manifest_command else None,
    )
    run = resolved(tmp_path, profiler_id=profiler_id, plan=plan, environment=view)

    wants_profile = profiler_id != "none" and manifest_command is not None
    if wants_profile and not view.captures_profile:
        with pytest.raises(ConfigurationError, match="profiling"):
            plan_of(run)
        return
    made = plan_of(run)

    supplied = {item.value for item in view.supported_profile_fields}
    assert _config(made).profiling is wants_profile
    assert made.facts.task is not None
    assert set(made.facts.task.profile_fields) == (supplied if wants_profile else set())


@pytest.mark.parametrize(
    ("environment", "sbatch_arguments", "declared", "exclusive"),
    [
        ("host", (), False, False),
        ("slurm", (), False, False),
        ("slurm", ("--exclusive",), False, True),
        ("slurm", ("--exclusive=user",), False, False),
        ("slurm", ("--exclusive", "--oversubscribe"), False, False),
        ("slurm", ("--exclusive", "--share"), False, False),
        ("slurm", ("--gres=gpu:4", "-c", "64"), True, True),
        ("slurm", ("--oversubscribe",), True, True),
    ],
)
def test_measurement_plans_carry_dedicated_resources_only_when_the_executor_declares_them(
    tmp_path: Path,
    environment: str,
    sbatch_arguments: tuple[str, ...],
    *,
    declared: bool,
    exclusive: bool,
) -> None:
    """The executor's capability reaches every measurement plan; only then may one be reused."""
    view = (
        candidate_environment()
        if environment == "host"
        else _slurm_view(
            tmp_path,
            service=False,
            sbatch_arguments=sbatch_arguments,
            dedicated_measurement_resources=declared,
        )
    )
    made = plan_of(resolved(tmp_path, environment=view))
    assert made.facts.dedicated_measurement_resources is exclusive


@_SETTINGS
@given(seconds=st.none() | st.integers(1, 10**9))
def test_the_deadline_is_the_run_budget_or_unbounded(
    tmp_path_factory: pytest.TempPathFactory, seconds: int | None
) -> None:
    plan = plan_of(resolved(tmp_path_factory.mktemp("deadline")), max_run_seconds=seconds)

    assert plan.deadline_seconds == (UNBOUNDED_DEADLINE_AT if seconds is None else float(seconds))


@_SETTINGS
@given(interval=st.integers(1, 600), extra=st.integers(0, 600))
def test_the_limits_carry_the_observe_pacing_the_run_declared(
    tmp_path_factory: pytest.TempPathFactory, interval: int, extra: int
) -> None:
    root = tmp_path_factory.mktemp("pacing")
    run = resolved(root)

    limits = plan_of(run, observe=(interval, interval + extra)).limits

    assert (limits.observe_interval, limits.observe_backoff_cap) == (interval, interval + extra)


@settings(max_examples=examples(pr=8, nightly=100))
@given(key=st.text("abcdefghijklmnopqrstuvwxyz_", min_size=3, max_size=14))
def test_an_unknown_launch_option_is_rejected_naming_the_key(key: str) -> None:
    known = set(DynamicLaunch.model_fields)
    unknown = key if key not in known else f"{key}_x"

    with pytest.raises(ValidationError, match=unknown):
        DynamicLaunch.model_validate({**OPTIONS, unknown: 1})


@_SETTINGS
@given(options=_LAUNCHES, profiling=st.booleans())
def test_the_search_config_keeps_the_search_keys_and_takes_profiling_from_the_run(
    options: dict[str, int], *, profiling: bool
) -> None:
    """Profiling is the evaluation plan's to decide; every other search key passes through."""
    launch = launch_of(options)

    config = launch.search_config(profiling=profiling, evaluation_capacity=EVALUATION_CAPACITY)

    assert config.profiling is profiling
    assert {name: getattr(config, name) for name in options} == options
    resolved = {"profiling", "evaluation_slots"}
    assert config.model_dump(exclude=resolved) == launch.model_dump(
        include=set(DynamicConfig.model_fields) - resolved
    )


@_SETTINGS
@given(
    options=_LAUNCHES,
    capacity=st.integers(1, 2 * _WIDEST),
    chosen=st.none() | st.integers(1, _WIDEST),
)
def test_the_run_holds_as_many_trusted_evaluations_as_its_executor_admits(
    tmp_path_factory: pytest.TempPathFactory,
    options: dict[str, int],
    capacity: int,
    chosen: int | None,
) -> None:
    """A run that holds more evaluations than its executor admits dispatches a measurement that
    waits in the executor's queue while its deadline runs, and settles hours of work as failed.
    The operator's own choice stands."""
    launch = options if chosen is None else {**options, "evaluation_slots": chosen}
    plan = plan_of(resolved(tmp_path_factory.mktemp("slots")), launch, evaluation_capacity=capacity)

    expected = min(capacity, _WIDEST) if chosen is None else chosen
    assert _config(plan).slots == expected


@settings(max_examples=examples(pr=8, nightly=100))
@given(accuracy=st.booleans(), benchmark=st.booleans(), needs_accuracy=st.booleans())
def test_a_benchmark_awaits_accuracy_only_where_the_task_requires_it(
    *, accuracy: bool, benchmark: bool, needs_accuracy: bool
) -> None:
    """``requires_accuracy = false`` frees the benchmark from the accuracy gate."""
    terms = terms_of(
        _RECIPE,
        Gates(accuracy=accuracy, benchmark=benchmark),
        MetricSpace(),
        Measuring({}, 1.0, benchmark_needs_accuracy=needs_accuracy),
    )

    assert bool(terms.accuracy_gated) == (accuracy and needs_accuracy)


def test_an_operator_cannot_set_profiling_at_launch() -> None:
    """Profiling follows the evaluation plan, so a launch that sets it is refused by name."""
    with pytest.raises(ValueError, match="profiling"):
        DynamicLaunch.model_validate_json('{"max_rounds": 2, "profiling": true}', strict=True)
