"""Integration tests for the evolutionary search loop.

Mocks the agent runner so the LLM-driven mutator/judge/profiler return
scripted responses. The public run context is built on a tmp_path
workspace (so git tracking, snapshots, and population persistence are
exercised end-to-end), but the model + sandbox + agent-runner factories
are patched out, the same pattern as ``tests/vibesys/loops/multi/test_orchestrate.py``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, Unpack, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.support.run_execution import run_execution_record

from vibesys.config import Config, as_config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend, DomainName
from vibesys.domains.registry import resolve_domain
from vibesys.evaluators.gates import (
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
    framework_command_timeout,
)
from vibesys.evaluators.input_manifest import BenchmarkResult, load_input_bundle
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.events import FrameworkWarningData
from vibesys.loops.evolve.entrypoint import EvolveOrchestrator
from vibesys.loops.evolve.loop import (
    _candidate_code,
    _candidate_runtime_notes,
    _evaluate_in_subcontext,
    _initialize_search_policy,
    _latest_wip_seed,
    _recent_failure_lessons,
    _teardown_candidate_deployment,
)
from vibesys.loops.evolve.orchestration import EvolveOptions, descriptor_from_options
from vibesys.loops.evolve.population import (
    Individual,
    Population,
)
from vibesys.loops.evolve.run import EvolveRun
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    OpenEvolveSearchPolicy,
)
from vibesys.loops.evolve.state import EvolutionStateStore
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runner import run_orchestration
from vibesys.profilers import ProfilerKind
from vibesys.render.sink import output_sink
from vibesys.roles.common import Verdict
from vibesys.roles.judge import JudgeResponse
from vibesys.run import EventJournal, GitTracker, RunState, RunStateNamespace
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.integration import LocalRunIntegration
from vibesys.sandbox.run_environment import CandidateRuntime, RunEnvironmentSpec
from vs_agent.api.testing import FakeAgentClient, FakeInvocation
from vs_project.api import (
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    RunEnvironmentRecord,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.evaluators.input_manifest import WorkspaceSource
    from vibesys.loops.evolve.search_policy import SearchPolicyName
    from vibesys.orchestration.runtime import RunContext
    from vibesys.run import RepositoryVisibility

_LLM_SERVING_DOMAIN = resolve_domain(DomainName.LLM_SERVING)


class _EvolveLoopKwargs(TypedDict, total=False):
    """Overrides accepted by the canonical evolve request fixture.

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


class _CandidateEnvironment(Protocol):
    def candidate_runtime(
        self, view: object, generation: int, child_idx: int
    ) -> CandidateRuntime: ...

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> object: ...


class _FakeRunContextOptions(TypedDict, total=False):
    git: GitTracker
    state: RunState
    run_environment: _CandidateEnvironment
    run_environment_view: object
    events: EventJournal
    log: Callable[[str], None]


class _FakeRunContext:
    """A small host-capability fake for evolve's focused helper assertions."""

    def __init__(self, **options: Unpack[_FakeRunContextOptions]) -> None:
        git = options.get("git")
        state = options.get("state")
        run_environment = options.get("run_environment")
        run_environment_view = options.get("run_environment_view")
        events = options.get("events")
        log = options.get("log", _discard_log)
        if events is not None:
            self.events = events
        if git is not None:
            self.git = git
            self.workspaces = SimpleNamespace(
                root=SimpleNamespace(candidate_patch=AsyncMock(side_effect=git.candidate_patch))
            )
        if state is not None:
            self.state = state
        if run_environment is not None:
            self.run_environment = run_environment
        if run_environment_view is not None:
            self.run_environment_view = run_environment_view
        if run_environment is not None:
            active_environment = run_environment

            def candidate_runtime(
                generation: int, child_idx: int, *, scope: object | None = None
            ) -> CandidateRuntime:
                assert scope is None
                return active_environment.candidate_runtime(
                    self.run_environment_view, generation, child_idx
                )

            self.environment = SimpleNamespace(
                candidate_runtime=candidate_runtime,
                teardown_deployment=AsyncMock(
                    side_effect=lambda name: active_environment.teardown_deployment(name, log=log)
                ),
            )
        self._log = log
        self.judge_accuracy_command: str | None = None
        self.judge_benchmark_command: str | None = None
        self.judge_backend = MagicMock()

    def log(self, text: str) -> None:
        """Record a log line the same way the real context would emit it."""
        self._log(text)

    def trusted_input_changes(self) -> list[str]:
        return []


def _as_run_context(fake: _FakeRunContext) -> RunContext:
    """Type the focused capability fake as the host accepted by evolve helpers."""
    return cast("RunContext", fake)


