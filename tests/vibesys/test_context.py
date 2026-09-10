import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from vibesys import boot_trace
from vibesys.agents.client import AgentClient
from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentTurnRequest,
    AgentTurnResult,
)
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.agents.session_store import DurableSessionStore
from vibesys.backends.cuda import CudaBackend
from vibesys.backends.cuda.gpu_monitor import GpuInfo
from vibesys.config import Config
from vibesys.context import (
    _resume_configuration_update,
    _RunContext,
    create_candidate_context,
    create_run_context,
)
from vibesys.domains.base import DomainName
from vibesys.domains.environment import EnvironmentPatch, NoopEnvironmentHooks
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks
from vibesys.errors import ConfigurationError
from vibesys.evaluators import (
    EvaluatorPackageRequirement,
    resolve_evaluator_package,
    tool_install_root,
)
from vibesys.input_manifest import WorkspaceSource
from vibesys.loops.agent.model import AgentRunState
from vibesys.profilers import ProfilerKind, ProfilerPreflightResult
from vibesys.run import (
    DeviceLease,
    LocalRunIntegration,
    RunIntegration,
    RunLogger,
    RunPaths,
    RunStateNamespace,
)
from vibesys.run.events import CoreEventType
from vibesys.sandbox.run_environment import RunEnvironmentSpec
from vs_loop_state import PlainLoopCursor
from vs_project import AgentRunConfiguration, Project, RunEnvironmentRecord
from vs_sandbox import HostResourceAccess, SandboxLifecycle


