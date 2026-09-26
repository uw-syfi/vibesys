import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, Unpack
from unittest.mock import MagicMock, patch

import pytest
from tests.support import run_test_command

from vibesys import boot_trace
from vibesys.api import open_run_store
from vibesys.api.agent import is_agent_run_manifest
from vibesys.config import Config
from vibesys.context import (
    RunSetup,
    WorkspaceResourceSpec,
    _RunResources,
    create_workspace_resources,
    open_run_resources,
)
from vibesys.domains.environment import (
    EnvironmentContext,
    EnvironmentHooks,
    EnvironmentPatch,
    NoopEnvironmentHooks,
)
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks
from vibesys.errors import ConfigurationError
from vibesys.evaluators import (
    EvaluatorPackageRequirement,
    resolve_evaluator_package,
    tool_install_root,
)
from vibesys.evaluators.input_manifest import (
    WorkspaceInput,
    WorkspaceSource,
    load_input_bundle,
    load_project_task,
)
from vibesys.evaluators.tools import CargoGitToolSpec
from vibesys.events import CoreEventType
from vibesys.loops.agent_options import (
    AgentOrchestrationOptions,
    compare_resume_descriptors,
    descriptor_from_options,
)
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.profilers import ProfilerKind, ProfilerPreflightResult
from vibesys.run import (
    LocalRunIntegration,
    RunStateNamespace,
)
from vibesys.sandbox.run_environment import RunEnvironmentSpec
from vibesys.search.hypothesis.state import HypothesisState
from vs_loop_state.api import PlainLoopCursor
from vs_project.api import OrchestrationRunManifest, Project
from vs_sandbox.api import HostResourceAccess, SandboxLifecycle, SandboxLifecycleHooks


class _FakeBackend:
    image = "fake-image"
    selected_device = None

    def __init__(self) -> None:
        self.sandbox = MagicMock()
        self.sandbox.execute.return_value = MagicMock(exit_code=0, output="", truncated=False)

    def make_sandbox(
        self,
        *_args: object,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        **_kwargs: object,
    ) -> object:
        SandboxLifecycle(lifecycle_hooks).before_ready(self.sandbox)
        return self.sandbox

    def make_monitor(self, _log_dir: object) -> None:
        return None


class _RecordingHooks:
    def __init__(self) -> None:
        self.prepared = 0
        self.torn_down = 0

    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch:
        del ctx
        self.prepared += 1
        return EnvironmentPatch()

    def teardown(self, ctx: EnvironmentContext) -> None:
        del ctx
        self.torn_down += 1


class _CreateContextOptions(TypedDict, total=False):
    runs_dir: Path | None
    evaluator: Path | None
    evaluator_package_root: Path | None
    exp_name: str
    existing: bool
    configuration: AgentOrchestrationOptions | None
    config: Config | None
    profiler_kind: ProfilerKind
    workspace_sources: tuple[WorkspaceSource, ...]
    agent_backend: str | None
    objective: str
    task_name: str | None
    task_root: Path | None
    remote_repo: str | None
    hooks: EnvironmentHooks | None
    integration: LocalRunIntegration | None


@pytest.fixture(autouse=True)
def context_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibesys.context.backends.get", lambda *_args, **_kwargs: _FakeBackend())
    monkeypatch.setattr(
        "vibesys.context.preflight_profiler_kind",
        lambda kind: ProfilerPreflightResult(kind, usable=True),
    )


def _write_project(root: Path, *, evaluator_name: str = "checker") -> Path:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Make the queue faster.\n")
    evaluator = root / evaluator_name
    evaluator.mkdir()
    (evaluator / "check.py").write_text("print('ok')\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        """\
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "_evaluator/checker/check.py"]

[benchmark]
command = ["python", "_evaluator/checker/check.py"]

[evaluator]
source = "checker"
"""
    )
    return evaluator


def _write_serving_task(root: Path, name: str = "latency") -> Path:
    task = root / ".vibesys" / "tasks" / name
    reference = task / "reference"
    reference.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Reduce latency.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text(
        """\