class _SharedFakeClient:
    """Give each spawned handle independent close ownership over one script."""

    def __init__(self, scripted: FakeAgentClient) -> None:
        self._scripted = scripted

    def __getattr__(self, name: str) -> object:
        return getattr(self._scripted, name)

    def close(self) -> None:
        """The scripted client remains available to other role handles."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ref_file(tmp_path: Path) -> str:
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


def _judge_response(verdict: Literal["pass", "fail"]) -> JudgeResponse:
    """The default judge verdict for a scripted round."""
    return JudgeResponse(
        analysis="ok",
        feedback="" if verdict == "pass" else "needs work",
        verdict=Verdict.PASS if verdict == "pass" else Verdict.FAIL,
    )


def _profiler_response(perf_metric: float, *, perf_unit: str = "tok/s") -> ProfilerSummary:
    return ProfilerSummary(
        analysis="ok",
        bottlenecks="none",
        suggestions="none",
        perf_metric=perf_metric,
        perf_unit=perf_unit,
    )


def _default_profiler_responses(n: int, *, start: float = 10.0) -> list[ProfilerSummary]:
    """Perf starts at 10.0 tok/s and increments by 1 per profiled candidate,
    so each gets a distinct fitness."""
    return [_profiler_response(start + i) for i in range(n)]


def _mutator_writes_callback(fake: FakeAgentClient) -> Callable[[FakeInvocation], None]:
    """Build an ``on_invoke`` callback that simulates a real mutator edit.

    Without a file change the cold-start snapshot is a no-op and no commit is
    recorded, so tests exercising WIP-seed/commit behavior need the workspace
    to actually change on every mutator (``kind="implementer"``) call.
    """

    def _write(call: FakeInvocation) -> None:
        if call.kind != "implementer":
            return
        n = len(fake.calls_for("implementer"))
        (call.workspace / f"mutant_{n}.py").write_text(f"# mutant {n}\n")

    return _write


def _invoke_loop(
    tmp_path: Path,
    ref_file: str,
    runner: FakeAgentClient,
    *,
    accuracy_gate: MagicMock | None = None,
    _accuracy_gate_feedbacks: list[str | None] | None = None,
    **kwargs: Unpack[_EvolveLoopKwargs],
) -> bool:
    """Shared plumbing — patch context globals, run the loop, return result.

    ``accuracy_gate`` patches in for the real framework accuracy gate
    (``vibesys.loops.evolve.loop._run_framework_accuracy_gate``); it is never
    read off the agent client. Pass a pre-built ``MagicMock`` when the caller
    needs to inspect its calls afterward; otherwise one is built here from
    ``_accuracy_gate_feedbacks`` (or accepts everything by default).
    """
    if accuracy_gate is None:
        accuracy_gate = MagicMock(return_value=None)
        if _accuracy_gate_feedbacks is not None:
            accuracy_gate.side_effect = list(_accuracy_gate_feedbacks)
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
    config = as_config(defaults["config"])
    bundle = load_input_bundle(Path(defaults["input_path"]))
    accuracy_command = tuple(shlex.split(defaults["accuracy_command"]))
    benchmark_command = tuple(shlex.split(defaults["benchmark_command"]))
    manifest = bundle.manifest.model_copy(
        update={
            "agent": bundle.manifest.agent.model_copy(
                update={"domain": defaults.get("domain", bundle.domain)}
            ),
            "accuracy": bundle.manifest.accuracy.model_copy(
                update={
                    "command": accuracy_command,
                    "timeout_seconds": defaults.get("accuracy_timeout_seconds"),
                }
            ),
            "benchmark": bundle.manifest.benchmark.model_copy(
                update={
                    "command": benchmark_command,
                    "timeout_seconds": defaults.get("benchmark_timeout_seconds"),
                    "result": defaults.get("benchmark_result"),
                    "result_protocol": defaults.get("benchmark_result_protocol"),
                }
            ),
        }
    )
    bundle = bundle.model_copy(
        update={
            "manifest": manifest,
            "resolved_accuracy_command": accuracy_command,
            "resolved_benchmark_command": benchmark_command,
        }
    )
    backend = defaults.get("backend", DEFAULT_COMPUTE_BACKEND)
    profiler = defaults.get("profiler_kind", ProfilerKind.AUTO)
    openevolve = defaults.get("openevolve_config")
    modality = defaults.get("modality")
    if modality is None and bundle.domain is DomainName.LLM_SERVING:
        modality = "text_generation"
    options = EvolveOptions(
        modality=modality,
        max_generations=defaults["max_generations"],
        children_per_generation=defaults["children_per_generation"],
        k_top_inspirations=defaults.get("k_top_inspirations", 2),
        k_random_inspirations=defaults.get("k_random_inspirations", 2),
        selection_temperature=defaults.get("selection_temperature", 0.5),
        seed=defaults["seed"],
        search_policy=cast(
            'Literal["vibesys", "openevolve"] | None',
            str(defaults["search_policy"]) if defaults.get("search_policy") is not None else None,
        ),
        openevolve_population_size=openevolve.population_size if openevolve else None,
        openevolve_archive_size=openevolve.archive_size if openevolve else None,
        openevolve_num_islands=openevolve.num_islands if openevolve else None,
        openevolve_migration_interval=openevolve.migration_interval if openevolve else None,
        openevolve_migration_rate=openevolve.migration_rate if openevolve else None,
        frontier_bias=defaults.get("frontier_bias", 0.7),
        bootstrap_max_attempts=defaults.get("bootstrap_max_attempts", 5),
        keep_deployments=defaults.get("keep_deployments", False),
        max_parallelism=defaults.get("max_parallelism", 1),
        metric_space=defaults["space"],
    )
    descriptor = descriptor_from_options(options)
    request = RunRequest(
        project_root=bundle.root,
        orchestration=descriptor,
        config=config,
        input_bundle=bundle,
        objective=defaults["objective"],
        exp_name=defaults["exp_name"],
        runs_dir=defaults["runs_dir"],
        resume=ResumeRef(run_id=defaults["exp_name"]) if defaults.get("existing") else None,
        debug=defaults.get("debug", False),
        profiler_kind=profiler,
        skills_dirs=defaults.get("skills_dirs"),
        run_environment=defaults.get("run_environment"),
        agent_backend=defaults.get("agent_backend"),
        cli_provider=defaults.get("cli_provider"),
        backend=backend,
    )

    async def execute() -> bool:
        integration = LocalRunIntegration()
        try:
            return await run_orchestration(request, integration, EvolveOrchestrator(descriptor))
        finally:
            integration.close()

    with (
        patch("vibesys.backends.cuda.make_local_shell_sandbox"),
        patch(
            "vibesys.orchestration.runtime.build_agent_client",
            side_effect=lambda **_kwargs: _SharedFakeClient(runner),
        ),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        patch(
            "vibesys.loops.evolve.loop._run_framework_accuracy_gate",
            AsyncMock(side_effect=accuracy_gate),
        ),
    ):
        return asyncio.run(execute())


def _invoke_bootstrap(
    tmp_path: Path,
    ref_file: str,
    runner: FakeAgentClient,
    *,
    accuracy_gate: MagicMock | None = None,
    _accuracy_gate_feedbacks: list[str | None] | None = None,
    **kwargs: Unpack[_EvolveLoopKwargs],
) -> bool:
    """Exercise bootstrap through a valid one-generation run contract."""
    overrides: _EvolveLoopKwargs = {"max_generations": 1}
    overrides.update(kwargs)
    with patch("vibesys.loops.evolve.run.EvolveRun._sample_candidate", return_value=None):
        return _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            accuracy_gate=accuracy_gate,
            _accuracy_gate_feedbacks=_accuracy_gate_feedbacks,
            **overrides,
        )


def _load_population(tmp_path: Path) -> Population:
    """Load the canonical portable population for the single test run."""
    return _evolution_state_store(_project_dir(tmp_path)).load_population()


def _project_dir(tmp_path: Path) -> Path:
    projects = [path for path in (tmp_path / "exp_env").iterdir() if path.is_dir()]
    assert len(projects) == 1, projects
    return projects[0]


def _evolution_descriptor() -> OrchestrationDescriptor:
    return OrchestrationDescriptor(id="evolve", config_version=1, options={})


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


def test_bootstrap_succeeds_first_try(tmp_path: Path, ref_file: str) -> None:
    """Bootstrap produces the first passing implementation as a generation-0
    seed with parent_id=None and a perf_metric from the profiler."""
    runner = FakeAgentClient().enqueue("profiler", *_default_profiler_responses(1))
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


def test_bootstrap_fails_all_attempts_returns_false(tmp_path: Path, ref_file: str) -> None:
    """When every bootstrap attempt fails the judge, the run aborts before the
    generation loop and returns False. Failed attempts are never profiled, and
    with no mutator edits they record no commit."""
    runner = FakeAgentClient().enqueue("judge", _judge_response("fail"), _judge_response("fail"))
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=2,
    )
    assert result is False
    assert len(runner.calls_for("implementer")) == 2
    assert len(runner.calls_for("judge")) == 2
    assert len(runner.calls_for("profiler")) == 0  # judged-fail attempts skip profiling

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    for ind in pop.all:
        assert ind.passed is False
        assert ind.generation == 0
        assert ind.commit is None  # no edits → no WIP snapshot
    assert "needs work" in pop.all[0].feedback


def test_bootstrap_failed_attempt_records_wip_seed_commit(tmp_path: Path, ref_file: str) -> None:
    """A failed bootstrap attempt whose mutator actually edited the workspace is
    snapshotted to a WIP commit, so a later attempt can repair it in place."""
    runner = FakeAgentClient().enqueue("judge", _judge_response("fail"))
    runner.on_invoke(_mutator_writes_callback(runner))
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


def test_bootstrap_repairs_wip_seed_across_attempts(tmp_path: Path, ref_file: str) -> None:
    """A second bootstrap attempt fix-forwards from the most-recent WIP seed:
    it checks that commit out and mutates on top, yielding a fresh WIP commit
    distinct from the first."""
    runner = FakeAgentClient().enqueue("judge", _judge_response("fail"), _judge_response("fail"))
    runner.on_invoke(_mutator_writes_callback(runner))
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
    assert first.passed is False
    assert second.passed is False
    assert first.generation == 0
    assert second.generation == 0
    assert first.parent_id is None
    assert second.parent_id is None
    # Both attempts snapshotted their (distinct) trees.
    assert first.commit
    assert second.commit
    assert first.commit != second.commit


def test_bootstrap_succeeds_after_repair(tmp_path: Path, ref_file: str) -> None:
    """Bootstrap that fails once then passes: the failed attempt is snapshotted,
    the passing attempt repairs it in place and becomes the gen-0 seed. Only the
    passing attempt is profiled."""
    runner = FakeAgentClient().enqueue("judge", _judge_response("fail"), _judge_response("pass"))
    runner.on_invoke(_mutator_writes_callback(runner))
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=3,
    )
    assert result is True
    assert len(runner.calls_for("profiler")) == 1  # only the passing attempt profiled

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    failed, seed = pop.all
    assert failed.passed is False
    assert failed.commit
    assert seed.passed is True
    assert seed.generation == 0
    assert seed.parent_id is None
    # The passing seed built on the repaired WIP tree → distinct commit.
    assert seed.commit
    assert seed.commit != failed.commit


def test_bootstrap_repairs_after_framework_accuracy_failure(tmp_path: Path, ref_file: str) -> None:
    """An LLM-approved seed still fails when the trusted oracle rejects it.

    The failed seed is never profiled, its oracle feedback is retained for the
    repair attempt, and the configured timeout reaches the framework gate.
    """
    failure = "Framework accuracy gate failed.\nstatus endpoint diverged"
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    accuracy_gate = MagicMock(side_effect=[failure, None])
    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        accuracy_gate=accuracy_gate,
        accuracy_timeout_seconds=37,
        bootstrap_max_attempts=2,
    )

    assert result is True
    assert len(runner.calls_for("judge")) == 2
    assert len(runner.calls_for("profiler")) == 1
    assert accuracy_gate.call_count == 2
    assert [call.kwargs["timeout_seconds"] for call in accuracy_gate.call_args_list] == [
        37,
        37,
    ]

    failed, seed = _load_population(tmp_path).all
    assert failed.passed is False
    assert failed.feedback == failure
    assert failed.commit
    assert seed.passed is True


def test_bootstrap_prompt_uses_cold_start_section(tmp_path: Path, ref_file: str) -> None:
    """The bootstrap attempt sees the cold-start branch of the mutator prompt
    (no parent block)."""
    runner = FakeAgentClient()
    _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
    )
    captured = runner.calls_for("implementer")
    assert len(captured) == 1
    prompt = captured[0].system_prompt
    assert "Bootstrap the first passing seed" in prompt
    assert "## Parent" not in prompt
    assert "LLM-serving implementation invariants" in prompt


def test_evolve_with_preexisting_passing_seed_skips_bootstrap(
    tmp_path: Path, ref_file: str
) -> None:
    """A resumed run whose population already has a passing seed skips the
    bootstrap phase entirely and evolves straight off the seed."""
    # First run: bootstrap-only, creates a passing gen-0 seed with a real commit.
    _invoke_bootstrap(tmp_path, ref_file, FakeAgentClient())
    exp_envs = list((tmp_path / "exp_env").iterdir())
    assert len(exp_envs) == 1
    exp_name = exp_envs[0].name
    recorded = Project.open(exp_envs[0]).state.load_run(exp_name)
    assert isinstance(recorded, OrchestrationRunManifest)
    assert recorded.orchestration.id == "evolve"

    # Second run increases the total budget; bootstrap must not be called,
    # and a gen-2 child must be appended off the seed.
    with patch("vibesys.loops.evolve.run._bootstrap_seed") as spy:
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
        spy.assert_not_called()
    assert result is True
    resumed = Project.open(exp_envs[0]).state.load_run(exp_name)
    assert isinstance(resumed, OrchestrationRunManifest)
    assert resumed.orchestration.options["max_generations"] == 2

    pop = _load_population(tmp_path)
    assert len(pop) == 2  # gen-0 seed + one gen-2 child (same exp dir, resumed)
    seed, child = pop.all
    assert seed.generation == 0
    assert seed.parent_id is None
    assert child.parent_id == seed.id
    assert child.generation == 2


# ---------------------------------------------------------------------------
# Multi-generation: parent selection + lineage tracking
# ---------------------------------------------------------------------------


def test_first_generation_uses_bootstrap_seed_as_parent(tmp_path: Path, ref_file: str) -> None:
    """Gen 1's child must be tagged with parent_id pointing at the bootstrap
    seed."""
    runner = FakeAgentClient().enqueue("profiler", *_default_profiler_responses(2))
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


def test_final_project_tree_is_the_deterministic_scalar_best(tmp_path: Path, ref_file: str) -> None:
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
    runner = FakeAgentClient().enqueue("profiler", *responses)
    runner.on_invoke(_mutator_writes_callback(runner))
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=2,
    )

    assert result is True
    best = _load_population(tmp_path).best(MetricSpace())
    assert best is not None
    assert best.perf_metric == 100.0
    project = _project_dir(tmp_path)
    assert (project / "mutant_2.py").is_file()
    assert not (project / "mutant_3.py").exists()


def test_failed_child_excluded_from_future_parent_pool(tmp_path: Path, ref_file: str) -> None:
    """Bootstrap: pass (the seed). Gen 1: fail (no commit, not eligible as
    parent). Gen 2: must still parent off the seed — never off the failed
    Gen 1 child."""
    # Judge order: bootstrap(pass), gen1(fail), gen2(pass).
    runner = FakeAgentClient().enqueue(
        "judge", _judge_response("pass"), _judge_response("fail"), _judge_response("pass")
    )
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=2,
        children_per_generation=1,
    )
    assert result is True
    assert len(runner.calls_for("implementer")) == 3
    assert len(runner.calls_for("judge")) == 3
    assert len(runner.calls_for("profiler")) == 2  # only the passes (seed + gen2)

    pop = _load_population(tmp_path)
    assert len(pop) == 3
    seed, g1, g2 = pop.all
    assert seed.passed is True
    assert g1.passed is False
    assert g1.commit is None
    # The gen-2 child must descend from the seed, NOT from the failed g1
    # (which has no commit and can't be selected).
    assert g2.parent_id == seed.id


def test_accuracy_rejected_child_is_not_profiled_or_selected(tmp_path: Path, ref_file: str) -> None:
    """The framework oracle can overrule an LLM PASS for an offspring."""
    failure = "Framework accuracy gate failed.\nbooking response diverged"
    runner = FakeAgentClient()
    accuracy_gate = MagicMock(side_effect=[None, failure, None])
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        accuracy_gate=accuracy_gate,
        max_generations=2,
        children_per_generation=1,
    )

    assert result is True
    assert len(runner.calls_for("judge")) == 3
    assert len(runner.calls_for("profiler")) == 2
    assert accuracy_gate.call_count == 3

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


def test_pareto_mode_records_metrics_dict_on_individuals(tmp_path: Path, ref_file: str) -> None:
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
    runner = FakeAgentClient().enqueue("profiler", *profiler_responses)
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


def test_pareto_addendum_appears_in_profiler_prompt(tmp_path: Path, ref_file: str) -> None:
    """When Pareto mode is on, the profiler system prompt gets an addendum
    explicitly listing the metric keys to emit. The judge stays unaffected."""
    runner = FakeAgentClient().enqueue(
        "profiler",
        ProfilerSummary(
            analysis="ok",
            bottlenecks="none",
            suggestions="none",
            perf_metric=10.0,
            perf_unit="tok/s",
            metrics={"tput": 10.0, "lat_ms": 50.0},
        ),
    )
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
    captured_profiler_prompts = runner.calls_for("profiler")
    assert len(captured_profiler_prompts) == 1
    prompt = captured_profiler_prompts[0].system_prompt
    assert "Pareto-frontier mode" in prompt
    assert "`tput`" in prompt
    assert "`lat_ms`" in prompt


def test_no_objectives_keeps_metrics_empty_and_legacy_behavior(
    tmp_path: Path, ref_file: str
) -> None:
    """A space with no axes keeps `Individual.metrics` empty even if the
    profiler stub doesn't supply one — preserves the pre-Pareto behavior."""
    runner = FakeAgentClient()
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


