"""Behavioral tests for the evolutionary search loop that aren't already
snapshotted by ``tests/vibesys/golden/test_evolve_golden.py``.

Drives the real, registered ``evolve`` strategy end to end
(``run_orchestration`` against a scripted ``FakeAgentClient``, real git
tracking and population persistence) through ``tests/vibesys/loops/evolve
/_support.py``'s ``_invoke_loop``/``_invoke_bootstrap``. A handful of edge
cases (candidate-runtime naming, deployment teardown, candidate code
diffing, an orphaned parallel-evaluation outcome) have no reachable path
through a full scripted run and instead call the loop's own host-capability
helpers directly against ``_FakeRunContext``, the narrowest fake that gets
them there.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from tests.support.run_execution import run_execution_record
from tests.vibesys.loops.evolve._support import (
    _as_run_context,
    _best,
    _default_profiler_responses,
    _evolution_descriptor,
    _FakeRunContext,
    _frontier,
    _invoke_bootstrap,
    _invoke_loop,
    _judge_response,
    _load_population,
    _mutator_writes_callback,
    _project_dir,
)

from vibesys.api.testing import FakeGateExecutor
from vibesys.constants import DomainName
from vibesys.domains.registry import resolve_domain
from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
    framework_command_timeout,
)
from vibesys.evaluators.input_manifest import BenchmarkResult
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.events import FrameworkWarningData
from vibesys.loops.evolve.loop import (
    _candidate_code,
    _candidate_runtime_notes,
    _evaluate_in_subcontext,
    _teardown_candidate_deployment,
)
from vibesys.loops.evolve.run import EvolveRun
from vibesys.loops.evolve.state import EvolutionStateStore
from vibesys.profilers import ProfilerKind
from vibesys.render.sink import output_sink
from vibesys.roles.profiler import ProfilerSummary
from vibesys.run import GitTracker, RunState, RunStateNamespace
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.sandbox.run_environment import CandidateRuntime
from vibesys.search.population.models import Individual, OpenEvolveSelectorConfig
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vibesys.evaluators.gates import TrustedGateContext

# ---------------------------------------------------------------------------
# Bootstrap phase: failure/repair/resume behaviors not covered by the golden
# bootstrap_pass / bootstrap_retry_then_pass / gate scenarios.
# ---------------------------------------------------------------------------


def test_bootstrap_fails_all_attempts_returns_false(tmp_path: Path, ref_file: str) -> None:
    """Abort handling: when every bootstrap attempt fails the judge, the run
    logs and stops before the generation loop, returning False without ever
    profiling an unverified candidate."""
    runner = FakeAgentClient().enqueue("judge", _judge_response("fail"), _judge_response("fail"))
    result = _invoke_bootstrap(tmp_path, ref_file, runner, bootstrap_max_attempts=2)

    assert result is False
    log_files = list(tmp_path.parent.rglob("*.log"))
    assert any("bootstrap produced no passing seed" in path.read_text() for path in log_files), (
        f"expected the abort message in one of {log_files}"
    )
    assert len(runner.calls_for("implementer")) == 2
    assert len(runner.calls_for("profiler")) == 0  # judged-fail attempts skip profiling

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    for ind in pop:
        assert ind.passed is False
        assert ind.generation == 0


def test_bootstrap_repairs_wip_seed_across_attempts(tmp_path: Path, ref_file: str) -> None:
    """WIP-seed regression: a second bootstrap attempt fix-forwards from the
    most-recent WIP seed (checks that commit out and mutates on top) rather
    than restarting from the reference, yielding a distinct WIP commit."""
    runner = FakeAgentClient().enqueue("judge", _judge_response("fail"), _judge_response("fail"))
    runner.on_invoke(_mutator_writes_callback(runner))
    result = _invoke_bootstrap(tmp_path, ref_file, runner, bootstrap_max_attempts=2)
    assert result is False

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    first, second = pop
    assert first.passed is False
    assert second.passed is False
    assert first.generation == 0
    assert second.generation == 0
    # Both attempts snapshotted their (distinct) trees.
    assert first.commit
    assert second.commit
    assert first.commit != second.commit


def test_bootstrap_repairs_after_framework_accuracy_failure(tmp_path: Path, ref_file: str) -> None:
    """An LLM-approved seed still fails when the trusted oracle rejects it:
    the failed seed is never profiled, its oracle feedback is retained for
    the repair attempt, and the configured timeout reaches the framework
    gate every attempt."""
    failure = "Framework accuracy gate failed.\nstatus endpoint diverged"
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    gate_executor = FakeGateExecutor()
    gate_executor.script_accuracy(
        AccuracyGateResult(
            command=None, passed=False, output=failure, feedback=failure, executed=True
        ),
        AccuracyGateResult(command=None, passed=True, output="", feedback=None, executed=True),
    )
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        gate_executor=gate_executor,
        accuracy_timeout_seconds=37,
        bootstrap_max_attempts=2,
    )

    assert result is True
    assert len(runner.calls_for("judge")) == 2
    assert len(runner.calls_for("profiler")) == 1
    assert len(gate_executor.accuracy_calls) == 2
    assert [call.timeout_seconds for call in gate_executor.accuracy_calls] == [37, 37]

    failed, seed = _load_population(tmp_path)
    assert failed.passed is False
    assert failed.feedback == failure
    assert failed.commit
    assert seed.passed is True


def test_evolve_with_preexisting_passing_seed_skips_bootstrap(
    tmp_path: Path, ref_file: str
) -> None:
    """A resumed run whose population already has a passing seed skips the
    bootstrap phase entirely and evolves straight off the seed."""
    _invoke_bootstrap(tmp_path, ref_file, FakeAgentClient())
    exp_envs = list((tmp_path / "exp_env").iterdir())
    assert len(exp_envs) == 1
    exp_name = exp_envs[0].name

    result = _invoke_loop(
        tmp_path,
        ref_file,
        FakeAgentClient(),
        exp_name=exp_name,
        input_path=str(exp_envs[0]),
        existing=True,
        max_generations=2,
        children_per_generation=1,
    )
    assert result is True
    resumed = Project.open(exp_envs[0]).state.load_run(exp_name)
    assert resumed.orchestration.options["max_generations"] == 2

    pop = _load_population(tmp_path)
    # A re-run bootstrap would have added a second generation-0 individual;
    # only the seed plus the new gen-2 child are present.
    assert len(pop) == 2
    seed, child = pop
    assert seed.generation == 0
    assert seed.parent_id is None
    assert child.parent_id == seed.id
    assert child.generation == 2


# ---------------------------------------------------------------------------
# Multi-generation: deterministic replay, lineage, final tree selection.
# ---------------------------------------------------------------------------


def test_final_project_tree_is_the_deterministic_scalar_best(tmp_path: Path, ref_file: str) -> None:
    """Deterministic replay: the run ends with the workspace checked out to
    the highest-fitness individual's tree, not the last-evaluated one."""
    responses = [
        _default_profiler_responses(1)[0],
        _default_profiler_responses(1, start=100.0)[0],
        _default_profiler_responses(1, start=20.0)[0],
    ]
    runner = FakeAgentClient().enqueue("profiler", *responses)
    runner.on_invoke(_mutator_writes_callback(runner))
    result = _invoke_loop(tmp_path, ref_file, runner, max_generations=1, children_per_generation=2)

    assert result is True
    best = _best(_load_population(tmp_path), MetricSpace())
    assert best is not None
    assert best.perf_metric == 100.0
    project = _project_dir(tmp_path)
    assert (project / "mutant_2.py").is_file()
    assert not (project / "mutant_3.py").exists()


