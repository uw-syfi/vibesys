"""Integration tests for the evolutionary search loop.

Mocks the agent runner so the LLM-driven mutator/judge/profiler return
scripted responses. The real ``_RunContext`` is built on a tmp_path
workspace (so git tracking, snapshots, and population persistence are
exercised end-to-end), but the model + sandbox + agent-runner factories
are patched out, the same pattern as ``tests/vibesys/loops/agent/test_orchestrate.py``.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack
from unittest.mock import MagicMock, patch

import pytest

from vibesys.agents import AgentClient
from vibesys.config import Config
from vibesys.context import create_run_context
from vibesys.domains.base import DomainName
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks
from vibesys.domains.registry import resolve_domain
from vibesys.loops.evolve import loop as evolve_loop
from vibesys.loops.evolve.loop import (
    _candidate_code,
    _candidate_runtime_notes,
    _CandidateOutcome,
    _evaluate_in_subcontext,
    _initialize_search_policy,
    _latest_wip_seed,
    _plan_candidate,
    _recent_failure_lessons,
    _run_framework_benchmark_gate,
    _run_generation_parallel,
    _teardown_candidate_deployment,
    run_evolve_loop,
)
from vibesys.loops.evolve.population import (
    Individual,
    Population,
)
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    OpenEvolveSearchPolicy,
    SearchSelection,
    VibeSysSearchPolicy,
)
from vibesys.loops.evolve.state import EvolutionStateStore
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.profilers import ProfilerKind
from vibesys.render.sink import output_sink
from vibesys.run import EventJournal, GitTracker, LoopContext, RunState, RunStateNamespace
from vibesys.run.events import FrameworkWarningData
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.sandbox.run_environment import CandidateRuntime, RunEnvironmentSpec
from vibesys.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict
from vs_project import EvolveRunConfiguration, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.constants import ComputeBackend
    from vibesys.input_manifest import BenchmarkResult, WorkspaceSource
    from vibesys.loops.evolve.search_policy import SearchPolicyName
    from vibesys.run import RepositoryVisibility

_LLM_SERVING_DOMAIN = resolve_domain(DomainName.LLM_SERVING)


class _EvolveLoopKwargs(TypedDict, total=False):
    """The keyword arguments ``run_evolve_loop`` accepts.

    ``_invoke_loop`` merges shared defaults with per-test overrides before
    splatting them into the loop, so the merged mapping needs a per-key type
    instead of the heterogeneous union a plain ``dict`` infers.
    """

    config: Config
    exp_name: str
    input_path: str
    accuracy_command: str
    benchmark_command: str
    objective: str
    runs_dir: Path | None
    task_name: str | None
    task_root: Path | None
    workspace_sources: tuple[WorkspaceSource, ...]
    evaluator_path: Path | None
    evaluator_package_root: Path | None
    accuracy_timeout_seconds: int | None
    benchmark_result: BenchmarkResult | None
    benchmark_result_protocol: Literal[2] | None
    benchmark_timeout_seconds: int | None
    max_generations: int
    children_per_generation: int
    k_top_inspirations: int
    k_random_inspirations: int
    selection_temperature: float
    seed: int | None
    pass_criteria: str
    existing: bool
    debug: bool
    profiler_kind: ProfilerKind
    skills_dirs: list[str] | None
    run_environment: RunEnvironmentSpec | None
    agent_backend: str | None
    cli_provider: str | None
    backend: ComputeBackend
    modality: str | None
    domain: DomainName | None
    space: MetricSpace
    frontier_bias: float
    bootstrap_max_attempts: int
    keep_deployments: bool
    max_parallelism: int
    search_policy: SearchPolicyName | str | None
    openevolve_config: OpenEvolveSearchConfig | None
    remote_repo: str | None
    repo_visibility: RepositoryVisibility


def _discard_log(_message: str) -> None:
    """Drop log output emitted by a helper under test."""


class _FakeLoopContext(LoopContext):
    """A ``LoopContext`` exposing only the members a helper under test reads.

    Subclassing the protocol keeps the fake assignable to the declared
    parameter type. Members a test does not wire up stay unset, so a helper
    that reaches past what the test set up fails loudly.
    """

    def __init__(  # noqa: PLR0913  # tracked: #288
        self,
        *,
        git: GitTracker | None = None,
        state: RunState | None = None,
        run_environment: object | None = None,
        run_environment_view: object | None = None,
        events: EventJournal | None = None,
        log: Callable[[str], None] = _discard_log,
    ) -> None:
        if events is not None:
            self.events = events
        if git is not None:
            self.git = git
        if state is not None:
            self.state = state
        if run_environment is not None:
            self.run_environment = run_environment
        if run_environment_view is not None:
            self.run_environment_view = run_environment_view
        self._log = log

    def lprint(self, text: str) -> None:
        """Record a log line the same way the real context would emit it."""
        self._log(text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ref_file(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """Reference *file* + sibling OBJECTIVE.md.

    A single-file reference avoids the model-weight resolution that a
    reference *directory* triggers, the same trick test_orchestrate.py
    uses to keep tests independent of HF cache state.
    """
    model_dir = tmp_path / "input_model"
    model_dir.mkdir()
    ref = model_dir / "ref.py"
    ref.write_text("def predict(x): return x * 2\n")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n")
    (model_dir / "vibesys.input.toml").write_text(
        """version = 1