def test_second_child_prompt_includes_parent_block(tmp_path: Path, ref_file: str) -> None:
    """Gen 1's mutator prompt mentions the parent (bootstrap seed) perf_metric —
    one of the few signals the mutator has to ground its change in fitness."""
    runner = FakeAgentClient().enqueue("profiler", *_default_profiler_responses(2))
    _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
    )
    captured = runner.calls_for("implementer")
    assert len(captured) == 2  # bootstrap (cold-start) + gen-1 (parent block)
    gen1_prompt = captured[1].system_prompt
    assert "Bootstrap the first passing seed" not in gen1_prompt
    assert "## Parent" in gen1_prompt
    # The seed's perf_metric (10.0) was emitted by the profiler and should
    # appear in the parent block.
    assert "10.0" in gen1_prompt


def test_generic_domain_prompts_exclude_llm_serving_contracts(
    tmp_path: Path, ref_file: str
) -> None:
    """The evolve loop uses registered domain sections instead of baking the
    LLM-serving contract into its mutator and judge base prompts."""
    runner = FakeAgentClient()

    result = _invoke_bootstrap(
        tmp_path,
        ref_file,
        runner,
        domain=DomainName.GENERIC,
        modality=None,
        profiler_kind=ProfilerKind.NONE,
    )

    assert result is True
    mutator_prompts = [call.system_prompt for call in runner.calls_for("implementer")]
    judge_prompts = [call.system_prompt for call in runner.calls_for("judge")]
    assert len(mutator_prompts) == len(judge_prompts) == 1
    combined = "\n".join(mutator_prompts + judge_prompts)
    assert "uv run python accuracy_checker/checker.py" in combined
    assert "uv run python benchmark/benchmark.py" in combined
    assert "Model weights are at `/model`" not in combined
    assert "serving-systems" not in combined
    assert "/health" not in combined
    assert "OpenAI-compatible" not in combined


