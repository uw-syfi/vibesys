"""Shared fixtures and helpers for the evolve loop's interface-level tests.

``_invoke_loop`` drives the real, registered ``evolve`` strategy end to end
through ``run_orchestration`` (real git tracking, workspace snapshots,
population persistence) against a scripted :class:`FakeAgentClient`, the
same pattern ``tests/vibesys/golden/harness.py`` generalizes across every
strategy. The three seams patched here (CUDA sandbox factory,
``build_agent_client``, ``PROJECT_ROOT``) are the same ones that harness
uses; ``ctx.gates`` (evolve's trusted accuracy/benchmark commands) is
injected through ``run_orchestration``'s ``gate_executor`` seam instead, so
callers that need to script it pass a pre-built
:class:`~vibesys.api.testing.FakeGateExecutor`.

``_FakeRunContext`` is a narrow, non-agent capability fake for the handful
of evolve helpers (candidate-runtime naming, deployment teardown, candidate
code diffing) that take a host capability directly and have no cheaper way
to reach their edge cases through a full scripted run.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, Unpack, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vibesys.api.testing import FakeGateExecutor
from vibesys.config import Config, as_config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, DomainName
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.evaluators.metrics import MetricSpace
from vibesys.events import FrameworkSource
from vibesys.loops.evolve.entrypoint import EvolveOrchestrator
from vibesys.loops.evolve.orchestration import EvolveOptions, descriptor_from_options
from vibesys.loops.evolve.state import EvolutionStateStore
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runner import run_orchestration
from vibesys.profilers import ProfilerKind
from vibesys.render.sink import output_sink
from vibesys.roles.common import Verdict
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.profiler import ProfilerSummary
from vibesys.run import GitTracker, RunState, RunStateNamespace
from vibesys.run.integration import LocalRunIntegration
from vibesys.search.population import vibesys_selector
from vs_project.api import OrchestrationDescriptor, Project

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.constants import ComputeBackend
    from vibesys.evaluators.input_manifest import BenchmarkResult, WorkspaceSource
    from vibesys.orchestration.runtime import RunContext
    from vibesys.run import RepositoryVisibility
    from vibesys.sandbox.run_environment import CandidateRuntime, RunEnvironmentSpec
    from vibesys.search.population.models import Individual, OpenEvolveSelectorConfig
    from vs_agent.api.testing import FakeAgentClient


class _EvolveLoopKwargs(TypedDict, total=False):
    """Overrides accepted by the canonical evolve request fixture."""

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
    search_policy: str | None
    openevolve_config: OpenEvolveSelectorConfig | None
    remote_repo: str | None
    repo_visibility: RepositoryVisibility


def _discard_log(_message: str) -> None:
    """Drop log output emitted by a helper under test."""


class _CandidateEnvironment(Protocol):
    def candidate_runtime(
        self, view: object, generation: int, child_idx: int
    ) -> CandidateRuntime: ...

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> object: ...


class _FakeRunContext:
    """A small host-capability fake for evolve's focused helper assertions."""

    def __init__(  # tracked: #288
        self,
        *,
        git: GitTracker | None = None,
        run_environment: _CandidateEnvironment | None = None,
        run_environment_view: object | None = None,
        events: object | None = None,
        log: Callable[[str], None] = _discard_log,
    ) -> None:
        if git is not None:
            self.git = git
            self.workspaces = SimpleNamespace(
                root=SimpleNamespace(candidate_patch=AsyncMock(side_effect=git.candidate_patch))
            )
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
        self.events = events if events is not None else MagicMock()
        self.judge_accuracy_command: str | None = None
        self.judge_benchmark_command: str | None = None
        self.judge_backend = MagicMock()
        self._log = log

    def log(self, text: str) -> None:
        """Record a log line the same way the real context would emit it."""
        self._log(text)

    def warning(
        self,
        summary: str,
        *,
        detail: str | None = None,
        source: FrameworkSource = FrameworkSource.LOOP,
        source_label: str | None = None,
        round_label: str | None = None,
    ) -> None:
        """Publish a framework warning the same way the real context would."""
        output_sink().framework_warning(
            summary,
            detail=detail,
            source=source,
            source_label=source_label,
            round_label=round_label,
        )

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