def test_failed_child_excluded_from_future_parent_pool(tmp_path: Path, ref_file: str) -> None:
    """Proposals are always derived from generation-start state, and only
    committed individuals are eligible parents: gen 2 must still parent off
    the seed, never off the failed, commit-less gen-1 child."""
    runner = FakeAgentClient().enqueue(
        "judge", _judge_response("pass"), _judge_response("fail"), _judge_response("pass")
    )
    result = _invoke_loop(tmp_path, ref_file, runner, max_generations=2, children_per_generation=1)
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 3
    seed, g1, g2 = pop
    assert seed.passed is True
    assert g1.passed is False
    assert g1.commit is None
    assert g2.parent_id == seed.id


def test_pareto_mode_records_metrics_dict_on_individuals(tmp_path: Path, ref_file: str) -> None:
    """With axes configured, the profiler's ``metrics`` dict is copied onto
    every passing ``Individual`` so the frontier can be computed."""
    space = MetricSpace(
        objectives=(
            Objective(name="tput", direction="max"),
            Objective(name="lat_ms", direction="min"),
        )
    )
    profiler_responses = [
        _profiler_metrics(100.0, {"tput": 100.0, "lat_ms": 80.0}),
        _profiler_metrics(80.0, {"tput": 80.0, "lat_ms": 50.0}),
    ]
    runner = FakeAgentClient().enqueue("profiler", *profiler_responses)
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
        space=space,
        frontier_bias=1.0,
    )
    assert result is True

    pop = _load_population(tmp_path)
    seed, child = pop
    assert seed.metrics == {"tput": 100.0, "lat_ms": 80.0}
    assert child.metrics == {"tput": 80.0, "lat_ms": 50.0}
    front_ids = {i.id for i in _frontier(pop, space)}
    assert front_ids == {seed.id, child.id}