def test_openevolve_policy_persists_multi_file_search_state(tmp_path: Path, ref_file: str) -> None:
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


def test_candidate_code_is_multi_file_but_excludes_framework_state(tmp_path: Path) -> None:
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
    code = asyncio.run(_candidate_code(_as_run_context(_FakeRunContext(git=tracker)), commit))
    assert "src/lib.rs" in code
    assert "src/ffi.rs" in code
    assert "population.json" not in code


def test_programmatic_openevolve_config_infers_policy(tmp_path: Path) -> None:
    ctx, state_store = _stateful_context(tmp_path)
    config = OpenEvolveSearchConfig(num_islands=1)

    name, policy = asyncio.run(
        _initialize_search_policy(
            _as_run_context(ctx),
            Population(),
            state_store,
            requested=None,
            seed=1,
            config=config,
            space=MetricSpace(),
        )
    )

    assert name.value == "openevolve"
    assert isinstance(policy, OpenEvolveSearchPolicy)
    assert policy.config == config


def test_programmatic_openevolve_config_rejects_vibesys_policy(tmp_path: Path) -> None:
    ctx, state_store = _stateful_context(tmp_path)

    with pytest.raises(ValueError, match="requires the OpenEvolve search policy"):
        asyncio.run(
            _initialize_search_policy(
                _as_run_context(ctx),
                Population(),
                state_store,
                requested="vibesys",
                seed=1,
                config=OpenEvolveSearchConfig(),
                space=MetricSpace(),
            )
        )