version = 1

[agent]
domain = "llm-serving"

[accuracy]
command = ["python", "checker.py"]

[benchmark]
command = ["python", "benchmark.py"]
""",
        encoding="utf-8",
    )
    (reference / "meta.json").write_text(
        '{"model_id": "org/model", "revision": "abc"}',
        encoding="utf-8",
    )
    return task


def _options(max_rounds: int = 1) -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=max_rounds,
        max_retries_per_round=1,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
    )


def _create_context(
    project: Path,
    **options: Unpack[_CreateContextOptions],
) -> _RunResources:
    task_root = options.get("task_root")
    evaluator = options.get("evaluator")
    evaluator_package_root = options.get("evaluator_package_root")
    workspace_sources = options.get("workspace_sources", ())
    if task_root is not None:
        selected_project = Project.open(project)
        bundle = load_project_task(
            selected_project, selected_project.select_task(options.get("task_name"))
        )
    else:
        bundle = load_input_bundle(project)
    updates: dict[str, object] = {}
    if evaluator is not None:
        updates["evaluator_path"] = evaluator
    if evaluator_package_root is not None:
        updates["evaluator_package_root"] = evaluator_package_root
    if workspace_sources:
        updates["manifest"] = bundle.manifest.model_copy(
            update={"workspace": WorkspaceInput(sources=workspace_sources)}
        )
    bundle = bundle.model_copy(update=updates)
    exp_name = options.get("exp_name", "queue")
    request = RunRequest(
        project_root=project,
        orchestration=descriptor_from_options(
            options.get("configuration") or _options(), orchestration_id="multi-agent"
        ),
        config=options.get("config") or Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=bundle,
        objective=options.get("objective", "Make the queue faster.\n"),
        exp_name=exp_name,
        resume=ResumeRef(run_id=exp_name) if options.get("existing", False) else None,
        runs_dir=options.get("runs_dir"),
        profiler_kind=options.get("profiler_kind", ProfilerKind.NONE),
        run_environment=RunEnvironmentSpec("local"),
        agent_backend=options.get("agent_backend", "stub"),
        remote_repo=options.get("remote_repo"),
    )
    setup = RunSetup(
        state_namespace="multi",
        state_slots={"state.json": HypothesisState},
        resume_policy=compare_resume_descriptors,
    )
    with patch(
        "vibesys.context.resolve_domain",
        return_value=SimpleNamespace(
            environment_hooks=options.get("hooks") or NoopEnvironmentHooks()
        ),
    ):
        return open_run_resources(
            request, setup, options.get("integration") or LocalRunIntegration()
        )


def _git(project: Path, *args: str) -> str:
    return run_test_command(
        [
            "git",
            "-c",
            "user.name=VibeSys Test",
            "-c",
            "user.email=test@vibesys.invalid",
            *args,
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_direct_run_uses_one_project_root_and_canonical_state(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as ctx:
        assert ctx.project_root == project
        assert ctx.workspace == project
        assert ctx.project.root == project
        assert ctx.log_dir == ctx.project.state.log_directory(ctx.run_id)
        assert (
            not ctx.state.local(RunStateNamespace.AGENT)
            .external_directory()
            .is_relative_to(project)
        )
        objective_path = Path(ctx.run_environment_view.paths.objective)
        assert objective_path == (
            ctx.project.state.portable_namespace(ctx.run_id, "runtime").external_directory()
            / "effective-objective.md"
        )
        assert objective_path.read_text() == "Make the queue faster.\n"
        assert objective_path.is_relative_to(ctx.workspace)

        policy = ctx.environment_request.project_path_policy
        state_paths = ctx.project.state.sandbox_paths()
        assert state_paths.read_only_path in policy.read_only_paths
        assert state_paths.hidden_path is None

    manifest = Project.open(project).state.load_run(ctx.run_id)
    assert manifest.branch == f"vibesys-runs/{ctx.run_id}"
    assert _git(project, "branch", "--show-current") == manifest.branch
    assert _git(project, "status", "--porcelain") == ""


def test_context_places_evaluator_tools_in_operator_cache_and_imports_it_read_only(
    tmp_path: Path,
) -> None:
    project = tmp_path / "queue"
    _write_project(project)
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )

    def install_command(tools: dict[str, CargoGitToolSpec], root: Path) -> str:
        root.mkdir(parents=True, exist_ok=True)
        for name, spec in tools.items():
            tool_install_root(root, name, spec).mkdir(parents=True)
        return "true"

    with (
        patch(
            "vibesys.evaluators.tools.evaluator_tools_install_command",
            side_effect=install_command,
        ),
        _create_context(project, evaluator_package_root=package.root) as ctx,
    ):
        tools_root = ctx.project.state.model_cache_directory("evaluator-tools")
        resources = {resource.path: resource.access for resource in ctx.agent_host_resources}
        expected_tool_roots = tuple(
            tool_install_root(tools_root, name, spec)
            for name, spec in package.metadata.tools.items()
        )

        assert ctx.evaluator_tools_root == tools_root
        assert ctx.evaluator_tool_roots == expected_tool_roots
        assert tools_root.is_dir()
        assert not tools_root.is_relative_to(project)
        assert tools_root not in resources
        assert all(root.is_dir() for root in expected_tool_roots)
        assert all(resources[root] is HostResourceAccess.READ_ONLY for root in expected_tool_roots)


def test_run_context_announces_canonical_experiment_state(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    integration = LocalRunIntegration()
    try:
        with _create_context(project, evaluator=evaluator, integration=integration) as ctx:
            changed = [
                event
                for event in integration.events.read()
                if event.type is CoreEventType.EXPERIMENTS_CHANGED
            ]

            assert ctx.integration is integration
            assert len(changed) == 1
            assert changed[0].run_id == ctx.run_id
            assert changed[0].data is not None
            assert changed[0].data.kind == "experiments_changed"
            assert changed[0].data.reason == "project_attached"
    finally:
        integration.close()


def test_context_assembly_logs_stage_timings(tmp_path: Path) -> None:
    """Every assembly span up to and past the experiments gate reaches the run log.

    This is a regression guard for the diagnostic used to find where
    ``open_run_resources`` spends time before the TUI's hypothesis screen
    can leave "loading experiments..." (the gate flips when the second
    ``LocalRunIntegration.attach`` records ``EXPERIMENTS_CHANGED``).
    """
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as ctx:
        log_text = ctx.run_log_path.read_text()

    for stage in (
        "config_and_inputs",
        "backend_and_model",
        "profiler_preflight",
        "workspace_materialize",
        "project_open",
        "log_bootstrap",
        "git_tracker_init",
        "project_state_resume",
        "round_transaction_recovery",
        "workspace_setup",
        "environment_open",
        "device_monitor_start",
    ):
        assert f"boot span context.{stage}: " in log_text, f"missing span timing for {stage!r}"
    # The enclosing span is assembly's total, recorded after its children.
    assert "boot span context: " in log_text
    assert "experiments gate open after " in log_text


def test_dispatch_preamble_spans_reach_run_log(tmp_path: Path) -> None:
    """Spans closed before ``open_run_resources`` land in the run log, first.

    ``_dispatch`` and ``_run_agent`` (main.py) do substantial work before a
    ``RunLogger`` exists and record ``boot_trace`` spans as they go.
    ``_assemble_run_resources`` must drain that buffer at entry, so the
    preamble's spans reach the persistent run log ahead of assembly's own.
    """
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    boot_trace.drain_log_lines()
    with boot_trace.span("agent_preamble"), boot_trace.span("load_config_and_skills"):
        pass
    with _create_context(project, evaluator=evaluator) as ctx:
        log_text = ctx.run_log_path.read_text()

    assert "boot span agent_preamble.load_config_and_skills: " in log_text
    assert "boot span agent_preamble: " in log_text
    # The preamble happened before assembly in real dispatch; the run log
    # should preserve that order.
    preamble_index = log_text.index("boot span agent_preamble: ")
    context_index = log_text.index("boot span context.config_and_inputs: ")
    assert preamble_index < context_index


def test_context_assembly_without_recorded_preamble_omits_preamble_lines(tmp_path: Path) -> None:
    """No preamble spans (e.g. a test-built context) means no stray lines."""
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    boot_trace.drain_log_lines()
    with _create_context(project, evaluator=evaluator) as ctx:
        log_text = ctx.run_log_path.read_text()

    assert "boot span agent_preamble" not in log_text
    assert "boot span dispatch" not in log_text


def test_context_assembly_spans_stay_off_stderr_by_default(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Boot spans are forensics in the run log, not narration at the operator."""
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as ctx:
        log_text = ctx.run_log_path.read_text()
        captured_err = capfd.readouterr().err

    assert "boot span context: " in log_text
    assert "boot span" not in captured_err


