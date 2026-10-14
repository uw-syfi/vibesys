"""The plan the evolve policy makes from a real run's resolved inputs is what core accepts.

The host resolves a run (its facts, evaluation plan and environment) and hands the policy a
`CoreRunContext`; the policy answers with the search, its config, the task's terms, the run's
limits, the evidence the judge vouches for and the variables its templates state. These tests
plan runs from generated launch options and check what the plan says about each.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve.core_policy._inputs import (
    COMMIT,
    OPTIONS,
    candidate_environment,
    launch_of,
    plan_of,
    ports,
    resolved,
)

from vibesys.candidates.api import Gates, Measuring, terms_of
from vibesys.core_wiring import ResolvedRun
from vibesys.evolve_core import evolve_operation_catalog
from vibesys.metrics import MetricSpace
from vibesys.orchestration.evolve.core_policy.api import requirements_for
from vibesys.orchestration.evolve.search.api import JUDGE, EvolveConfig, evolve
from vs_core.api import ArtifactId, ArtifactRef, AssessmentKind, RevisionRef
from vs_runtime.api.core import CoreStartup, new_core_state
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
    "max_generations": st.integers(1, 4),
    "children_per_generation": st.integers(1, 4),
    "bootstrap_max_attempts": st.integers(1, 4),
    "max_parallelism": st.integers(1, 4),
    "k_top_inspirations": st.integers(0, 3),
}
_LAUNCHES = st.fixed_dictionaries({}, optional=_SEARCH_KEYS)
_RECIPE = ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe")


def _config(plan: CorePlan) -> EvolveConfig:
    assert isinstance(plan.config, EvolveConfig)
    return plan.config


def _started(plan: CorePlan, root: Path, run: ResolvedRun) -> CoreState:
    """The state a shell builds from the plan on the production catalog: what core accepts."""
    catalog = evolve_operation_catalog(ports(root, run))
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
    plan = plan_of(run)

    state = _started(plan, tmp_path, run)

    assert state.run.facts.baseline == RevisionRef.of_git_commit(COMMIT)
    assert plan.search.name == evolve.name == "evolve"


@_SETTINGS
@given(options=_LAUNCHES)
def test_the_limits_admit_exactly_the_attempts_the_config_schedules(
    tmp_path_factory: pytest.TempPathFactory, options: dict[str, int]
) -> None:
    root = tmp_path_factory.mktemp("limits")
    run = resolved(root)
    plan = plan_of(run, options)
    config = _config(plan)

    assert config == launch_of(options).search_config(profiling=False)
    assert plan.limits.max_attempts == config.total_attempts
    assert plan.limits.max_parallel == config.max_parallelism
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
        (AssessmentKind.LOCAL_VALIDATION, evolve.ids.role_id(JUDGE.name).root, JUDGE.reply_schema)
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


@pytest.mark.parametrize("profiler_id", ["none", "rocprof", "ncu"])
def test_the_profiler_the_run_chose_is_stated_to_the_templates_and_turns_profiling_on(
    tmp_path: Path, profiler_id: str
) -> None:
    plan = plan_of(resolved(tmp_path, profiler_id=profiler_id))
    stated = {k: v for k, v in plan.prompt_variables.items() if k.startswith("profil")}

    assert _config(plan).profiling is (profiler_id != "none")
    assert (stated.get("profiler_kind") == profiler_id) is (profiler_id != "none")
    assert (set(stated) == set()) is (profiler_id == "none")
    assert plan.prompt_variables["interface"] == "inprocess"
    assert plan.prompt_variables["reference_path"] == "."


def test_profiling_is_not_the_operators_to_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="'profiling' is not a launch option"):
        plan_of(resolved(tmp_path), {"profiling": True})


def test_a_run_environment_that_cannot_open_a_sandbox_per_candidate_runs_one_child_at_a_time_in_its_root(
    tmp_path: Path,
) -> None:
    wide = plan_of(resolved(tmp_path), {"max_parallelism": 3})
    assert isinstance(wide.config, EvolveConfig)
    assert (wide.config.exclusive_root, wide.config.max_parallelism) == (False, 3)

    environment = candidate_environment(share_agent_session=True)
    shared = plan_of(resolved(tmp_path, environment=environment), {"max_parallelism": 3})
    assert isinstance(shared.config, EvolveConfig)
    assert (shared.config.exclusive_root, shared.config.max_parallelism) == (True, 1)
    assert shared.limits.max_parallel == 1


def test_every_launch_option_the_policy_reads_is_the_searchs_or_the_tasks() -> None:
    assert set(OPTIONS) <= set(EvolveConfig.model_fields)
    assert set(type(launch_of()).model_fields) - set(EvolveConfig.model_fields) == {
        "modality",
        "metric_space",
    }