# ---------------------------------------------------------------------------
# Helper units: failure lessons, WIP-seed lookup, per-candidate deployment
# ---------------------------------------------------------------------------


def _ind(
    id_: int,
    *,
    passed: bool = False,
    parent_id: int | None = None,
    commit: str | None = None,
    feedback: str = "",
) -> Individual:
    return Individual(
        id=id_,
        generation=1,
        parent_id=parent_id,
        passed=passed,
        commit=commit,
        feedback=feedback,
    )


def test_recent_failure_lessons_dedupes_and_orders_most_recent_first() -> None:
    pop = Population()
    pop.add(_ind(1, feedback="crash: CUDA out of memory"))
    pop.add(_ind(2, feedback="crash: CUDA out of memory"))  # duplicate → collapsed
    pop.add(_ind(3, feedback="server never bound to port"))
    pop.add(_ind(4, passed=True, feedback="ignored because it passed"))

    lessons = _recent_failure_lessons(pop, limit=3)
    assert lessons == ["server never bound to port", "crash: CUDA out of memory"]


def test_recent_failure_lessons_truncates_long_feedback() -> None:
    pop = Population()
    pop.add(_ind(1, feedback="x" * 5000))
    (lesson,) = _recent_failure_lessons(pop, limit=1, max_chars=100)
    assert lesson.endswith("…")
    assert len(lesson) <= 102  # 100 chars + space + ellipsis