def _profiler_metrics(perf_metric: float, metrics: dict[str, float]):  # noqa: ANN202  # LW-040169 [ANN202];  tracked: #288.

    return ProfilerSummary(
        analysis="ok",
        bottlenecks="none",
        suggestions="none",
        perf_metric=perf_metric,
        perf_unit="tput",
        metrics=metrics,
    )


def test_no_objectives_keeps_metrics_empty_and_legacy_behavior(
    tmp_path: Path, ref_file: str
) -> None:
    """A space with no axes keeps ``Individual.metrics`` empty (single-
    objective mode), even though the profiler stub doesn't supply one."""
    result = _invoke_bootstrap(tmp_path, ref_file, FakeAgentClient())
    assert result is True
    pop = _load_population(tmp_path)
    assert pop[0].metrics == {}


def test_openevolve_policy_persists_state_as_data(tmp_path: Path, ref_file: str) -> None:
    """R4: OpenEvolve state is held entirely inside the committed
    ``PopulationState``, so it never grows an on-disk snapshot directory."""

    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        domain=DomainName.GENERIC,
        modality=None,
        profiler_kind=ProfilerKind.NONE,
        search_policy="openevolve",
        openevolve_config=OpenEvolveSelectorConfig(
            population_size=10,
            archive_size=5,
            num_islands=2,
            migration_interval=1,
            migration_rate=1.0,
        ),
        max_generations=1,
        children_per_generation=1,
    )
    assert result is True

    project = Project.open(_project_dir(tmp_path))
    run = project.state.resolve_run()
    state = RunState(project, MagicMock(history_root=project.root, run_id=run.run_id), run.run_id)
    namespace = state.portable(RunStateNamespace.EVOLVE)
    store = EvolutionStateStore(namespace)
    loaded = store.load()
    assert loaded is not None
    selector_state = loaded.population.selector_state
    assert selector_state is not None
    assert not (namespace.external_directory("openevolve") / "snapshots").exists()
    child = next(i for i in loaded.population.individuals if i.generation == 1)
    assert set(selector_state.admitted_individual_ids) == {1, 2}
    assert child.policy_parent_id == "vibesys-1"
    assert child.policy_target_island == 0