def test_boot_trace_env_puts_assembly_spans_on_stderr(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    monkeypatch.setenv(boot_trace.BOOT_TRACE_ENV, "1")
    with _create_context(project, evaluator=evaluator) as ctx:
        assert "boot span context: " in ctx.run_log_path.read_text()
        captured_err = capfd.readouterr().err

    assert "boot span context.config_and_inputs: " in captured_err


def test_repository_task_exposes_its_actual_reference_path(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    task = project / ".vibesys" / "tasks" / "latency"
    reference = task / "reference"
    reference.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Reduce latency.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text(
        """\
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "_evaluator/checker/check.py"]

[benchmark]
command = ["python", "_evaluator/checker/check.py"]
""",
        encoding="utf-8",
    )
    (reference / "baseline.py").write_text("VALUE = 1\n", encoding="utf-8")

    with _create_context(
        project,
        evaluator=evaluator,
        task_name="latency",
        task_root=task,
    ) as ctx:
        assert ctx.ref_name == ".vibesys/tasks/latency/reference/baseline.py"


def test_copied_repository_task_materializes_model_outside_authored_inputs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "serving"
    _write_project(project)
    task = _write_serving_task(project)
    reference = task / "reference"
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    runs_dir = tmp_path / "runs"

    with patch("huggingface_hub.snapshot_download", return_value=str(downloaded)):
        with _create_context(
            project,
            runs_dir=runs_dir,
            task_name="latency",
            task_root=task,
            hooks=LLMServingEnvironmentHooks(),
        ) as ctx:
            runtime_model = runs_dir / ".cache" / "llm-serving" / ctx.run_id / "model"
            copied_reference = ctx.project_root / ".vibesys" / "tasks" / "latency" / "reference"

            assert not (reference / "model").exists()
            assert not (copied_reference / "model").exists()
            assert runtime_model.resolve() == downloaded
            assert ctx.git.trusted_input_changes() == []

        assert _git(ctx.project_root, "status", "--porcelain") == ""


def test_direct_repository_task_materializes_model_in_local_state(tmp_path: Path) -> None:
    project = tmp_path / "serving"
    evaluator = _write_project(project)
    task = _write_serving_task(project)
    reference = task / "reference"
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()

    with patch("huggingface_hub.snapshot_download", return_value=str(downloaded)):
        with _create_context(
            project,
            evaluator=evaluator,
            task_name="latency",
            task_root=task,
            hooks=LLMServingEnvironmentHooks(),
        ) as ctx:
            runtime_model = ctx.project.state.model_cache_directory("llm-serving") / "model"

            assert not (reference / "model").exists()
            assert runtime_model.resolve() == downloaded
            assert ctx.git.trusted_input_changes() == []

        assert _git(project, "status", "--porcelain") == ""


def test_copied_run_provisions_self_contained_project_in_collection(tmp_path: Path) -> None:
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"

    with _create_context(source, runs_dir=runs_dir, evaluator=evaluator) as ctx:
        project = ctx.project_root
        assert project.parent == runs_dir
        assert project.name == ctx.run_id
        assert ctx.workspace == project
        assert (project / "queue.py").is_file()
        assert not (project / "checker").exists()
        assert (project / "_evaluator" / "checker" / "check.py").is_file()
        manifest_text = (project / "vibesys.input.toml").read_text()
        assert 'source = "_evaluator/checker"' in manifest_text
        assert "[workspace]" not in manifest_text
        assert ctx.log_dir == ctx.project.state.log_directory(ctx.run_id)

    assert _git(project, "status", "--porcelain") == ""


def test_agent_v4_run_resumes_with_larger_round_budget(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.run_id

    stored = Project.open(project).state.load_run(run_id)
    assert isinstance(stored, OrchestrationRunManifest)
    assert stored.orchestration.id == "multi-agent"
    assert stored.orchestration.options["max_rounds"] == 1
    assert is_agent_run_manifest(stored)
    assert open_run_store(Project.open(project)).get_run(run_id).loop == "multi-agent"

    with _create_context(
        project,
        evaluator=evaluator,
        exp_name=run_id,
        existing=True,
        configuration=_options(max_rounds=2),
    ):
        pass

    resumed = Project.open(project).state.load_run(run_id)
    assert isinstance(resumed, OrchestrationRunManifest)
    assert resumed.orchestration.options["max_rounds"] == 2
    assert _git(project, "branch", "--show-current") == f"vibesys-runs/{run_id}"


def test_collection_resume_pushes_existing_origin_on_teardown(tmp_path: Path) -> None:
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"
    with _create_context(source, runs_dir=runs_dir, evaluator=evaluator) as first:
        project = first.project_root
        run_id = first.run_id

    remote = tmp_path / "remote.git"
    run_test_command(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
    )
    run_test_command(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=project,
        check=True,
    )

    with _create_context(
        project,
        runs_dir=runs_dir,
        evaluator=project / "_evaluator" / "checker",
        exp_name=run_id,
        existing=True,
    ):
        pass

    branch = run_test_command(
        ["git", "--git-dir", str(remote), "branch", "--list", f"vibesys-runs/{run_id}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"vibesys-runs/{run_id}" in branch


def test_direct_resume_republishes_an_already_published_run(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.run_id

    remote = tmp_path / "remote.git"
    run_test_command(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
    )
    run_test_command(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=project,
        check=True,
    )
    run_test_command(
        ["git", "push", "-q", "-u", "origin", f"vibesys-runs/{run_id}"],
        cwd=project,
        check=True,
    )

    with (
        patch("vibesys.context.ExperimentRepository.push") as push,
        _create_context(
            project,
            evaluator=evaluator,
            exp_name=run_id,
            existing=True,
        ),
    ):
        pass

    push.assert_called_once_with()


def test_direct_resume_does_not_publish_an_untracked_source_origin(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.run_id

    remote = tmp_path / "remote.git"
    run_test_command(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
    )
    _git(project, "remote", "add", "origin", str(remote))

    with (
        patch("vibesys.context.ExperimentRepository.push") as push,
        _create_context(
            project,
            evaluator=evaluator,
            exp_name=run_id,
            existing=True,
        ),
    ):
        pass

    push.assert_not_called()


def test_explicit_repository_rejects_a_different_existing_origin(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", ".")
    _git(
        project,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    _git(project, "remote", "add", "origin", "https://github.com/example/source.git")

    with pytest.raises(ConfigurationError) as caught:
        _create_context(
            project,
            evaluator=evaluator,
            remote_repo="example/destination",
        )

    assert caught.value.diagnostic.code == "repository_setup_failed"
    assert "does not match" in caught.value.diagnostic.message


def test_direct_run_rejects_unmaterialized_workspace_source(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    source = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="library",
    )

    with pytest.raises(ConfigurationError, match="pass --runs-dir"):
        _create_context(project, evaluator=evaluator, workspace_sources=(source,))


def test_omnigent_accepts_active_profiler_configuration(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    configuration = _options().model_copy(
        update={
            "agent_backend": "cli",
            "agent_driver": "omnigent",
            "cli_provider": "codex",
            "profiler": "macos_cpu",
        }
    )

    with _create_context(
        project,
        evaluator=evaluator,
        configuration=configuration,
        config=Config.model_validate(
            {
                "model": {"name": "gpt-test"},
                "agent": {"backend": "cli", "driver": "omnigent", "cli_provider": "codex"},
            }
        ),
        profiler_kind=ProfilerKind.MACOS_CPU,
        agent_backend=None,
    ) as context:
        assert context.profiler_kind is ProfilerKind.MACOS_CPU


def test_portable_state_snapshot_replaces_namespace_exactly(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)

    with _create_context(project, evaluator=evaluator) as ctx:
        state = ctx.state.portable(RunStateNamespace.EVOLVE)
        state.save("old.json", PlainLoopCursor(round_idx=1))
        ctx.state.commit("state 1", state)

        state.delete("old.json")
        state.save("new.json", PlainLoopCursor(round_idx=2))
        ctx.state.commit("state 2", state)

    tree = _git(project, "ls-tree", "-r", "--name-only", "HEAD")
    portable = ctx.project.state.portable_namespace(ctx.run_id, "evolve")
    assert portable.agent_visible_path("new.json") in tree
    assert portable.agent_visible_path("old.json") not in tree


def test_candidate_resources_use_project_worktree_directory(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)

    with _create_context(project, evaluator=evaluator) as parent:
        parent_commit = parent.git.current_sha()
        assert parent_commit is not None
        candidate = create_workspace_resources(
            parent,
            WorkspaceResourceSpec(
                scope_id="g2c3",
                revision=parent_commit,
                config=Config.model_validate({"model": {"name": "gpt-test"}}),
                log_namespace=RunStateNamespace.EVOLVE,
                log_directory="candidates",
                agent_backend="stub",
            ),
        )
        candidate_root = candidate.workspace
        assert candidate_root == parent.project.state.candidate_worktree_directory(
            parent.run_id,
            "g2c3",
        )
        assert candidate.log_dir == (
            parent.state.local(RunStateNamespace.EVOLVE).external_directory("candidates/g2c3/logs")
        )
        assert Path(candidate.run_environment_view.paths.objective).read_text() == (
            parent.effective_objective
        )
        candidate.close()
        assert not candidate_root.exists()


def test_construction_failure_removes_new_copy_and_tears_down_hooks(tmp_path: Path) -> None:
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"
    hooks = _RecordingHooks()

    with (
        patch("vibesys.context.RunState", side_effect=RuntimeError("state failed")),
        pytest.raises(RuntimeError, match="state failed"),
    ):
        _create_context(source, runs_dir=runs_dir, evaluator=evaluator, hooks=hooks)

    assert hooks.prepared == 1
    assert hooks.torn_down == 1
    assert not runs_dir.exists() or not list(runs_dir.iterdir())


def test_hook_teardown_runs_when_provisioning_fails(tmp_path: Path) -> None:
    source = tmp_path / "input"
    evaluator = _write_project(source)
    hooks = _RecordingHooks()

    with (
        patch("vibesys.context.provision_project", side_effect=RuntimeError("copy failed")),
        pytest.raises(RuntimeError, match="copy failed"),
    ):
        _create_context(source, runs_dir=tmp_path / "runs", evaluator=evaluator, hooks=hooks)

    assert hooks.prepared == 1
    assert hooks.torn_down == 1


def test_log_switch_retargets_stderr_tee(tmp_path: Path) -> None:
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    original_stderr = sys.stderr
    with _create_context(project, evaluator=evaluator) as ctx:
        original_file = ctx.logger.file
        ctx.switch_log_file("round001")

        assert original_file.closed
        sys.stderr.write("\033[31mcolored diagnostic\033[0m\n")
        run_log_path = ctx.run_log_path

    assert sys.stderr is original_stderr
    assert "colored diagnostic" in run_log_path.read_text()
    assert "\033[31m" not in run_log_path.read_text()