def test_latest_wip_seed_returns_most_recent_failed_seed_with_commit() -> None:
    pop = Population()
    pop.add(_ind(1, commit="aaa"))  # failed cold-start seed
    pop.add(_ind(2, commit="bbb"))  # newer failed cold-start seed
    pop.add(_ind(3, passed=True, commit="ccc"))  # passing → not a WIP seed
    pop.add(_ind(4, parent_id=2, commit="ddd"))  # has a parent → not cold-start

    seed = _latest_wip_seed(pop)
    assert seed is not None
    assert seed.id == 2


def test_latest_wip_seed_none_when_no_snapshotted_failure() -> None:
    pop = Population()
    pop.add(_ind(1, commit=None))  # failed but never snapshotted
    pop.add(_ind(2, passed=True, commit="ccc"))
    assert _latest_wip_seed(pop) is None


def test_candidate_runtime_notes_delegates_deployment_naming_to_environment() -> None:
    base = "run-20260720-abcd1234-llama3"
    ctx = _FakeRunContext(
        run_environment_view=SimpleNamespace(
            deployment_namespace=base,
            prompt_notes=f"Deploy to Modal app {base}; endpoint {base}-web.",
        ),
        run_environment=MagicMock(
            candidate_runtime=lambda _view, generation, child_idx: CandidateRuntime(
                prompt_notes="provider-owned candidate instructions",
                deployment_name=f"candidate-{generation}-{child_idx}",
            )
        ),
    )
    notes, app = _candidate_runtime_notes(_as_run_context(ctx), generation=3, child_idx=2)
    assert app == "candidate-3-2"
    assert notes == "provider-owned candidate instructions"