# ---------------------------------------------------------------------------
# Benchmark-contract wiring: evolve-specific composition of a declared,
# framework-owned fitness result over the profiler's self-report.
# ---------------------------------------------------------------------------


def test_benchmark_contract_owns_seed_and_child_fitness(tmp_path: Path, ref_file: str) -> None:
    """A declared benchmark result contract, not the profiler agent's self-
    report, records every candidate's fitness."""

    runner = FakeAgentClient().enqueue("profiler", *_default_profiler_responses(2))
    gate_executor = FakeGateExecutor()
    gate_executor.script_benchmark(_passing_gate_result(42.5), _passing_gate_result(43.75))
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        gate_executor=gate_executor,
        max_generations=1,
        children_per_generation=1,
        benchmark_result=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
    )

    assert result is True
    assert len(gate_executor.benchmark_calls) == 2
    pop = _load_population(tmp_path)
    assert [item.perf_metric for item in pop] == [42.5, 43.75]
    # The scalar contract declares a metric name, not a unit; the recorded
    # unit stays the profiler's.
    assert {item.perf_unit for item in pop} == {"tok/s"}
    assert pop[0].metrics == {"total_ops_per_sec": 42.5}
    assert len(runner.calls_for("profiler")) == 2  # ran for diagnostics; self-report unused


def test_benchmark_contract_failure_fails_the_candidate_before_profiling(
    tmp_path: Path, ref_file: str
) -> None:

    failing = BenchmarkGateResult(
        command="trusted-benchmark --json /tmp/result.json",
        output="benchmark exploded",
        executed=True,
        outcome=FrameworkBenchmarkOutcome(
            feedback="Framework benchmark failed.\nbenchmark exploded"
        ),
    )
    gate_executor = FakeGateExecutor()
    gate_executor.script_benchmark(failing)
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        FakeAgentClient(),
        gate_executor=gate_executor,
        benchmark_result=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
        bootstrap_max_attempts=1,
    )

    assert result is False
    pop = _load_population(tmp_path)
    assert len(pop) == 1
    failed = pop[0]
    assert failed.passed is False
    assert "Framework benchmark failed." in (failed.feedback or "")


def test_scalar_contract_keeps_the_profilers_other_axes_on_the_frontier(
    tmp_path: Path, ref_file: str
) -> None:
    """Regression: a one-metric contract must not empty a two-axis frontier.

    The trusted row now overrides only the axis it measures and leaves the
    rest of the profiler's row in place, so both trade-off individuals stay
    on the frontier instead of both losing the second axis and starving
    Pareto parent selection.
    """

    space = MetricSpace(
        objectives=(
            Objective(name="total_ops_per_sec", direction="max"),
            Objective(name="p99_latency_ns", direction="min"),
        )
    )
    profiler_responses = [
        _profiler_metrics(100.0, {"total_ops_per_sec": 100.0, "p99_latency_ns": 500.0}),
        _profiler_metrics(80.0, {"total_ops_per_sec": 80.0, "p99_latency_ns": 800.0}),
    ]
    runner = FakeAgentClient().enqueue("profiler", *profiler_responses)
    gate_executor = FakeGateExecutor()
    gate_executor.script_benchmark(_passing_gate_result(42.5), _passing_gate_result(43.75))
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        gate_executor=gate_executor,
        max_generations=1,
        children_per_generation=1,
        space=space,
        frontier_bias=1.0,
        benchmark_result=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
    )

    assert result is True
    pop = _load_population(tmp_path)
    seed, child = pop
    assert seed.metrics == {"total_ops_per_sec": 42.5, "p99_latency_ns": 500.0}
    assert child.metrics == {"total_ops_per_sec": 43.75, "p99_latency_ns": 800.0}
    assert {item.id for item in _frontier(pop, space)} == {seed.id, child.id}