class _FakeBackend:
    image = "fake-image"
    selected_device = None

    def __init__(self) -> None:
        self.sandbox = MagicMock()
        self.sandbox.execute.return_value = MagicMock(exit_code=0, output="", truncated=False)

    def make_sandbox(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        SandboxLifecycle(_kwargs.get("lifecycle_hooks")).before_ready(self.sandbox)
        return self.sandbox

    def make_monitor(self, _log_dir):  # noqa: ANN001, ANN202
        return None


class _RecordingHooks:
    def __init__(self) -> None:
        self.prepared = 0
        self.torn_down = 0

    def prepare(self, _ctx):  # noqa: ANN001, ANN202
        self.prepared += 1
        return EnvironmentPatch()

    def teardown(self, _ctx):  # noqa: ANN001, ANN202
        self.torn_down += 1


@pytest.fixture(autouse=True)
def context_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibesys.context.backends.get", lambda *_args, **_kwargs: _FakeBackend())
    monkeypatch.setattr("vibesys.context.build_agent_client", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(
        "vibesys.context.preflight_profiler_kind",
        lambda kind: ProfilerPreflightResult(kind, True),  # noqa: FBT003
    )


def _configuration(max_rounds: int = 1) -> AgentRunConfiguration:
    return AgentRunConfiguration(
        outer_loop="agent",
        run_environment=RunEnvironmentRecord(name="local"),
        inner_loop="multi-agent",
        interface="inprocess",
        model="gpt-test",
        agent_backend="stub",
        compute_backend="cpu",
        profiler="none",
        max_rounds=max_rounds,
        max_retries_per_round=1,
        judge_every=1,
        official_eval_every=1,
        memory_layout="files",
    )


def test_resume_adopts_objectives_omitted_by_legacy_agent_manifest() -> None:
    requested = _configuration().model_copy(update={"objectives": ("throughput:max",)})
    legacy_payload = requested.model_dump(exclude={"objectives"})
    recorded = AgentRunConfiguration.model_validate(legacy_payload)

    assert "objectives" not in recorded.model_fields_set
    assert _resume_configuration_update(recorded, requested) == requested


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


def _create_context(  # noqa: PLR0913
    project: Path,
    *,
    runs_dir: Path | None = None,
    evaluator: Path | None = None,
    evaluator_package_root: Path | None = None,
    exp_name: str = "queue",
    existing: bool = False,
    configuration: AgentRunConfiguration | None = None,
    objective: str = "Make the queue faster.\n",
    task_name: str | None = None,
    task_root: Path | None = None,
    remote_repo: str | None = None,
    hooks=None,  # noqa: ANN001
    integration: RunIntegration | None = None,
) -> _RunContext:
    return create_run_context(
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        exp_name=exp_name,
        runs_dir=runs_dir,
        input_path=str(project),
        accuracy_command="python _evaluator/checker/check.py",
        benchmark_command="python _evaluator/checker/check.py",
        task_name=task_name,
        task_root=task_root,
        evaluator_path=evaluator,
        evaluator_package_root=evaluator_package_root,
        objective=objective,
        existing=existing,
        project_configuration=configuration or _configuration(),
        profiler_kind=ProfilerKind.NONE,
        profiler_domain=DomainName.GENERIC,
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        environment_hooks=hooks or NoopEnvironmentHooks(),
        remote_repo=remote_repo,
        agent_state_model_type=AgentRunState,
        integration=integration,
    )


def _git(project: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
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


def test_direct_run_uses_one_project_root_and_canonical_state(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    runner = MagicMock()

    with patch("vibesys.context.build_agent_client", return_value=runner) as build_runner:
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
            objective_path = Path(ctx.objective_location)
            assert objective_path == (
                ctx.project.state.portable_namespace(ctx.run_id, "runtime").external_directory()
                / "effective-objective.md"
            )
            assert objective_path.read_text() == "Make the queue faster.\n"
            assert objective_path.is_relative_to(ctx.workspace)

        policy = build_runner.call_args.kwargs["project_path_policy"]
        state_paths = Project.open(project).state.sandbox_paths()
        assert state_paths.read_only_path in policy.read_only_paths
        assert state_paths.hidden_path is None
        runner.close.assert_called_once_with()

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

    def install_command(tools, root):  # noqa: ANN001, ANN202
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


def test_run_context_announces_canonical_experiment_state(tmp_path):  # noqa: ANN001, ANN201
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


def test_context_assembly_logs_stage_timings(tmp_path):  # noqa: ANN001, ANN201
    """Every assembly span up to and past the experiments gate reaches the run log.

    This is a regression guard for the diagnostic used to find where
    ``create_run_context`` spends time before the TUI's hypothesis screen
    can leave "loading experiments..." (the gate flips when the second
    ``RunIntegration.attach`` records ``EXPERIMENTS_CHANGED``).
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
        "agent_client_build",
    ):
        assert f"boot span context.{stage}: " in log_text, f"missing span timing for {stage!r}"
    # The enclosing span is assembly's total, recorded after its children.
    assert "boot span context: " in log_text
    assert "experiments gate open after " in log_text


def test_dispatch_preamble_spans_reach_run_log(tmp_path):  # noqa: ANN001, ANN201
    """Spans closed before ``create_run_context`` land in the run log, first.

    ``_dispatch`` and ``_run_agent`` (main.py) do substantial work before a
    ``RunLogger`` exists and record ``boot_trace`` spans as they go.
    ``_assemble_run_context`` must drain that buffer at entry, so the
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


def test_context_assembly_without_recorded_preamble_omits_preamble_lines(tmp_path):  # noqa: ANN001, ANN201
    """No preamble spans (e.g. a test-built context) means no stray lines."""
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    boot_trace.drain_log_lines()
    with _create_context(project, evaluator=evaluator) as ctx:
        log_text = ctx.run_log_path.read_text()

    assert "boot span agent_preamble" not in log_text
    assert "boot span dispatch" not in log_text


def test_context_assembly_spans_stay_off_stderr_by_default(tmp_path, capfd):  # noqa: ANN001, ANN201
    """Boot spans are forensics in the run log, not narration at the operator."""
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as ctx:
        log_text = ctx.run_log_path.read_text()
        captured_err = capfd.readouterr().err

    assert "boot span context: " in log_text
    assert "boot span" not in captured_err


def test_boot_trace_env_puts_assembly_spans_on_stderr(tmp_path, capfd, monkeypatch):  # noqa: ANN001, ANN201
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
    (task / "vibesys.input.toml").write_text("version = 1\n", encoding="utf-8")
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
            assert ctx.trusted_input_changes() == []

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
            assert ctx.trusted_input_changes() == []

        assert _git(project, "status", "--porcelain") == ""


def test_copied_run_provisions_self_contained_project_in_collection(tmp_path):  # noqa: ANN001, ANN201
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


def test_resume_reuses_project_and_run_id_and_only_increases_limit(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.run_id

    with _create_context(
        project,
        evaluator=evaluator,
        exp_name=run_id,
        existing=True,
        configuration=_configuration(max_rounds=2),
    ) as resumed:
        assert resumed.project_root == project
        assert resumed.run_id == run_id

    stored = Project.open(project).state.load_run(run_id)
    assert isinstance(stored.configuration, AgentRunConfiguration)
    assert stored.configuration.max_rounds == 2
    assert _git(project, "branch", "--show-current") == f"vibesys-runs/{run_id}"


def test_resume_migrates_legacy_objectives_with_dirty_candidate(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.run_id

    state = Project.open(project).state
    manifest_path = state._run_manifest_path(run_id)  # noqa: SLF001  # migration fixture
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["configuration"].pop("objectives")
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _git(project, "add", str(manifest_path.relative_to(project)))
    _git(project, "commit", "-m", "simulate legacy run manifest")
    candidate = project / "queue.py"
    candidate.write_text(candidate.read_text() + "\n# interrupted edit\n")

    requested = _configuration().model_copy(update={"objectives": ("total_ops_per_sec:max",)})
    with _create_context(
        project,
        evaluator=evaluator,
        exp_name=run_id,
        existing=True,
        configuration=requested,
    ):
        pass

    stored = state.load_run(run_id)
    assert isinstance(stored.configuration, AgentRunConfiguration)
    assert stored.configuration.objectives == ("total_ops_per_sec:max",)
    assert "# interrupted edit" in candidate.read_text()
    assert "queue.py" in _git(project, "status", "--porcelain")
    assert "# interrupted edit" not in _git(project, "show", "HEAD:queue.py")


def test_collection_resume_pushes_existing_origin_on_teardown(tmp_path):  # noqa: ANN001, ANN201
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"
    with _create_context(source, runs_dir=runs_dir, evaluator=evaluator) as first:
        project = first.project_root
        run_id = first.run_id

    remote = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603
        ["git", "init", "--bare", "-q", str(remote)],  # noqa: S607
        check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "remote", "add", "origin", str(remote)],  # noqa: S607
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

    branch = subprocess.run(  # noqa: S603
        ["git", "--git-dir", str(remote), "branch", "--list", f"vibesys-runs/{run_id}"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"vibesys-runs/{run_id}" in branch


def test_direct_resume_republishes_an_already_published_run(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.run_id

    remote = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603
        ["git", "init", "--bare", "-q", str(remote)],  # noqa: S607
        check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "remote", "add", "origin", str(remote)],  # noqa: S607
        cwd=project,
        check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "push", "-q", "-u", "origin", f"vibesys-runs/{run_id}"],  # noqa: S607
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


def test_direct_resume_does_not_publish_an_untracked_source_origin(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    with _create_context(project, evaluator=evaluator) as first:
        run_id = first.run_id

    remote = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603
        ["git", "init", "--bare", "-q", str(remote)],  # noqa: S607
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


def test_explicit_repository_rejects_a_different_existing_origin(tmp_path):  # noqa: ANN001, ANN201
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


def test_direct_run_rejects_unmaterialized_workspace_source(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    source = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="library",
    )

    with pytest.raises(ConfigurationError, match="pass --runs-dir"):
        create_run_context(
            config=Config.model_validate({"model": {"name": "gpt-test"}}),
            exp_name="queue",
            runs_dir=None,
            input_path=str(project),
            accuracy_command="true",
            benchmark_command="true",
            workspace_sources=(source,),
            evaluator_path=evaluator,
            project_configuration=_configuration(),
            profiler_kind=ProfilerKind.NONE,
            profiler_domain=DomainName.GENERIC,
            run_environment=RunEnvironmentSpec("local"),
            agent_backend="stub",
        )


def test_omnigent_accepts_active_profiler_configuration(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)
    configuration = _configuration().model_copy(
        update={
            "agent_backend": "cli",
            "agent_driver": "omnigent",
            "cli_provider": "codex",
            "profiler": "macos_cpu",
        }
    )

    with create_run_context(
        config=Config.model_validate(
            {
                "model": {"name": "gpt-test"},
                "agent": {"backend": "cli", "driver": "omnigent", "cli_provider": "codex"},
            }
        ),
        exp_name="queue",
        runs_dir=None,
        input_path=str(project),
        accuracy_command="python _evaluator/checker/check.py",
        benchmark_command="python _evaluator/checker/check.py",
        evaluator_path=evaluator,
        project_configuration=configuration,
        profiler_kind=ProfilerKind.MACOS_CPU,
        profiler_domain=DomainName.GENERIC,
        run_environment=RunEnvironmentSpec("local"),
        agent_state_model_type=AgentRunState,
    ) as context:
        assert context.profiler_kind is ProfilerKind.MACOS_CPU


def test_portable_state_snapshot_replaces_namespace_exactly(tmp_path):  # noqa: ANN001, ANN201
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


def test_candidate_context_uses_project_worktree_directory(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "queue"
    evaluator = _write_project(project)

    with _create_context(project, evaluator=evaluator) as parent:
        parent_commit = parent.git.current_sha()
        assert parent_commit is not None
        candidate = create_candidate_context(
            parent,
            config=Config.model_validate({"model": {"name": "gpt-test"}}),
            generation=2,
            child_idx=3,
            parent_commit=parent_commit,
            agent_backend="stub",
        )
        candidate_root = candidate.workspace
        assert candidate_root == parent.project.state.candidate_worktree_directory(
            parent.run_id,
            "g2c3",
        )
        assert candidate.log_dir == (
            parent.state.local(RunStateNamespace.EVOLVE).external_directory("candidates/g2c3/logs")
        )
        assert Path(candidate.objective_location).read_text() == parent.effective_objective
        candidate.close()
        assert not candidate_root.exists()


def test_construction_failure_removes_new_copy_and_tears_down_hooks(tmp_path):  # noqa: ANN001, ANN201
    source = tmp_path / "input"
    evaluator = _write_project(source)
    runs_dir = tmp_path / "runs"
    hooks = _RecordingHooks()

    with (
        patch("vibesys.context.build_agent_client", side_effect=RuntimeError("runner failed")),
        pytest.raises(RuntimeError, match="runner failed"),
    ):
        _create_context(source, runs_dir=runs_dir, evaluator=evaluator, hooks=hooks)

    assert hooks.prepared == 1
    assert hooks.torn_down == 1
    assert not runs_dir.exists() or not list(runs_dir.iterdir())


def test_hook_teardown_runs_when_provisioning_fails(tmp_path):  # noqa: ANN001, ANN201
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


def test_log_switch_retargets_stderr_tee(tmp_path):  # noqa: ANN001, ANN201
    ctx = object.__new__(_RunContext)
    original_stderr = sys.stderr
    ctx.logger = RunLogger(tmp_path)
    ctx._paths = RunPaths(  # noqa: SLF001
        project_root=tmp_path,
        log_dir=tmp_path,
        run_log_path=ctx.logger.path,
    )
    original_file = ctx.logger.file
    ctx.agent_client = MagicMock()

    ctx.switch_log_file("round001")

    assert original_file.closed
    ctx.agent_client.set_log_file.assert_called_once_with(ctx.logger.writer)
    print("\033[31mcolored diagnostic\033[0m", file=sys.stderr)  # noqa: T201
    ctx.logger.close()
    assert sys.stderr is original_stderr
    assert "colored diagnostic" in ctx.run_log_path.read_text()
    assert "\033[31m" not in ctx.run_log_path.read_text()


def test_agent_client_gets_a_machine_local_provider_session_store(tmp_path):  # noqa: ANN001, ANN201
    """The run's agent client checkpoints provider sessions outside the repo.

    Provider transcripts are host-local, so the map must live in the run's
    machine-local namespace, never in the portable snapshot that travels
    between machines.
    """
    project = tmp_path / "queue"
    evaluator = _write_project(project)

    with (
        patch("vibesys.context.build_agent_client", return_value=MagicMock()) as build_runner,
        _create_context(project, evaluator=evaluator) as ctx,
    ):
        store = build_runner.call_args.kwargs["session_store"]
        assert isinstance(store, DurableSessionStore)
        store.record(
            AgentSessionKey(SessionScope.HYPOTHESIS, "H-01"),
            spec_fingerprint="fingerprint",
            provider="codex",
            model="gpt-test",
            session_id="thread-1",
        )
        local_dir = ctx.state.local(RunStateNamespace.AGENT).external_directory()

    assert (local_dir / "sessions.json").is_file()
    assert not local_dir.is_relative_to(project)


def test_evolve_candidate_clients_get_no_provider_session_store(tmp_path):  # noqa: ANN001, ANN201
    """Candidates never name a durable conversation, so they checkpoint nothing."""
    project = tmp_path / "queue"
    evaluator = _write_project(project)

    with _create_context(project, evaluator=evaluator) as parent:
        parent_commit = parent.git.current_sha()
        assert parent_commit is not None
        with patch("vibesys.context.build_agent_client", return_value=MagicMock()) as build_runner:
            candidate = create_candidate_context(
                parent,
                config=Config.model_validate({"model": {"name": "gpt-test"}}),
                generation=2,
                child_idx=3,
                parent_commit=parent_commit,
                agent_backend="stub",
            )
            assert "session_store" not in build_runner.call_args.kwargs
            candidate.close()


# ---------------------------------------------------------------------------
# The device pin and the agent execution mode
# ---------------------------------------------------------------------------


class _PinResponse(BaseModel):
    answer: str


@dataclass
class _PinSession:
    """One turn that answers whatever the schema asked for."""

    turns: list[AgentTurnRequest] = field(default_factory=list)

    def run_turn(self, request, observer=None):  # noqa: ANN001, ANN202
        del observer
        self.turns.append(request)
        return AgentTurnResult('{"answer":"ok"}')

    def resume_provider_session(self, session_id: str) -> bool:
        del session_id
        return False

    def cancel(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class _PinDriver:
    """Records the session specs the client builds, and its execution mode."""

    containerized: bool
    specs: list = field(default_factory=list)

    @property
    def capabilities(self):  # noqa: ANN202
        return AgentCapabilities(container_execution=self.containerized)

    def create_session(self, spec):  # noqa: ANN001, ANN202
        self.specs.append(spec)
        return _PinSession()

    def close(self) -> None: ...


def _pin_context(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    *,
    containerized: bool,
    device_index: int = 3,
) -> tuple[_RunContext, _PinDriver]:
    """A context whose only real parts are the device lease and agent client."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    backend = CudaBackend(log_dir=log_dir, log=lambda _message: None)
    backend.selected_device = GpuInfo(
        index=device_index,
        uuid=f"GPU-{device_index}",
        name="test-gpu",
        memory_used_mib=0,
        memory_total_mib=1,
        utilization_pct=0,
    )
    driver = _PinDriver(containerized=containerized)

    ctx = object.__new__(_RunContext)
    ctx.integration = LocalRunIntegration()
    request.addfinalizer(ctx.integration.close)
    ctx.events = ctx.integration.events
    ctx._progress_stack = []  # noqa: SLF001
    ctx._paths = RunPaths(  # noqa: SLF001
        project_root=tmp_path,
        log_dir=log_dir,
        run_log_path=tmp_path / "run.log",
    )
    ctx.device = DeviceLease(backend, log_dir=log_dir)
    ctx.agent_client = AgentClient(driver, provider="claude", model_name="m", log_dir=log_dir)
    return ctx, driver


def test_a_host_agent_is_pinned_to_the_selected_device(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    ctx, driver = _pin_context(tmp_path, request, containerized=False)

    assert ctx.gpu_env() == {"CUDA_VISIBLE_DEVICES": "3"}

    ctx.invoke(
        kind="judge",
        system_prompt="system",
        user_prompt="user",
        response_cls=_PinResponse,
        fallback_factory=lambda: _PinResponse(answer="fallback"),
    )

    assert driver.specs[0].environment == (("CUDA_VISIBLE_DEVICES", "3"),)


def test_a_container_agent_carries_no_host_device_pin(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """``--gpus device=N`` already makes the chosen GPU device 0 inside.

    A host index forwarded into the container would name a device that is not
    there, and re-pinning it mid-run would change the session fingerprint and
    evict the live conversation.
    """
    ctx, driver = _pin_context(tmp_path, request, containerized=True)

    assert ctx.gpu_env() == {}

    ctx.invoke(
        kind="judge",
        system_prompt="system",
        user_prompt="user",
        response_cls=_PinResponse,
        fallback_factory=lambda: _PinResponse(answer="fallback"),
    )

    assert driver.specs[0].environment == ()