def test_candidate_runtime_notes_noop_without_named_deployment() -> None:
    notes_in = "Local run; no named deployment."
    ctx = _FakeRunContext(
        run_environment_view=SimpleNamespace(deployment_namespace=None, prompt_notes=notes_in),
        run_environment=MagicMock(
            candidate_runtime=lambda view, _generation, _child_idx: CandidateRuntime(
                prompt_notes=view.prompt_notes
            )
        ),
    )
    notes, app = _candidate_runtime_notes(_as_run_context(ctx), generation=1, child_idx=1)
    assert app is None
    assert notes == notes_in


# ---------------------------------------------------------------------------
# Candidate-app teardown
# ---------------------------------------------------------------------------


def test_teardown_candidate_deployment_delegates_to_run_environment() -> None:
    """The loop stays backend-agnostic: it hands the deployment name to the run
    environment, which decides how to release it."""
    run_env = MagicMock()
    ctx = _FakeRunContext(run_environment=run_env)

    asyncio.run(
        _teardown_candidate_deployment(_as_run_context(ctx), "vibesys-run-g1c2", keep=False)
    )

    assert run_env.teardown_deployment.call_args.args[0] == "vibesys-run-g1c2"


def test_teardown_candidate_deployment_noop_when_kept_or_absent() -> None:
    run_env = MagicMock()
    ctx = _FakeRunContext(run_environment=run_env)

    # Opt-out: keep the app for post-hoc inspection.
    asyncio.run(_teardown_candidate_deployment(_as_run_context(ctx), "vibesys-run-g1c2", keep=True))
    # No per-candidate deployment (non-Modal env).
    asyncio.run(_teardown_candidate_deployment(_as_run_context(ctx), None, keep=False))

    run_env.teardown_deployment.assert_not_called()


# ---------------------------------------------------------------------------
# Parallel generation orchestration
# ---------------------------------------------------------------------------


def _stateful_context(
    tmp_path: Path,
    log: Callable[[str], None] | None = None,
) -> tuple[_FakeRunContext, EvolutionStateStore]:
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "test",
        run_id="run-1",
        branch="vibesys/run-1",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=_evolution_descriptor(),
    )
    project.state.create_run(run)
    git = MagicMock(history_root=project.root, run_id=run.run_id)
    state = RunState(project, git, run.run_id)
    ctx = _FakeRunContext(git=git, state=state, log=log if log is not None else _discard_log)
    return ctx, EvolutionStateStore(state.portable(RunStateNamespace.EVOLVE))


# ---------------------------------------------------------------------------
# Isolated sub-context evaluation (worktree + own logger/agent-runner)
# ---------------------------------------------------------------------------


def test_evaluate_in_subcontext_skips_parent_without_commit() -> None:
    """A parent with no commit can't seed a worktree — folded into a failed
    outcome without ever building a sub-context."""
    criteria = "crit"
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
                domain_definition=_LLM_SERVING_DOMAIN,
                pass_criteria=criteria,
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


def test_max_parallelism_ignored_without_environment_capability(
    tmp_path: Path, ref_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment without isolated evaluation support stays serial."""
    called = {"parallel": False}
    monkeypatch.setattr(
        EvolveRun,
        "_evaluate_parallel_pool",
        lambda *_a, **_k: called.update(parallel=True),
    )
    runner = FakeAgentClient().enqueue(
        "judge", _judge_response("pass"), _judge_response("pass"), _judge_response("pass")
    )
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


def test_loop_tears_down_candidate_on_pass_and_fail_paths(tmp_path: Path, ref_file: str) -> None:
    """Teardown fires exactly once per candidate on every exit path — the
    fails the judge."""
    # bootstrap passes (1 attempt), gen-1 candidate passes, gen-2 candidate fails.
    runner = FakeAgentClient().enqueue(
        "judge", _judge_response("pass"), _judge_response("pass"), _judge_response("fail")
    )
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


def _passing_gate_result(
    metric_value: float,
    *,
    unit: str | None = None,
    row: dict[str, float] | None = None,
) -> BenchmarkGateResult:
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


def test_benchmark_gate_extends_timeout_by_environment_setup_allowance() -> None:
    """Environment-owned deployment/readiness time must not eat the benchmark
    command's declared budget: the evolve gate forwards setup + contract, the
    same setup-aware policy the agent path uses."""
    ctx = _FakeRunContext(
        run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=90),
        events=MagicMock(),
    )
    assert framework_command_timeout(ctx, 120) == 210


def test_benchmark_gate_timeout_unchanged_without_setup_allowance() -> None:
    """With no setup allowance the forwarded budget is exactly the contract's."""
    ctx = _FakeRunContext(
        run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=0),
        events=MagicMock(),
    )
    assert framework_command_timeout(ctx, 120) == 120