@pytest.fixture
def ref_file(tmp_path: Path) -> str:
    """Reference *file* + sibling OBJECTIVE.md.

    A single-file reference avoids the model-weight resolution that a
    reference *directory* triggers, keeping the fixture independent of any
    developer-host HF cache state.
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


def _mutator_writes_callback(fake: FakeAgentClient):  # noqa: ANN202  # tracked: #288
    """Build an ``on_invoke`` callback that simulates a real mutator edit.

    Without a file change the cold-start snapshot is a no-op and no commit is
    recorded, so tests exercising WIP-seed/commit behavior need the workspace
    to actually change on every mutator (``kind="implementer"``) call.
    """

    def _write(call):  # noqa: ANN001, ANN202  # tracked: #288
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
    gate_executor: FakeGateExecutor | None = None,
    **kwargs: Unpack[_EvolveLoopKwargs],
) -> bool:
    """Drive the registered ``evolve`` strategy end to end.

    ``gate_executor`` is the same ``ctx.gates`` injection seam
    ``run_orchestration`` accepts for every strategy
    (``vibesys.orchestration.gates.GateExecutor``); it is never read off the
    agent client. Pass a pre-built, pre-scripted
    :class:`~vibesys.api.testing.FakeGateExecutor` when the caller needs to
    script outcomes or inspect its recorded calls afterward; otherwise one is
    built here, which accepts every accuracy/benchmark check by default.
    """
    if gate_executor is None:
        gate_executor = FakeGateExecutor()
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
            return await run_orchestration(
                request,
                integration,
                EvolveOrchestrator(descriptor),
                gate_executor=gate_executor,
            )
        finally:
            integration.close()

    with (
        patch("vibesys.backends.cuda.make_local_shell_sandbox"),
        patch(
            "vibesys.orchestration.runtime.build_agent_client",
            side_effect=lambda **_kwargs: _SharedFakeClient(runner),
        ),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
    ):
        return asyncio.run(execute())


def _invoke_bootstrap(
    tmp_path: Path,
    ref_file: str,
    runner: FakeAgentClient,
    *,
    gate_executor: FakeGateExecutor | None = None,
    **kwargs: Unpack[_EvolveLoopKwargs],
) -> bool:
    """Exercise bootstrap through a valid one-generation run contract."""
    overrides: _EvolveLoopKwargs = {"max_generations": 1}
    overrides.update(kwargs)
    with patch(
        "vibesys.loops.evolve.run.EvolveRun._proposals",
        lambda self, generation_start: [None] * self.options.children_per_generation,  # noqa: ARG005
    ):
        return _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            gate_executor=gate_executor,
            **overrides,
        )


def _project_dir(tmp_path: Path) -> Path:
    projects = [path for path in (tmp_path / "exp_env").iterdir() if path.is_dir()]
    assert len(projects) == 1, projects
    return projects[0]


def _evolution_descriptor() -> OrchestrationDescriptor:
    return OrchestrationDescriptor(id="evolve", config_version=1, options={})


def _evolution_state_store(project_root: Path) -> EvolutionStateStore:
    project = Project.open(project_root)
    run = project.state.resolve_run()
    state = RunState(project, MagicMock(history_root=project.root, run_id=run.run_id), run.run_id)
    return EvolutionStateStore(state.portable(RunStateNamespace.EVOLVE))


def _load_population(tmp_path: Path) -> tuple[Individual, ...]:
    """Load the canonical portable population for the single test run."""
    state = _evolution_state_store(_project_dir(tmp_path)).load()
    assert state is not None
    return state.population.individuals


def _best(individuals: tuple[Individual, ...], space: MetricSpace) -> Individual | None:
    return vibesys_selector.best(individuals, space)


def _frontier(individuals: tuple[Individual, ...], space: MetricSpace) -> list[Individual]:
    return vibesys_selector.frontier(individuals, space)