[agent]
domain = "llm-serving"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('ok')"]
""",
        encoding="utf-8",
    )
    return str(ref)


def _make_runner(  # noqa: ANN202, C901, PLR0913  # tracked: #288
    *,
    judge_verdicts: list[str] | None = None,
    profiler_responses: list[ProfilerSummary] | None = None,
    capture_mutator_prompts: list[str] | None = None,
    capture_judge_prompts: list[str] | None = None,
    capture_profiler_prompts: list[str] | None = None,
    mutator_writes: bool = False,
):
    """Build a MagicMock AgentClient with scripted responses.

    The mutator (``kind="implementer"`` + ``response_cls=MutatorResponse``)
    always returns a stub MutatorResponse. Judge verdicts default to
    ``"pass"``; profiler responses default to a fitness of ``10.0 tok/s``
    incrementing by 1 per call so each child has a distinct perf number.
    """
    judge_q = list(judge_verdicts or [])
    prof_q = list(profiler_responses or [])
    counters = {"mutator": 0, "judge": 0, "profiler": 0}

    runner = MagicMock(spec=AgentClient)
    runner.backend_name = "deepagents"
    # Every invocation event carries the client's attribution, so the mock
    # supplies real strings the event payload can validate.
    runner.driver_name = "mock"
    runner.provider = "mock"
    runner.model_for_kind.return_value = "mock-model"

    def _invoke(*, kind, response_cls, fallback_factory, system_prompt="", **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001  # tracked: #288
        if response_cls is MutatorResponse:
            counters["mutator"] += 1
            if capture_mutator_prompts is not None:
                capture_mutator_prompts.append(system_prompt)
            # Simulate a real edit so the cold-start snapshot has something to
            # commit (a WIP repair-seed). Without a file change the snapshot is
            # a no-op and no commit is recorded.
            if mutator_writes:
                workspace = kwargs.get("workspace")
                if workspace is not None:
                    (Path(workspace) / f"mutant_{counters['mutator']}.py").write_text(
                        f"# mutant {counters['mutator']}\n"
                    )
            return MutatorResponse(
                summary=f"mutator call {counters['mutator']}",
                hypothesis="should be faster",
                expected_behavior="ok",
            )
        if kind == "judge":
            if capture_judge_prompts is not None:
                capture_judge_prompts.append(system_prompt)
            idx = counters["judge"]
            counters["judge"] += 1
            v = judge_q[idx] if idx < len(judge_q) else "pass"
            return JudgeResponse(
                analysis="ok",
                feedback="" if v == "pass" else "needs work",
                verdict=Verdict.PASS if v == "pass" else Verdict.FAIL,
            )
        if kind == "profiler":
            if capture_profiler_prompts is not None:
                capture_profiler_prompts.append(system_prompt)
            idx = counters["profiler"]
            counters["profiler"] += 1
            if idx < len(prof_q):
                return prof_q[idx]
            return ProfilerSummary(
                analysis="ok",
                bottlenecks="none",
                suggestions="none",
                perf_metric=10.0 + idx,
                perf_unit="tok/s",
            )
        raise AssertionError(  # noqa: TRY003  # tracked: #288
            f"unexpected agent_runner.invoke call: kind={kind} response_cls={response_cls}"
        )

    runner.invoke.side_effect = _invoke
    runner.counters = counters
    return runner


def _invoke_loop(
    tmp_path: Path,
    ref_file: str,
    runner: MagicMock,
    *,
    _accuracy_gate_feedbacks: list[str | None] | None = None,
    **kwargs: Unpack[_EvolveLoopKwargs],
) -> bool:
    """Shared plumbing — patch context globals, run the loop, return result."""
    accuracy_gate = MagicMock(return_value=None)
    if _accuracy_gate_feedbacks is not None:
        accuracy_gate.side_effect = list(_accuracy_gate_feedbacks)
    runner.accuracy_gate = accuracy_gate
    defaults: _EvolveLoopKwargs = {
        "config": Config.model_validate({"model": {"name": "claude-sonnet-4-6"}}),
        "exp_name": "test-evolve",
        "runs_dir": tmp_path / "exp_env",
        "input_path": str(Path(ref_file).parent),
        "accuracy_command": "uv run python accuracy_checker/checker.py",
        "benchmark_command": "uv run python benchmark/benchmark.py",
        "objective": "Maximize tok/s throughput.",
        "max_generations": 2,
        "children_per_generation": 1,
        "seed": 0,
        "domain": DomainName.LLM_SERVING,
        "space": MetricSpace(),
    }
    defaults.update(kwargs)
    with (
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.backends.cuda.make_local_shell_sandbox"),
        patch("vibesys.context.build_agent_client", return_value=runner),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        patch(
            "vibesys.loops.evolve.loop._run_framework_accuracy_gate",
            accuracy_gate,
        ),
    ):
        return run_evolve_loop(**defaults)


def _invoke_bootstrap(
    tmp_path: Path,
    ref_file: str,
    runner: MagicMock,
    *,
    _accuracy_gate_feedbacks: list[str | None] | None = None,
    **kwargs: Unpack[_EvolveLoopKwargs],
) -> bool:
    """Exercise bootstrap through a valid one-generation run contract."""
    overrides: _EvolveLoopKwargs = {"max_generations": 1}
    overrides.update(kwargs)
    with patch("vibesys.loops.evolve.loop._run_generation_serial"):
        return _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            _accuracy_gate_feedbacks=_accuracy_gate_feedbacks,
            **overrides,
        )


def _load_population(tmp_path) -> Population:  # noqa: ANN001  # tracked: #288
    """Load the canonical portable population for the single test run."""
    return _evolution_state_store(_project_dir(tmp_path)).load_population()


def _project_dir(tmp_path: Path) -> Path:
    projects = [path for path in (tmp_path / "exp_env").iterdir() if path.is_dir()]
    assert len(projects) == 1, projects
    return projects[0]


def _evolution_configuration() -> EvolveRunConfiguration:
    return EvolveRunConfiguration(
        outer_loop="evolve",
        run_environment=RunEnvironmentRecord(name="local"),
        agent_backend="stub",
        compute_backend="cpu",
        max_generations=1,
        children_per_generation=1,
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        frontier_bias=0.7,
        bootstrap_max_attempts=1,
        keep_deployments=False,
        max_parallelism=1,
    )


def _evolution_state_store(project_root: Path) -> EvolutionStateStore:
    project = Project.open(project_root)
    run = project.state.resolve_run()
    state = RunState(
        project,
        MagicMock(history_root=project.root, run_id=run.run_id),
        run.run_id,
    )
    return EvolutionStateStore(state.portable(RunStateNamespace.EVOLVE))


# ---------------------------------------------------------------------------
# Bootstrap phase (runs before the generation loop)
# ---------------------------------------------------------------------------
#
# Focused bootstrap tests stub the generation runner while still constructing a
# valid one-generation persisted run configuration.


def test_bootstrap_succeeds_first_try(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Bootstrap produces the first passing implementation as a generation-0
    seed with parent_id=None and a perf_metric from the profiler."""
    runner = _make_runner()
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 1
    seed = pop.all[0]
    assert seed.id == 1
    assert seed.generation == 0
    assert seed.parent_id is None
    assert seed.passed is True
    assert seed.perf_metric == 10.0
    assert seed.perf_unit == "tok/s"
    assert seed.commit  # an actual git SHA was recorded


def test_bootstrap_fails_all_attempts_returns_false(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """When every bootstrap attempt fails the judge, the run aborts before the
    generation loop and returns False. Failed attempts are never profiled, and
    with no mutator edits they record no commit."""
    runner = _make_runner(judge_verdicts=["fail", "fail"])
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=2,
    )
    assert result is False
    assert runner.counters["mutator"] == 2
    assert runner.counters["judge"] == 2
    assert runner.counters["profiler"] == 0  # judged-fail attempts skip profiling

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    for ind in pop.all:
        assert ind.passed is False
        assert ind.generation == 0
        assert ind.commit is None  # no edits → no WIP snapshot
    assert "needs work" in pop.all[0].feedback


def test_bootstrap_failed_attempt_records_wip_seed_commit(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """A failed bootstrap attempt whose mutator actually edited the workspace is
    snapshotted to a WIP commit, so a later attempt can repair it in place."""
    runner = _make_runner(judge_verdicts=["fail"], mutator_writes=True)
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=1,
    )
    assert result is False  # single attempt, failed → no seed

    pop = _load_population(tmp_path)
    assert len(pop) == 1
    failed = pop.all[0]
    assert failed.passed is False
    assert failed.generation == 0
    assert failed.parent_id is None
    assert failed.commit  # WIP snapshot recorded because the tree changed