def test_benchmark_contract_owns_seed_and_child_fitness(tmp_path: Path, ref_file: str) -> None:
    """A declared benchmark result contract, not the profiler agent's
    self-report, records every candidate's fitness."""

    runner = FakeAgentClient().enqueue("profiler", *_default_profiler_responses(2))
    gate = MagicMock(side_effect=[_passing_gate_result(42.5), _passing_gate_result(43.75)])
    with patch("vibesys.orchestration.gates.run_benchmark_gate", gate):
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
    assert gate.call_args.kwargs["contract"].result_spec.metric == "total_ops_per_sec"
    pop = _load_population(tmp_path)
    assert [item.perf_metric for item in pop.all] == [42.5, 43.75]
    # The scalar contract declares a metric name, not a unit, so the recorded
    # unit stays the profiler's. A metric name is not a unit.
    assert {item.perf_unit for item in pop.all} == {"tok/s"}
    assert pop.all[0].metrics == {"total_ops_per_sec": 42.5}
    # The profiler still ran for diagnostics; its self-report was not recorded.
    assert len(runner.calls_for("profiler")) == 2


def test_benchmark_contract_failure_fails_the_candidate_before_profiling(
    tmp_path: Path, ref_file: str
) -> None:
    runner = FakeAgentClient()
    failing = BenchmarkGateResult(
        command="trusted-benchmark --json /tmp/result.json",
        output="benchmark exploded",
        executed=True,
        outcome=FrameworkBenchmarkOutcome(
            feedback="Framework benchmark failed.\nbenchmark exploded"
        ),
    )
    with patch("vibesys.orchestration.gates.run_benchmark_gate", MagicMock(return_value=failing)):
        result = _invoke_bootstrap(
            tmp_path,
            ref_file,
            runner,
            benchmark_result=BenchmarkResult(json_argument="--json", metric="total_ops_per_sec"),
            bootstrap_max_attempts=1,
        )

    assert result is False
    assert len(runner.calls_for("profiler")) == 0
    pop = _load_population(tmp_path)
    assert len(pop) == 1
    failed = pop.all[0]
    assert failed.passed is False
    assert "Framework benchmark failed." in (failed.feedback or "")


def test_no_benchmark_contract_keeps_profiler_fitness(tmp_path: Path, ref_file: str) -> None:
    runner = FakeAgentClient().enqueue("profiler", *_default_profiler_responses(1))
    gate = MagicMock()
    with patch("vibesys.orchestration.gates.run_benchmark_gate", gate):
        result = _invoke_bootstrap(tmp_path, ref_file, runner)

    assert result is True
    gate.assert_not_called()
    seed = _load_population(tmp_path).all[0]
    assert seed.perf_metric == 10.0
    assert seed.perf_unit == "tok/s"


def test_scalar_contract_keeps_the_profilers_other_axes_on_the_frontier(
    tmp_path: Path, ref_file: str
) -> None:
    """Regression: a one-metric contract must not empty a two-axis frontier.

    ``Population.frontier`` keeps only individuals carrying a value for every
    configured axis. The scalar result contract reports one number, so writing
    the trusted row *over* the profiler's row left every individual missing the
    second axis and the frontier came back empty, which in turn starves Pareto
    parent selection. The trusted row now overrides the axes it measures and
    leaves the rest of the profiler's row in place.
    """

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
    runner = FakeAgentClient().enqueue("profiler", *profiler_responses)
    gate = MagicMock(side_effect=[_passing_gate_result(42.5), _passing_gate_result(43.75)])
    with patch("vibesys.orchestration.gates.run_benchmark_gate", gate):
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


def test_protocol_contract_records_the_evaluator_declared_unit(
    tmp_path: Path, ref_file: str
) -> None:
    """The recorded unit is the evaluator's declaration when it supplies one."""
    runner = FakeAgentClient()
    gate = MagicMock(return_value=_passing_gate_result(42.5, unit="ops/s"))
    with patch("vibesys.orchestration.gates.run_benchmark_gate", gate):
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


def test_evolve_accuracy_gate_extends_timeout_by_environment_setup_allowance() -> None:
    """The accuracy gate charges environment setup the way the benchmark gate
    does; otherwise a Modal/SkyPilot deployment eats the accuracy command's
    declared budget and the candidate fails on a timeout it was never given
    the time to avoid."""
    ctx = _FakeRunContext(
        run_environment_view=SimpleNamespace(framework_setup_timeout_seconds=90),
    )
    assert framework_command_timeout(ctx, 120) == 210