def test_protocol_contract_records_the_evaluator_declared_unit(
    tmp_path: Path, ref_file: str
) -> None:
    gate_executor = FakeGateExecutor()
    gate_executor.script_benchmark(_passing_gate_result(42.5, unit="ops/s"))
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        FakeAgentClient(),
        gate_executor=gate_executor,
        benchmark_result_protocol=2,
    )

    assert result is True
    seed = _load_population(tmp_path)[0]
    assert seed.perf_metric == 42.5
    assert seed.perf_unit == "ops/s"


def _passing_gate_result(metric_value: float, *, unit: str | None = None):  # noqa: ANN202  # LW-040176 [ANN202];  tracked: #288.

    return BenchmarkGateResult(
        command="trusted-benchmark --json /tmp/result.json",
        output="ok",
        executed=True,
        outcome=FrameworkBenchmarkOutcome(
            metric_name="total_ops_per_sec",
            metric_value=metric_value,
            metric_direction="max",
            metric_unit=unit,
        ),
    )


# ---------------------------------------------------------------------------
# Serial fallback without parallel-capable environment support.
# ---------------------------------------------------------------------------


def test_max_parallelism_ignored_without_environment_capability(
    tmp_path: Path, ref_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment without isolated evaluation support stays serial even
    when a larger ``max_parallelism`` is requested."""
    called = {"parallel": False}
    monkeypatch.setattr(
        EvolveRun,
        "_evaluate_parallel_pool",
        lambda *a, **k: called.__setitem__("parallel", True),  # noqa: ARG005, FBT003  # LW-040178 [ARG005, FBT003];  tracked: #288.
    )
    runner = FakeAgentClient().enqueue(
        "judge", _judge_response("pass"), _judge_response("pass"), _judge_response("pass")
    )
    _invoke_loop(
        tmp_path, ref_file, runner, max_generations=1, children_per_generation=1, max_parallelism=4
    )
    assert called["parallel"] is False
    assert len(_load_population(tmp_path)) == 2  # bootstrap seed + one serial gen-1 candidate


# ---------------------------------------------------------------------------
# Host-capability helpers with no reachable edge case through a full run.
# ---------------------------------------------------------------------------


def test_evaluate_in_subcontext_skips_parent_without_commit() -> None:
    """A parent with no commit can't seed a worktree: folded into a failed
    outcome without ever building a sub-context."""

    parent_ctx = _FakeRunContext(log=lambda _line: None)
    parentless = Individual(id=3, generation=1, parent_id=1, commit=None, passed=True, summary="x")

    seen = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        outcome = asyncio.run(
            _evaluate_in_subcontext(
                _as_run_context(parent_ctx),
                generation=2,
                child_idx=1,
                parent=parentless,
                inspirations=[],
                objective="obj",
                space=MetricSpace(),
                modality="text_generation",
                domain_definition=resolve_domain(DomainName.LLM_SERVING),
                pass_criteria="crit",  # noqa: S106  # LW-040180 [S106];  tracked: #288.
                keep_deployments=False,
                policy_parent_id=None,
                target_island=None,
            )
        )
    finally:
        unsubscribe()

    assert outcome.passed is False
    assert outcome.parent_id == 3
    assert "no parent commit" in outcome.summary
    warnings = [e.data.summary for e in seen if isinstance(e.data, FrameworkWarningData)]
    assert any("no parent commit" in summary for summary in warnings)


def test_candidate_runtime_notes_delegates_and_noop_without_deployment() -> None:
    """The loop stays backend-agnostic: naming and prompt notes come from
    the run environment, and a non-named deployment is a pass-through."""
    base = "run-20260720-abcd1234-llama3"
    named = _FakeRunContext(
        run_environment_view=SimpleNamespace(
            deployment_namespace=base,
            prompt_notes=f"Deploy to Modal app {base}; endpoint {base}-web.",
        ),
        run_environment=MagicMock(
            candidate_runtime=lambda view, generation, child_idx: CandidateRuntime(  # noqa: ARG005  # LW-040182 [ARG005];  tracked: #288.
                prompt_notes="provider-owned candidate instructions",
                deployment_name=f"candidate-{generation}-{child_idx}",
            )
        ),
    )
    notes, app = _candidate_runtime_notes(_as_run_context(named), generation=3, child_idx=2)
    assert app == "candidate-3-2"
    assert notes == "provider-owned candidate instructions"

    notes_in = "Local run; no named deployment."
    local = _FakeRunContext(
        run_environment_view=SimpleNamespace(deployment_namespace=None, prompt_notes=notes_in),
        run_environment=MagicMock(
            candidate_runtime=lambda view, generation, child_idx: CandidateRuntime(  # noqa: ARG005  # LW-040183 [ARG005];  tracked: #288.
                prompt_notes=view.prompt_notes
            )
        ),
    )
    notes, app = _candidate_runtime_notes(_as_run_context(local), generation=1, child_idx=1)
    assert app is None
    assert notes == notes_in


def test_teardown_candidate_deployment_delegates_skips_when_kept_or_absent() -> None:
    """The loop hands the deployment name to the run environment, which
    decides how to release it; a kept or absent deployment is a no-op."""

    run_env = MagicMock()
    ctx = _FakeRunContext(run_environment=run_env)

    asyncio.run(
        _teardown_candidate_deployment(_as_run_context(ctx), "vibesys-run-g1c2", keep=False)
    )
    assert run_env.teardown_deployment.call_args.args[0] == "vibesys-run-g1c2"

    run_env.reset_mock()
    asyncio.run(_teardown_candidate_deployment(_as_run_context(ctx), "vibesys-run-g1c2", keep=True))
    asyncio.run(_teardown_candidate_deployment(_as_run_context(ctx), None, keep=False))
    run_env.teardown_deployment.assert_not_called()


def test_candidate_code_is_multi_file_but_excludes_framework_state(tmp_path: Path) -> None:
    """The diff shown to agents never leaks evolve's own framework-state
    files (population/metrics documents) alongside real candidate code."""

    tracker = GitTracker(tmp_path, run_id="test-evolve", events=NullGitTrackerEvents())
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("baseline\n")
    (tmp_path / "src" / "ffi.rs").write_text("baseline\n")
    tracker.init(existing=False)
    project = Project.open(tmp_path)
    project.state.create_project("evolve patch test")
    assert tracker.project_branch is not None
    assert tracker.trusted_input_baseline is not None
    project.state.create_run(
        project.state.new_run_manifest(
            "evolve patch test",
            run_id="test-evolve",
            branch=tracker.project_branch,
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=run_execution_record(),
            orchestration=_evolution_descriptor(),
            trusted_input_baseline=tracker.trusted_input_baseline,
        )
    )
    tracker.snapshot_with_framework_metadata(
        "initialize state", project.state.initialization_snapshot("test-evolve")
    )

    (tmp_path / "src" / "lib.rs").write_text("optimized lib\n")
    (tmp_path / "src" / "ffi.rs").write_text("optimized ffi\n")
    tracker.snapshot("candidate")
    evolve_state = project.state.portable_namespace("test-evolve", "evolve")
    (evolve_state.external_directory() / "population.json").write_text("[]\n")
    tracker.snapshot_framework_state("evolve state", evolve_state.snapshot())
    commit = tracker.current_sha()

    assert commit is not None
    code = asyncio.run(_candidate_code(_as_run_context(_FakeRunContext(git=tracker)), commit))
    assert "src/lib.rs" in code
    assert "src/ffi.rs" in code
    assert "population.json" not in code


def test_evolve_accuracy_gate_extends_timeout_by_environment_setup_allowance() -> None:
    """Environment-owned deployment/readiness time must not eat the
    accuracy/benchmark command's declared budget."""
    ctx = _FakeRunContext(run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=90))
    assert framework_command_timeout(cast("TrustedGateContext", ctx), 120) == 210
    zero = _FakeRunContext(run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=0))
    assert framework_command_timeout(cast("TrustedGateContext", zero), 120) == 120