def test_bootstrap_repairs_wip_seed_across_attempts(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """A second bootstrap attempt fix-forwards from the most-recent WIP seed:
    it checks that commit out and mutates on top, yielding a fresh WIP commit
    distinct from the first."""
    runner = _make_runner(judge_verdicts=["fail", "fail"], mutator_writes=True)
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=2,
    )
    assert result is False

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    first, second = pop.all
    assert first.passed is False and second.passed is False  # noqa: PT018  # tracked: #288
    assert first.generation == 0 and second.generation == 0  # noqa: PT018  # tracked: #288
    assert first.parent_id is None and second.parent_id is None  # noqa: PT018  # tracked: #288
    # Both attempts snapshotted their (distinct) trees.
    assert first.commit and second.commit  # noqa: PT018  # tracked: #288
    assert first.commit != second.commit


def test_bootstrap_succeeds_after_repair(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Bootstrap that fails once then passes: the failed attempt is snapshotted,
    the passing attempt repairs it in place and becomes the gen-0 seed. Only the
    passing attempt is profiled."""
    runner = _make_runner(judge_verdicts=["fail", "pass"], mutator_writes=True)
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=3,
    )
    assert result is True
    assert runner.counters["profiler"] == 1  # only the passing attempt profiled

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    failed, seed = pop.all
    assert failed.passed is False and failed.commit  # noqa: PT018  # tracked: #288
    assert seed.passed is True
    assert seed.generation == 0
    assert seed.parent_id is None
    # The passing seed built on the repaired WIP tree → distinct commit.
    assert seed.commit and seed.commit != failed.commit  # noqa: PT018  # tracked: #288


def test_bootstrap_repairs_after_framework_accuracy_failure(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """An LLM-approved seed still fails when the trusted oracle rejects it.

    The failed seed is never profiled, its oracle feedback is retained for the
    repair attempt, and the configured timeout reaches the framework gate.
    """
    failure = "Framework accuracy gate failed.\nstatus endpoint diverged"
    runner = _make_runner(mutator_writes=True)
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        _accuracy_gate_feedbacks=[failure, None],
        accuracy_timeout_seconds=37,
        bootstrap_max_attempts=2,
    )

    assert result is True
    assert runner.counters["judge"] == 2
    assert runner.counters["profiler"] == 1
    assert runner.accuracy_gate.call_count == 2
    assert [call.kwargs["timeout_seconds"] for call in runner.accuracy_gate.call_args_list] == [
        37,
        37,
    ]

    failed, seed = _load_population(tmp_path).all
    assert failed.passed is False
    assert failed.feedback == failure
    assert failed.commit
    assert seed.passed is True


def test_bootstrap_prompt_uses_cold_start_section(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """The bootstrap attempt sees the cold-start branch of the mutator prompt
    (no parent block)."""
    captured: list[str] = []
    runner = _make_runner(capture_mutator_prompts=captured)
    _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
    )
    assert len(captured) == 1
    prompt = captured[0]
    assert "Bootstrap the first passing seed" in prompt
    assert "## Parent" not in prompt
    assert "LLM-serving implementation invariants" in prompt


def test_evolve_with_preexisting_passing_seed_skips_bootstrap(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """A resumed run whose population already has a passing seed skips the
    bootstrap phase entirely and evolves straight off the seed."""
    # First run: bootstrap-only, creates a passing gen-0 seed with a real commit.
    _invoke_bootstrap(tmp_path, ref_file, _make_runner())
    exp_envs = list((tmp_path / "exp_env").iterdir())
    assert len(exp_envs) == 1
    exp_name = exp_envs[0].name

    # Second run resumes that exp dir; bootstrap must NOT be called, and a
    # gen-1 child must be appended off the seed.
    with patch("vibesys.loops.evolve.loop._bootstrap_seed") as spy:
        result = _invoke_loop(
            tmp_path,
            ref_file,
            _make_runner(),
            exp_name=exp_name,
            input_path=str(exp_envs[0]),
            existing=True,
            max_generations=1,
            children_per_generation=1,
        )
        spy.assert_not_called()
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 2  # gen-0 seed + one gen-1 child (same exp dir, resumed)
    seed, child = pop.all
    assert seed.generation == 0 and seed.parent_id is None  # noqa: PT018  # tracked: #288
    assert child.parent_id == seed.id


# ---------------------------------------------------------------------------
# Multi-generation: parent selection + lineage tracking
# ---------------------------------------------------------------------------


def test_first_generation_uses_bootstrap_seed_as_parent(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Gen 1's child must be tagged with parent_id pointing at the bootstrap
    seed."""
    runner = _make_runner()
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 2  # bootstrap seed + one gen-1 child
    seed, child = pop.all
    assert seed.generation == 0
    assert seed.parent_id is None
    assert child.parent_id == seed.id
    assert child.passed is True
    # Seed and child were profiled separately; two distinct stub values.
    assert {seed.perf_metric, child.perf_metric} == {10.0, 11.0}


def test_final_project_tree_is_the_deterministic_scalar_best(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    responses = [
        ProfilerSummary(
            analysis="seed",
            bottlenecks="none",
            suggestions="none",
            perf_metric=10.0,
            perf_unit="tok/s",
        ),
        ProfilerSummary(
            analysis="best",
            bottlenecks="none",
            suggestions="none",
            perf_metric=100.0,
            perf_unit="tok/s",
        ),
        ProfilerSummary(
            analysis="regression",
            bottlenecks="none",
            suggestions="none",
            perf_metric=20.0,
            perf_unit="tok/s",
        ),
    ]
    result = _invoke_loop(
        tmp_path,
        ref_file,
        _make_runner(profiler_responses=responses, mutator_writes=True),
        max_generations=1,
        children_per_generation=2,
    )

    assert result is True
    best = _load_population(tmp_path).best(MetricSpace())
    assert best is not None and best.perf_metric == 100.0  # noqa: PT018  # tracked: #288
    project = _project_dir(tmp_path)
    assert (project / "mutant_2.py").is_file()
    assert not (project / "mutant_3.py").exists()


def test_failed_child_excluded_from_future_parent_pool(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Bootstrap: pass (the seed). Gen 1: fail (no commit, not eligible as
    parent). Gen 2: must still parent off the seed — never off the failed
    Gen 1 child."""
    # Judge order: bootstrap(pass), gen1(fail), gen2(pass).
    runner = _make_runner(judge_verdicts=["pass", "fail", "pass"])
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=2,
        children_per_generation=1,
    )
    assert result is True
    assert runner.counters["mutator"] == 3
    assert runner.counters["judge"] == 3
    assert runner.counters["profiler"] == 2  # only the passes (seed + gen2)

    pop = _load_population(tmp_path)
    assert len(pop) == 3
    seed, g1, g2 = pop.all
    assert seed.passed is True
    assert g1.passed is False
    assert g1.commit is None
    # The gen-2 child must descend from the seed, NOT from the failed g1
    # (which has no commit and can't be selected).
    assert g2.parent_id == seed.id


def test_accuracy_rejected_child_is_not_profiled_or_selected(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """The framework oracle can overrule an LLM PASS for an offspring."""
    failure = "Framework accuracy gate failed.\nbooking response diverged"
    runner = _make_runner()
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        _accuracy_gate_feedbacks=[None, failure, None],
        max_generations=2,
        children_per_generation=1,
    )

    assert result is True
    assert runner.counters["judge"] == 3
    assert runner.counters["profiler"] == 2
    assert runner.accuracy_gate.call_count == 3

    seed, rejected, accepted = _load_population(tmp_path).all
    assert rejected.passed is False
    assert rejected.commit is None
    assert rejected.perf_metric is None
    assert rejected.feedback == failure
    assert accepted.passed is True
    assert accepted.parent_id == seed.id


# ---------------------------------------------------------------------------
# Mutator prompt content
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pareto / multi-objective mode
# ---------------------------------------------------------------------------


def test_pareto_mode_records_metrics_dict_on_individuals(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """When axes are configured the loop should pass the space through to
    selection AND copy `ProfilerSummary.metrics` onto every passing Individual
    so the frontier can be computed across the run."""
    space = MetricSpace(
        objectives=(
            Objective(name="tput", direction="max"),
            Objective(name="lat_ms", direction="min"),
        )
    )
    profiler_responses = [
        ProfilerSummary(
            analysis="ok",
            bottlenecks="none",
            suggestions="none",
            perf_metric=100.0,
            perf_unit="tput",
            metrics={"tput": 100.0, "lat_ms": 80.0},
        ),
        ProfilerSummary(
            analysis="ok",
            bottlenecks="none",
            suggestions="none",
            perf_metric=80.0,
            perf_unit="tput",
            metrics={"tput": 80.0, "lat_ms": 50.0},
        ),
    ]
    runner = _make_runner(profiler_responses=profiler_responses)
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,  # bootstrap seed + one gen-1 child
        children_per_generation=1,
        space=space,
        frontier_bias=1.0,
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    seed, child = pop.all
    assert seed.metrics == {"tput": 100.0, "lat_ms": 80.0}
    assert child.metrics == {"tput": 80.0, "lat_ms": 50.0}

    # The two individuals trade off — both should be on the frontier.
    front_ids = {i.id for i in pop.frontier(space)}
    assert front_ids == {seed.id, child.id}


def test_pareto_addendum_appears_in_profiler_prompt(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """When Pareto mode is on, the profiler system prompt gets an addendum
    explicitly listing the metric keys to emit. The judge stays unaffected."""
    captured_profiler_prompts: list[str] = []

    def _make_runner_with_profiler_capture():  # noqa: ANN202  # tracked: #288
        runner = _make_runner(
            profiler_responses=[
                ProfilerSummary(
                    analysis="ok",
                    bottlenecks="none",
                    suggestions="none",
                    perf_metric=10.0,
                    perf_unit="tok/s",
                    metrics={"tput": 10.0, "lat_ms": 50.0},
                )
            ],
        )
        original = runner.invoke.side_effect

        def spy(*, kind, response_cls, system_prompt="", **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
            if kind == "profiler":
                captured_profiler_prompts.append(system_prompt)
            return original(
                kind=kind,
                response_cls=response_cls,
                system_prompt=system_prompt,
                **kwargs,
            )

        runner.invoke.side_effect = spy
        return runner

    runner = _make_runner_with_profiler_capture()
    space = MetricSpace(
        objectives=(
            Objective(name="tput", direction="max"),
            Objective(name="lat_ms", direction="min"),
        )
    )
    _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        space=space,
        frontier_bias=1.0,
    )
    assert len(captured_profiler_prompts) == 1
    prompt = captured_profiler_prompts[0]
    assert "Pareto-frontier mode" in prompt
    assert "`tput`" in prompt
    assert "`lat_ms`" in prompt


def test_no_objectives_keeps_metrics_empty_and_legacy_behavior(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """A space with no axes keeps `Individual.metrics` empty even if the
    profiler stub doesn't supply one — preserves the pre-Pareto behavior."""
    runner = _make_runner()
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        # Note: an empty space → single-objective mode.
    )
    assert result is True
    pop = _load_population(tmp_path)
    assert len(pop) == 1
    assert pop.all[0].metrics == {}


def test_second_child_prompt_includes_parent_block(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Gen 1's mutator prompt mentions the parent (bootstrap seed) perf_metric —
    one of the few signals the mutator has to ground its change in fitness."""
    captured: list[str] = []
    runner = _make_runner(capture_mutator_prompts=captured)
    _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
    )
    assert len(captured) == 2  # bootstrap (cold-start) + gen-1 (parent block)
    gen1_prompt = captured[1]
    assert "Bootstrap the first passing seed" not in gen1_prompt
    assert "## Parent" in gen1_prompt
    # The seed's perf_metric (10.0) was emitted by the profiler and should
    # appear in the parent block.
    assert "10.0" in gen1_prompt


def test_generic_domain_prompts_exclude_llm_serving_contracts(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """The evolve loop uses registered domain sections instead of baking the
    LLM-serving contract into its mutator and judge base prompts."""
    mutator_prompts: list[str] = []
    judge_prompts: list[str] = []
    runner = _make_runner(
        capture_mutator_prompts=mutator_prompts,
        capture_judge_prompts=judge_prompts,
    )

    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        domain=DomainName.GENERIC,
        modality=None,
        profiler_kind=ProfilerKind.NONE,
    )

    assert result is True
    assert len(mutator_prompts) == len(judge_prompts) == 1
    combined = "\n".join(mutator_prompts + judge_prompts)
    assert "uv run python accuracy_checker/checker.py" in combined
    assert "uv run python benchmark/benchmark.py" in combined
    assert "Model weights are at `/model`" not in combined
    assert "serving-systems" not in combined
    assert "/health" not in combined
    assert "OpenAI-compatible" not in combined


def test_openevolve_policy_persists_multi_file_search_state(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_runner(mutator_writes=True)

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        domain=DomainName.GENERIC,
        modality=None,
        profiler_kind=ProfilerKind.NONE,
        search_policy="openevolve",
        openevolve_config=OpenEvolveSearchConfig(
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
    state_store = _evolution_state_store(_project_dir(tmp_path))
    state_dir = state_store.namespace.external_directory("openevolve")
    snapshot_dir = state_dir / "snapshots" / (state_dir / "CURRENT").read_text()
    metadata = json.loads((snapshot_dir / "metadata.json").read_text())
    programs = [json.loads(path.read_text()) for path in (snapshot_dir / "programs").glob("*.json")]
    mapped = [program for program in programs if program["id"].startswith("vibesys-")]
    child = next(
        individual for individual in state_store.load_population().all if individual.generation == 1
    )
    assert {program["metadata"]["vibesys_individual_id"] for program in mapped} == {1, 2}
    assert all("diff --git" in program["code"] for program in mapped)
    assert metadata["island_generations"] == [1, 0]
    assert child.policy_parent_id == "vibesys-1"
    assert child.policy_target_island == 0


def test_candidate_code_is_multi_file_but_excludes_framework_state(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
            configuration=_evolution_configuration(),
            trusted_input_baseline=tracker.trusted_input_baseline,
        )
    )
    tracker.snapshot_with_framework_metadata(
        "initialize state",
        project.state.initialization_snapshot("test-evolve"),
    )

    (tmp_path / "src" / "lib.rs").write_text("optimized lib\n")
    (tmp_path / "src" / "ffi.rs").write_text("optimized ffi\n")
    tracker.snapshot("candidate")
    evolve_state = project.state.portable_namespace("test-evolve", "evolve")
    (evolve_state.external_directory() / "population.json").write_text("[]\n")
    tracker.snapshot_framework_state("evolve state", evolve_state.snapshot())
    commit = tracker.current_sha()

    assert commit is not None
    code = _candidate_code(_FakeLoopContext(git=tracker), commit)
    assert "src/lib.rs" in code
    assert "src/ffi.rs" in code
    assert "population.json" not in code


def test_search_policy_initialization_failure_closes_context(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ctx = MagicMock(
        run_log_path=log_dir / "run.log",
        exp_dir=tmp_path,
        log_dir=log_dir,
    )

    with (
        patch.object(evolve_loop, "create_run_context", return_value=ctx),
        patch.object(
            evolve_loop,
            "OpenEvolveSearchPolicy",
            side_effect=ValueError("incompatible saved topology"),
        ),
    ):
        result = run_evolve_loop(
            Config.model_validate({"model": {"name": "test-model"}}),
            "test",
            "input",
            "check",
            "benchmark",
            "objective",
            runs_dir=tmp_path / "exp_env",
            domain=DomainName.GENERIC,
            space=MetricSpace(),
            search_policy="openevolve",
        )

    assert result is False
    ctx.close.assert_called_once_with()


def test_programmatic_openevolve_config_infers_policy(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ctx, state_store = _stateful_context(tmp_path)
    config = OpenEvolveSearchConfig(num_islands=1)

    name, policy = _initialize_search_policy(
        ctx,
        Population(),
        state_store,
        requested=None,
        seed=1,
        config=config,
        space=MetricSpace(),
    )

    assert name.value == "openevolve"
    assert isinstance(policy, OpenEvolveSearchPolicy)
    assert policy.config == config


def test_programmatic_openevolve_config_rejects_vibesys_policy(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ctx, state_store = _stateful_context(tmp_path)

    with pytest.raises(ValueError, match="requires the OpenEvolve search policy"):
        _initialize_search_policy(
            ctx,
            Population(),
            state_store,
            requested="vibesys",
            seed=1,
            config=OpenEvolveSearchConfig(),
            space=MetricSpace(),
        )


# ---------------------------------------------------------------------------
# Helper units: failure lessons, WIP-seed lookup, per-candidate deployment
# ---------------------------------------------------------------------------


def _ind(id_, *, passed=False, parent_id=None, commit=None, feedback=""):  # noqa: ANN001, ANN202  # tracked: #288
    return Individual(
        id=id_,
        generation=1,
        parent_id=parent_id,
        passed=passed,
        commit=commit,
        feedback=feedback,
    )


def test_recent_failure_lessons_dedupes_and_orders_most_recent_first():  # noqa: ANN201  # tracked: #288
    pop = Population()
    pop.add(_ind(1, feedback="crash: CUDA out of memory"))
    pop.add(_ind(2, feedback="crash: CUDA out of memory"))  # duplicate → collapsed
    pop.add(_ind(3, feedback="server never bound to port"))
    pop.add(_ind(4, passed=True, feedback="ignored because it passed"))

    lessons = _recent_failure_lessons(pop, limit=3)
    assert lessons == ["server never bound to port", "crash: CUDA out of memory"]


def test_recent_failure_lessons_truncates_long_feedback():  # noqa: ANN201  # tracked: #288
    pop = Population()
    pop.add(_ind(1, feedback="x" * 5000))
    (lesson,) = _recent_failure_lessons(pop, limit=1, max_chars=100)
    assert lesson.endswith("…")
    assert len(lesson) <= 102  # 100 chars + space + ellipsis


def test_latest_wip_seed_returns_most_recent_failed_seed_with_commit():  # noqa: ANN201  # tracked: #288
    pop = Population()
    pop.add(_ind(1, commit="aaa"))  # failed cold-start seed
    pop.add(_ind(2, commit="bbb"))  # newer failed cold-start seed
    pop.add(_ind(3, passed=True, commit="ccc"))  # passing → not a WIP seed
    pop.add(_ind(4, parent_id=2, commit="ddd"))  # has a parent → not cold-start

    seed = _latest_wip_seed(pop)
    assert seed is not None and seed.id == 2  # noqa: PT018  # tracked: #288


def test_latest_wip_seed_none_when_no_snapshotted_failure():  # noqa: ANN201  # tracked: #288
    pop = Population()
    pop.add(_ind(1, commit=None))  # failed but never snapshotted
    pop.add(_ind(2, passed=True, commit="ccc"))
    assert _latest_wip_seed(pop) is None


def test_candidate_runtime_notes_delegates_deployment_naming_to_environment():  # noqa: ANN201  # tracked: #288
    base = "run-20260720-abcd1234-llama3"
    ctx = _FakeLoopContext(
        run_environment_view=SimpleNamespace(
            deployment_namespace=base,
            prompt_notes=f"Deploy to Modal app {base}; endpoint {base}-web.",
        ),
        run_environment=SimpleNamespace(
            candidate_runtime=lambda view, generation, child_idx: CandidateRuntime(  # noqa: ARG005  # tracked: #288
                prompt_notes="provider-owned candidate instructions",
                deployment_name=f"candidate-{generation}-{child_idx}",
            )
        ),
    )
    notes, app = _candidate_runtime_notes(ctx, generation=3, child_idx=2)
    assert app == "candidate-3-2"
    assert notes == "provider-owned candidate instructions"


def test_candidate_runtime_notes_noop_without_named_deployment():  # noqa: ANN201  # tracked: #288
    notes_in = "Local run; no named deployment."
    ctx = _FakeLoopContext(
        run_environment_view=SimpleNamespace(deployment_namespace=None, prompt_notes=notes_in),
        run_environment=SimpleNamespace(
            candidate_runtime=lambda view, generation, child_idx: CandidateRuntime(  # noqa: ARG005  # tracked: #288
                prompt_notes=view.prompt_notes
            )
        ),
    )
    notes, app = _candidate_runtime_notes(ctx, generation=1, child_idx=1)
    assert app is None
    assert notes == notes_in


# ---------------------------------------------------------------------------
# Candidate-app teardown
# ---------------------------------------------------------------------------


def test_teardown_candidate_deployment_delegates_to_run_environment():  # noqa: ANN201  # tracked: #288
    """The loop stays backend-agnostic: it hands the deployment name to the run
    environment, which decides how to release it."""
    run_env = MagicMock()
    ctx = _FakeLoopContext(run_environment=run_env)

    _teardown_candidate_deployment(ctx, "vibesys-run-g1c2", keep=False)

    run_env.teardown_deployment.assert_called_once_with("vibesys-run-g1c2", log=ctx.lprint)


def test_teardown_candidate_deployment_noop_when_kept_or_absent():  # noqa: ANN201  # tracked: #288
    run_env = MagicMock()
    ctx = _FakeLoopContext(run_environment=run_env)

    # Opt-out: keep the app for post-hoc inspection.
    _teardown_candidate_deployment(ctx, "vibesys-run-g1c2", keep=True)
    # No per-candidate deployment (non-Modal env).
    _teardown_candidate_deployment(ctx, None, keep=False)

    run_env.teardown_deployment.assert_not_called()


# ---------------------------------------------------------------------------
# Parallel generation orchestration
# ---------------------------------------------------------------------------


def _passing_seed(commit: str = "seedsha", perf: float = 1.0) -> Individual:
    return Individual(
        id=1,
        generation=0,
        parent_id=None,
        commit=commit,
        perf_metric=perf,
        perf_unit="tok/s",
        metrics={"aggregate_throughput": perf},
        passed=True,
        summary="seed",
    )


def _stateful_context(
    tmp_path: Path,
    log=None,  # noqa: ANN001  # tracked: #288
) -> tuple[_FakeLoopContext, EvolutionStateStore]:
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "test",
        run_id="run-1",
        branch="vibesys/run-1",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        configuration=_evolution_configuration(),
    )
    project.state.create_run(run)
    git = MagicMock(history_root=project.root, run_id=run.run_id)
    state = RunState(project, git, run.run_id)
    ctx = _FakeLoopContext(git=git, state=state, log=log or _discard_log)
    return ctx, EvolutionStateStore(state.portable(RunStateNamespace.EVOLVE))


def test_plan_candidate_falls_back_to_latest_passer_then_none(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ctx, state_store = _stateful_context(tmp_path)
    rng = random.Random(0)  # noqa: S311  # tracked: #288

    # No passers at all → None (candidate skipped).
    empty = Population([])
    assert (
        _plan_candidate(
            ctx,
            empty,
            state_store,
            rng,
            k_top_inspirations=1,
            k_random_inspirations=1,
            selection_temperature=0.5,
            space=MetricSpace(),
            frontier_bias=0.7,
        )
        is None
    )

    # A passer exists → returned as parent.
    pop = Population([_passing_seed()])
    plan = _plan_candidate(
        ctx,
        pop,
        state_store,
        rng,
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        space=MetricSpace(),
        frontier_bias=0.7,
    )
    assert plan is not None
    assert plan.parent.id == 1


def test_run_generation_parallel_bounds_concurrency_and_records_all(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    """Candidates run concurrently up to the cap; every result is recorded once,
    in child order, on the orchestrator thread (population never races)."""
    population = Population([_passing_seed()])
    logs: list[str] = []
    ctx, state_store = _stateful_context(tmp_path, logs.append)

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_eval(parent_ctx, *, generation, child_idx, parent, inspirations, **_kw):  # noqa: ANN001, ANN003, ANN202, ARG001  # tracked: #288
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return _CandidateOutcome(
            passed=True,
            parent_id=parent.id,
            inspiration_ids=[i.id for i in inspirations],
            summary=f"cand-{child_idx}",
            feedback="",
            commit=f"child-{child_idx}",
            perf_metric=float(child_idx),
            perf_unit="tok/s",
            metrics={"aggregate_throughput": float(child_idx)},
        )

    monkeypatch.setattr(evolve_loop, "_evaluate_in_subcontext", fake_eval)

    _run_generation_parallel(
        ctx,
        config=Config.model_validate({"model": {"name": "m"}}),
        agent_backend=None,
        cli_provider=None,
        max_parallelism=2,
        generation=1,
        children_per_generation=5,
        population=population,
        state_store=state_store,
        rng=random.Random(0),  # noqa: S311  # tracked: #288
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        objective="obj",
        space=MetricSpace(),
        frontier_bias=0.7,
        modality="text_generation",
        domain_definition=_LLM_SERVING_DOMAIN,
        pass_criteria="crit",  # noqa: S106  # tracked: #288
        keep_deployments=False,
        search_policy=VibeSysSearchPolicy(),
    )

    # Cap respected, never exceeded.
    assert peak <= 2
    # All 5 children recorded (ids 2..6) plus the seed.
    assert len(population) == 6
    recorded = [i for i in population.all if i.generation == 1]
    assert len(recorded) == 5
    assert {i.commit for i in recorded} == {f"child-{c}" for c in range(1, 6)}
    assert state_store.load_population().all == population.all


def test_run_generation_parallel_skips_parent_without_commit(tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    """A parent with no commit can't be isolated into a worktree → skipped, not
    dispatched."""
    seed = _passing_seed()
    seed.commit = None  # passer but nothing to branch from
    # ``passed`` requires a commit, so this seed won't be selectable; give the
    # planner a stub that returns it anyway to exercise the guard.
    population = Population([_passing_seed(), seed])
    ctx, state_store = _stateful_context(tmp_path)

    monkeypatch.setattr(
        evolve_loop,
        "_plan_candidate",
        lambda *a, **k: SearchSelection(parent=seed, inspirations=[]),  # noqa: ARG005  # tracked: #288
    )
    called = False

    def fake_eval(*a, **k):  # noqa: ANN002, ANN003, ANN202, ARG001  # tracked: #288
        nonlocal called
        called = True
        return _CandidateOutcome(True, seed.id, [], "s", "")  # noqa: FBT003  # tracked: #288

    monkeypatch.setattr(evolve_loop, "_evaluate_in_subcontext", fake_eval)

    _run_generation_parallel(
        ctx,
        config=Config.model_validate({"model": {"name": "m"}}),
        agent_backend=None,
        cli_provider=None,
        max_parallelism=2,
        generation=1,
        children_per_generation=2,
        population=population,
        state_store=state_store,
        rng=random.Random(0),  # noqa: S311  # tracked: #288
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        objective="obj",
        space=MetricSpace(),
        frontier_bias=0.7,
        modality="text_generation",
        domain_definition=_LLM_SERVING_DOMAIN,
        pass_criteria="crit",  # noqa: S106  # tracked: #288
        keep_deployments=False,
        search_policy=VibeSysSearchPolicy(),
    )

    assert called is False  # commit-less parent never dispatched
    assert all(i.generation == 0 for i in population.all)  # nothing recorded


# ---------------------------------------------------------------------------
# Isolated sub-context evaluation (worktree + own logger/agent-runner)
# ---------------------------------------------------------------------------


def test_evaluate_in_subcontext_skips_parent_without_commit():  # noqa: ANN201  # tracked: #288
    """A parent with no commit can't seed a worktree — folded into a failed
    outcome without ever building a sub-context."""
    parent_ctx = _FakeLoopContext(log=lambda _line: None)
    parentless = Individual(id=3, generation=1, parent_id=1, commit=None, passed=True, summary="x")

    seen = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        outcome = _evaluate_in_subcontext(
            parent_ctx,
            config=Config.model_validate({"model": {"name": "m"}}),
            agent_backend=None,
            cli_provider=None,
            generation=2,
            child_idx=1,
            parent=parentless,
            inspirations=[],
            objective="obj",
            space=MetricSpace(),
            modality="text_generation",
            domain_definition=_LLM_SERVING_DOMAIN,
            pass_criteria="crit",  # noqa: S106  # tracked: #288
            keep_deployments=False,
            policy_parent_id=None,
            target_island=None,
            worktree_lock=threading.Lock(),
        )
    finally:
        unsubscribe()

    assert outcome.passed is False
    assert outcome.parent_id == 3
    assert "no parent commit" in outcome.summary
    warnings = [e.data.summary for e in seen if isinstance(e.data, FrameworkWarningData)]
    assert any("no parent commit" in summary for summary in warnings)


def test_evaluate_in_subcontext_builds_worktree_and_evaluates(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """End-to-end: a real parent context spawns an isolated candidate sub-context
    (git worktree at the parent commit + its own logger/agent-runner), evaluates
    it, and the offspring commit lands in the parent's shared object store."""
    runner = _make_runner(mutator_writes=True)
    with (
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.backends.cuda.make_local_shell_sandbox"),
        patch("vibesys.context.build_agent_client", return_value=runner),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        patch("vibesys.loops.evolve.loop._run_framework_accuracy_gate", return_value=None),
        create_run_context(
            config=Config.model_validate({"model": {"name": "claude-sonnet-4-6"}}),
            exp_name="test-parallel-subctx",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=LLMServingEnvironmentHooks(),
            project_configuration=EvolveRunConfiguration(
                outer_loop="evolve",
                run_environment=RunEnvironmentRecord(name="local"),
                agent_backend="deepagents",
                compute_backend="cuda",
                max_generations=1,
                children_per_generation=1,
                k_top_inspirations=1,
                k_random_inspirations=1,
                selection_temperature=0.5,
                frontier_bias=0.7,
                bootstrap_max_attempts=1,
                keep_deployments=False,
                max_parallelism=1,
            ),
        ) as parent,
    ):
        base_commit = parent.git.current_sha()
        assert base_commit is not None
        parent_ind = Individual(
            id=1,
            generation=0,
            parent_id=None,
            commit=base_commit,
            perf_metric=1.0,
            perf_unit="tok/s",
            passed=True,
            summary="seed",
        )

        outcome = _evaluate_in_subcontext(
            parent,
            config=Config.model_validate({"model": {"name": "claude-sonnet-4-6"}}),
            agent_backend=None,
            cli_provider=None,
            generation=1,
            child_idx=1,
            parent=parent_ind,
            inspirations=[],
            objective="Maximize tok/s throughput.",
            space=MetricSpace(),
            modality="text_generation",
            domain_definition=_LLM_SERVING_DOMAIN,
            pass_criteria="be faster",  # noqa: S106  # tracked: #288
            keep_deployments=False,
            policy_parent_id=None,
            target_island=None,
            worktree_lock=threading.Lock(),
        )

        assert outcome.passed is True
        assert outcome.parent_id == 1
        assert outcome.perf_metric == 10.0
        assert outcome.commit and outcome.commit != base_commit  # noqa: PT018  # tracked: #288
        # The offspring commit is reachable from the parent repo — the worktree
        # shared its object store, so the candidate joins the one lineage.
        assert (
            parent.git.run(["git", "cat-file", "-e", outcome.commit], check=False).returncode == 0
        )
        # The candidate's worktree is torn down when its sub-context closes.
        cand_ws = parent.project.state.candidate_worktree_directory(parent.run_id, "g1c1")
        assert not cand_ws.exists()
        retained = f"refs/vibesys/{parent.run_id}/candidates/g1c1"
        resolved = parent.git.run(["git", "rev-parse", retained])
        assert resolved.stdout.decode().strip() == outcome.commit


def test_max_parallelism_ignored_without_environment_capability(tmp_path, ref_file, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
    """An environment without isolated evaluation support stays serial."""
    called = {"parallel": False}
    monkeypatch.setattr(
        evolve_loop,
        "_run_generation_parallel",
        lambda *a, **k: called.__setitem__("parallel", True),  # noqa: ARG005, FBT003  # tracked: #288
    )
    runner = _make_runner(judge_verdicts=["pass", "pass", "pass"])
    _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
        max_parallelism=4,  # local env → must downgrade to serial
    )
    assert called["parallel"] is False
    pop = _load_population(tmp_path)
    assert len(pop) == 2  # bootstrap seed + one serial gen-1 candidate


def test_loop_tears_down_candidate_on_pass_and_fail_paths(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Teardown fires exactly once per candidate on every exit path — the
    fails the judge."""
    # bootstrap passes (1 attempt), gen-1 candidate passes, gen-2 candidate fails.
    runner = _make_runner(judge_verdicts=["pass", "pass", "fail"])
    with patch("vibesys.loops.evolve.loop._teardown_candidate_deployment") as teardown:
        _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            max_generations=2,
            children_per_generation=1,
        )

    # 1 bootstrap attempt + 2 generation candidates = 3 teardown calls.
    assert teardown.call_count == 3


# ---------------------------------------------------------------------------
# Declared benchmark result contract (framework-owned fitness)
# ---------------------------------------------------------------------------


def _passing_gate_result(  # noqa: ANN202
    metric_value: float,
    *,
    unit: str | None = None,
    row: dict[str, float] | None = None,
):
    from vibesys.loops.gates import (  # noqa: PLC0415  # tracked: #288
        BenchmarkGateResult,
        FrameworkBenchmarkOutcome,
    )

    return BenchmarkGateResult(
        command="trusted-benchmark --json /tmp/result.json",
        output="ok",
        executed=True,
        outcome=FrameworkBenchmarkOutcome(
            metric_name="total_ops_per_sec",
            metric_value=metric_value,
            metric_direction="max",
            metric_unit=unit,
            row=row,
        ),
    )


def test_benchmark_gate_extends_timeout_by_environment_setup_allowance():  # noqa: ANN201  # tracked: #288
    """Environment-owned deployment/readiness time must not eat the benchmark
    command's declared budget: the evolve gate forwards setup + contract, the
    same setup-aware policy the agent path uses."""
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288
    from vibesys.loops.gates import BenchmarkContract  # noqa: PLC0415  # tracked: #288

    ctx = _FakeLoopContext(
        run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=90),
        events=MagicMock(),
    )
    contract = BenchmarkContract(
        result_spec=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
        timeout_seconds=120,
    )
    gate = MagicMock(return_value=_passing_gate_result(1.0))
    with patch("vibesys.loops.evolve.loop.run_benchmark_gate", gate):
        _run_framework_benchmark_gate(
            ctx,
            generation=0,
            child_idx=0,
            contract=contract,
            space=MetricSpace(),
        )

    # 120 (contract budget) + 90 (environment setup allowance), not the bare 120.
    assert gate.call_args.kwargs["timeout_seconds"] == 210


def test_benchmark_gate_timeout_unchanged_without_setup_allowance():  # noqa: ANN201  # tracked: #288
    """With no setup allowance the forwarded budget is exactly the contract's."""
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288
    from vibesys.loops.gates import BenchmarkContract  # noqa: PLC0415  # tracked: #288

    ctx = _FakeLoopContext(
        run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=0),
        events=MagicMock(),
    )
    contract = BenchmarkContract(
        result_spec=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
        timeout_seconds=120,
    )
    gate = MagicMock(return_value=_passing_gate_result(1.0))
    with patch("vibesys.loops.evolve.loop.run_benchmark_gate", gate):
        _run_framework_benchmark_gate(
            ctx,
            generation=0,
            child_idx=0,
            contract=contract,
            space=MetricSpace(),
        )

    assert gate.call_args.kwargs["timeout_seconds"] == 120


def test_benchmark_contract_owns_seed_and_child_fitness(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """A declared benchmark result contract, not the profiler agent's
    self-report, records every candidate's fitness."""
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288

    runner = _make_runner()
    gate = MagicMock(side_effect=[_passing_gate_result(42.5), _passing_gate_result(43.75)])
    with patch("vibesys.loops.evolve.loop.run_benchmark_gate", gate):
        result = _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            max_generations=1,
            children_per_generation=1,
            benchmark_result=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
        )

    assert result is True
    assert gate.call_count == 2
    assert gate.call_args.kwargs["result_spec"].metric == "total_ops_per_sec"
    pop = _load_population(tmp_path)
    assert [item.perf_metric for item in pop.all] == [42.5, 43.75]
    # The scalar contract declares a metric name, not a unit, so the recorded
    # unit stays the profiler's. A metric name is not a unit.
    assert {item.perf_unit for item in pop.all} == {"tok/s"}
    assert pop.all[0].metrics == {"total_ops_per_sec": 42.5}
    # The profiler still ran for diagnostics; its self-report was not recorded.
    assert runner.counters["profiler"] == 2


def test_benchmark_contract_failure_fails_the_candidate_before_profiling(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288
    from vibesys.loops.gates import (  # noqa: PLC0415  # tracked: #288
        BenchmarkGateResult,
        FrameworkBenchmarkOutcome,
    )

    runner = _make_runner()
    failing = BenchmarkGateResult(
        command="trusted-benchmark --json /tmp/result.json",
        output="benchmark exploded",
        executed=True,
        outcome=FrameworkBenchmarkOutcome(
            feedback="Framework benchmark failed.\nbenchmark exploded"
        ),
    )
    with patch("vibesys.loops.evolve.loop.run_benchmark_gate", MagicMock(return_value=failing)):
        result = _invoke_bootstrap(
            tmp_path,
            ref_file,
            runner,
            benchmark_result=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
            bootstrap_max_attempts=1,
        )

    assert result is False
    assert runner.counters["profiler"] == 0
    pop = _load_population(tmp_path)
    assert len(pop) == 1
    failed = pop.all[0]
    assert failed.passed is False
    assert "Framework benchmark failed." in (failed.feedback or "")


def test_no_benchmark_contract_keeps_profiler_fitness(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_runner()
    gate = MagicMock()
    with patch("vibesys.loops.evolve.loop.run_benchmark_gate", gate):
        result = _invoke_bootstrap(tmp_path, ref_file, runner)

    assert result is True
    gate.assert_not_called()
    seed = _load_population(tmp_path).all[0]
    assert seed.perf_metric == 10.0
    assert seed.perf_unit == "tok/s"


def test_scalar_contract_keeps_the_profilers_other_axes_on_the_frontier(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Regression: a one-metric contract must not empty a two-axis frontier.

    ``Population.frontier`` keeps only individuals carrying a value for every
    configured axis. The scalar result contract reports one number, so writing
    the trusted row *over* the profiler's row left every individual missing the
    second axis and the frontier came back empty, which in turn starves Pareto
    parent selection. The trusted row now overrides the axes it measures and
    leaves the rest of the profiler's row in place.
    """
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288

    space = MetricSpace(
        objectives=(
            Objective(name="total_ops_per_sec", direction="max"),
            Objective(name="p99_latency_ns", direction="min"),
        )
    )
    profiler_responses = [
        ProfilerSummary(
            analysis="ok",
            bottlenecks="none",
            suggestions="none",
            perf_metric=100.0,
            perf_unit="ops/s",
            metrics={"total_ops_per_sec": 100.0, "p99_latency_ns": 500.0},
        ),
        ProfilerSummary(
            analysis="ok",
            bottlenecks="none",
            suggestions="none",
            perf_metric=80.0,
            perf_unit="ops/s",
            metrics={"total_ops_per_sec": 80.0, "p99_latency_ns": 800.0},
        ),
    ]
    runner = _make_runner(profiler_responses=profiler_responses)
    gate = MagicMock(side_effect=[_passing_gate_result(42.5), _passing_gate_result(43.75)])
    with patch("vibesys.loops.evolve.loop.run_benchmark_gate", gate):
        result = _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            max_generations=1,
            children_per_generation=1,
            space=space,
            frontier_bias=1.0,
            benchmark_result=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
        )

    assert result is True
    pop = _load_population(tmp_path)
    seed, child = pop.all
    # The contract owns the axis it measures; the profiler keeps the other.
    assert seed.metrics == {"total_ops_per_sec": 42.5, "p99_latency_ns": 500.0}
    assert child.metrics == {"total_ops_per_sec": 43.75, "p99_latency_ns": 800.0}
    # The two trade off on the second axis, so neither dominates and both are
    # on the frontier. Before the fix neither carried `p99_latency_ns` at all
    # and the frontier was empty.
    assert {item.id for item in pop.frontier(space)} == {seed.id, child.id}


def test_protocol_contract_records_the_evaluator_declared_unit(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """The recorded unit is the evaluator's declaration when it supplies one."""
    runner = _make_runner()
    gate = MagicMock(return_value=_passing_gate_result(42.5, unit="ops/s"))
    with patch("vibesys.loops.evolve.loop.run_benchmark_gate", gate):
        result = _invoke_bootstrap(
            tmp_path,
            ref_file,
            runner,
            benchmark_result_protocol=2,
        )

    assert result is True
    seed = _load_population(tmp_path).all[0]
    assert seed.perf_metric == 42.5
    assert seed.perf_unit == "ops/s"


def test_evolve_accuracy_gate_extends_timeout_by_environment_setup_allowance():  # noqa: ANN201  # tracked: #288
    """The accuracy gate charges environment setup the way the benchmark gate
    does; otherwise a Modal/SkyPilot deployment eats the accuracy command's
    declared budget and the candidate fails on a timeout it was never given
    the time to avoid."""
    ctx = _FakeLoopContext(
        run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=90),
    )
    gate = MagicMock(return_value=SimpleNamespace(feedback=None))
    with patch("vibesys.loops.evolve.loop.run_accuracy_gate", gate):
        evolve_loop._run_framework_accuracy_gate(  # noqa: SLF001  # tracked: #288
            ctx,
            generation=0,
            child_idx=0,
            timeout_seconds=120,
        )

    assert gate.call_args.kwargs["timeout_seconds"] == 210
